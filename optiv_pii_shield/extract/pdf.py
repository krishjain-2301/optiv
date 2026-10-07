"""PDF extraction: native text layer when present, layout-aware OCR when the page is a scan.

All coordinates are stored in PDF points so the masking step can draw directly on the page.
"""
from __future__ import annotations

import statistics
from pathlib import Path

import pymupdf as fitz
import numpy as np

from ..config import Settings
from ..models import Document, ImageRef, Span, Visual, Word
from . import visual
from .common import IdGen, clean, decode_image, image_spans, span_from_lines
from .layout import (_overlap, assign_words_to_cells, find_captioned_figures, find_header_bar_tables, find_image_regions, find_tables, group_blocks, inside,
                     is_screenshot_grid)
from .ocr import get_engine

MIN_TEXT_CHARS = 25  # below this a page is treated as having no usable text layer


def extract_pdf(path: str | Path, settings: Settings, on_page=None) -> Document:
    """``on_page(done, total)`` is called after each page (OCR of a long scan takes minutes)."""
    path = Path(path)
    doc = Document(file=path.name, path=str(path), file_type="pdf")
    ids = IdGen(path.stem[:12])
    pdf = fitz.open(path)
    try:
        if pdf.needs_pass:
            raise ValueError(f"{path.name} is password-protected; it cannot be read, so nothing is passed on")
        if pdf.page_count > settings.max_pages:
            raise ValueError(f"{path.name} has {pdf.page_count} pages; the limit is {settings.max_pages} (Settings.max_pages)")
        doc.pages = pdf.page_count
        counts = {"pages": pdf.page_count, "headings": 0, "paragraphs": 0, "tables": 0, "rows": 0, "cells": 0,
                  "images": 0}
        _metadata_spans(pdf, doc, ids)
        _bookmark_spans(pdf, doc, ids)
        for pno in range(pdf.page_count):
            page = pdf[pno]
            doc.page_sizes[pno + 1] = (page.rect.width, page.rect.height)
            native = page.get_text("text").strip()
            if len(native) >= MIN_TEXT_CHARS:
                _native_page(page, pno + 1, doc, ids, settings, counts)
            else:
                doc.ocr_pages.append(pno + 1)
                _scanned_page(page, pno + 1, doc, ids, settings, counts)
            if on_page is not None:
                on_page(pno + 1, pdf.page_count)
    finally:
        pdf.close()  # Windows keeps the file locked while open: a cancelled run's upload must be deletable
    doc.structure = counts
    if doc.ocr_pages:
        doc.warnings.append(f"{len(doc.ocr_pages)} of {doc.pages} pages have no text layer and were OCR'd")
    return doc


def _metadata_spans(pdf: fitz.Document, doc: Document, ids: IdGen) -> None:
    for key in ("author", "creator", "producer", "title", "subject", "keywords"):
        val = clean(pdf.metadata.get(key) or "")
        if val:
            doc.spans.append(Span(id=ids(), file=doc.file, text=val, kind="metadata", location=f"metadata: {key}",
                                  header="producing application" if key in ("creator", "producer") else key,
                                  anchor=f"meta:{key}"))


def _bookmark_spans(pdf: fitz.Document, doc: Document, ids: IdGen) -> None:
    """Bookmark titles ("Appendix C - R. Mendoza record") are text a reader sees in the side panel
    and a parser reads from the outline; they are detected and rewritten like any other text."""
    for i, (_level, title, page, *_rest) in enumerate(pdf.get_toc(simple=True)):
        title = clean(title or "")
        if title:
            doc.spans.append(Span(id=ids(), file=doc.file, text=title, kind="bookmark", anchor=f"toc:{i}",
                                  location=f"bookmark {i + 1} (to page {page})"))


def _add_visuals(doc: Document, arr, settings: Settings, page, scale: float = 1.0, dx: float = 0.0, dy: float = 0.0,
                 image_ref=None) -> None:
    for kind, (x0, y0, x1, y1) in visual.detect(arr, settings):
        doc.visuals.append(Visual(kind, (x0 * scale + dx, y0 * scale + dy, x1 * scale + dx, y1 * scale + dy), page, image_ref))


# --------------------------------------------------------------------------- native pages
def _native_page(page, pno, doc, ids, settings, counts):
    table_boxes = []
    try:
        tables = page.find_tables().tables
    except Exception:
        tables = []
    for ti, tab in enumerate(tables):
        rows = tab.extract()
        table_boxes.append(fitz.Rect(tab.bbox))
        counts["tables"] += 1
        counts["rows"] += len(rows)
        header = [clean(c or "") for c in rows[0]] if rows else []
        tindex = counts["tables"] - 1
        for ri, row in enumerate(tab.rows):
            for ci, cell_box in enumerate(row.cells):
                text = (rows[ri][ci] or "").strip() if ri < len(rows) and ci < len(rows[ri]) else ""
                if not cell_box or not text:
                    continue
                counts["cells"] += 1
                doc.spans.append(Span(
                    id=ids(), file=doc.file, text=text, kind="table_cell", page=pno,
                    location=f"page {pno}, table {tindex + 1}, row {ri + 1}, col {ci + 1}", bbox=tuple(cell_box),
                    table=(tindex, ri, ci), header=header[ci] if ri > 0 and ci < len(header) else None,
                    words=_native_words(page, fitz.Rect(cell_box), text),
                ))

    blocks = page.get_text("dict")["blocks"]
    sizes = [s["size"] for b in blocks if b["type"] == 0 for l in b["lines"] for s in l["spans"] if s["text"].strip()]
    body_size = statistics.median(sizes) if sizes else 10
    for b in blocks:
        if b["type"] != 0:
            continue
        rect = fitz.Rect(b["bbox"])
        if any(rect.intersects(t) and (rect & t).get_area() > 0.5 * rect.get_area() for t in table_boxes):
            continue
        text = "\n".join(" ".join(s["text"] for s in l["spans"]).strip() for l in b["lines"]).strip()
        if not text:
            continue
        size = max(s["size"] for l in b["lines"] for s in l["spans"])
        is_heading = size > body_size * 1.2 and len(text) < 150
        kind = "heading" if is_heading else "paragraph"
        counts["headings" if is_heading else "paragraphs"] += 1
        doc.spans.append(Span(id=ids(), file=doc.file, text=text, kind=kind, page=pno,
                              location=f"page {pno}, block {b['number'] + 1}", bbox=tuple(rect),
                              words=_native_words(page, rect, text)))

    if settings.ocr_embedded_images:
        for img in page.get_images(full=True):
            xref = img[0]
            rects = page.get_image_rects(xref)
            if not rects:
                continue
            blob = page.parent.extract_image(xref)["image"]
            arr = decode_image(blob)
            ref = ImageRef(id=f"p{pno}-img{xref}", file=doc.file, page=pno, location=f"page {pno}, image xref {xref}",
                           bbox=tuple(rects[0]))
            doc.images.append(ref)
            counts["images"] += 1
            if arr is not None:
                ref.height, ref.width = arr.shape[:2]
            if arr is None or min(arr.shape[:2]) < settings.min_image_px:
                ref.ocr_status = "unreadable" if arr is None else "skipped"
                continue
            r = rects[0]
            _add_visuals(doc, arr, settings, pno, r.width / arr.shape[1], r.x0, r.y0)
            spans, conf = image_spans(arr, settings, ids=ids, file=doc.file, page=pno, location=ref.location,
                                      image_ref=ref.id, scale=r.width / arr.shape[1], dx=r.x0, dy=r.y0)
            _set_status(ref, conf, spans, settings)
            doc.spans.extend(spans)


def _native_words(page, rect: fitz.Rect, text: str) -> list[Word]:
    """Map words in ``text`` to their boxes on the page (same reading order as the text layer)."""
    boxes = [w for w in page.get_text("words", clip=rect)]
    words, cursor, bi = [], 0, 0
    for tok in text.split():
        idx = text.find(tok, cursor)
        if idx < 0:
            continue
        bbox = None
        for j in range(bi, min(bi + 4, len(boxes))):
            if boxes[j][4] == tok or tok in boxes[j][4] or boxes[j][4] in tok:
                bbox, bi = tuple(boxes[j][:4]), j + 1
                break
        words.append(Word(tok, idx, idx + len(tok), bbox))
        cursor = idx + len(tok)
    return words


# -------------------------------------------------------------------------- scanned pages
def _scanned_page(page, pno, doc, ids, settings, counts):
    dpi = settings.ocr_dpi
    scale = 72 / dpi  # px -> pt
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    preview = page.get_pixmap(dpi=100)
    doc.page_images[pno] = preview.tobytes("png")
    engine = get_engine(settings.ocr_engine)
    _add_visuals(doc, img, settings, pno, scale)

    lines = engine.read(img)
    tables, regions = page_layout(img, lines, settings)
    covered = [t.bbox for t in tables] + regions
    free_lines = [l for l in lines if not any(inside(l, box) for box in covered)]

    # Tables: assign each page-OCR word to the cell containing its centre. Word boxes come from
    # per-character positions, so text never leaks across columns even when OCR joins a whole
    # row into one line, and no extra OCR pass is needed per cell.
    for tgrid in tables:
        counts["tables"] += 1
        tindex = counts["tables"] - 1
        counts["rows"] += tgrid.n_rows
        per_cell = assign_words_to_cells(tgrid, [l for l in lines if inside(l, tgrid.bbox)])
        cell_text: dict[tuple[int, int], str] = {}
        cell_spans: list[Span] = []
        for (ri, ci), clines in sorted(per_cell.items()):
            span = span_from_lines(clines, sid=ids(), file=doc.file, page=pno, kind="table_cell", source="ocr",
                                   location=f"page {pno}, table {tindex + 1}, row {ri + 1}, col {ci + 1}",
                                   scale=scale, table=(tindex, ri, ci))
            cell_text[(ri, ci)] = span.text
            cell_spans.append(span)
            counts["cells"] += 1
        header = {ci: cell_text.get((0, ci), "") for ci in range(tgrid.n_cols)}
        for sp in cell_spans:
            if sp.table[1] > 0:
                sp.header = clean(header.get(sp.table[2], "")) or None
        doc.spans.extend(cell_spans)

    # Screenshots, badges and charts inside the scan: re-read at higher resolution.
    for ri, (x0, y0, x1, y1) in enumerate(regions):
        ref = ImageRef(id=f"p{pno}-region{ri + 1}", file=doc.file, page=pno,
                       location=f"page {pno}, image region {ri + 1}", width=x1 - x0, height=y1 - y0,
                       bbox=(x0 * scale, y0 * scale, x1 * scale, y1 * scale))
        doc.images.append(ref)
        counts["images"] += 1
        spans, conf = image_spans(img[y0:y1, x0:x1], settings, ids=ids, file=doc.file, page=pno,
                                  location=ref.location, image_ref=ref.id, scale=scale, dx=x0 * scale, dy=y0 * scale)
        _set_status(ref, conf, spans, settings)
        doc.spans.extend(spans)

    # Running text.
    heights = [l.height for l in free_lines] or [1]
    body_h = statistics.median(heights)
    for bi, block in enumerate(group_blocks(free_lines)):
        is_heading = len(block) == 1 and block[0].height > body_h * 1.3 and len(block[0].text) < 120
        kind = "heading" if is_heading else "ocr_block"
        span = span_from_lines(block, sid=ids(), file=doc.file, page=pno, kind=kind, source="ocr",
                               location=f"page {pno}, block {bi + 1}", scale=scale)
        counts["headings" if is_heading else "paragraphs"] += 1
        doc.spans.append(span)


def page_layout(img: np.ndarray, lines, settings: Settings) -> tuple[list, list]:
    """Tables (ruled grids and header-bar tables) and image regions on one scanned page.
    Needs the page-level OCR lines: their height sets the scale of every threshold."""
    if not settings.detect_regions:
        return [], []
    heights = sorted(l.height for l in lines) or [30]
    body_h = heights[len(heights) // 2]
    grids = find_tables(img)
    shots = [t.bbox for t in grids if is_screenshot_grid(t, body_h)]
    # A one-row grid that is not a screenshot is a header bar whose label separators look like
    # vertical rules; the header-bar detector reads that table properly.
    grids = [t for t in grids if not is_screenshot_grid(t, body_h) and t.n_rows >= 2]
    # Image regions first: a screenshot's dark title bar must not be read as a table header.
    # Captioned figures win over anything detected inside them (org-chart boxes form grids).
    figures = find_captioned_figures(img, lines, body_h)
    grids = [t for t in grids if not any(_overlap(t.bbox, f) > 0.5 for f in figures)]
    regions = find_image_regions(img, [t.bbox for t in grids])
    bars = find_header_bar_tables(img, body_h, lines, exclude=[t.bbox for t in grids] + shots + figures + regions)
    tables = sorted(grids + bars, key=lambda t: t.bbox[1])
    for s in shots + figures:
        if not any(_overlap(s, r) > 0.8 or _overlap(r, s) > 0.8 for r in regions):
            regions.append(s)
    regions = [r for r in regions if not any(r != o and _overlap(r, o) > 0.9 for o in regions)]
    return tables, sorted(regions, key=lambda b: (b[1], b[0]))


def _set_status(ref: ImageRef, conf, spans, settings: Settings) -> None:
    ref.ocr_conf = round(conf, 3) if conf is not None else None
    if not spans:
        ref.ocr_status = "no_text"
    elif conf is not None and conf < settings.low_conf_ocr:
        ref.ocr_status = "low_confidence"
    else:
        ref.ocr_status = "read"
