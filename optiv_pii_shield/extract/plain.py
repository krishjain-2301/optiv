"""Text-based formats: plain text, CSV / TSV and e-mail messages (.eml).

Each format has a ``read_*`` function that parses the file into pieces keyed by an anchor, and a
``build_*`` function that writes the file back from (possibly replaced) pieces. Extraction and
masking share them, so anchors line up.

An e-mail's attachments are not read: they are listed in a warning, left out of the text for the
LLM and dropped from the masked copy (fail closed).
"""
from __future__ import annotations

import csv
import io
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

from ..config import Settings
from ..models import Document, Span
from .common import IdGen

ADDRESS_HEADERS = ("From", "Sender", "Reply-To", "To", "Cc", "Bcc")


def _decode(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig", errors="replace")


# ------------------------------------------------------------------------------------- text
def read_text(path: Path) -> list[str]:
    return [p for p in _decode(path).replace("\r\n", "\n").split("\n\n") if p.strip()]


def build_text(paragraphs: list[str]) -> str:
    return "\n\n".join(paragraphs) + "\n"


def extract_text(path: Path, settings: Settings) -> Document:
    doc = Document(file=path.name, path=str(path), file_type="text", pages=1)
    ids = IdGen(path.stem[:12])
    for i, para in enumerate(read_text(path)):
        doc.spans.append(Span(id=ids(), file=path.name, text=para, kind="paragraph", page=1,
                              location=f"paragraph {i + 1}", anchor=f"p[{i}]"))
    doc.structure = {"paragraphs": len(doc.spans)}
    return doc


# -------------------------------------------------------------------------------------- CSV
def read_csv(path: Path) -> tuple[list[list[str]], type[csv.Dialect]]:
    text = _decode(path)
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    return [row for row in csv.reader(io.StringIO(text, newline=""), dialect)], dialect


def build_csv(rows: list[list[str]], dialect) -> str:
    buf = io.StringIO(newline="")
    csv.writer(buf, dialect).writerows(rows)
    return buf.getvalue()


def csv_anchor(ri: int, ci: int) -> str:
    return f"csv/r[{ri}]/c[{ci}]"


def extract_csv(path: Path, settings: Settings) -> Document:
    doc = Document(file=path.name, path=str(path), file_type="csv", pages=1)
    ids = IdGen(path.stem[:12])
    rows, _ = read_csv(path)
    header = [c.strip() for c in rows[0]] if rows else []
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            if cell.strip():
                doc.spans.append(Span(id=ids(), file=path.name, text=cell, kind="table_cell", page=1,
                                      location=f"row {ri + 1}, col {ci + 1}", anchor=csv_anchor(ri, ci), table=(0, ri, ci),
                                      header=(header[ci] or None) if ri > 0 and ci < len(header) else None))
    doc.structure = {"tables": 1 if rows else 0, "rows": len(rows), "cells": len(doc.spans)}
    return doc


# ------------------------------------------------------------------------------------ e-mail
def _html_text(html: str) -> str:
    from lxml import html as lxml_html

    try:
        root = lxml_html.fromstring(html)
    except Exception:
        return ""
    for el in root.iter("script", "style"):
        el.drop_tree()
    for el in root.iter("br", "p", "div", "tr", "li"):
        el.tail = "\n" + (el.tail or "")
    return root.text_content()


def read_eml(path: Path) -> dict:
    """{"headers": [(name, [(display name, address), ...])], "subject", "date", "body": [paragraphs],
    "attachments": n}"""
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    headers = []
    for name in ADDRESS_HEADERS:
        values = msg.get_all(name) or []
        pairs = [(n.strip(), a.strip()) for n, a in getaddresses([str(v) for v in values]) if n.strip() or a.strip()]
        if pairs:
            headers.append((name, pairs))
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            text = body.get_content()
        except Exception:
            text = (body.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if body.get_content_subtype() == "html":
            text = _html_text(text)
    paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    return {"headers": headers, "subject": str(msg.get("Subject") or ""), "date": str(msg.get("Date") or ""),
            "body": paragraphs, "attachments": sum(1 for _ in msg.iter_attachments())}


def eml_pieces(mail: dict) -> list[tuple[str, str, str, str, dict]]:
    """(anchor, kind, location, text, extra) for every piece of an e-mail that holds text."""
    out = []
    for name, pairs in mail["headers"]:
        for i, (display, addr) in enumerate(pairs):
            if display:
                out.append((f"hdr:{name}:{i}:name", "metadata", f"e-mail header {name}: name", display, {"header": "name"}))
            if addr:
                out.append((f"hdr:{name}:{i}:addr", "metadata", f"e-mail header {name}: address", addr, {"header": "e-mail"}))
    if mail["subject"].strip():
        out.append(("hdr:Subject", "heading", "e-mail subject", mail["subject"], {"page": 1, "level": 1}))
    for i, para in enumerate(mail["body"]):
        out.append((f"body:{i}", "paragraph", f"body paragraph {i + 1}", para, {"page": 1}))
    return out


def build_eml(mail: dict, new: dict[str, str]) -> str:
    """The message as plain text with replaced pieces. Attachments are not carried over."""
    lines = []
    for name, pairs in mail["headers"]:
        shown = []
        for i, (display, addr) in enumerate(pairs):
            display = new.get(f"hdr:{name}:{i}:name", display)
            addr = new.get(f"hdr:{name}:{i}:addr", addr)
            shown.append(f"{display} <{addr}>" if display and addr else display or addr)
        lines.append(f"{name}: {', '.join(shown)}")
    if mail["date"]:
        lines.append(f"Date: {mail['date']}")
    lines.append(f"Subject: {new.get('hdr:Subject', mail['subject'])}")
    note = f"masked copy; {mail['attachments']} attachment(s) removed" if mail["attachments"] else "masked copy"
    lines.append(f"X-PII-Shield: {note}")
    body = [new.get(f"body:{i}", p) for i, p in enumerate(mail["body"])]
    return "\n".join(lines) + "\n\n" + "\n\n".join(body) + "\n"


def extract_eml(path: Path, settings: Settings) -> Document:
    doc = Document(file=path.name, path=str(path), file_type="eml", pages=1)
    ids = IdGen(path.stem[:12])
    mail = read_eml(path)
    for anchor, kind, location, text, extra in eml_pieces(mail):
        doc.spans.append(Span(id=ids(), file=path.name, text=text, kind=kind, location=location, anchor=anchor,
                              page=extra.get("page"), header=extra.get("header"), level=extra.get("level")))
    if mail["attachments"]:
        doc.warnings.append(f"{mail['attachments']} attachment(s) were not read; they are left out of the LLM text and "
                            "removed from the masked copy (fail closed). Scan them as separate files.")
    doc.structure = {"headers": len(mail["headers"]), "paragraphs": len(mail["body"]), "attachments": mail["attachments"]}
    return doc
