"""What a personal name looks like, and what it doesn't. Shared by L2 filtering, L3 and L4.

Names are matched by *shape*, not by ASCII: any Unicode letters ("Müller", "Łukasz", "García"),
joined by hyphens or apostrophes, in Title case, ALL CAPS or (where the caller has other evidence)
lowercase.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

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
# Stop-list words that are also common given names or surnames ("April Smith", "Larry Page",
# "Grace West", "Tom Low"). Next to a real name word they are part of the name, not noise.
NAME_LIKE_STOPWORDS = {
    "april", "may", "june", "august", "north", "south", "east", "west", "page", "low", "key", "field", "case",
    "head", "lead", "grant", "mark", "will", "bill", "rose", "hope", "grace", "summer", "dawn", "faith", "joy",
}
ORG_SUFFIXES = {"ltd", "llc", "inc", "corp", "corporation", "plc", "gmbh", "sa", "ag", "bv", "pvt", "limited",
                "group", "holdings", "services", "solutions", "systems", "technologies", "partners", "bank", "university"}

POSSESSIVE = re.compile(r"(?:'s|’s|'|’)$")
# One word of a name: Unicode letters, optionally joined by hyphens / apostrophes, optional final "."
WORD = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*\.?")

TITLE, UPPER, LOWER, INITIAL = "title", "upper", "lower", "initial"
ANY_CASE = (TITLE, UPPER, LOWER)


def strip_possessive(text: str) -> str:
    return POSSESSIVE.sub("", text.strip())


def name_tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", strip_possessive(text).strip(" ,.;:()[]\"")) if t]


def fold(text: str) -> str:
    """Lowercase, accents removed ("Łukasz" -> "lukasz"), for list lookups."""
    t = text.lower().replace("ł", "l").replace("ø", "o").replace("ß", "ss").replace("đ", "d")
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))


def token_case(tok: str) -> str | None:
    """Shape of one name word: initial ("M." / "M"), title ("Müller", "McDonald", "O'Neil",
    "Mendoza-Kowalski"), upper ("RAMAN"), lower ("verma"), or None when it is not a word."""
    t = tok.rstrip(".")
    if len(t) == 1:
        return INITIAL if t.isalpha() and t.isupper() else None
    if tok.endswith(".") or not WORD.fullmatch(t):
        return None  # abbreviations ("Inc.", "Dept.") are not name words
    letters = [c for c in t if c.isalpha()]
    if all(c.isupper() for c in letters):
        return UPPER
    if all(c.islower() for c in letters):
        return LOWER
    if letters[0].isupper():
        return TITLE  # inner capitals allowed: McDonald, DeAndre, Mendoza-Kowalski
    return None


def looks_like_name(text: str, allow_list: set[str] | None = None, min_tokens: int = 1,
                    cases: tuple[str, ...] = (TITLE, UPPER)) -> bool:
    """Name-shaped tokens (initials, hyphens, apostrophes and particles allowed), not business vocabulary.

    ``cases`` says which letter cases the caller accepts. All-caps is accepted only for two or more
    words (a lone "RAMAN" is far more often an acronym); lowercase only when the caller has other
    evidence (a "Name" column, a gazetteer given name, a confirmed person).
    """
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
    shapes = [token_case(t) for t in real]
    if not real or None in shapes:
        return False
    long_tokens = [t for t, c in zip(real, shapes) if c != INITIAL]
    if not long_tokens:
        return False
    styles = {c for c in shapes if c != INITIAL}
    if len(styles) > 1 or not styles <= set(cases):
        return False  # "Priya RAMAN" mixed case, or a case the caller did not accept
    if styles & {UPPER, LOWER} and len(long_tokens) < 2:
        return False  # a lone "RAMAN" is usually an acronym, a lone "notify" a word
    if all(t.lower() in NOT_A_NAME_WORDS or t.lower() in COMMON_TITLECASE for t in long_tokens):
        return False
    if any(t.lower().rstrip(".") in ORG_SUFFIXES for t in long_tokens):
        return False
    if allow_list and any(t.lower() in allow_list for t in long_tokens) and len(long_tokens) == 1:
        return False
    return True


def trim_to_name(text: str, allow_list: set[str] | None = None,
                 cases: tuple[str, ...] = (TITLE, UPPER)) -> tuple[int, int] | None:
    """Trim non-name words from both ends of an NER hit ("M. Vossberg CEO" -> "M. Vossberg").
    Returns (start, end) offsets within ``text`` or None when nothing name-like remains.

    A stop-list word that is also a common name ("April", "Page", "West") is kept when it sits next
    to a real name word: "Larry Page" stays whole, "Page 3 Larry" loses "Page 3"."""
    matches = list(re.finditer(r"\S+", text))
    all_upper = all(token_case(strip_possessive(m.group()).strip(" ,.;:()[]\"")) in (UPPER, INITIAL, None)
                    for m in matches)

    def clean(tok: str) -> str:
        return strip_possessive(tok).strip(" ,.;:()[]\"")

    def stop(t: str) -> bool:
        return t.lower() in NOT_A_NAME_WORDS or t.lower() in COMMON_TITLECASE

    def bad(i: int, toward: int) -> bool:
        t = clean(matches[i].group())
        shape = token_case(t) if t else None
        if not t or (allow_list is not None and t.lower() in allow_list):
            return True
        if shape is None:
            return t.lower() not in PARTICLES
        if shape == UPPER and not all_upper:
            return True  # an acronym next to a Title-case name ("CEO", "HR")
        if stop(t):
            if t.lower() in NAME_LIKE_STOPWORDS and 0 <= toward < len(matches):
                nb = clean(matches[toward].group())
                return not (token_case(nb) in (TITLE, UPPER) and not stop(nb) and nb.lower() not in (allow_list or ()))
            return True
        return False

    lo, hi = 0, len(matches)
    while lo < hi and bad(lo, lo + 1 if lo + 1 < hi else -1):
        lo += 1
    while hi > lo and bad(hi - 1, hi - 2 if hi - 2 >= lo else -1):
        hi -= 1
    if lo >= hi:
        return None
    start, end = matches[lo].start(), matches[hi - 1].end()
    stripped = strip_possessive(text[start:end]).rstrip(" ,.;:)")
    end = start + len(stripped)
    return (start, end) if looks_like_name(text[start:end], allow_list, cases=cases) else None


def name_variants(full: str) -> dict[str, float]:
    """Mentions of a confirmed person to search for elsewhere, with the score each variant earns."""
    toks = [t for t in name_tokens(full) if t.lower().rstrip(".") not in HONORIFICS]
    if toks and all(token_case(t) in (UPPER, LOWER, INITIAL) for t in toks):
        toks = [t if token_case(t) == INITIAL else t[0].upper() + t[1:].lower() for t in toks]  # "PRIYA RAMAN" -> "Priya Raman"
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
    return (len(t) >= 3 and t[0].isupper() and t.lower() not in NOT_A_NAME_WORDS and t.lower() not in COMMON_TITLECASE
            and t.lower() not in NAME_LIKE_STOPWORDS)


OCR_DIGITS = {"0": "o", "1": "l", "5": "s", "8": "b", "6": "b", "4": "a"}


def ocr_denoise(tok: str) -> str:
    """Undo common OCR digit-for-letter confusions in a word ("C1ark" -> "Clark", "5teven" ->
    "Steven"). Words with more than two digits, or fewer than three letters, are left alone."""
    digits = sum(c.isdigit() for c in tok)
    letters = sum(c.isalpha() for c in tok)
    if not digits or digits > 2 or letters < 3 or any(c.isdigit() and c not in OCR_DIGITS for c in tok):
        return tok
    out = [OCR_DIGITS.get(c, c) for c in tok]
    if tok[0].isdigit():
        out[0] = out[0].upper()
    return "".join(out)


def surname_like(tok: str, shape: str, allow_list: set[str] | None = None) -> bool:
    """Could ``tok`` continue a name written in ``shape`` (title / upper)? OCR noise tolerated."""
    t = ocr_denoise(strip_possessive(tok).rstrip(".,;:!?"))
    low = t.lower()
    return (token_case(t) == shape and len(t) >= 2 and low not in NOT_A_NAME_WORDS and low not in COMMON_TITLECASE
            and low not in ORG_SUFFIXES and low not in HONORIFICS and not (allow_list and low in allow_list))


# ---------------------------------------------------------------------------------- gazetteer
@lru_cache(maxsize=1)
def given_names() -> frozenset[str]:
    path = Path(__file__).resolve().parents[1] / "data" / "given_names.txt"
    words = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            words.update(fold(w) for w in line.split())
    return frozenset(words)


def is_given_name(tok: str) -> bool:
    t = fold(tok.rstrip("."))
    return t in given_names() or ("-" in t and t.split("-")[0] in given_names())  # "Paul-Gerhard"


GAZETTEER_SCORE = {TITLE: 0.62, UPPER: 0.62, LOWER: 0.5}


def gazetteer_names(text: str, allow_list: set[str] | None = None) -> list[tuple[int, int, float, str]]:
    """Listed given name + surname-shaped word(s) in the same letter case, e.g. "rahul verma",
    "PRIYA RAMAN", "Łukasz Nowak", "Hans van der Berg". Returns (start, end, score, reason).
    Lowercase hits land in the review band: still redacted (fail closed), flagged for a human."""
    words = list(WORD.finditer(text))
    out = []
    i = 0
    while i < len(words) - 1:
        first = words[i].group().rstrip(".")
        shape = token_case(first)
        if shape not in GAZETTEER_SCORE or not is_given_name(first):
            i += 1
            continue
        # Extend over particles and further words of the same shape, separated by single spaces.
        j, end_word = i + 1, None
        while j < len(words) and j - i <= 4:
            gap = text[words[j - 1].end():words[j].start()]
            w = words[j].group().rstrip(".")
            if gap not in (" ", " "):
                break
            if w.lower() in PARTICLES and shape != UPPER:
                j += 1
                continue
            if token_case(w) != shape or w.lower() in NOT_A_NAME_WORDS or w.lower() in COMMON_TITLECASE \
                    or w.lower() in ORG_SUFFIXES or (allow_list and w.lower() in allow_list) or len(w) < 2:
                break
            end_word = j
            j += 1
            if not is_given_name(w):
                break  # surname reached: "rahul verma approved" stops at "verma"
        if end_word is None:
            i += 1
            continue
        start, end = words[i].start(), words[end_word].end()
        end = start + len(text[start:end].rstrip("."))
        out.append((start, end, GAZETTEER_SCORE[shape], f"given name '{first}' (gazetteer) + surname-shaped word ({shape} case)"))
        i = end_word + 1
    return out


def upper_runs(text: str, min_words: int = 2, max_words: int = 4) -> list[tuple[int, int]]:
    """Runs of 2-4 ALL-CAPS words ("PRIYA RAMAN") that a case-sensitive NER model cannot see."""
    out, run = [], []
    for m in WORD.finditer(text):
        ok = token_case(m.group().rstrip(".")) == UPPER and len(m.group()) > 1
        if ok and run and text[run[-1].end():m.start()] in (" ", " ") and len(run) < max_words:
            run.append(m)
            continue
        if len(run) >= min_words:
            out.append((run[0].start(), run[-1].end()))
        run = [m] if ok else []
    if len(run) >= min_words:
        out.append((run[0].start(), run[-1].end()))
    return out
