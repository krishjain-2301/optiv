"""Layer L4: propagation. Every confirmed person is searched for across the whole document set
by full name, initial + surname, surname alone, first name alone and possessive forms. This is
what catches "Rafael's own record" and "Raman escalated..." in unlabelled narrative prose.

Matching ignores letter case for multi-word variants ("PRIYA RAMAN", "priya raman"). A single
word matches only as written or in capitals ("Raman", "RAMAN"), or in lowercase when it is a
listed given name: lowercase single words are too often ordinary vocabulary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from ..models import Document, Finding
from .names import INITIAL, TITLE, WORD, is_given_name, name_tokens, name_variants, strip_possessive, token_case


@dataclass
class PersonIndex:
    """Canonical people and every variant that points at them. Keys are lowercase."""

    canon: dict[str, str] = field(default_factory=dict)  # variant (lower) -> canonical full name
    forms: dict[str, str] = field(default_factory=dict)  # variant (lower) -> variant as written
    ambiguous: set[str] = field(default_factory=set)
    scores: dict[str, float] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)  # canonical -> where first confirmed
    aliases: dict[str, str] = field(default_factory=dict)  # OCR misreading (lower) -> canonical ("Jahn Davis")

    def add(self, full: str, origin: str) -> None:
        toks = name_tokens(full)
        if not toks:
            return
        variants = name_variants(full)
        display = max(variants, key=lambda v: (len(v.split()), variants[v])) if variants else " ".join(toks)
        # A shorter name that is already a variant of a longer confirmed name joins that person.
        canonical = self.canon.get(display.lower(), display)
        self.origin.setdefault(canonical, origin)
        for variant, score in variants.items():
            key = variant.lower()
            existing = self.canon.get(key)
            if existing and existing != canonical and len(variant.split()) == 1:
                if not _same_person(existing, canonical):
                    self.ambiguous.add(key)
                continue
            self.canon.setdefault(key, canonical)
            self.forms.setdefault(key, variant)
            self.scores[key] = max(self.scores.get(key, 0), score)

    def canonical(self, text: str) -> str:
        t = " ".join(name_tokens(strip_possessive(text)))
        return self.canon.get(t.lower()) or self.aliases.get(t.lower()) or t

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


def name_candidates(text: str) -> list[tuple[int, int]]:
    """Two or three Title-case words on one line, optionally with a middle initial (any script):
    candidate names in OCR'd text."""
    out, run = [], []

    def flush():
        # Every window of 2-3 words (plus initials) that starts and ends on a full word, so
        # "Policy Owner Jahn Davis" still yields "Jahn Davis"; idx.fuzzy decides which one is a person.
        for i in range(len(run)):
            for j in range(i + 1, min(i + 4, len(run))):
                window = run[i:j + 1]
                long_words = sum(token_case(w.group()) == TITLE for w in window)
                if 2 <= long_words <= 3 and token_case(window[0].group()) == TITLE == token_case(window[-1].group()):
                    out.append((window[0].start(), window[-1].end()))

    for m in WORD.finditer(text):
        shape = token_case(m.group())
        joined = run and re.fullmatch(r"[ \t]+", text[run[-1].end():m.start()] or "x")
        if shape in (TITLE, INITIAL) and (joined or not run):
            run.append(m)
            continue
        flush()
        run = [m] if shape in (TITLE, INITIAL) else []
    flush()
    return out


def _same_person(a: str, b: str) -> bool:
    ta, tb = {t.lower() for t in name_tokens(a)}, {t.lower() for t in name_tokens(b)}
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


def _accept(matched: str, key: str, idx: PersonIndex) -> bool:
    """Case rule for one match (see module docstring)."""
    if len(key.split()) > 1:
        return True
    written = idx.forms.get(key, matched)
    return matched in (written, written.upper()) or (matched.islower() and is_given_name(matched))


def propagate(docs: dict[str, Document], idx: PersonIndex) -> list[Finding]:
    keys = sorted(idx.canon, key=len, reverse=True)
    if not keys:
        return []
    pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(k) for k in keys) + r")(?:'s|’s|'|’)?(?![\w-])",
                         re.IGNORECASE)
    out: list[Finding] = []
    for doc in docs.values():
        for span in doc.spans:
            for m in pattern.finditer(span.text):
                variant = m.group(1)
                key = variant.lower()
                if key not in idx.canon or not _accept(variant, key, idx):
                    continue
                canonical = idx.canon[key]
                score = idx.scores.get(key, 0.7)
                reasons = [f"matches confirmed person '{canonical}' (first confirmed at {idx.origin.get(canonical, '?')})"]
                if variant != idx.forms.get(key, variant):
                    reasons.append("letter case differs from the confirmed mention")
                if key in idx.ambiguous:
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
    for a, b in name_candidates(span.text):
        toks = name_tokens(span.text[a:b])
        cand = " ".join(toks)
        if cand.lower() in idx.canon:
            continue  # exact match: handled by the pattern above
        hit = idx.fuzzy(cand)
        if hit is None:
            continue
        full, score = hit
        idx.aliases[cand.lower()] = full
        out.append(Finding(span_id=span.id, file=span.file, start=a, end=b, text=span.text[a:b],
                           entity_type="PERSON", score=0.72, recognizer="propagation:fuzzy-ocr",
                           layer="L4 propagation",
                           reasons=[f"OCR variant of confirmed person '{full}' (similarity {score:.0f}%)",
                                    f"first confirmed at {idx.origin.get(full, '?')}"]))
    return out
