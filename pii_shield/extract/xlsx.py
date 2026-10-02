"""XLSX extraction with openpyxl: every sheet (hidden and very hidden included), every non-empty
cell as a table cell under its column header, formulas (string literals can hold names), cell
comments and their authors, sheet names, headers/footers and document properties.

Spreadsheets are where a lot of personal data lives (contact lists, HR extracts), so they are read
in full rather than rejected. What openpyxl cannot read faithfully (charts, images, pivot caches)
is not passed to the LLM, is removed from the masked copy where possible, and the leak gate checks
whatever remains.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from ..config import Settings
from ..models import Document, Span
from .common import IdGen

PROPS = ("creator", "lastModifiedBy", "title", "subject", "description", "keywords", "category", "identifier")
HEADER_PARTS = ("oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter")


def cell_text(value) -> str | None:
    """Text a cell shows (or, for a formula, holds). Long integers are kept: phone numbers and IDs
    are often stored as numbers. Short numbers, floats and booleans cannot identify anyone."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, int):
        return str(value) if len(str(abs(value))) >= 6 else None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]  # under a "Date of birth" header this is PII
    return None


def walk(wb):
    """Yield (anchor, kind, location, text, extra) for every piece of text. Shared by extraction
    and masking so anchors line up."""
    for name in PROPS:
        v = getattr(wb.properties, name, None)
        if isinstance(v, str) and v.strip():
            yield f"prop:{name}", "metadata", f"docProps/core.xml: <{name}>", v, {"header": name}
    for si, ws in enumerate(wb.worksheets):
        page = si + 1
        state = "" if ws.sheet_state == "visible" else f" ({ws.sheet_state})"
        yield f"sheet[{si}]/title", "heading", f"sheet '{ws.title}'{state}: name", ws.title, {"page": page, "level": 2}
        for part in HEADER_PARTS:
            hf = getattr(ws, part, None)
            for pos in ("left", "center", "right"):
                sec = getattr(hf, pos, None) if hf is not None else None
                if sec is not None and sec.text and sec.text.strip():
                    kind = "header" if "Header" in part else "footer"
                    yield f"sheet[{si}]/{part}/{pos}", kind, f"sheet '{ws.title}': {part} {pos}", sec.text, {"page": page}
        rows = [r for r in ws.iter_rows() if any(cell_text(c.value) for c in r)]
        if not rows:
            continue
        r0 = rows[0][0].row
        c0 = min(c.column for r in rows for c in r if cell_text(c.value))
        header = {c.column: cell_text(c.value) for c in rows[0] if cell_text(c.value)}
        for r in rows:
            for c in r:
                text = cell_text(c.value)
                if text:
                    yield (f"sheet[{si}]/r[{c.row - r0}]/c[{c.column - c0}]", "table_cell",
                           f"sheet '{ws.title}'{state}, cell {c.coordinate}", text,
                           {"page": page, "table": (si, c.row - r0, c.column - c0),
                            "header": header.get(c.column) if c.row != r0 else None})
                if c.comment is not None:
                    yield (f"sheet[{si}]/{c.coordinate}/comment", "comment", f"sheet '{ws.title}', comment on {c.coordinate}",
                           c.comment.text, {"page": page})
                    if c.comment.author:
                        yield (f"sheet[{si}]/{c.coordinate}/comment-author", "metadata",
                               f"sheet '{ws.title}', comment author on {c.coordinate}", c.comment.author, {"header": "author"})


def extract_xlsx(path: str | Path, settings: Settings) -> Document:
    import openpyxl

    path = Path(path)
    doc = Document(file=path.name, path=str(path), file_type="xlsx")
    ids = IdGen(path.stem[:12])
    wb = openpyxl.load_workbook(path, data_only=False)
    doc.pages = len(wb.worksheets)
    counts = {"sheets": doc.pages, "cells": 0, "comments": 0, "hidden_sheets": 0}
    for anchor, kind, location, text, extra in walk(wb):
        doc.spans.append(Span(id=ids(), file=doc.file, text=text, kind=kind, location=location, anchor=anchor,
                              page=extra.get("page"), table=extra.get("table"), header=extra.get("header"),
                              level=extra.get("level")))
        counts["cells"] += kind == "table_cell"
        counts["comments"] += kind == "comment"
    counts["hidden_sheets"] = sum(ws.sheet_state != "visible" for ws in wb.worksheets)
    images = sum(len(getattr(ws, "_images", [])) for ws in wb.worksheets)
    charts = sum(len(getattr(ws, "_charts", [])) for ws in wb.worksheets)
    if images or charts:
        doc.warnings.append(f"{images} image(s) and {charts} chart(s) in the workbook are not read; they are removed "
                            "from the masked copy (fail closed)")
    if counts["hidden_sheets"]:
        doc.warnings.append(f"{counts['hidden_sheets']} hidden sheet(s) were read and are redacted like the others")
    doc.structure = counts
    return doc
