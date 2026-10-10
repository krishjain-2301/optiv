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
from .names import INITIAL, TITLE, UPPER, WORD, is_given_name, name_tokens, name_variants, strip_possessive, token_case
from .vocab import is_vocabulary


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
                long_words = sum(token_case(w.group()) != INITIAL for w in window)
                if 2 <= long_words <= 3 and token_case(window[0].group()) != INITIAL and token_case(window[-1].group()) == TITLE:
                    out.append((window[0].start(), window[-1].end()))

    for m in WORD.finditer(text):
        shape = token_case(m.group())
        if shape == UPPER and len(m.group()) <= 3:
            shape = TITLE  # two or three capitals used as a first name ("TK Moreau")
        joined = run and re.fullmatch(r"[ \t]+", text[run[-1].end():m.start()] or "x")
        if shape in (TITLE, INITIAL) and (joined or not run):
            run.append(m)
            continue
        flush()
        run = [m] if shape in (TITLE, INITIAL) else []
    flush()
    return out


def _same_person(a: str, b: str) -> bool:
    """One name is the other with words left out or cut to initials: "N. C. Okpara", "Ngozi C.
    Okpara" and "Okpara" are one person; "Anna Zubiri" and "Tomas Zubiri" are two."""
    ta, tb = [t.lower().rstrip(".") for t in name_tokens(a)], [t.lower().rstrip(".") for t in name_tokens(b)]
    if not ta or not tb or ta[-1] != tb[-1]:
        return bool(set(ta) & set(tb)) and (set(ta) <= set(tb) or set(tb) <= set(ta))
    short, long_ = sorted((ta[:-1], tb[:-1]), key=len)
    return all(any(x == y or (min(len(x), len(y)) == 1 and x[0] == y[0]) for y in long_) for x in short)


def build_index(findings: dict[str, list[Finding]], extra_names: list[str]) -> PersonIndex:
    idx = PersonIndex()
    # Only strong evidence seeds propagation: multi-token names, or single names read from a
    # labelled field / name column. A lone NER word ("Training") must never spread.
    confirmed = [f for fs in findings.values() for f in fs if f.entity_type == "PERSON" and f.decision == "redact"
                 and (len(name_tokens(f.text)) >= 2 or f.layer.startswith("L3") or f.recognizer == "rule:honorific_name")]
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


def _screen_name(matched: str, key: str, idx: PersonIndex) -> bool:
    """Screens write a person's first name in lowercase (a user name, "tamara.Cadence")."""
    full = idx.canon.get(key, "").split()
    return matched.islower() and len(matched) >= 5 and len(full) >= 2 and full[0].lower() == key


# A name ends where a word ends, or where OCR ran two words together ("ChairYelena", "OlsenRob").
EDGE = r"(?:{plain}|(?<=(?-i:[a-z]))(?=(?-i:[A-Z])))"
LEFT, RIGHT = EDGE.format(plain=r"(?<!\w)"), EDGE.format(plain=r"(?!\w)")
HYPHEN_BEFORE, HYPHEN_AFTER = re.compile(r"([^\W\d_]+)-$"), re.compile(r"-([^\W\d_]+)")


def _other_half(word: str, vocab: frozenset[str]) -> bool:
    """Does the word joined on by a hyphen make the match part of an ordinary compound ("Page-level",
    "Park-and-ride")? A capitalised word or a code does not ("CTL-Tomas", "Moreau-Unlikely":
    OCR ran two table columns together), and neither does an unknown second surname: the known
    half of "Varga-Lindt" is still masked."""
    return word.islower() or (is_vocabulary(word, vocab) and token_case(word) != TITLE)


def propagate(docs: dict[str, Document], idx: PersonIndex, vocab: frozenset[str] = frozenset()) -> list[Finding]:
    keys = sorted(idx.canon, key=len, reverse=True)
    if not keys:
        return []
    pattern = re.compile(LEFT + "(" + "|".join(re.escape(k) for k in keys) + r")(?:'s|’s|'|’)?" + RIGHT, re.IGNORECASE)
    out: list[Finding] = []
    for doc in docs.values():
        for span in doc.spans:
            for m in pattern.finditer(span.text):
                variant = m.group(1)
                key = variant.lower()
                if key not in idx.canon:
                    continue
                before, after = HYPHEN_BEFORE.search(span.text, 0, m.start(1)), HYPHEN_AFTER.match(span.text, m.end())
                if (before and _other_half(before.group(1), vocab)) or (after and _other_half(after.group(1), vocab)):
                    continue
                glued = (m.start(1) > 0 and span.text[m.start(1) - 1].isalnum()) or span.text[m.end():m.end() + 1].isalnum()
                screen = span.source != "native" and not glued and _screen_name(variant, key, idx)
                if not (screen or (_accept(variant, key, idx) and not (glued and variant != idx.forms.get(key)))):
                    continue
                canonical = idx.canon[key]
                score = 0.5 if screen and not _accept(variant, key, idx) else idx.scores.get(key, 0.7)
                reasons = [f"matches confirmed person '{canonical}' (first confirmed at {idx.origin.get(canonical, '?')})"]
                if glued:
                    reasons.append("written as one word with its neighbour")
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
                out.extend(_fuzzy_initial_surnames(span, idx))
    return out


INITIALS_SURNAME = re.compile(r"(?<![\w.])([A-Z])\.[  ]?(?:[A-Z](?:\.[  ]?|[  ])){0,2}"
                              r"([^\W\d_]{4,}(?:-[^\W\d_]+)?)(?![\w@])")


def _fuzzy_initial_surnames(span, idx: PersonIndex) -> list[Finding]:
    """Initials and a misread surname in OCR'd text ("F.L.Kittelsn", "C.A kittels"): matched to a
    confirmed person by the surname, more loosely when the first initial agrees as well."""
    out = []
    people = {c for c in idx.canon.values() if len(c.split()) >= 2}
    for m in INITIALS_SURNAME.finditer(span.text):
        surname = m.group(2).lower()
        if surname in idx.canon:
            continue  # spelled right: the pattern in propagate() has it
        best = None
        for full in people:
            toks = full.split()
            score = fuzz.ratio(surname, toks[-1].lower())
            if len(toks[-1]) >= 5 and (score >= 88 or (score >= 78 and toks[0][0].upper() == m.group(1))):
                if best is None or score > best[1]:
                    best = (full, score)
        if best:
            out.append(Finding(span_id=span.id, file=span.file, start=m.start(), end=m.end(), text=m.group(),
                               entity_type="PERSON", score=0.72, recognizer="propagation:fuzzy-ocr", layer="L4 propagation",
                               reasons=[f"OCR variant of confirmed person '{best[0]}' (surname similarity {best[1]:.0f}%)",
                                        f"first confirmed at {idx.origin.get(best[0], '?')}"]))
    return out


ID_CORE = re.compile(r"\d{5,}")
ID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_-]*[A-Za-z0-9]")


def propagate_ids(docs: dict[str, Document], findings: dict[str, list[Finding]]) -> list[Finding]:
    """The number of a confirmed employee ID inside another code ("CFG-47216-0091-2026" on a badge
    issued to EMP-47216) identifies the same person. A bare number is left alone: five digits on
    their own are as likely a postcode or an amount."""
    cores: dict[str, str] = {}
    found: dict[str, list[tuple[int, int]]] = {}
    for fs in findings.values():
        for f in fs:
            if f.decision != "drop":
                found.setdefault(f.span_id, []).append((f.start, f.end))
            if f.entity_type == "EMPLOYEE_ID" and f.decision == "redact" and len(m := ID_CORE.findall(f.text)) == 1:
                cores.setdefault(m[0], f.text)
    if not cores:
        return []
    pattern = re.compile(r"(?<!\d)(?:" + "|".join(sorted(cores, key=len, reverse=True)) + r")(?!\d)")
    out = []
    for doc in docs.values():
        for span in doc.spans:
            for tok in ID_TOKEN.finditer(span.text):
                m = pattern.search(tok.group())
                if not m or tok.group().isdigit():
                    continue
                at = tok.start() + m.start()
                if any(a < at + len(m.group()) and b > at for a, b in found.get(span.id, [])):
                    continue  # already found as an ID, an e-mail address, ...
                if not re.search(r"[-/_]\d|\d[-/_]|[A-Za-z]{2,}\d|\d[A-Za-z]{2,}", tok.group()):
                    continue
                out.append(Finding(span_id=span.id, file=span.file, start=tok.start(), end=tok.end(), text=tok.group(),
                                   entity_type="EMPLOYEE_ID", score=0.5, recognizer="propagation:id-number",
                                   layer="L4 propagation",
                                   reasons=[f"contains the number of confirmed ID '{cores[m.group()]}'"]))
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
