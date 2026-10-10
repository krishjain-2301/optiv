"""Layer L3: structure. A column header ("E-mail", "National ID") or a field label
("Full name:", "TIN:") says what the value next to it is, even when no pattern or model fires.
Document properties (author, last modified by) are handled the same way."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional

from ..config import HEADER_CATEGORIES, NOT_A_NAME_WORDS
from ..models import Finding, Span
from .names import ANY_CASE, COMMON_TITLECASE, is_given_name, looks_like_name

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
                 "customer", "caller", "participant", "audience", "presenter", "chair", "all", "everyone", "q", "a",
                 "question", "answer", "response", "reply", "comment", "comments", "note", "result", "summary",
                 "action", "attribute", "section", "condition", "example", "step", "status", "subject", "topic"}


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
        # A repeated label inside a table cell is a field of a form ("Question: 1.6", "Response: Yes").
        repeated = labels.count(name) >= 2 and span.kind != "table_cell" and not NON_PERSON_HEADER.search(name.lower())
        if not (is_given_name(name.split()[0]) or repeated) or name.lower() in NOT_A_SPEAKER or header_category(name):
            continue
        if plausible(name, "PERSON", allow)[0]:
            out.append(_finding(span, m.start("name"), m.end("name"), "PERSON", 0.7,
                                ["a speaker label at the start of a line"], "structure:speaker"))
    return out


def name_list_findings(docs: dict, findings: dict[str, list[Finding]], allow: set[str]) -> list[Finding]:
    """A table cell that lists people, one per line: when at least half of its lines are names
    already found, the other name-shaped lines are people too ("DK Lindt" between "Sarah
    Varga" and "John Lindt"), whatever the column is called."""
    people: dict[str, list[tuple[int, int]]] = {}
    for fs in findings.values():
        for f in fs:
            if f.entity_type == "PERSON" and f.decision == "redact":
                people.setdefault(f.span_id, []).append((f.start, f.end))
    out = []
    for doc in docs.values():
        for span in doc.spans:
            if span.kind != "table_cell" or span.id not in people:
                continue
            lines = [m for m in re.finditer(r"[^\n]+", span.text) if m.group().strip()]
            if len(lines) < 3:
                continue

            def named(m) -> bool:
                got = sum(min(b, m.end()) - max(a, m.start()) for a, b in people[span.id] if a < m.end() and b > m.start())
                return got >= 0.8 * len(m.group().strip())

            rest = [m for m in lines if not named(m)]
            if len(lines) - len(rest) < max(2, len(lines) / 2):
                continue
            for m in rest:
                value = m.group().strip()
                if looks_like_name(value, allow, min_tokens=2):
                    start = m.start() + len(m.group()) - len(m.group().lstrip())
                    out.append(_finding(span, start, start + len(value), "PERSON", 0.65,
                                        [f"a line in a cell that lists {len(lines) - len(rest)} people"], "structure:name-list"))
    return out


# Given names that are also everyday words. Left out of the gazetteer, because "mark the form" is
# not a person; but alone on a line of a table or a screen, "Mark Lund" is one.
WORD_LIKE_GIVEN = {"mark", "will", "bill", "grace", "hope", "rose", "joy", "dawn", "faith", "jack", "frank", "grant",
                   "rob", "bob", "pat", "sue", "ray", "dan", "don", "nick", "josh", "rod", "sandy", "sally", "amber",
                   "art", "carol", "dean", "earl", "holly", "ivy", "lance", "miles", "pearl", "penny", "ruby", "victor"}
NAME_LINE = re.compile(r"(?m)^[ \t]*([A-Z][a-z]+)[ \t]([A-Z][a-z]+(?:[-'’][A-Z][a-z]+)?)[ \t]*$")


def _name_line_findings(span: Span, allow: set[str]) -> list[Finding]:
    if span.kind != "table_cell" and span.source != "image_ocr":
        return []
    return [_finding(span, m.start(1), m.end(2), "PERSON", 0.5,
                     [f"'{m.group(1)}' is a given name and the line holds nothing but two capitalised words"], "structure:name-line")
            for m in NAME_LINE.finditer(span.text)
            if m.group(1).lower() in WORD_LIKE_GIVEN and m.group(2).lower() not in allow
            and m.group(2).lower() not in NOT_A_NAME_WORDS and m.group(2).lower() not in COMMON_TITLECASE]


def structure_findings(span: Span, allow: set[str]) -> list[Finding]:
    out: list[Finding] = _speaker_findings(span, allow) + _name_line_findings(span, allow)
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
