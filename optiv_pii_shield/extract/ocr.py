"""OCR backends behind one interface.

``rapidocr`` (PP-OCR models on ONNX runtime, installed by pip) is the default because it reads
small screenshot text far better than Tesseract and needs no system install. Tesseract is used
when requested and its binary is on PATH. Both return lines made of words with boxes and
confidences, so everything downstream is engine-agnostic.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

import numpy as np

from .. import modelstore

log = logging.getLogger(__name__)


@dataclass
class OcrWord:
    text: str
    bbox: tuple[float, float, float, float]
    conf: float
    char_x: Optional[list[tuple[float, float]]] = None  # per-character x ranges, when the engine gives them

    def split_at(self, x: float) -> list["OcrWord"]:
        """Split a word OCR glued across a column boundary ("EMP-41077IAM") at page x-position."""
        if not self.char_x or len(self.char_x) != len(self.text):
            return [self]
        k = sum(1 for a, b in self.char_x if (a + b) / 2 < x)
        if k in (0, len(self.text)):
            return [self]
        y0, y1 = self.bbox[1], self.bbox[3]
        left, right = self.char_x[:k], self.char_x[k:]
        return [OcrWord(self.text[:k], (left[0][0], y0, left[-1][1], y1), self.conf, left),
                OcrWord(self.text[k:], (right[0][0], y0, right[-1][1], y1), self.conf, right)]


@dataclass
class OcrLine:
    words: list[OcrWord] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs0, ys0, xs1, ys1 = zip(*(w.bbox for w in self.words))
        return min(xs0), min(ys0), max(xs1), max(ys1)

    @property
    def conf(self) -> float:
        return float(np.mean([w.conf for w in self.words])) if self.words else 0.0

    @property
    def height(self) -> float:
        b = self.bbox
        return b[3] - b[1]

    def transformed(self, scale: float, dx: float = 0, dy: float = 0) -> "OcrLine":
        """Map coordinates from an upscaled crop back to page pixels."""
        return OcrLine(
            [
                OcrWord(w.text, (w.bbox[0] / scale + dx, w.bbox[1] / scale + dy, w.bbox[2] / scale + dx, w.bbox[3] / scale + dy),
                        w.conf, [(a / scale + dx, b / scale + dx) for a, b in w.char_x] if w.char_x else None)
                for w in self.words
            ]
        )


class OcrEngine:
    name = "base"

    def read(self, img: np.ndarray) -> list[OcrLine]:  # img: RGB or grayscale uint8
        raise NotImplementedError


class RapidOcrEngine(OcrEngine):
    """PP-OCR on ONNX Runtime.

    Two deliberate settings, both measured on the synthetic scans:
    * the **English** recognition model: the bundled Chinese model drops the spaces between English
      words ("Fullname:RafaelMendoza-Kowalski"), which breaks NER and label parsing;
    * the angle classifier is **off**: on upright scans it flips long lines 180 degrees and they come
      back as garbage or empty, silently losing whole sentences.
    """

    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR

        # The bundled recogniser drops spaces between words ("PriyaRaman"), which hides names
        # from NER: not an acceptable silent fallback. locate() raises ModelMissing.
        rec = modelstore.locate(modelstore.OCR_EN)
        self._engine = RapidOCR(rec_model_path=str(rec), use_cls=False)
        self.model = rec.stem

    def read(self, img: np.ndarray) -> list[OcrLine]:
        if img.ndim == 2:
            img = np.stack([img] * 3, axis=-1)
        result, _ = self._engine(np.ascontiguousarray(img), use_cls=False, return_word_box=True)
        lines: list[OcrLine] = []
        for item in result or []:
            box, raw, score = item[0], item[1] or "", float(item[2])
            char_boxes = item[3] if len(item) > 3 else None
            if not raw.strip():
                continue
            pts = np.array(box, dtype=float)
            bbox = (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())
            words = _words_from_chars(raw, char_boxes, bbox, score) or _split_line(raw.strip(), bbox, score)
            lines.append(OcrLine(words))
        return lines


class TesseractEngine(OcrEngine):
    name = "tesseract"

    def __init__(self) -> None:
        import pytesseract  # noqa: F401

        self._tess = pytesseract

    def read(self, img: np.ndarray) -> list[OcrLine]:
        data = self._tess.image_to_data(img, output_type=self._tess.Output.DICT, config="--psm 3")
        grouped: dict[tuple, OcrLine] = {}
        for i, text in enumerate(data["text"]):
            text = (text or "").strip()
            conf = float(data["conf"][i])
            if not text or conf < 0:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            grouped.setdefault(key, OcrLine()).words.append(OcrWord(text, (x, y, x + w, y + h), conf / 100))
        return list(grouped.values())


def _words_from_chars(text: str, char_boxes, bbox: tuple, conf: float) -> list[OcrWord]:
    """Word boxes from the recogniser's per-character positions (accurate enough to mask a single
    digit). Returns [] when the boxes do not line up 1:1 with the text."""
    if not char_boxes or len(char_boxes) != len(text):
        return []
    y0, y1 = bbox[1], bbox[3]
    words, cur, x0, x1, cx = [], "", None, None, []
    for ch, cb in zip(text + " ", list(char_boxes) + [None]):
        if ch.isspace() or cb is None:
            if cur:
                words.append(OcrWord(cur, (x0, y0, x1, y1), conf, cx))
            cur, x0, x1, cx = "", None, None, []
            continue
        xs = [p[0] for p in cb]
        x0 = min(xs) if x0 is None else x0
        x1 = max(xs)
        cx.append((min(xs), max(xs)))
        cur += ch
    return words


def _split_line(text: str, bbox: tuple, conf: float) -> list[OcrWord]:
    """Fallback when no character boxes are available: split the line box by character share."""
    x0, y0, x1, y1 = bbox
    tokens = text.split()
    total = max(len(text), 1)
    words, cursor = [], 0
    for tok in tokens:
        idx = text.index(tok, cursor)
        wx0 = x0 + (x1 - x0) * idx / total
        wx1 = x0 + (x1 - x0) * (idx + len(tok)) / total
        words.append(OcrWord(tok, (wx0, y0, wx1, y1), conf))
        cursor = idx + len(tok)
    return words


@lru_cache(maxsize=4)
def get_engine(name: str = "auto") -> OcrEngine:
    if name == "tesseract" or (name == "auto" and _tesseract_available() and not _rapid_available()):
        return TesseractEngine()
    if name in ("auto", "rapidocr"):
        return RapidOcrEngine()
    raise ValueError(f"Unknown OCR engine: {name}")


def _tesseract_available() -> bool:
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


def _rapid_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True
