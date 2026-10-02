"""End-to-end run: extract -> detect -> tokenise -> redact -> report. Fully offline."""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import Settings
from .detect import Detector
from .extract import extract
from .models import Document, Finding
from .redact.files import write_masked
from .redact.leakcheck import LeakError, build_needles, scrub_package, scrub_text
from .redact.text import redacted_markdown
from .redact.tokens import TokenVault
from .report import write_reports

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    run_id: str
    docs: dict[str, Document]
    findings: dict[str, list[Finding]]
    vault: TokenVault
    redacted: dict[str, str]
    components: list[str]
    timings: dict[str, float] = field(default_factory=dict)
    outputs: dict[str, Path] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def output_stem(file: str) -> str:
    """"report.docx" -> "report.docx": the extension stays in the name, so report.docx and
    report.pptx never overwrite each other's outputs."""
    return Path(file).name


SHAREABLE_REPORTS = ("register_csv", "register_xlsx", "summary_json", "audit_log")


def _gate_shareable_reports(outputs: dict[str, Path], needles, errors: dict[str, str]) -> None:
    """Reports meant for sharing get the same leak gate as masked files. Values are masked when the
    reports are built; anything that still matches is a bug, so it is scrubbed and reported."""
    for key in SHAREABLE_REPORTS:
        path = outputs.get(key)
        if path is None or not path.exists():
            continue
        if path.suffix == ".xlsx":
            data, n = scrub_package(path.read_bytes(), needles)
            if n:
                path.write_bytes(data)
        else:
            text, n = scrub_text(path.read_text(encoding="utf-8"), needles)
            if n:
                path.write_text(text, encoding="utf-8")
        if n:
            log.error("%s: %d original value(s) found in a shareable report and scrubbed", path.name, n)
            errors[path.name] = f"{n} original value(s) had to be scrubbed from this report (report bug)"


_DETECTOR: dict[tuple, Detector] = {}


def get_detector(settings: Settings) -> Detector:
    """Loading spaCy takes seconds; reuse one detector per configuration."""
    key = (settings.use_spacy, settings.spacy_model, settings.use_gliner, settings.gliner_model,
           tuple(settings.allow_list), settings.gliner_threshold)
    if key not in _DETECTOR:
        _DETECTOR[key] = Detector(settings)
    det = _DETECTOR[key]
    det.settings = settings
    return det


def run(paths: list[str | Path], settings: Optional[Settings] = None, out_dir: Optional[str | Path] = None,
        progress: Optional[Callable[[str, float], None]] = None) -> RunResult:
    settings = settings or Settings()
    progress = progress or (lambda msg, frac: log.info(msg))
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    timings: dict[str, float] = {}
    docs: dict[str, Document] = {}
    errors: dict[str, str] = {}

    t0 = time.perf_counter()
    # Models first: a missing NER model must stop the run before minutes of OCR, not after.
    detector = get_detector(settings)
    for i, p in enumerate(paths):
        p = Path(p)
        progress(f"Extracting {p.name}", i / max(len(paths), 1) * 0.6)
        t = time.perf_counter()
        try:
            doc = extract(p, settings)
        except Exception as exc:  # fail closed: a file we cannot read is reported, never passed on
            log.exception("extraction failed for %s", p)
            errors[p.name] = f"{type(exc).__name__}: {exc}"
            continue
        timings[f"extract:{p.name}"] = round(time.perf_counter() - t, 2)
        docs[doc.file] = doc

    progress("Detecting PII", 0.65)
    t = time.perf_counter()
    findings = detector.detect_all(docs)
    timings["detect"] = round(time.perf_counter() - t, 2)

    progress("Assigning tokens", 0.8)
    vault = TokenVault(detector.person_index)
    vault.assign_all(docs, findings)
    needles = build_needles(vault, findings)
    redacted = {}
    for f, doc in docs.items():
        # Leak gate for the LLM text: any vault value still present anywhere in it (a mention the
        # detectors did not locate) is replaced by its token, and the catch is reported.
        text, n = scrub_text(redacted_markdown(doc, findings[f], settings), needles)
        doc.gate["llm_text_scrubbed"] = n
        if n:
            doc.warnings.append(f"final scrub replaced {n} value(s) in the LLM text that the detectors had not located")
        redacted[f] = text

    outputs: dict[str, Path] = {}
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        progress("Writing masked files and reports", 0.85)
        for f, doc in docs.items():
            name = output_stem(f)
            (out / f"{name}.extracted.SENSITIVE.md").write_text(doc.markdown, encoding="utf-8")
            outputs[f"redacted:{f}"] = out / f"{name}.redacted.md"
            outputs[f"redacted:{f}"].write_text(redacted[f], encoding="utf-8")
            try:
                masked = write_masked(doc, findings[f], out, settings, needles)
                if masked:
                    outputs[f"masked:{f}"] = masked
                    doc.gate["masked"] = "written"
            except LeakError as exc:
                doc.gate["masked"] = "withheld"
                log.error("masked copy of %s withheld: %s", f, exc)
                errors[f] = f"masked copy withheld (fail closed): {exc}"
            except Exception as exc:
                doc.gate["masked"] = "failed"
                log.exception("masking failed for %s", f)
                errors[f] = f"masking failed: {type(exc).__name__}: {exc}"
        if settings.vault_passphrase:
            outputs["vault"] = vault.save(out / "token_vault.SENSITIVE.enc.json", settings.vault_passphrase)
        else:
            log.warning("no vault passphrase: token vault not saved (tokens cannot be reversed later)")
        outputs.update(write_reports(out, docs, findings, run_id, detector.components))
        _gate_shareable_reports(outputs, needles, errors)
    timings["total"] = round(time.perf_counter() - t0, 2)
    progress("Done", 1.0)
    return RunResult(run_id, docs, findings, vault, redacted, detector.components, timings, outputs, errors)
