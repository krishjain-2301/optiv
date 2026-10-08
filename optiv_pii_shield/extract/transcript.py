"""Meeting transcripts and captions: WebVTT (.vtt) and SubRip (.srt).

A transcript is a list of cues: a time range, then what was said, often with who said it, as a
voice tag (``<v Dana Whitlock>``) or a prefix (``Dana Whitlock:``). The speaker of every cue is a
span of its own, so a name is found because of where it stands, not only by what it looks like,
and is then looked for in everything that was said.

``read`` parses the file into items and ``build`` writes it back from (possibly replaced) pieces;
extraction and masking share them, so anchors line up and timings are never touched.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..config import Settings
from ..models import Document, Span
from .common import IdGen

TIMING = re.compile(r"^\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}")
VOICE = re.compile(r"^(\s*<v(?:\.[\w.-]+)*\s+)([^>\n]+?)(\s*>\s*)")
PREFIX = re.compile(r"^(\s*(?:[-–>]+\s*)?)([^\W\d_][^:<>\n]{0,48}?)(\s*:\s+)(?=\S)")


def _decode(path: Path) -> list[str]:
    return path.read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _speaker(payload: str) -> tuple[str, str, str, str]:
    """(before the speaker, speaker, between speaker and speech, speech). No speaker: ("", "", "", payload)."""
    m = VOICE.match(payload) or PREFIX.match(payload)
    if m and len(m.group(2).split()) <= 5:
        return m.group(1), m.group(2), m.group(3), payload[m.end():]
    return "", "", "", payload


def read(path: Path) -> list[dict]:
    """Items in file order:
    {"kind": "raw", "line"}                                    written back as it is (timings, numbers, blanks)
    {"kind": "text", "pre", "text", "what"}                    one line that can hold text: a title, a note, a cue name
    {"kind": "cue", "no", "time", "pre", "speaker", "mid", "text"}   what was said; ``text`` keeps its line breaks
    """
    lines = _decode(path)
    items: list[dict] = []
    i, cue_no, block = 0, 0, ""  # block: "" | "note" | "skip" (STYLE / REGION)
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        m = TIMING.match(line)
        if m:
            items.append({"kind": "raw", "line": line})
            payload = []
            i += 1
            while i < len(lines) and lines[i].strip() and not TIMING.match(lines[i]):
                payload.append(lines[i])
                i += 1
            if payload:
                cue_no += 1
                pre, speaker, mid, text = _speaker("\n".join(payload))
                items.append({"kind": "cue", "no": cue_no, "time": m.group(1), "pre": pre, "speaker": speaker, "mid": mid, "text": text})
            block = ""
            continue
        if not stripped:
            block = ""
            items.append({"kind": "raw", "line": line})
        elif block == "skip":
            items.append({"kind": "raw", "line": line})
        elif stripped.startswith("WEBVTT"):
            title = line[line.index("WEBVTT") + 6:]
            items.append({"kind": "text", "pre": line[:line.index("WEBVTT") + 6], "text": title, "what": "title"} if title.strip()
                         else {"kind": "raw", "line": line})
        elif re.match(r"^(?:STYLE|REGION)\b", stripped):
            block = "skip"
            items.append({"kind": "raw", "line": line})
        elif block == "note" or re.match(r"^NOTE\b", stripped):
            head = re.match(r"^\s*NOTE\b\s*", line) if block != "note" else None
            block = "note"
            pre = head.group() if head else ""
            items.append({"kind": "text", "pre": pre, "text": line[len(pre):], "what": "note"} if line[len(pre):].strip()
                         else {"kind": "raw", "line": line})
        elif stripped.isdigit():
            items.append({"kind": "raw", "line": line})  # a SubRip cue number
        else:
            items.append({"kind": "text", "pre": "", "text": line, "what": "cue name"})  # a WebVTT cue identifier, or stray text
        i += 1
    return items


def anchor(index: int, part: str) -> str:
    return f"t[{index}]:{part}"


def build(items: list[dict], new: dict[str, str]) -> str:
    out = []
    for i, it in enumerate(items):
        if it["kind"] == "raw":
            out.append(it["line"])
        elif it["kind"] == "text":
            out.append(it["pre"] + new.get(anchor(i, "text"), it["text"]))
        else:
            out.append(it["pre"] + new.get(anchor(i, "speaker"), it["speaker"]) + it["mid"] + new.get(anchor(i, "text"), it["text"]))
    return "\n".join(out)


def extract_transcript(path: Path, settings: Settings) -> Document:
    doc = Document(file=path.name, path=str(path), file_type="transcript", pages=1)
    ids = IdGen(path.stem[:12])
    speakers: set[str] = set()
    cues = 0
    for i, it in enumerate(read(path)):
        if it["kind"] == "text":
            doc.spans.append(Span(id=ids(), file=path.name, text=it["text"], kind="paragraph", page=1,
                                  location=f"transcript {it['what']}", anchor=anchor(i, "text")))
        elif it["kind"] == "cue":
            cues += 1
            where = f"cue {it['no']} at {it['time']}"
            if it["speaker"].strip():
                speakers.add(it["speaker"].strip().lower())
                doc.spans.append(Span(id=ids(), file=path.name, text=it["speaker"], kind="speaker", page=1,
                                      location=f"{where}, speaker", anchor=anchor(i, "speaker")))
            if it["text"].strip():
                doc.spans.append(Span(id=ids(), file=path.name, text=it["text"], kind="paragraph", page=1, location=where,
                                      anchor=anchor(i, "text")))
    doc.structure = {"cues": cues, "speakers": len(speakers)}
    if not cues:
        doc.warnings.append("no timed cues were found: the file was read line by line as text")
    return doc
