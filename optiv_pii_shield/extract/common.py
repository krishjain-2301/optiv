"""Helpers shared by the per-format extractors."""
from __future__ import annotations

import io
import itertools
import re
from typing import Iterable, Optional

import cv2
import numpy as np
from PIL import Image

from ..config import Settings
from ..models import Span, Word
from . import visual
from .layout import group_blocks
from .ocr import OcrLine, get_engine


class IdGen:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self._c = itertools.count(1)

    def __call__(self) -> str:
        return f"{self.prefix}-{next(self._c):05d}"


def clean(text: str) -> str:
    return " ".join((text or "").replace("\u00a0", " ").split())


def span_from_lines(
    lines: list[OcrLine],
    *,
    sid: str,
    file: str,
    page: Optional[int],
    kind: str,
    source: str,
    location: str,
    scale: float = 1.0,
    **extra,
) -> Span:
    """Join OCR lines into one span, keeping each word's char range, box (scaled) and confidence."""
    words: list[Word] = []
    parts: list[str] = []
    pos = 0
    widths = [l.bbox[2] - l.bbox[0] for l in lines if l.words]
    full = max(widths) if widths else 0
    for li, line in enumerate(lines):
        for wi, w in enumerate(line.words):
            if parts:
                parts.append(_line_sep(lines[li - 1], line, full, kind) if wi == 0 and li > 0 else " ")
                pos += 1
            parts.append(w.text)
            b = tuple(v * scale for v in w.bbox)
            words.append(Word(w.text, pos, pos + len(w.text), b, round(w.conf, 3)))
            pos += len(w.text)
    text = "".join(parts)
    bbox = None
    if words:
        xs0, ys0, xs1, ys1 = zip(*(w.bbox for w in words))
        bbox = (min(xs0), min(ys0), max(xs1), max(ys1))
    conf = round(float(np.mean([w.conf for w in words])), 3) if words else None
    return Span(id=sid, file=file, text=text, kind=kind, page=page, location=location, source=source,
                bbox=bbox, ocr_conf=conf, words=words, **extra)


LABEL_START = re.compile(r"^[A-Z][\w .#/()'-]{1,40}:\s")


def _line_sep(prev: OcrLine, line: OcrLine, full_width: float, kind: str) -> str:
    """Keep a line break where the layout has one (cells, short lines, "Label:" fields);
    join wrapped lines of running prose with a space so NER sees whole sentences."""
    if kind == "table_cell" or LABEL_START.match(line.text):
        return "\n"
    if prev.words and (prev.bbox[2] - prev.bbox[0]) < 0.75 * full_width:
        return "\n"
    return " "


def decode_image(blob: bytes) -> Optional[np.ndarray]:
    """Decode any raster format PIL understands into RGB. Vector formats (EMF/WMF/SVG) return None."""
    try:
        img = Image.open(io.BytesIO(blob))
        img.load()
    except Exception:
        return None
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    return np.array(img.convert("RGB"))


def ocr_image(img: np.ndarray, settings: Settings) -> list[OcrLine]:
    """OCR an embedded image. Small screenshots are upscaled first, which is where most recall is won."""
    h, w = img.shape[:2]
    scale = 1.0
    if max(h, w) < 1600:
        scale = min(settings.region_upscale if max(h, w) > 600 else 3.0, 4000 / max(h, w))
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    lines = get_engine(settings.ocr_engine).read(img)
    return [l.transformed(scale) for l in lines] if scale != 1.0 else lines


def image_spans(
    img: np.ndarray,
    settings: Settings,
    *,
    ids: IdGen,
    file: str,
    page: Optional[int],
    location: str,
    image_ref: str,
    scale: float = 1.0,
    dx: float = 0.0,
    dy: float = 0.0,
    overprint: Optional[list] = None,
) -> tuple[list[Span], Optional[float]]:
    """OCR an image and return one span per text block. Boxes are in image px * scale + offset.
    ``overprint`` collects, in the same coordinates, where a stamp runs across the text."""
    lines = ocr_image(img, settings)
    if overprint is not None:
        overprint += [(x0 * scale + dx, y0 * scale + dy, x1 * scale + dx, y1 * scale + dy)
                      for x0, y0, x1, y1 in overprinted(img, lines, settings)]
    spans = []
    for bi, block in enumerate(group_blocks(lines)):
        if dx or dy:
            block = [l.transformed(1.0, dx / scale, dy / scale) for l in block]
        spans.append(
            span_from_lines(block, sid=ids(), file=file, page=page, kind="image_ocr", source="image_ocr",
                            location=f"{location}, text block {bi + 1}", scale=scale, image_ref=image_ref)
        )
    conf = float(np.mean([l.conf for l in lines])) if lines else None
    return spans, conf


def overprinted(img: np.ndarray, lines: list[OcrLine], settings: Settings) -> list[tuple]:
    """Boxes, in pixels of ``img``, of text a stamp or pen mark runs across (see visual.overprinted)."""
    if not settings.detect_overprint:
        return []
    return visual.overprinted(img, [w.bbox for l in lines for w in l.words])


def mostly_unreadable(spans: list[Span], settings: Settings) -> bool:
    """A picture where at least half of the words were read below the screenshot confidence floor
    (a screenshot shrunk until its text blurs). The words OCR did make out are no guide to what
    the picture shows, so it is treated like a picture that could not be read: its text is kept
    from the LLM and the picture is blanked in the masked copy."""
    words = [w for s in spans for w in s.words if w.conf is not None]
    return len(words) >= 5 and 2 * sum(w.conf < settings.low_conf_image_ocr for w in words) >= len(words)


def md_table(rows: Iterable[list[str]]) -> str:
    rows = [[c.replace("|", "\\|").replace("\n", "<br>") for c in r] for r in rows]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)
