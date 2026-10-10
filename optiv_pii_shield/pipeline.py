"""End-to-end run: extract -> detect -> tokenise -> redact -> verify -> report. Fully offline.

The run has two halves. ``run`` reads the files and detects; ``redact`` turns findings into
outputs. ``redact`` can run again on the same result after a reviewer changed the findings
(``apply_review``), so every output always reflects the current decisions.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import audit, modelstore, review
from .config import Settings
from .detect import Detector
from .detect.resolver import annotate
from .errors import ModelMissing, RunCancelled
from .extract import extract, visual
from .extract.ocr import get_engine
from .models import Document, Finding
from .redact.files import masked_path, write_masked
from .redact.leakcheck import LeakError, Needles, build_needles, find, scrub_package, scrub_text
from .redact.text import redacted_markdown
from .redact.tokens import TokenVault
from .report import write_reports

log = logging.getLogger(__name__)
GATE_LAYER = "L5 leak gate"
Progress = Callable[[str, float], None]


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
    settings: Optional[Settings] = None
    started: str = ""
    extract_errors: dict[str, str] = field(default_factory=dict)  # files that could not be read
    reviews: list[dict] = field(default_factory=list)  # what reviewers changed, in order
    out_dir: Optional[Path] = None
    manifest: dict = field(default_factory=dict)


def output_stem(file: str) -> str:
    """"report.docx" -> "report.docx": the extension stays in the name, so report.docx and
    report.pptx never overwrite each other's outputs."""
    return Path(file).name


SHAREABLE_REPORTS = ("register_csv", "register_xlsx", "summary_json")


def _gate_shareable_reports(outputs: dict[str, Path], needles, errors: dict[str, str]) -> None:
    """Reports meant for sharing get the same leak gate as masked files. Values are masked when the
    reports are built; anything that still matches is a bug, so it is scrubbed and reported.
    (The audit log is gated before it is written: it is append-only.)"""
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
    key = (settings.use_spacy, settings.spacy_model, settings.use_gliner, settings.gliner_model, settings.gliner_revision,
           tuple(settings.allow_list), settings.gliner_threshold)
    if key not in _DETECTOR:
        _DETECTOR[key] = Detector(settings)
    det = _DETECTOR[key]
    det.settings = settings
    return det


# Where each stage starts on the 0-1 progress scale (the share is a rough guess at its duration).
STAGES = (("Load models", 0.0), ("Extract", 0.03), ("Detect", 0.50), ("Tokenise", 0.66), ("Redact and verify", 0.68),
          ("Report", 0.97))
REVIEW_STAGES = (("Apply decisions", 0.0), ("Tokenise", 0.03), ("Redact and verify", 0.06), ("Report", 0.95))
_START = dict(STAGES)


def stage_of(frac: float, stages=STAGES) -> int:
    """Index into ``stages`` of the stage running at ``frac``; len(stages) once the run is done."""
    return len(stages) if frac >= 1.0 else max(i for i, (_, start) in enumerate(stages) if frac >= start)


def _unique(name: str, taken: set[str]) -> str:
    """"report.pdf" from two folders must not collide: the second becomes "report~2.pdf"."""
    if name not in taken:
        return name
    p, n = Path(name), 2
    while f"{p.stem}~{n}{p.suffix}" in taken:
        n += 1
    return f"{p.stem}~{n}{p.suffix}"


def _rename(doc: Document, name: str) -> None:
    doc.file = name
    for s in doc.spans:
        s.file = name
    for i in doc.images:
        i.file = name


def run(paths: list[str | Path], settings: Optional[Settings] = None, out_dir: Optional[str | Path] = None,
        progress: Optional[Progress] = None) -> RunResult:
    """``progress(message, fraction)`` is called at every stage, PDF page, 20 text elements and
    output file, so a caller can show where a long run is, and stop it by raising RunCancelled."""
    settings = settings or Settings()
    progress = progress or (lambda msg, frac: log.info(msg))
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    started = audit.now()
    timings: dict[str, float] = {}
    docs: dict[str, Document] = {}
    errors: dict[str, str] = {}

    t0 = time.perf_counter()
    # Models first: a missing NER model must stop the run before minutes of OCR, not after.
    progress("Loading models", _START["Load models"])
    detector = get_detector(settings)
    get_engine(settings.ocr_engine)  # the OCR model too (cached; raises ModelMissing)
    visual.check_models(settings)
    lo, width = _START["Extract"], _START["Detect"] - _START["Extract"]
    for i, p in enumerate(paths):
        p = Path(p)
        progress(f"Extracting {p.name}", lo + i / max(len(paths), 1) * width)
        t = time.perf_counter()
        try:
            doc = extract(p, settings, lambda done, total, i=i, p=p: progress(
                f"Extracting {p.name}: page {done} of {total}", lo + (i + done / total) / len(paths) * width))
        except (ModelMissing, RunCancelled):
            raise  # a missing model or a cancel ends the whole run; neither is one bad file
        except Exception as exc:  # fail closed: a file we cannot read is reported, never passed on
            log.exception("extraction failed for %s", p)
            errors[_unique(p.name, set(errors))] = f"{type(exc).__name__}: {exc}"
            continue
        name = _unique(doc.file, set(docs))
        if name != doc.file:
            doc.warnings.append(f"another input is also called {doc.file}; this one ({p}) is reported as {name}")
            _rename(doc, name)
        timings[f"extract:{doc.file}"] = round(time.perf_counter() - t, 2)
        docs[doc.file] = doc

    lo, width = _START["Detect"], _START["Tokenise"] - _START["Detect"]
    progress("Detecting PII", lo)
    t = time.perf_counter()
    findings = detector.detect_all(docs, lambda f, done, total: progress(
        f"Detecting PII in {f}: {done:,} of {total:,} text elements", lo + done / max(total, 1) * width * 0.9))
    timings["detect"] = round(time.perf_counter() - t, 2)

    res = RunResult(run_id, docs, findings, TokenVault(detector.person_index), {}, detector.components, timings,
                    settings=settings, started=started, extract_errors=errors,
                    out_dir=Path(out_dir) if out_dir is not None else None)
    redact(res, progress, start=_START["Tokenise"])
    timings["total"] = round(time.perf_counter() - t0, 2)
    progress("Done", 1.0)
    return res


def locate_missed(docs: dict[str, Document], findings: dict[str, list[Finding]], needles: Needles, vault: TokenVault) -> int:
    """A value the detectors found in one place may sit in another where no layer fired. Every
    such place becomes a finding, so it is redacted in the text *and* covered in the masked file
    (a box on a scanned page cannot be added by scrubbing text afterwards)."""
    n = 0
    for file, doc in docs.items():
        covered: dict[str, list[tuple[int, int]]] = {}
        for f in findings[file]:
            if f.decision in review.LIVE:
                covered.setdefault(f.span_id, []).append((f.start, f.end))
        for span in doc.spans:
            for s, e, matched in sorted(find(span.text, needles), key=lambda h: (h[0], -h[1])):
                if any(a < e and b > s for a, b in covered.get(span.id, [])):
                    continue
                token = needles.token_for(matched)
                f = Finding(span_id=span.id, file=file, start=s, end=e, text=span.text[s:e],
                            entity_type=vault.entity_of.get(token, "PERSON"), score=0.9, recognizer="gate:needle",
                            layer=GATE_LAYER, decision="redact", token=token,
                            reasons=["a value found elsewhere in this run also appears here, where no detector fired"])
                findings[file].append(annotate(f, span))
                covered.setdefault(span.id, []).append((s, e))
                n += 1
        doc.gate["located"] = sum(f.layer == GATE_LAYER for f in findings[file])
        findings[file].sort(key=lambda f: (f.page or 0, f.span_id, f.start))
    return n


def redact(res: RunResult, progress: Optional[Progress] = None, start: float = 0.0, events: Optional[list[dict]] = None,
           only=None) -> RunResult:
    """Tokenise, write the LLM text and the masked copies, verify them, write the reports.
    Runs on a fresh result and again after a review; ``start`` is where it begins on the progress
    scale. ``events`` and ``only`` are passed to the audit log (see report.write_reports)."""
    settings, docs, findings = res.settings, res.docs, res.findings
    progress = progress or (lambda msg, frac: log.info(msg))
    first = not res.outputs and not res.redacted
    span = 1.0 - start
    at = lambda share: start + span * share  # noqa: E731

    progress("Assigning tokens", at(0.0))
    res.errors = dict(res.extract_errors)
    for f, doc in docs.items():
        findings[f] = [x for x in findings[f] if x.layer != GATE_LAYER]
        if doc.base_warnings is None:
            doc.base_warnings = list(doc.warnings)
        doc.warnings, doc.gate = list(doc.base_warnings), {}
    vault = res.vault = TokenVault(res.vault.persons, settings.token_key, settings.profile)
    vault.assign_all(docs, findings)
    needles = build_needles(vault, findings)
    locate_missed(docs, findings, needles, vault)
    res.redacted = {}
    for f, doc in docs.items():
        # Leak gate for the LLM text: any vault value still present anywhere in it is replaced by
        # its token, and the catch is reported.
        text, n = scrub_text(redacted_markdown(doc, findings[f], settings), needles)
        doc.gate["llm_text_scrubbed"] = n + doc.gate.get("located", 0)
        if doc.gate["llm_text_scrubbed"]:
            doc.warnings.append(f"the leak gate redacted {doc.gate['llm_text_scrubbed']} mention(s) the detectors had not located")
        res.redacted[f] = text
        if doc.visuals:
            kinds = [v.kind for v in doc.visuals]
            doc.warnings.append(f"{kinds.count('face')} face(s), {kinds.count('qr')} QR code(s) and {kinds.count('overprint')} "
                                "stretch(es) of text under a stamp found in pictures; they are blanked in the masked copy")

    outputs: dict[str, Path] = {}
    if res.out_dir is not None:
        out = res.out_dir
        out.mkdir(parents=True, exist_ok=True)
        lo, width = 0.03, 0.89
        for j, (f, doc) in enumerate(docs.items()):
            here = at(lo + j / len(docs) * width)
            progress(f"Writing the masked copy of {f}", here)
            name = output_stem(f)
            outputs[f"extracted:{f}"] = out / f"{name}.extracted.SENSITIVE.md"
            outputs[f"extracted:{f}"].write_text(doc.markdown, encoding="utf-8")
            outputs[f"redacted:{f}"] = out / f"{name}.redacted.md"
            outputs[f"redacted:{f}"].write_text(res.redacted[f], encoding="utf-8")
            masked_path(doc, out).unlink(missing_ok=True)  # a copy from before a review must not outlive a refusal
            try:
                masked = write_masked(doc, findings[f], out, settings, needles, tick=lambda msg, here=here: progress(msg, here))
                if masked:
                    outputs[f"masked:{f}"] = masked
                    doc.gate["masked"] = "written"
            except RunCancelled:
                raise
            except LeakError as exc:
                doc.gate["masked"] = "withheld"
                log.error("masked copy of %s withheld: %s", f, exc)
                res.errors[f] = f"masked copy withheld (fail closed): {exc}"
            except Exception as exc:
                doc.gate["masked"] = "failed"
                log.exception("masking failed for %s", f)
                res.errors[f] = f"masking failed: {type(exc).__name__}: {exc}"
        if settings.vault_passphrase:
            outputs["vault"] = vault.save(out / "token_vault.SENSITIVE.enc.json", settings.vault_passphrase)
        else:
            log.warning("no vault passphrase: token vault not saved (tokens cannot be reversed later)")
        progress("Writing reports", at(0.93))
        log_path = out / "audit_log.jsonl"
        if first and log_path.exists():  # an earlier run's log in a reused folder: kept, under another name
            log_path.replace(out / f"audit_log.before-{res.run_id}.jsonl")
        alog = audit.AuditLog(log_path, res.run_id)
        if first:
            events = [{"event": "run_started", "timestamp": res.started, "operator": settings.operator,
                       "profile": settings.profile, "components": res.components,
                       "inputs": [{"file": f, "sha256": d.sha256} for f, d in docs.items()]}] + list(events or [])
        outputs.update(write_reports(out, docs, findings, res.run_id, res.components, alog, needles, events, only))
        _gate_shareable_reports(outputs, needles, res.errors)
        res.manifest = _manifest(res, alog)
        outputs["manifest"] = audit.write_manifest(out, res.manifest, list(outputs.values()))
    res.outputs = outputs
    return res


def _manifest(res: RunResult, alog: audit.AuditLog) -> dict:
    s = res.settings
    return {
        "run_id": res.run_id, "started": res.started, "written": audit.now(), "operator": s.operator,
        "tool": {"name": "optiv-pii-shield", "version": _version()}, "versions": audit.versions(),
        "models": {"components": res.components, "files": modelstore.digests(),
                   "gliner_revision": s.gliner_revision if s.use_gliner else None},
        "settings": audit.settings_record(s),
        "inputs": [{"file": f, "type": d.file_type, "sha256": d.sha256, "bytes": Path(d.path).stat().st_size
                    if Path(d.path).exists() else None} for f, d in res.docs.items()],
        "not_read": res.extract_errors,
        "gate": {f: d.gate for f, d in res.docs.items()},
        "reviews": res.reviews,
        "audit": {"records": alog.count, "head": alog.head},
    }


def _version() -> str:
    from . import __version__

    return __version__


def apply_review(res: RunResult, decisions: list[review.Decision], additions: list[review.Addition],
                 operator: Optional[str] = None, progress: Optional[Progress] = None) -> dict:
    """Apply a reviewer's decisions and write every output again. Returns what changed."""
    operator = operator or res.settings.operator
    progress = progress or (lambda msg, frac: log.info(msg))
    progress("Applying the reviewer's decisions", 0.0)
    counts = review.apply(res.docs, res.findings, decisions, additions, operator, res.vault.persons)
    entry = {"timestamp": audit.now(), "operator": operator, **counts}
    res.reviews.append(entry)
    redact(res, progress, start=0.03, events=[{"event": "review_applied", **entry}],
           only=lambda f: bool(f.review) or f.layer == GATE_LAYER)
    progress("Done", 1.0)
    return counts
