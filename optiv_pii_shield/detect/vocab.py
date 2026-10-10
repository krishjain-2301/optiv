"""Ordinary words, learned from the documents themselves.

A capitalised word that the same documents also write in lowercase ("Question" / "question",
"Response" / "response") is vocabulary, not a name. This is what stops a field label or a
heading from being taken for a person and then spread through every file by propagation.
A listed given name is never vocabulary, however it is written ("Grace", "Mark", "tamara").
"""
from __future__ import annotations

import re
from collections import Counter

from rapidfuzz import fuzz, process

from ..config import NOT_A_NAME_WORDS
from ..models import Document
from .names import COMMON_TITLECASE, HONORIFICS, INITIAL, PARTICLES, is_given_name, name_tokens, token_case

LOWER_WORD = re.compile(r"[a-z]{3,}")
PUNCT = ".,;:!?()[]\"'“”‘’"
MIN_COUNT = 2  # one lowercase sighting may be OCR noise or a name typed without its capital
SUFFIXES = ("s", "es", "ed", "d", "ing", "ly")
# Capitals inside a word: a surname prefix ("McDonald", "DeAndre", "O'Neil") or two words written
# as one ("ServPro", "TierContains", "YesTemplate"). The second kind is a product or field code.
NAME_PREFIX = re.compile(r"(?:Mc|Mac|De|Di|Du|Da|La|Le|Lo|Van|Von|Fitz|St|Al|El|Ab|O|D)(?=[A-Z])")
CAMEL = re.compile(r"[a-z][A-Z]")


def corpus_vocabulary(docs: dict[str, Document]) -> frozenset[str]:
    counts: Counter[str] = Counter()
    for doc in docs.values():
        for span in doc.spans:
            # Whole words only: "okpara" inside "n.okpara@example.org" is a surname, not a word.
            counts.update(w for w in (t.strip(PUNCT) for t in span.text.split()) if LOWER_WORD.fullmatch(w))
    return frozenset(w for w, n in counts.items() if n >= MIN_COUNT and not is_given_name(w))


def is_vocabulary(tok: str, vocab: frozenset[str]) -> bool:
    """Is this one word ordinary vocabulary (stop lists, or lowercase elsewhere in the documents)?"""
    low = tok.lower().strip(".,;:()")
    if not low or is_given_name(low):
        return False
    if low in NOT_A_NAME_WORDS or low in COMMON_TITLECASE or low in vocab:
        return True
    for suf in SUFFIXES:
        if low.endswith(suf) and len(low) - len(suf) >= 3:
            stem = low[:-len(suf)]
            if stem in vocab or stem in NOT_A_NAME_WORDS or stem + "e" in vocab:
                return True
    return low + "s" in vocab


def is_code_word(tok: str) -> bool:
    """Two words written as one ("ServPro"), unless the capitals are a surname prefix or a given
    name starts at one of them ("SmithDaniel": glued names, handled as names)."""
    t = tok.strip(".,;:()")
    if not CAMEL.search(t) or "-" in t or "'" in t or "’" in t:
        return False
    if NAME_PREFIX.match(t) and not CAMEL.search(t[NAME_PREFIX.match(t).end():]):
        return False
    parts = re.findall(r"[A-Z][a-z]+|[a-z]+", t)
    return not any(len(p) >= 3 and is_given_name(p) for p in parts)


def near_vocabulary(tok: str, vocab: frozenset[str]) -> str | None:
    """The ordinary word an OCR misreading stands for ("Managememt" -> "management", "Contracti"
    -> "contracting"), or None. Six letters or more: shorter words are too close to each other."""
    low = tok.lower().strip(".,;:()")
    if len(low) < 6 or is_given_name(low):
        return None
    words = vocab | NOT_A_NAME_WORDS
    for w in words:
        if w.startswith(low) and len(w) - len(low) <= 4:
            return w
    hit = process.extractOne(low, [w for w in words if abs(len(w) - len(low)) <= 2], scorer=fuzz.ratio, score_cutoff=86)
    return hit[0] if hit else None


def all_vocabulary(text: str, vocab: frozenset[str], ocr: bool = False) -> str | None:
    """Why ``text`` is not a person's name: every word of it is vocabulary or a code word. None when
    any word could be a name. Initials, particles and honorifics do not count either way, except
    that an honorific in front ("Ms Page") is evidence of a name."""
    toks = name_tokens(text)
    if any(t.lower().rstrip(".") in HONORIFICS for t in toks):
        return None
    words = [t for t in toks if t.lower() not in PARTICLES and token_case(t) != INITIAL]
    if not words:
        return None
    kinds = []
    for t in words:
        if is_vocabulary(t, vocab):
            kinds.append("ordinary word")
        elif is_code_word(t):
            kinds.append("code word")
        elif ocr and (near := near_vocabulary(t, vocab)):
            kinds.append(f"OCR misreading of '{near}'")
        else:
            return None
    return ", ".join(sorted(set(kinds)))
