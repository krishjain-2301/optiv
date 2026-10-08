"""Classification markings: "Confidential", "Internal Use Only", "Do Not Distribute".

A marking is the author saying what the text is. It is not a value to replace: a prompt that
carries one is a matter for policy (the prompt guard blocks it by default).

A phrase of several words counts wherever it is written. A single word ("Confidential",
"Restricted") is an ordinary word in a sentence, so it counts only where it is used as a marking:
after a label such as "Classification:", in brackets, or on a short line of its own.
"""
from __future__ import annotations

import re
from functools import lru_cache

LABEL = re.compile(r"(?i)\b(?:classification|marking|sensitivity|security\s+level|data\s+class(?:ification)?|handling)\s*[:=-]\s*$")
SHORT_LINE_WORDS = 8


@lru_cache(maxsize=8)
def _pattern(phrases: tuple[str, ...]) -> re.Pattern | None:
    alts = sorted({p.strip() for p in phrases if p.strip()}, key=len, reverse=True)
    if not alts:
        return None
    body = "|".join(r"[\s_-]+".join(re.escape(w) for w in re.split(r"[\s_-]+", a)) for a in alts)
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE)


def find(text: str, phrases: list[str]) -> list[dict]:
    """[{"line": 1-based, "text": as written, "why"}] for every marking in ``text``, one per line."""
    rx = _pattern(tuple(phrases))
    out: list[dict] = []
    if rx is None:
        return out
    for no, line in enumerate(text.split("\n"), 1):
        for m in rx.finditer(line):
            written = m.group()
            why = None
            if len(re.split(r"[\s_-]+", written)) > 1:
                why = "a classification phrase"
            elif LABEL.search(line[:m.start()]):
                why = "follows a classification label"
            elif re.search(r"[\[(*<|]\s*$", line[:m.start()]) and re.match(r"\s*[\])*>|]", line[m.end():]):
                why = "set apart in brackets"
            elif len(line.split()) <= SHORT_LINE_WORDS and (written.isupper() or len(line.split()) <= 3):
                why = "a marking on a line of its own"
            if why:
                out.append({"line": no, "text": written, "why": why})
                break
    return out
