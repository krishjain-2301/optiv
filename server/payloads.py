"""What the dashboard is sent: the run as one JSON document, one document's detail, the evaluation.

The dashboard filters and aggregates in the browser, so the run payload carries every finding as
a row rather than pre-computed charts.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from optiv_pii_shield import RunResult, audit, review
from optiv_pii_shield.config import PROFILES, SPECIAL_CATEGORIES
from optiv_pii_shield.evaluate import evaluate, load_gold, structure_retention
from optiv_pii_shield.redact.files import finding_boxes
from optiv_pii_shield.report import file_summary

SHAREABLE_REPORTS = ("pii_exposure_register.csv", "pii_exposure_register.xlsx", "summary.json", "audit_log.jsonl",
                     "run_manifest.json")
KINDS = {".md": "LLM text", ".csv": "Register", ".xlsx": "Register", ".json": "Summary", ".jsonl": "Audit log"}
FINDING_FIELDS = ("file", "page", "location", "kind", "source", "context_type", "entity_type", "text", "token", "score",
                  "decision", "layer", "recognizer", "review")


def shareable_outputs(res: RunResult, out: Path) -> list[Path]:
    """Explicit allow-list: only outputs that passed the leak gate or never hold raw values.
    Anything else in the folder (extracted text, vault, full register, files added later) stays out."""
    keep = [p for k, p in res.outputs.items() if k.startswith(("masked:", "redacted:"))]
    keep += [out / n for n in SHAREABLE_REPORTS]
    return sorted(p for p in keep if p.exists() and "SENSITIVE" not in p.name)


def sensitive_outputs(res: RunResult, out: Path) -> list[Path]:
    safe = set(shareable_outputs(res, out))
    return [p for p in sorted(out.iterdir()) if p.is_file() and p not in safe]


def _file_row(p: Path) -> dict:
    kind = ("Masked copy" if ".masked." in p.name else "Token vault" if "vault" in p.name
            else "Manifest" if p.name == "run_manifest.json" else KINDS.get(p.suffix, "Other"))
    return {"name": p.name, "kind": kind, "size": p.stat().st_size}


def _finding(f) -> dict:
    row = {k: getattr(f, k) for k in FINDING_FIELDS}
    row["reasons"] = list(f.reasons)
    return row


def run_payload(res: RunResult, out: Path, settings, has_gold: bool) -> dict:
    files = []
    for name, doc in res.docs.items():
        s = file_summary(doc, res.findings[name])
        s["extract_seconds"] = res.timings.get(f"extract:{name}")
        s["gate"] = dict(doc.gate)
        files.append(s)
    every = [f for fs in res.findings.values() for f in fs]
    live = [f for f in every if f.decision != "drop"]
    tokens: dict[str, dict] = {}
    for f in live:
        if f.token:
            t = tokens.setdefault(f.token, {"token": f.token, "entity_type": f.entity_type, "occurrences": 0, "files": set(),
                                            "linked": f.token in res.vault.linked})
            t["occurrences"] += 1
            t["files"].add(f.file)
    docs = list(res.docs.values())
    masked = [d.gate.get("masked") for d in docs]
    check = audit.verify_run(out) if (out / "run_manifest.json").exists() else {"ok": False, "signed": False, "problems": []}
    verified = [d.gate.get("verified", {}) for d in docs]
    return {
        "run_id": res.run_id,
        "components": res.components,
        "timings": res.timings,
        "errors": res.errors,
        "thresholds": {"redact": settings.redact_threshold, "review": settings.review_threshold},
        "profile": {"name": settings.profile, "label": PROFILES.get(settings.profile, PROFILES["default"])["label"]},
        "operator": settings.operator,
        "keyed_tokens": bool(settings.token_key),
        "special_categories": sorted(SPECIAL_CATEGORIES),
        "review_queue": review.queue(res.findings),
        "reviews": res.reviews,
        "integrity": {"ok": check["ok"], "signed": check["signed"], "problems": check["problems"],
                      "audit_records": check.get("audit", {}).get("records", 0), "public_key": check.get("public_key")},
        "has_gold": has_gold,
        "files": files,
        "findings": [_finding(f) for f in live],
        "dropped": [_finding(f) for f in every if f.decision == "drop"],
        "tokens": [{**t, "files": len(t["files"])} for t in tokens.values()],
        "pipeline": {
            "spans": sum(len(d.spans) for d in docs), "ocr_pages": sum(len(d.ocr_pages) for d in docs),
            "images": sum(len(d.images) for d in docs), "tokens": len(res.vault.values),
            "people": len(res.vault.person_no),
            "caught": sum(d.gate.get("llm_text_scrubbed", 0) + d.gate.get("masked_scrubbed", 0) for d in docs),
            "masked_written": masked.count("written"), "masked_withheld": masked.count("withheld"),
            "verified_pages": sum(v.get("pages", 0) for v in verified),
            "verified_pictures": sum(v.get("pictures", 0) for v in verified),
            "verify_covered": sum(v.get("covered", 0) for v in verified),
            "verify_on": settings.verify_outputs,
            "faces": sum(v.kind == "face" for d in docs for v in d.visuals),
            "qr_codes": sum(v.kind == "qr" for d in docs for v in d.visuals),
            "outputs": len(res.outputs),
        },
        "outputs": {
            "folder": str(out), "vault": "vault" in res.outputs,
            "safe": [_file_row(p) for p in shareable_outputs(res, out)],
            "sensitive": [_file_row(p) for p in sensitive_outputs(res, out)],
        },
    }


def doc_payload(res: RunResult, name: str) -> dict:
    doc = res.docs[name]
    by_span: dict[str, list[dict]] = {}
    boxes: dict[int, list[dict]] = {}
    for f in res.findings[name]:
        if f.decision == "drop":
            continue
        by_span.setdefault(f.span_id, []).append({
            "start": f.start, "end": f.end, "entity_type": f.entity_type, "score": f.score, "layer": f.layer,
            "decision": f.decision, "context_type": f.context_type})
        if doc.file_type == "pdf" and f.page in doc.page_sizes:
            w, h = doc.page_sizes[f.page]
            for x0, y0, x1, y1 in finding_boxes(doc.span(f.span_id), f):
                # fractions of the page, so the browser can lay them over the image at any size
                boxes.setdefault(f.page, []).append({
                    "x": x0 / w, "y": y0 / h, "w": (x1 - x0) / w, "h": (y1 - y0) / h,
                    "entity_type": f.entity_type, "score": f.score, "decision": f.decision})
    spans = [{"id": s.id, "page": s.page, "location": s.location, "kind": s.kind, "source": s.source,
              "ocr_conf": s.ocr_conf, "header": s.header, "text": s.text} for s in doc.spans]
    return {
        "file": doc.file, "file_type": doc.file_type, "pages": doc.pages, "ocr_pages": doc.ocr_pages,
        "previewable": doc.file_type == "pdf" and doc.pages > 0,
        "masked_preview": doc.file_type == "pdf" and f"masked:{name}" in res.outputs,
        "visuals": [{"kind": v.kind, "page": v.page} for v in doc.visuals],
        "verification": doc.gate.get("verified", {}),
        "markdown": doc.markdown, "redacted": res.redacted[name], "structure": doc.structure,
        "kinds": dict(Counter(s.kind for s in doc.spans).most_common()),
        "sources": dict(Counter(s.source for s in doc.spans).most_common()),
        "spans": spans,
        "context": [{**s, "findings": sorted(by_span[s["id"]], key=lambda x: x["start"])} for s in spans if s["id"] in by_span],
        "boxes": boxes,
        "images": [{"location": i.location, "page": i.page, "status": i.ocr_status, "ocr_conf": i.ocr_conf,
                    "width": i.width, "height": i.height} for i in doc.images],
        "warnings": doc.warnings,
    }


def page_png(res: RunResult, name: str, page: int, masked: bool = False) -> bytes:
    """A page of the original, or of the masked copy as it was written (after verification)."""
    import pymupdf as fitz

    doc = res.docs[name]
    with fitz.open(res.outputs[f"masked:{name}"] if masked else doc.path) as pdf:
        return pdf[page - 1].get_pixmap(dpi=110).tobytes("png")


def evaluation_payload(res: RunResult, gold: Path | None, transcriptions: dict[str, str] | None = None) -> dict:
    retention = {name: structure_retention(doc, (transcriptions or {}).get(name)) for name, doc in res.docs.items()}
    if gold is None:
        return {"has_gold": False, "retention": retention}
    items = load_gold(gold)
    ev = evaluate(items, res.findings, res.redacted)
    return {
        "has_gold": True, "gold_rows": len(items), "retention": retention,
        "scores": {**ev.as_dict(), "recalled": ev.recalled, "category_correct": ev.category_correct,
                   "true_positive_findings": ev.true_positive_findings, "auto_findings": ev.auto_findings},
    }
