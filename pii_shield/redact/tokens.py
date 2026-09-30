"""Stable pseudonymous tokens, consistent across every file in a run.

Each person gets a number; their e-mail, phone and IDs reuse it when they can be linked (same
table row, or the e-mail's local part matches the name), so an LLM can still reason about
"who did what": ``[PERSON_007]`` and ``[EMAIL_007]`` are the same individual.

The vault (token -> original value) is the only place the originals survive. It is written to
the output folder for authorised re-identification and must be stored as sensitive data.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from ..detect.names import name_tokens
from ..detect.propagation import PersonIndex
from ..models import Document, Finding

SHORT = {
    "PERSON": "PERSON", "EMAIL_ADDRESS": "EMAIL", "PHONE_NUMBER": "PHONE", "EMPLOYEE_ID": "EMP_ID",
    "VENDOR_ID": "VENDOR_ID", "US_SSN": "SSN", "PASSPORT": "PASSPORT", "IN_PAN": "PAN", "PL_PESEL": "PESEL",
    "TAX_ID": "TIN", "NATIONAL_ID": "NATIONAL_ID", "CREDIT_CARD": "CARD", "IBAN_CODE": "IBAN",
    "DATE_OF_BIRTH": "DOB", "ADDRESS": "ADDRESS", "LOW_CONFIDENCE_OCR": "UNREADABLE",
}
LINKABLE = {"EMAIL_ADDRESS", "PHONE_NUMBER", "EMPLOYEE_ID", "US_SSN", "PASSPORT", "IN_PAN", "PL_PESEL", "TAX_ID",
            "DATE_OF_BIRTH", "ADDRESS", "NATIONAL_ID"}


def normalise(entity: str, value: str) -> str:
    v = value.strip()
    if entity == "EMAIL_ADDRESS":
        return re.sub(r"\s", "", v.lower())
    if entity in ("PHONE_NUMBER", "CREDIT_CARD", "US_SSN", "TAX_ID"):
        return re.sub(r"\D", "", v)
    if entity == "PERSON":
        return " ".join(name_tokens(v)).lower()
    return re.sub(r"\s+", " ", v.upper())


class TokenVault:
    def __init__(self, persons: PersonIndex | None = None):
        self.persons = persons or PersonIndex()
        self.person_no: dict[str, int] = {}  # canonical person -> number
        self.tokens: dict[tuple[str, str], str] = {}  # (entity, normalised value) -> token
        self.values: dict[str, set[str]] = defaultdict(set)  # token -> original surface forms
        self.counters: dict[str, int] = defaultdict(int)

    # ---------------------------------------------------------------------------------
    def _person_number(self, canonical: str) -> int:
        if canonical not in self.person_no:
            self.person_no[canonical] = len(self.person_no) + 1
        return self.person_no[canonical]

    def person_for_email(self, email: str) -> str | None:
        local = re.sub(r"[^a-z]", " ", email.split("@")[0].lower()).split()
        joined = "".join(local)
        best = None
        for canonical in self.person_no:
            toks = [t.lower().rstrip(".") for t in name_tokens(canonical)]
            if len(toks) < 2:
                continue
            first, last = toks[0], re.sub(r"[^a-z]", "", toks[-1])
            forms = {first + last, first[0] + last, last + first[0], last + first, first + "." + last}
            if joined in {f.replace(".", "") for f in forms} or (last in joined and first[0] == joined[0]):
                best = canonical
                break
        return best

    def assign(self, f: Finding, row_person: str | None = None) -> str:
        entity = f.entity_type
        short = SHORT.get(entity, entity)
        key = (entity, normalise(entity, f.text))
        if entity == "PERSON":
            canonical = self.persons.canonical(f.text)
            key = (entity, canonical.lower())
            if key not in self.tokens:
                self.tokens[key] = f"[PERSON_{self._person_number(canonical):03d}]"
        elif key not in self.tokens:
            owner = None
            if entity in LINKABLE:
                owner = row_person or (self.person_for_email(f.text) if entity == "EMAIL_ADDRESS" else None)
            if owner is not None:
                n = self._person_number(owner)
                base = f"{short}_{n:03d}"
                existing = [t for t in self.tokens.values() if t.startswith(f"[{base}")]
                token = f"[{base}]" if not existing else f"[{base}_{len(existing) + 1}]"
            else:
                self.counters[short] += 1
                token = f"[{short}_U{self.counters[short]:03d}]"
            self.tokens[key] = token
        token = self.tokens[key]
        self.values[token].add(f.text)
        f.token = token
        return token

    # ---------------------------------------------------------------------------------
    def assign_all(self, docs: dict[str, Document], findings: dict[str, list[Finding]]) -> None:
        """People first (so numbers follow first appearance), then everything else, linking by table row."""
        live = [f for fs in findings.values() for f in fs if f.decision in ("redact", "review")]
        for f in live:
            if f.entity_type == "PERSON":
                self.assign(f)
        row_owner: dict[tuple, str] = {}
        for f in live:
            if f.entity_type != "PERSON":
                continue
            span = docs[f.file].span(f.span_id)
            if span.table is not None:
                row_owner.setdefault((f.file, span.page, span.table[0], span.table[1]), self.persons.canonical(f.text))
        for f in live:
            if f.entity_type == "PERSON":
                continue
            span = docs[f.file].span(f.span_id)
            owner = None
            if span.table is not None:
                owner = row_owner.get((f.file, span.page, span.table[0], span.table[1]))
            self.assign(f, owner)

    def to_json(self) -> dict:
        return {
            "warning": "SENSITIVE: maps pseudonymous tokens back to original PII. Store encrypted, restrict access.",
            "tokens": {t: sorted(v) for t, v in sorted(self.values.items())},
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
