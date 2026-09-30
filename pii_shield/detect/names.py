"""What a personal name looks like, and what it doesn't. Shared by L2 filtering, L3 and L4."""
from __future__ import annotations

import re

from ..config import NOT_A_NAME_WORDS

PARTICLES = {"van", "von", "de", "da", "del", "della", "di", "du", "la", "le", "bin", "binti", "al", "el", "dos", "das", "ter", "den", "der", "y"}
HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "dame"}
COMMON_TITLECASE = {
    "the", "this", "that", "these", "those", "a", "an", "and", "or", "if", "when", "while", "during", "after",
    "before", "for", "from", "with", "without", "within", "on", "in", "at", "by", "to", "of", "all", "any", "each",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
    "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "she", "he", "they",
    "we", "you", "it", "his", "her", "their", "our", "mr", "mrs", "ms", "dr", "tier", "note", "notes", "see",
    "please", "confirm", "email", "call", "contact", "onboarding", "approvals", "north", "south", "east", "west",
}
ORG_SUFFIXES = {"ltd", "llc", "inc", "corp", "corporation", "plc", "gmbh", "sa", "ag", "bv", "pvt", "limited",
                "group", "holdings", "services", "solutions", "systems", "technologies", "partners", "bank", "university"}
_TOKEN = re.compile(r"^(?:[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)*|[A-Z]\.?|[A-Z][a-z]*[A-Z][a-z]+|[A-Z]'[A-Z][a-z]+)$")

POSSESSIVE = re.compile(r"(?:'s|’s|'|’)$")


def strip_possessive(text: str) -> str:
    return POSSESSIVE.sub("", text.strip())


def name_tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", strip_possessive(text).strip(" ,.;:()[]\"")) if t]


def looks_like_name(text: str, allow_list: set[str] | None = None, min_tokens: int = 1) -> bool:
    """Title-cased tokens (initials, hyphens, apostrophes and particles allowed), not business vocabulary."""
    text = strip_possessive(text).strip(" ,.;:()[]\"")
    if not text or len(text) > 60 or re.search(r"[\d_@/\\|#=<>{}]", text):
        return False
    if allow_list and text.lower() in allow_list:
        return False
    toks = name_tokens(text)
    toks = [t for t in toks if t.lower().rstrip(".") not in HONORIFICS]
    if not (min_tokens <= len(toks) <= 5):
        return False
    real = [t for t in toks if t.lower() not in PARTICLES]
    if not real or not all(_TOKEN.match(t) for t in real):
        return False
    long_tokens = [t for t in real if len(t.rstrip(".")) > 1]
    if not long_tokens:
        return False
    if all(t.lower() in NOT_A_NAME_WORDS or t.lower() in COMMON_TITLECASE for t in long_tokens):
        return False
    if any(t.lower().rstrip(".") in ORG_SUFFIXES for t in long_tokens):
        return False
    if allow_list and any(t.lower() in allow_list for t in long_tokens) and len(long_tokens) == 1:
        return False
    return True


def trim_to_name(text: str, allow_list: set[str] | None = None) -> tuple[int, int] | None:
    """Trim non-name words from both ends of an NER hit ("M. Vossberg CEO" -> "M. Vossberg").
    Returns (start, end) offsets within ``text`` or None when nothing name-like remains."""
    matches = list(re.finditer(r"\S+", text))
    lo, hi = 0, len(matches)

    def bad(tok: str) -> bool:
        t = strip_possessive(tok).strip(" ,.;:()[]\"")
        return (not t or t.isupper() and len(t) > 1 or t.lower() in NOT_A_NAME_WORDS or t.lower() in COMMON_TITLECASE
                or (allow_list is not None and t.lower() in allow_list) or not _TOKEN.match(t) and t.lower() not in PARTICLES)

    while lo < hi and bad(matches[lo].group()):
        lo += 1
    while hi > lo and bad(matches[hi - 1].group()):
        hi -= 1
    if lo >= hi:
        return None
    start, end = matches[lo].start(), matches[hi - 1].end()
    sub = text[start:end]
    stripped = strip_possessive(sub).rstrip(" ,.;:)")
    end = start + len(stripped)
    return (start, end) if looks_like_name(text[start:end], allow_list) else None


def name_variants(full: str) -> dict[str, float]:
    """Mentions of a confirmed person to search for elsewhere, with the score each variant earns."""
    toks = [t for t in name_tokens(full) if t.lower().rstrip(".") not in HONORIFICS]
    variants: dict[str, float] = {}
    if len(toks) >= 2:
        variants[" ".join(toks)] = 0.95
        first, last = toks[0], toks[-1]
        if len(first.rstrip(".")) > 1:
            variants[f"{first[0]}. {last}"] = 0.85
            variants[f"{first[0]} {last}"] = 0.8
            variants[f"{first} {last}"] = 0.9
        for t in (first, last):
            if _usable_single(t):
                variants[t] = 0.72
        if "-" in last:  # hyphenated surname: each part may be used alone
            for part in last.split("-"):
                if _usable_single(part):
                    variants.setdefault(part, 0.6)
    elif len(toks) == 1 and _usable_single(toks[0]):
        variants[toks[0]] = 0.72
    return variants


def _usable_single(tok: str) -> bool:
    t = tok.rstrip(".")
    return len(t) >= 3 and t[0].isupper() and t.lower() not in NOT_A_NAME_WORDS and t.lower() not in COMMON_TITLECASE
