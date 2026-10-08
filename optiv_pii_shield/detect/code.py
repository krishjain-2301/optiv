"""Is this text source code?

Personal data has shapes a rule can match; a company's source code does not. What can be told
offline is that a text *is* code: its lines end in ";" or "{", start with ``def`` or ``import``,
call ``a.b(c)``. That is all this module says. It does not judge whether the code is confidential.

Each line gets a score from the patterns below; a line scoring 2 or more is code, and weaker lines
count when they sit between code lines. Text inside a ``` fence is code whatever it looks like.
"""
from __future__ import annotations

import re

# (points, pattern) on a line with its indentation removed
SIGNS: list[tuple[int, re.Pattern]] = [(p, re.compile(rx)) for p, rx in [
    (2, r"^(?:async\s+)?def\s+\w+\s*\(|^class\s+\w+.*:\s*$"),
    (2, r"^(?:from\s+[\w.]+\s+)?import\s+[\w.*{]"),
    (2, r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\b|^(?:export\s+)?(?:const|let|var)\s+[\w${\[]"),
    (2, r"^(?:public|private|protected|static|final|abstract|override|virtual)\s+[\w<>\[\], ]+[({;=]"),
    (2, r"^#\s*(?:include|define|ifdef|ifndef|endif|pragma)\b|^#!"),
    (2, r"^(?:package|namespace|using)\s+[\w.]+\s*[;{]?$|^func\s+[\w(]"),
    (2, r"^(?:if|for|while|switch|catch|foreach|else\s+if)\s*\(.*\)\s*\{?$|^\}?\s*else\s*\{$|^try\s*\{$"),
    (2, r"^(?:if|elif|for|while|with|except)\b.+:\s*(?:#.*)?$|^(?:else|try|finally)\s*:\s*$"),
    (2, r"(?i)^(?:select\b.+\bfrom\b|insert\s+into\b|update\s+\w+\s+set\b|delete\s+from\b|create\s+(?:table|index|view)\b|alter\s+table\b)"),
    (2, r"^SELECT\b|^select\s+(?:\*|distinct\b|[\w.]+\s*,)"),
    (1, r"(?i)^(?:from|where|(?:inner|left|right|outer|cross)\s+join|join|group\s+by|order\s+by|having|values|limit)\b"),
    (2, r"^(?:export\s+)?[A-Z][A-Z0-9_]{2,}=\S*$|^\$\s+[a-z][\w.-]*(?:\s|$)"),
    (2, r"[;{]\s*(?://.*)?$|^[}\])]+[;,)]*\s*$"),
    (2, r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\([^)]*\)?|=>\s*[{(\w]|->\s*\w+\s*[:{]|::\w+\("),
    (2, r"^@\w[\w.]*(?:\(.*\))?$|^</?[a-zA-Z][\w-]*(?:\s[^>]*)?/?>"),
    (2, r"^\"[\w$.-]+\"\s*:\s*(?:[\"\[{\d-]|true|false|null)"),
    (1, r"^(?:return|raise|throw|yield|await|break|continue|pass|print|echo|export|sudo|pip|npm|git|docker|kubectl|curl|cd)\b"),
    (1, r"^[A-Za-z_][\w.\[\]\"']*\s*(?:[+\-*/|&]?=|:=)\s*\S"),
    (1, r"^(?://|/\*|\*/|\*\s|#\s|--\s|<!--)"),
    (1, r"\b\w+\([^)]*\)"),
    (1, r"^[\w-]+:\s*(?:\S.*)?$|^-\s+[\w-]+:\s"),
    (1, r"^\$\s+\w+"),
]]
LANGUAGES: dict[str, re.Pattern] = {name: re.compile(rx) for name, rx in {
    "Python": r"^\s*(?:def |class \w+.*:|from [\w.]+ import |import \w+$|elif |except\b|print\(|for \w+ in .+:$|(?:if|while) .+:$)"
              r"|\bself\.|\bNone\b|__\w+__|\.append\(",
    "JavaScript / TypeScript": r"\b(?:const|let|var)\s+\w+\s*=|=>|\bfunction\b|console\.log|\brequire\(|\bexport\s+(?:default|const)|: (?:string|number|boolean)\b",
    "Java / C#": r"\b(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\]]+\s+\w+\s*\(|System\.out|Console\.Write|\bnew\s+\w+\(|^\s*using\s+System",
    "C / C++": r"^\s*#\s*include|\bint\s+main\s*\(|std::|\bprintf\(|\w+\s*->\s*\w+",
    "SQL": r"(?i)^\s*(?:select|from|where|insert\s+into|update\s+\w+\s+set|create\s+table|(?:inner|left|right)\s+join|order\s+by|group\s+by)\b",
    "Shell": r"^#!.*sh\b|^\s*(?:\$\s+)?(?:sudo|export|echo|curl|chmod|grep|cd|pip|npm|git|docker|kubectl)\s",
    "Go": r"^\s*func\s+[\w(]|^\s*package\s+\w+$|:=|\bfmt\.\w+\(",
    "Configuration": r"^\s*\"[\w$.-]+\"\s*:|^\s*[\w-]+:\s|^\s*[A-Z][A-Z0-9_]+=",
}.items()}
FENCE = re.compile(r"^\s*(?:```|~~~)")


def line_score(line: str) -> int:
    s = line.strip()
    return sum(points for points, rx in SIGNS if rx.search(s)) if s else 0


def analyse(text: str) -> dict:
    """{"detected", "lines", "code_lines", "share", "languages", "blocks": [[first line, last line], ...]}
    with 1-based line numbers. ``lines`` counts the lines that are not blank."""
    lines = text.split("\n")
    scores = [line_score(ln) for ln in lines]
    code = [s >= 2 for s in scores]
    fenced, inside = False, False
    for i, ln in enumerate(lines):
        if FENCE.match(ln):
            inside, fenced = not inside, True
        elif inside and ln.strip():
            code[i] = True
    # A weak line between or beside code lines belongs to the code ("x = 1" under "def f():").
    changed = True
    while changed:
        changed = False
        for i, ln in enumerate(lines):
            if code[i] or not ln.strip() or not (scores[i] >= 1 or ln[:1] in " \t"):
                continue
            near = [j for j in (i - 1, i + 1) if 0 <= j < len(lines)]
            if any(code[j] for j in near):
                code[i] = changed = True

    blocks: list[list[int]] = []
    for i, is_code in enumerate(code):
        if not is_code:
            continue
        gap = i - blocks[-1][1] if blocks else None  # in lines; blocks hold 1-based numbers
        if blocks and (gap == 0 or (gap == 1 and not lines[i - 1].strip())):
            blocks[-1][1] = i + 1
        else:
            blocks.append([i + 1, i + 1])
    total = sum(bool(ln.strip()) for ln in lines)
    n = sum(code)
    longest = max((sum(code[a - 1:b]) for a, b in blocks), default=0)
    detected = fenced and n > 0 or longest >= 3 or (n >= 2 and n >= 0.6 * total)

    votes = {name: sum(bool(rx.search(ln)) for ln, c in zip(lines, code) if c) for name, rx in LANGUAGES.items()}
    ranked = sorted((v, name) for name, v in votes.items() if v)
    languages = [name for v, name in ranked[::-1] if v >= max(2, ranked[-1][0] * 0.5)][:2] if ranked else []
    if not languages and ranked:
        languages = [ranked[-1][1]]
    return {"detected": bool(detected), "lines": total, "code_lines": n if detected else 0,
            "share": round(n / total, 3) if detected and total else 0.0, "languages": languages if detected else [],
            "blocks": blocks if detected else []}
