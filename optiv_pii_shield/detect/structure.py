"""Layer L3: structure. A column header ("E-mail", "National ID") or a field label
("Full name:", "TIN:") says what the value next to it is, even when no pattern or model fires.
Document properties (author, last modified by) are handled the same way."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional

from ..config import HEADER_CATEGORIES
from ..models import Finding, Span
from .names import ANY_CASE, is_given_name, looks_like_name

EMPTY = {"", "-", "–", "—", "n/a", "na", "none", "tbd", "tbc", "nil", "null", "yes", "no", "x", "?", "various", "all"}
LABEL_LINE = re.compile(r"(?m)^[ \t]*(?P<label>[A-Za-z][A-Za-z .#/()'-]{1,40}?)[ \t]*[:：][ \t]*(?P<value>[^\n]+?)[ \t]*$")
SEGMENT_SPLIT = re.compile(r"\s{3,}|\s[|;]\s")


@lru_cache(maxsize=512)
def header_category(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    lab = label.strip().lower()
    if len(lab) > 60:
        return None
    for pattern, cat in HEADER_CATEGORIES:
        if re.search(pattern, lab):
            if cat == "PERSON" and NON_PERSON_HEADER.search(lab):
                continue  # "Vendor name", "System owner group", "File name" ...
            return cat
    return None


NON_PERSON_HEADER = re.compile(
    r"vendor|supplier|compan|organi[sz]ation|engagement|product|system|application|project|file|document|"
    r"template|field|control name|risk name|process name|group|department|team|business unit|site|entity|"
    r"task|trigger|rule|stage|workflow|role|attribute|question|report|form\b|queue|object|step|status"
)


def plausible(value: str, cat: str, allow: set[str]) -> tuple[bool, str]:
    v = value.strip()
    if v.lower() in EMPTY or len(v) < 2:
        return False, "empty"
    digits = sum(c.isdigit() for c in v)
    if cat == "PERSON":
        # The header / label already says "this is a person": accept any letter case.
        return looks_like_name(v, allow, cases=ANY_CASE), "name-shaped"
    if cat == "EMAIL_ADDRESS":
        return "@" in v or re.search(r"\w[.\s]\w+\s?(?:com|org|net|example)\b", v) is not None, "contains @"
    if cat == "PHONE_NUMBER":
        return digits >= 7, "7+ digits"
    if cat == "DATE_OF_BIRTH":
        return digits >= 2 and re.search(r"(?:19|20)?\d{2}", v) is not None, "date-shaped"
    if cat == "ADDRESS":
        return len(v) >= 8 and (digits > 0 or "," in v), "address-shaped"
    # identifiers: need some digits or a long alphanumeric token
    return digits >= 3 or bool(re.fullmatch(r"[A-Z0-9-]{6,}", v.replace(" ", ""))), "identifier-shaped"


def _finding(span: Span, start: int, end: int, cat: str, score: float, reasons: list[str], rec: str) -> Finding:
    return Finding(span_id=span.id, file=span.file, start=start, end=end, text=span.text[start:end], entity_type=cat,
                   score=score, recognizer=rec, layer="L3 structure", reasons=reasons)


# Who is speaking, in a transcript or a chat export: "Dana Whitlock: ...", "[10:32] Dana: ...".
SPEAKER_LINE = re.compile(r"(?m)^[ \t]*(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?[ \t]+)?(?P<name>[A-Z][\w'.-]*(?:[ \t]+[A-Z][\w'.-]*){0,3})"
                          r"[ \t]*(?:\(\d{1,2}:\d{2}(?::\d{2})?\))?:[ \t]+\S")
NOT_A_SPEAKER = {"speaker", "interviewer", "interviewee", "moderator", "host", "operator", "narrator", "unknown", "agent",
                 "customer", "caller", "participant", "audience", "presenter", "chair", "all", "everyone", "q", "a"}


def _speaker_findings(span: Span, allow: set[str]) -> list[Finding]:
    text = span.text
    if span.kind == "speaker":  # a transcript's own speaker field (extract/transcript.py)
        value = text.strip()
        if re.sub(r"[\d\s#]+$", "", value).lower() in NOT_A_SPEAKER or not plausible(value, "PERSON", allow)[0]:
            return []
        start = len(text) - len(text.lstrip())
        return [_finding(span, start, start + len(value), "PERSON", 0.8, ["named as the speaker of a transcript cue"], "structure:speaker")]
    out = []
    labels = [m.group("name") for m in SPEAKER_LINE.finditer(text)]
    for m in SPEAKER_LINE.finditer(text):
        name = m.group("name")
        # In running text a capitalised label is usually a heading ("Next Steps:"). It is taken for a
        # speaker when it starts with a listed given name, or when it speaks more than once.
        if not (is_given_name(name.split()[0]) or labels.count(name) >= 2) or name.lower() in NOT_A_SPEAKER or header_category(name):
            continue
        if plausible(name, "PERSON", allow)[0]:
            out.append(_finding(span, m.start("name"), m.end("name"), "PERSON", 0.7,
                                ["a speaker label at the start of a line"], "structure:speaker"))
    return out


def structure_findings(span: Span, allow: set[str]) -> list[Finding]:
    out: list[Finding] = _speaker_findings(span, allow)
    text = span.text

    # 1. Column header / metadata field describing the whole span.
    cat = header_category(span.header) if span.kind in ("table_cell", "metadata") else None
    if cat:
        for m in re.finditer(r"[^\n]+", text):
            line = m.group()
            lstrip = len(line) - len(line.lstrip())
            value = line.strip()
            ok, why = plausible(value, cat, allow)
            if not ok:
                continue
            start = m.start() + lstrip
            what = "column header" if span.kind == "table_cell" else "document property"
            out.append(_finding(span, start, start + len(value), cat, 0.75,
                                [f"{what} '{span.header}' implies {cat}", f"value is {why}"], f"structure:{span.kind}"))

    # 2. "Label: value" lines inside running text or cells.
    for m in LABEL_LINE.finditer(text):
        lcat = header_category(m.group("label"))
        if not lcat:
            continue
        value_start = m.start("value")
        value = m.group("value")
        # A value may be followed by further columns on the same OCR line: keep the first segment.
        seg = SEGMENT_SPLIT.split(value)[0]
        if lcat == "PERSON":
            seg = re.split(r",|\s[-–(]\s?|\(", seg)[0].strip()
        seg = seg.strip().rstrip(".;,")
        ok, why = plausible(seg, lcat, allow)
        if not ok:
            continue
        s = value_start + value.find(seg)
        out.append(_finding(span, s, s + len(seg), lcat, 0.72,
                            [f"field label '{m.group('label').strip()}:' implies {lcat}", f"value is {why}"],
                            "structure:label"))
    return out
