"""Standalone image files (PNG, JPEG, TIFF, ...): layout-aware OCR, same as a scanned page."""
from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..models import Document, ImageRef, Visual
from . import visual
from .common import IdGen, decode_image, image_spans


def extract_image(path: str | Path, settings: Settings) -> Document:
    path = Path(path)
    doc = Document(file=path.name, path=str(path), file_type="image", pages=1)
    ids = IdGen(path.stem[:12])
    blob = path.read_bytes()
    arr = decode_image(blob)
    ref = ImageRef(id="image", file=doc.file, page=1, location="whole image")
    doc.images.append(ref)
    if arr is None:
        ref.ocr_status = "unreadable"
        doc.warnings.append("Image could not be decoded; it is treated as fully sensitive (fail closed)")
        return doc
    ref.height, ref.width = arr.shape[:2]
    doc.page_sizes[1] = (arr.shape[1], arr.shape[0])
    doc.page_images[1] = blob
    doc.ocr_pages = [1]
    doc.visuals.extend(Visual(kind, box, 1, ref.id) for kind, box in visual.detect(arr, settings))
    spans, conf = image_spans(arr, settings, ids=ids, file=doc.file, page=1, location="image", image_ref=ref.id)
    ref.ocr_conf = round(conf, 3) if conf is not None else None
    ref.ocr_status = "read" if spans and (conf or 0) >= settings.low_conf_ocr else ("no_text" if not spans else "low_confidence")
    doc.spans.extend(spans)
    doc.structure = {"pages": 1, "paragraphs": len(spans), "images": 1}
    return doc
