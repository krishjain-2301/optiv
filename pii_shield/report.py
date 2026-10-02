"""PII exposure register, per-file summary and audit log.

Every finding carries: file, page/slide, element, location, category, token, score, the layer and
recognizer that fired, and the reasons behind the score. Dropped candidates are kept in the audit
log (with why they were dropped) so false-positive controls can be inspected too.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .exposure import file_exposure
from .models import Document, Finding

COLUMNS = ["file", "page", "location", "kind", "source", "context_type", "entity_type", "text", "token", "score",
           "decision", "layer", "recognizer", "reasons"]


def mask_value(v: str) -> str:
    """Partially masked value for reports that may be shared (first/last char only)."""
    v = v.strip()
    if len(v) <= 4:
        return "*" * len(v)
    return v[0] + "*" * (len(v) - 2) + v[-1]


def value_set(findings: dict[str, list[Finding]]) -> re.Pattern | None:
    """Every value the run looked at (kept or dropped), and each word of it, as one pattern."""
    vals = set()
    for fs in findings.values():
        for f in fs:
            v = f.text.strip()
            if len(v) >= 3:
                vals.add(v)
                vals.update(w for w in re.split(r"[\s,;()]+", v) if len(w) >= 3)
    if not vals:
        return None
    body = "|".join(re.escape(v) for v in sorted(vals, key=len, reverse=True))
    return re.compile(rf"(?<![^\W_])(?:{body})(?![^\W_])", re.IGNORECASE)


def mask_reasons(reasons: list[str], values: re.Pattern | None) -> list[str]:
    """Reasons quote what they matched ("matches confirmed person 'Priya Raman'", "trimmed 'X' to
    'Y'"). In shareable outputs every value the run looked at, or any word of one, is masked
    wherever it occurs in a reason; labels and keywords ("field label 'Full name:'") stay readable."""
    if values is None:
        return list(reasons)
    return [values.sub(lambda m: mask_value(m.group()), r) for r in reasons]


def findings_frame(findings: dict[str, list[Finding]], include_dropped: bool = False, reveal: bool = True) -> pd.DataFrame:
    rows = []
    values = None if reveal else value_set(findings)
    for fs in findings.values():
        for f in fs:
            if f.decision == "drop" and not include_dropped:
                continue
            d = f.to_dict()
            d["reasons"] = "; ".join(f.reasons if reveal else mask_reasons(f.reasons, values))
            if not reveal:
                d["text"] = mask_value(f.text)
                d["location"] = mask_reasons([f.location], values)[0]  # sheet / shape names can be names
            rows.append(d)
    df = pd.DataFrame(rows, columns=COLUMNS + ["span_id", "start", "end"]) if rows else pd.DataFrame(columns=COLUMNS)
    return df[COLUMNS + [c for c in ("span_id", "start", "end") if c in df.columns]]


def file_summary(doc: Document, findings: list[Finding]) -> dict:
    live = [f for f in findings if f.decision != "drop"]
    return {
        "file": doc.file,
        "type": doc.file_type,
        "pages": doc.pages,
        "ocr_pages": doc.ocr_pages,
        "spans": len(doc.spans),
        "structure": doc.structure,
        "images": {
            "total": len(doc.images),
            "by_status": dict(Counter(i.ocr_status for i in doc.images)),
        },
        "findings": len(live),
        "redacted": sum(f.decision == "redact" for f in live),
        "review_queue": sum(f.decision == "review" for f in live),
        "dropped_candidates": sum(f.decision == "drop" for f in findings),
        "by_category": dict(Counter(f.entity_type for f in live).most_common()),
        "by_context": dict(Counter(f.context_type for f in live).most_common()),
        "by_layer": dict(Counter(f.layer for f in live).most_common()),
        "image_only_values": image_only_values(live),
        "warnings": doc.warnings,
        "exposure": (exp := file_exposure(doc, findings)),
        # flat copies for the spreadsheet summary
        "exposure_score": exp["score"],
        "exposure_per_1k_words": exp["per_1k_words"],
        "exposure_rating": exp["rating"],
        "residual_estimated_missed": exp["residual"]["estimated_missed"]["instances"],
        "residual_unreadable_images": exp["residual"]["unreadable"]["images_withheld"],
        "masked_copy": exp["residual"]["known"]["masked_copy"],
    }


def image_only_values(findings: list[Finding]) -> list[dict]:
    """E.6 prompt 3: identifiers that appear only inside images, never in native/OCR'd body text."""
    seen_text = {f.text.lower() for f in findings if f.context_type != "image"}
    out = {}
    for f in findings:
        if f.context_type == "image" and f.text.lower() not in seen_text:
            out[(f.entity_type, f.text.lower())] = {"entity_type": f.entity_type, "token": f.token, "location": f.location}
    return list(out.values())


def audit_records(findings: dict[str, list[Finding]], run_id: str) -> list[dict]:
    ts = datetime.now(timezone.utc).isoformat()
    recs = []
    values = value_set(findings)
    for fs in findings.values():
        for f in fs:
            recs.append({
                "run_id": run_id, "timestamp": ts, "file": f.file, "page": f.page,
                "location": mask_reasons([f.location], values)[0],
                "span_id": f.span_id, "start": f.start, "end": f.end, "entity_type": f.entity_type,
                "value_masked": mask_value(f.text), "token": f.token, "score": f.score, "decision": f.decision,
                "layer": f.layer, "recognizer": f.recognizer, "reasons": mask_reasons(f.reasons, values),
            })
    return recs


def write_reports(out_dir: Path, docs: dict[str, Document], findings: dict[str, list[Finding]], run_id: str,
                  components: list[str]) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    # The shareable register shows values partially masked; the full register is SENSITIVE (it is
    # a list of every personal value found) and is kept apart from anything that can be shared.
    paths["register_sensitive_csv"] = out_dir / "pii_exposure_register.SENSITIVE.csv"
    findings_frame(findings).to_csv(paths["register_sensitive_csv"], index=False)
    df = findings_frame(findings, reveal=False)
    paths["register_csv"] = out_dir / "pii_exposure_register.csv"
    df.to_csv(paths["register_csv"], index=False)
    paths["register_xlsx"] = out_dir / "pii_exposure_register.xlsx"
    with pd.ExcelWriter(paths["register_xlsx"]) as xw:
        df.to_excel(xw, sheet_name="findings", index=False)
        summ = pd.DataFrame([{k: v for k, v in file_summary(docs[f], fs).items() if not isinstance(v, (dict, list))}
                             for f, fs in findings.items()])
        summ.to_excel(xw, sheet_name="summary", index=False)
        if not df.empty:
            pivot = df.pivot_table(index="entity_type", columns="context_type", values="text", aggfunc="count", fill_value=0)
            pivot.to_excel(xw, sheet_name="category_x_context")
    paths["summary_json"] = out_dir / "summary.json"
    summaries = [file_summary(docs[f], fs) for f, fs in findings.items()]
    paths["summary_json"].write_text(json.dumps({
        "run_id": run_id, "components": components,
        "exposure_ranking": [
            {"file": s["file"], "rating": s["exposure_rating"], "score": s["exposure_score"],
             "per_1k_words": s["exposure_per_1k_words"], "estimated_missed": s["residual_estimated_missed"]}
            for s in sorted(summaries, key=lambda s: -s["exposure_per_1k_words"])],
        "files": summaries,
    }, indent=2, default=str), encoding="utf-8")
    paths["audit_log"] = out_dir / "audit_log.jsonl"
    with open(paths["audit_log"], "w", encoding="utf-8") as fh:
        for rec in audit_records(findings, run_id):
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return paths
