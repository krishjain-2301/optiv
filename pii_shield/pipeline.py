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
    detector = get_detector(settings)
    findings = detector.detect_all(docs)
    timings["detect"] = round(time.perf_counter() - t, 2)

    progress("Assigning tokens", 0.8)
    vault = TokenVault(detector.person_index)
    vault.assign_all(docs, findings)
    redacted = {f: redacted_markdown(docs[f], findings[f], settings) for f in docs}

    outputs: dict[str, Path] = {}
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        progress("Writing masked files and reports", 0.85)
        for f, doc in docs.items():
            stem = Path(f).stem
            (out / f"{stem}.extracted.md").write_text(doc.markdown, encoding="utf-8")
            (out / f"{stem}.redacted.md").write_text(redacted[f], encoding="utf-8")
            try:
                masked = write_masked(doc, findings[f], out, settings)
                if masked:
                    outputs[f"masked:{f}"] = masked
            except Exception as exc:
                log.exception("masking failed for %s", f)
                errors[f] = f"masking failed: {type(exc).__name__}: {exc}"
        vault.save(out / "token_vault.SENSITIVE.json")
        outputs.update(write_reports(out, docs, findings, run_id, detector.components))
    timings["total"] = round(time.perf_counter() - t0, 2)
    progress("Done", 1.0)
    return RunResult(run_id, docs, findings, vault, redacted, detector.components, timings, outputs, errors)
