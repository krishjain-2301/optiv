"""Resolver: clean up person hits, merge overlapping findings, score agreement, route by confidence."""
from __future__ import annotations

import re
from dataclasses import replace

from ..config import Settings
from ..models import Document, Finding, Span
from .names import looks_like_name, trim_to_name

# When two different categories claim overlapping text, the more specific one wins.
PRIORITY = {
    "EMAIL_ADDRESS": 9, "US_SSN": 9, "IN_PAN": 9, "PL_PESEL": 9, "PASSPORT": 9, "CREDIT_CARD": 9, "IBAN_CODE": 9,
    "EMPLOYEE_ID": 9, "VENDOR_ID": 8, "TAX_ID": 8, "NATIONAL_ID": 8, "DATE_OF_BIRTH": 7, "PHONE_NUMBER": 6,
    "ADDRESS": 5, "PERSON": 4, "LOW_CONFIDENCE_OCR": 1,
}
LABEL_BEFORE = re.compile(r"[A-Za-z][A-Za-z .#/()'-]{1,40}[:：]\s*$")


def clean_person(f: Finding, span: Span, allow: set[str]) -> Finding | None:
    """NER over-reaches ("M. Vossberg CEO", "Vendor Tier", "QRC_BYOD"). Trim or drop."""
    if f.entity_type != "PERSON":
        return f
    if f.layer.startswith("L3") or f.layer.startswith("L4"):
        return f  # already validated as name-shaped
    raw = span.text[f.start:f.end]
    if "\n" in raw.strip():
        # Names do not span line breaks (cells, form fields): keep the first line, the resolver
        # keeps the structure-layer hits for the others.
        first = raw.strip().split("\n")[0]
        lead = len(raw) - len(raw.lstrip())
        f.end = f.start + lead + len(first)
        raw = span.text[f.start:f.end]
    trimmed = trim_to_name(raw, allow)
    if trimmed is None:
        f.decision, f.reasons = "drop", f.reasons + ["not name-shaped after trimming (business term, acronym or field name)"]
        return f
    s, e = trimmed
    if (s, e) != (0, len(raw)):
        f.reasons.append(f"trimmed '{raw}' to '{raw[s:e]}'")
    f.start, f.end = f.start + s, f.start + e
    f.text = span.text[f.start:f.end]
    if len(f.text.split()) == 1 and f.score > SINGLE_TOKEN_NER_CAP:
        # A lone capitalised word is the weakest NER signal (headings, product words). Keep it in the
        # review band: still redacted (fail closed), not used to propagate, flagged for a human.
        f.score = SINGLE_TOKEN_NER_CAP
        f.reasons.append(f"single-token NER hit: capped at {SINGLE_TOKEN_NER_CAP:.2f} (review)")
    if span.kind in ("heading", "metadata") and len(f.text.split()) == 1:
        f.decision = "drop"
        f.reasons.append(f"single-token NER hit in a {span.kind}: dropped")
    return f


SINGLE_TOKEN_NER_CAP = 0.5


def merge(findings: list[Finding]) -> list[Finding]:
    """Within one span: collapse overlapping hits. Agreement between layers raises the score."""
    out: list[Finding] = []
    by_span: dict[str, list[Finding]] = {}
    for f in findings:
        by_span.setdefault(f.span_id, []).append(f)
    for span_id, group in by_span.items():
        group.sort(key=lambda f: (f.start, -f.end))
        clusters: list[list[Finding]] = []
        for f in group:
            if clusters and f.start < max(g.end for g in clusters[-1]):
                clusters[-1].append(f)
            else:
                clusters.append([f])
        for cl in clusters:
            out.extend(_resolve_cluster(cl))
    return out


def _resolve_cluster(cl: list[Finding]) -> list[Finding]:
    if len(cl) == 1:
        return cl
    winner = max(cl, key=lambda f: (PRIORITY.get(f.entity_type, 3) * (1 if f.score >= 0.35 else 0), f.score, f.end - f.start))
    agree = [f for f in cl if f.entity_type == winner.entity_type and f is not winner]
    others = [f for f in cl if f.entity_type != winner.entity_type]
    # Same category from different layers: widen to the union and reward agreement.
    for f in agree:
        # Only widen on comparable evidence: a weak fuzzy match must not stretch a strong exact one.
        if f.start < winner.end and f.end > winner.start and f.score >= winner.score - 0.15:
            new_start, new_end = min(winner.start, f.start), max(winner.end, f.end)
            if new_end - new_start <= (winner.end - winner.start) * 1.8 + 3:
                winner.start, winner.end = new_start, new_end
    layers = sorted({f.layer for f in [winner] + agree})
    if len(layers) > 1:
        bonus = round(min(0.1 * (len(layers) - 1), 0.2), 2)
        winner.score = round(min(1.0, max(f.score for f in [winner] + agree) + bonus), 3)
        winner.reasons.append(f"confirmed by {len(layers)} layers ({', '.join(layers)}) (+{bonus:.2f})")
    kept = [winner]
    for f in agree:
        if f.start >= winner.start and f.end <= winner.end:
            winner.reasons.extend(r for r in f.reasons if r not in winner.reasons)
        else:
            kept.append(f)  # e.g. a second name in the same cell: never silently discard a hit
    # A different category survives only if it is not swallowed by the winner.
    for f in others:
        if f.start >= winner.start and f.end <= winner.end:
            continue
        if f.start < winner.end and f.end > winner.start and PRIORITY.get(f.entity_type, 3) <= PRIORITY.get(winner.entity_type, 3):
            continue
        kept.append(f)
    return kept


def route(f: Finding, s: Settings) -> Finding:
    if f.decision == "drop":
        return f
    if f.score >= s.redact_threshold:
        f.decision = "redact"
    elif f.score >= s.review_threshold:
        f.decision = "review"  # still redacted downstream: fail closed, a human confirms later
    else:
        f.decision = "drop"
    return f


def annotate(f: Finding, span: Span) -> Finding:
    f.page, f.location, f.kind, f.source = span.page, span.location, span.kind, span.source
    f.text = span.text[f.start:f.end]
    if span.kind == "table_cell":
        f.context_type = "table"
    elif span.source == "image_ocr" or span.kind == "image_ocr":
        f.context_type = "image"
    elif span.kind == "metadata":
        f.context_type = "metadata"
    else:
        line_start = span.text.rfind("\n", 0, f.start) + 1
        f.context_type = "labelled" if LABEL_BEFORE.search(span.text[line_start:f.start]) else "narrative"
    return f


def finalise(doc_findings: dict[str, list[Finding]], docs: dict[str, Document], settings: Settings) -> dict[str, list[Finding]]:
    allow = {a.lower() for a in settings.allow_list}
    out = {}
    for file, findings in doc_findings.items():
        doc = docs[file]
        cleaned = []
        # Work on copies: finalise runs again after propagation and must start from the raw hits.
        for f in (replace(f, reasons=list(f.reasons)) for f in findings):
            span = doc.span(f.span_id)
            f = clean_person(f, span, allow)
            if f is not None:
                cleaned.append(f)
        live = [f for f in cleaned if f.decision != "drop"]
        dropped = [f for f in cleaned if f.decision == "drop"]
        merged = merge(live)
        out[file] = sorted(
            [annotate(route(f, settings), doc.span(f.span_id)) for f in merged] + dropped,
            key=lambda f: (f.page or 0, f.span_id, f.start),
        )
    return out


def is_person_confirmed(f: Finding, settings: Settings) -> bool:
    return f.entity_type == "PERSON" and f.decision == "redact" and looks_like_name(f.text)
