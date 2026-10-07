"""Personal data in pictures that is not text: faces and QR codes.

OCR only finds what is written. A badge photo identifies its holder as surely as the name under
it, and a QR code holds text (a profile link, a vCard) that no reader sees. Both are located here
and blanked in the masked copy. Nothing is recognised: a face is found, never matched to a person,
and a QR code's content is not kept.

Signatures drawn on a scanned page are not detected (no reliable offline detector); a signature
stored as its own picture is blanked with the other pictures that hold no text.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import cv2
import numpy as np

from .. import modelstore
from ..config import Settings

log = logging.getLogger(__name__)
Box = tuple[float, float, float, float]
DETECT_SIDE = 1280  # images are scaled down to this before detection
FACE_PAD = 0.25  # the detector boxes the face; hair and chin are covered too


@lru_cache(maxsize=1)
def _face_model_path() -> str:
    return str(modelstore.locate(modelstore.FACE))


def check_models(settings: Settings) -> None:
    """Raises ModelMissing before any file is read, like the other models."""
    if settings.detect_faces:
        _face_model_path()


def _scaled(img: np.ndarray) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    scale = min(1.0, DETECT_SIDE / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (max(int(w * scale), 1), max(int(h * scale), 1)), interpolation=cv2.INTER_AREA)
    return img, scale


def faces(img: np.ndarray, threshold: float) -> list[Box]:
    """Face boxes in pixels of ``img`` (RGB), padded."""
    h, w = img.shape[:2]
    if min(h, w) < 24:
        return []
    small, scale = _scaled(img)
    bgr = cv2.cvtColor(small, cv2.COLOR_RGB2BGR)
    det = cv2.FaceDetectorYN.create(_face_model_path(), "", (bgr.shape[1], bgr.shape[0]), threshold)
    _, found = det.detect(bgr)
    out = []
    for row in found if found is not None else []:
        x, y, fw, fh = (float(v) / scale for v in row[:4])
        if fw < 12 or fh < 12:
            continue
        out.append((max(x - fw * FACE_PAD, 0), max(y - fh * FACE_PAD, 0),
                    min(x + fw * (1 + FACE_PAD), w), min(y + fh * (1 + FACE_PAD), h)))
    return out


def qr_codes(img: np.ndarray) -> list[Box]:
    h, w = img.shape[:2]
    if min(h, w) < 40:
        return []
    small, scale = _scaled(img)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    try:
        # The ArUco-based detector finds several codes on a page; the classic one is the fallback.
        ok, points = cv2.QRCodeDetectorAruco().detectMulti(gray)
        if not ok or points is None:
            ok, points = cv2.QRCodeDetector().detect(gray)
    except (cv2.error, AttributeError):
        return []
    out = []
    for quad in points if ok and points is not None else []:
        xs, ys = quad[:, 0] / scale, quad[:, 1] / scale
        bw, bh = xs.max() - xs.min(), ys.max() - ys.min()
        if bw < 20 or bh < 20 or not 0.5 < bw / max(bh, 1) < 2.0:
            continue  # a QR code is a square
        out.append((max(float(xs.min()) - 4, 0), max(float(ys.min()) - 4, 0), min(float(xs.max()) + 4, w), min(float(ys.max()) + 4, h)))
    return out


def detect(img: np.ndarray, settings: Settings) -> list[tuple[str, Box]]:
    """(kind, box in pixels) for every face and QR code in an RGB image."""
    if img.ndim == 2:
        img = np.stack([img] * 3, axis=-1)
    img = np.ascontiguousarray(img)
    out: list[tuple[str, Box]] = []
    if settings.detect_faces:
        out += [("face", b) for b in faces(img, settings.face_threshold)]
    if settings.detect_qr:
        out += [("qr", b) for b in qr_codes(img)]
    return out
