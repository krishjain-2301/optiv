"""Layer L4: propagation. Every confirmed person is searched for across the whole document set
by full name, initial + surname, surname alone, first name alone and possessive forms. This is
what catches "Rafael's own record" and "Raman escalated..." in unlabelled narrative prose."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from ..models import Document, Finding
from .names import name_tokens, name_variants, strip_possessive


@dataclass
class PersonIndex:
    """Canonical people and every variant that points at them."""

    canon: dict[str, str] = field(default_factory=dict)  # variant -> canonical full name
    ambiguous: set[str] = field(default_factory=set)
    scores: dict[str, float] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)  # canonical -> where first confirmed
    aliases: dict[str, str] = field(default_factory=dict)  # OCR misreading -> canonical ("Jahn Davis")

    def add(self, full: str, origin: str) -> None:
        full = " ".join(name_tokens(full))
        if not full:
            return
        # A shorter name that is already a variant of a longer confirmed name joins that person.
        canonical = self.canon.get(full, full)
        self.origin.setdefault(canonical, origin)
        for variant, score in name_variants(full).items():
            existing = self.canon.get(variant)
            if existing and existing != canonical and len(variant.split()) == 1:
                if not _same_person(existing, canonical):
                    self.ambiguous.add(variant)
                continue
            self.canon.setdefault(variant, canonical)
            self.scores[variant] = max(self.scores.get(variant, 0), score)

    def canonical(self, text: str) -> str:
        t = " ".join(name_tokens(strip_possessive(text)))
        return self.canon.get(t) or self.aliases.get(t) or t

    def fuzzy(self, text: str) -> tuple[str, float] | None:
        """Closest confirmed full name for an OCR'd name ("Jahn Davis" -> "John Davis").
        Same token count, whole-name similarity >= FUZZY_MIN and surname similarity >= 80, so two
        different people who merely share a surname are not merged."""
        toks = name_tokens(text)
        best = None
        for full in {c for c in self.canon.values() if len(c.split()) == len(toks) >= 2}:
            score = fuzz.ratio(" ".join(toks).lower(), full.lower())
            if score >= FUZZY_MIN and fuzz.ratio(toks[-1].lower(), full.split()[-1].lower()) >= 80:
                if best is None or score > best[1]:
                    best = (full, score)
        return best


FUZZY_MIN = 85
# Two or three capitalised tokens (initials allowed): candidate names in OCR'd text.
NAME_CANDIDATE = re.compile(r"(?<![\w-])([A-Z][a-zA-Z'’-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z'’-]+){1,2})(?![\w-])")


def _same_person(a: str, b: str) -> bool:
    ta, tb = set(name_tokens(a)), set(name_tokens(b))
    return bool(ta & tb) and (ta <= tb or tb <= ta)


def build_index(findings: dict[str, list[Finding]], extra_names: list[str]) -> PersonIndex:
    idx = PersonIndex()
    # Only strong evidence seeds propagation: multi-token names, or single names read from a
    # labelled field / name column. A lone NER word ("Training") must never spread.
    confirmed = [f for fs in findings.values() for f in fs if f.entity_type == "PERSON" and f.decision == "redact"
                 and (len(name_tokens(f.text)) >= 2 or f.layer.startswith("L3"))]
    # Longest names first so "Rafael" attaches to "Rafael Mendoza-Kowalski", not the reverse.
    for f in sorted(confirmed, key=lambda f: -len(name_tokens(f.text))):
        idx.add(f.text, f"{f.file} / {f.location}")
    for n in extra_names:
        idx.add(n, "deny-list")
    return idx


def propagate(docs: dict[str, Document], idx: PersonIndex) -> list[Finding]:
    variants = sorted(idx.canon, key=len, reverse=True)
    if not variants:
        return []
    pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(v) for v in variants) + r")(?:'s|â€™s|')?(?![\w-])")
    out: list[Finding] = []
    for doc in docs.values():
        for span in doc.spans:
            for m in pattern.finditer(span.text):
                variant = m.group(1)
                canonical = idx.canon[variant]
                score = idx.scores.get(variant, 0.7)
                reasons = [f"matches confirmed person '{canonical}' (first confirmed at {idx.origin.get(canonical, '?')})"]
                if variant in idx.ambiguous:
                    score -= 0.1
                    reasons.append(f"'{variant}' is shared by more than one confirmed person (-0.10)")
                out.append(Finding(span_id=span.id, file=span.file, start=m.start(1), end=m.end(1), text=variant,
                                   entity_type="PERSON", score=round(score, 3), recognizer="propagation:person",
                                   layer="L4 propagation", reasons=reasons))
            if span.source != "native":
                out.extend(_fuzzy_ocr_names(span, idx))
    return out


def _fuzzy_ocr_names(span, idx: PersonIndex) -> list[Finding]:
    """OCR misreads names in small text ("Jahn Davis"). Match them to confirmed people so they are
    redacted and get the same token as the correctly spelled name."""
    out = []
    for m in NAME_CANDIDATE.finditer(span.text):
        cand = " ".join(name_tokens(m.group(1)))
        if cand in idx.canon:
            continue  # exact match: handled by the pattern above
        hit = idx.fuzzy(cand)
        if hit is None:
            continue
        full, score = hit
        idx.aliases[cand] = full
        out.append(Finding(span_id=span.id, file=span.file, start=m.start(1), end=m.end(1), text=m.group(1),
                           entity_type="PERSON", score=0.72, recognizer="propagation:fuzzy-ocr",
                           layer="L4 propagation",
                           reasons=[f"OCR variant of confirmed person '{full}' (similarity {score:.0f}%)",
                                    f"first confirmed at {idx.origin.get(full, '?')}"]))
    return out
