"""Verification pass: read the masked outputs again, as pictures.

The leak gate (leakcheck.py) searches text: XML parts, the text layer of a PDF, metadata. It
cannot see pixels, and the largest share of personal data in scanned documents is pixels. So after
a masked file is written, every scanned page and every picture left in it is OCR'd again, exactly
the way extraction read it (page pass plus the enlarged pass over screenshots), and searched for
every value in the vault.

A value still readable is covered and the file is checked again; if it cannot be covered the file
is withheld. This is a second reading by the same OCR engine, not a proof: a value the engine
cannot read either way is not found by it. What it catches is a mask that missed its target.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw

from ..config import Settings
from ..extract.common import decode_image, ocr_image
from ..extract.ocr import OcrLine, get_engine
from ..models import Document
from .leakcheck import Hit, LeakError, Needles, find

Box = tuple[float, float, float, float]
RASTER = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp")
MAX_ROUNDS = 2


def _text_map(lines: list[OcrLine]) -> tuple[str, list[tuple[int, int, Box]]]:
    """The lines as one text, with the character range and box of every word."""
    parts, words, pos = [], [], 0
    for line in lines:
        for i, w in enumerate(line.words):
            sep = "" if not parts else (" " if i else "\n")
            parts.append(sep + w.text)
            pos += len(sep)
            words.append((pos, pos + len(w.text), w.bbox))
            pos += len(w.text)
    return "".join(parts), words


def leaks_in(lines: list[OcrLine], needles: Needles) -> list[tuple[Box, str]]:
    """(word box, matched value) for every vault value readable in OCR lines."""
    text, words = _text_map(lines)
    out = []
    for s, e, matched in find(text, needles):
        out += [(box, matched) for a, b, box in words if a < e and b > s]
    return out


def read_picture(img: np.ndarray, settings: Settings, regions: Optional[list[Box]] = None) -> list[OcrLine]:
    """Page-level OCR plus the enlarged pass over each region (pixels of ``img``)."""
    lines = list(get_engine(settings.ocr_engine).read(img))
    for x0, y0, x1, y1 in regions or []:
        x0, y0, x1, y1 = max(int(x0), 0), max(int(y0), 0), min(int(x1), img.shape[1]), min(int(y1), img.shape[0])
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        lines += [l.transformed(1.0, x0, y0) for l in ocr_image(img[y0:y1, x0:x1], settings)]
    return lines


# -------------------------------------------------------------------------------------- PDF
def _pdf_pages_to_check(doc: Document) -> list[int]:
    pages = set(doc.ocr_pages) | {i.page for i in doc.images if i.page}
    return sorted(p for p in pages if 1 <= p <= doc.pages)


def verify_pdf(doc: Document, path: Path, settings: Settings, needles: Needles,
               tick: Optional[Callable[[str], None]] = None) -> dict:
    """Re-read every scanned page and every page holding a picture. Covers what it finds; raises
    LeakError when a value is still readable after ``MAX_ROUNDS`` attempts."""
    pages = _pdf_pages_to_check(doc)
    scale = 72 / settings.ocr_dpi
    covered = 0
    todo = pages
    for round_no in range(MAX_ROUNDS + 1):
        found: dict[int, list[tuple[Box, str]]] = {}
        with fitz.open(path) as pdf:
            for n, pno in enumerate(todo, 1):
                if tick:
                    tick(f"Verifying {doc.file}: page {n} of {len(todo)}" + (f" (pass {round_no + 1})" if round_no else ""))
                pix = pdf[pno - 1].get_pixmap(dpi=settings.ocr_dpi, colorspace=fitz.csRGB)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
                regions = [tuple(v / scale for v in i.bbox) for i in doc.images if i.page == pno and i.bbox]
                hits = leaks_in(read_picture(img, settings, regions), needles)
                if hits:
                    found[pno] = hits
        if not found:
            return {"method": "re-OCR", "pages": len(pages), "covered": covered}
        if round_no == MAX_ROUNDS:
            raise LeakError(path.name, [Hit(f"page {p} (pixels)", needles.token_for(m), m) for p, hs in found.items() for _, m in hs])
        # Cover what was read and look again, at those pages only.
        pdf = fitz.open(path)
        for pno, hits in found.items():
            page = pdf[pno - 1]
            for (x0, y0, x1, y1), _ in hits:
                h = (y1 - y0) * scale
                page.add_redact_annot(fitz.Rect(x0 * scale - h * 0.4, y0 * scale - h * 0.15, x1 * scale + h * 0.4, y1 * scale + h * 0.15),
                                      fill=(0, 0, 0))
                covered += 1
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
        data = pdf.tobytes(garbage=4, deflate=True)
        pdf.close()
        path.write_bytes(data)
        todo = sorted(found)
    return {"method": "re-OCR", "pages": len(pages), "covered": covered}


# ------------------------------------------------------------------------------ DOCX / PPTX
def _placeholder(img: Image.Image, fmt: str) -> bytes:
    out = Image.new("RGB", img.size, (60, 60, 60))
    ImageDraw.Draw(out).text((10, 10), "IMAGE WITHHELD", fill="white")
    buf = io.BytesIO()
    out.save(buf, format="JPEG" if fmt.upper() in ("JPEG", "JPG") else "PNG")
    return buf.getvalue()


def verify_package(doc: Document, data: bytes, settings: Settings, needles: Needles,
                   tick: Optional[Callable[[str], None]] = None) -> tuple[bytes, dict]:
    """OCR every raster picture left in a masked OOXML package. A picture in which a vault value
    is still readable is replaced by a blank (fail closed). Returns (package bytes, summary)."""
    src = zipfile.ZipFile(io.BytesIO(data))
    pictures = [i for i in src.infolist() if i.filename.lower().endswith(RASTER)]
    replaced: dict[str, bytes] = {}
    checked = 0
    for n, info in enumerate(pictures, 1):
        blob = src.read(info)
        arr = decode_image(blob)
        if arr is None or min(arr.shape[:2]) < settings.min_image_px:
            continue
        if tick:
            tick(f"Verifying {doc.file}: picture {n} of {len(pictures)}")
        checked += 1
        if leaks_in(ocr_image(arr, settings), needles):
            with Image.open(io.BytesIO(blob)) as img:
                replaced[info.filename] = _placeholder(img, img.format or "PNG")
    summary = {"method": "re-OCR", "pictures": checked, "covered": len(replaced)}
    if not replaced:
        return data, summary
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info, replaced.get(info.filename, src.read(info)))
    return buf.getvalue(), summary


# ------------------------------------------------------------------------------------ image
def verify_image(doc: Document, path: Path, settings: Settings, needles: Needles,
                 tick: Optional[Callable[[str], None]] = None) -> dict:
    covered = 0
    for round_no in range(MAX_ROUNDS + 1):
        if tick:
            tick(f"Verifying {doc.file}")
        blob = path.read_bytes()
        arr = decode_image(blob)
        if arr is None:
            raise LeakError(path.name, [Hit("image", "[IMAGE WITHHELD]", "the masked image cannot be read back")])
        hits = leaks_in(ocr_image(arr, settings), needles)
        if not hits:
            return {"method": "re-OCR", "pictures": 1, "covered": covered}
        if round_no == MAX_ROUNDS:
            raise LeakError(path.name, [Hit("image (pixels)", needles.token_for(m), m) for _, m in hits])
        with Image.open(io.BytesIO(blob)) as img:
            fmt = img.format or "PNG"
            img = img.convert("RGB")
        d = ImageDraw.Draw(img)
        for (x0, y0, x1, y1), _ in hits:
            h = y1 - y0
            d.rectangle([x0 - h * 0.4, y0 - h * 0.15, x1 + h * 0.4, y1 + h * 0.15], fill="black")
            covered += 1
        buf = io.BytesIO()
        img.save(buf, format="JPEG" if fmt.upper() in ("JPEG", "JPG") else "PNG", quality=92)
        path.write_bytes(buf.getvalue())
    return {"method": "re-OCR", "pictures": 1, "covered": covered}
