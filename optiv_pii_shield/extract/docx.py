"""DOCX extraction by walking the OOXML directly (body, tables, text boxes, content controls,
headers, footers, footnotes, comments, document properties, customXml and embedded images).

Table cells keep their paragraphs separated by newlines, so names in multi-paragraph cells are
never glued together ("Tamika OliverShimane Smith").
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import docx

from ..config import Settings
from ..models import Document, ImageRef, Span, Visual
from . import visual
from .common import IdGen, decode_image, image_spans
from .ooxml import TextUnit, docx_units, image_parts, package_units
from .vector import svg_text


def extract_docx(path: str | Path, settings: Settings) -> Document:
    path = Path(path)
    doc = Document(file=path.name, path=str(path), file_type="docx")
    ids = IdGen(path.stem[:12])
    d = docx.Document(str(path))
    units = list(docx_units(d)) + list(package_units(d.part.package))
    doc.spans.extend(unit_to_span(u, ids(), doc.file) for u in units)
    counts = count_structure(units)

    if settings.ocr_embedded_images:
        for part in image_parts(d.part.package):
            doc.spans.extend(ocr_part(part, doc, ids, settings, page=None))
    counts["images"] = len(doc.images)
    doc.structure = counts
    _package_warnings(path, doc)
    return doc


def unit_to_span(u: TextUnit, sid: str, file: str) -> Span:
    return Span(id=sid, file=file, text=u.text, kind=u.kind, page=u.page, location=u.location, bbox=u.bbox,
                table=u.table, header=u.header, anchor=u.anchor, level=u.level)


def count_structure(units: list[TextUnit]) -> dict:
    """Body structure (comparable with the document's own counts) plus the other text containers."""
    body_cells = [u for u in units if u.kind == "table_cell" and u.part == "body"]
    return {
        "headings": sum(u.kind == "heading" for u in units),
        "paragraphs": sum(u.kind == "paragraph" for u in units),
        "tables": len({u.anchor.split("/r[")[0] for u in body_cells}),
        "rows": len({u.anchor.split("/c[")[0] for u in body_cells}),
        "cells": len(body_cells),
        "text_boxes": sum(u.kind == "text_box" for u in units),
        "headers_footers": sum(u.part in ("header", "footer") for u in units),
        "footnotes_endnotes": sum(u.part in ("footnote", "endnote") for u in units),
        "comments": sum(u.part == "comment" for u in units),
        "notes": sum(u.kind == "notes" for u in units),
    }


def ocr_part(part, doc: Document, ids: IdGen, settings: Settings, page, location: str | None = None,
             scale: float = 1.0, dx: float = 0.0, dy: float = 0.0, bbox=None) -> list[Span]:
    """OCR one embedded image part and register it as an ImageRef."""
    name = str(part.partname)
    ref = ImageRef(id=f"img:{name}" + (f"@{page}" if page else ""), file=doc.file, page=page,
                   location=location or f"embedded image {name}", part_name=name, bbox=bbox)
    doc.images.append(ref)
    ctype = str(part.content_type)
    if ctype == "image/svg+xml":
        text = svg_text(part.blob)
        ref.ocr_status = "read" if text else "no_text"
        if not text:
            return []
        return [Span(id=ids(), file=doc.file, text=text, kind="image_ocr", page=page, source="image_ocr",
                     location=f"{ref.location} (SVG text)", image_ref=ref.id)]
    arr = decode_image(part.blob)
    if arr is None:
        ref.ocr_status = "unreadable"  # EMF/WMF and other vector formats: masked in outputs (fail closed)
        return []
    ref.height, ref.width = arr.shape[:2]
    if min(arr.shape[:2]) < settings.min_image_px:
        ref.ocr_status = "skipped"
        return []
    doc.visuals.extend(Visual(kind, box, page, ref.id) for kind, box in visual.detect(arr, settings))
    # Word boxes stay in image pixels so the masking step can paint over them on the image itself.
    spans, conf = image_spans(arr, settings, ids=ids, file=doc.file, page=page, location=ref.location,
                              image_ref=ref.id, scale=scale, dx=dx, dy=dy)
    ref.ocr_conf = round(conf, 3) if conf is not None else None
    if not spans:
        ref.ocr_status = "no_text"
    elif conf is not None and conf < settings.low_conf_ocr:
        ref.ocr_status = "low_confidence"
    else:
        ref.ocr_status = "read"
    return spans


def _package_warnings(path: Path, doc: Document) -> None:
    with zipfile.ZipFile(path) as zf:
        trash = [n for n in zf.namelist() if n.startswith("[trash]/")]
    if trash:
        doc.warnings.append(f"{len(trash)} deleted file(s) left in the package's [trash] folder; "
                            "they are not reachable from the document and are dropped from the masked copy")
    unread = [i for i in doc.images if i.ocr_status == "unreadable"]
    if unread:
        doc.warnings.append(f"{len(unread)} vector image(s) (EMF/WMF) could not be read; "
                            "they are blanked in the masked copy (fail closed)")
