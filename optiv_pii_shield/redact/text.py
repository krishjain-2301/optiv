"""Redacted text for the LLM: every span with its PII replaced by stable tokens."""
from __future__ import annotations

from ..config import Settings
from ..models import Document, Finding
from ..render import render_markdown

LIVE = ("redact", "review")  # review-band findings are redacted too: fail closed


def merged_ranges(findings: list[Finding]) -> list[tuple[int, int, str]]:
    """Non-overlapping (start, end, token) ranges; overlapping hits collapse into the first token."""
    ranges = sorted((f.start, f.end, f.token or f"[{f.entity_type}]") for f in findings if f.decision in LIVE)
    out: list[list] = []
    for s, e, t in ranges:
        if out and s < out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e, t])
    return [tuple(r) for r in out]


def apply(text: str, ranges: list[tuple[int, int, str]]) -> str:
    for s, e, t in sorted(ranges, reverse=True):
        text = text[:s] + t + text[e:]
    return text


def redacted_span_texts(doc: Document, findings: list[Finding], settings: Settings) -> dict[str, str]:
    by_span: dict[str, list[Finding]] = {}
    for f in findings:
        by_span.setdefault(f.span_id, []).append(f)
    withheld = {i.id for i in doc.images if i.ocr_status in ("unreadable", "low_confidence")} if settings.withhold_low_conf_images else set()
    out = {}
    for s in doc.spans:
        if s.image_ref in withheld:
            out[s.id] = "[IMAGE_TEXT_WITHHELD]"
        elif s.id in by_span:
            out[s.id] = apply(s.text, merged_ranges(by_span[s.id]))
    return out


def redacted_markdown(doc: Document, findings: list[Finding], settings: Settings) -> str:
    return render_markdown(doc, redacted_span_texts(doc, findings, settings))
