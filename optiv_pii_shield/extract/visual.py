"""Personal data in pictures that OCR cannot give back: faces, QR codes and text under a stamp.

OCR only finds what is written. A badge photo identifies its holder as surely as the name under
it, and a QR code holds text (a profile link, a vCard) that no reader sees. Both are located here
and blanked in the masked copy. Nothing is recognised: a face is found, never matched to a person,
and a QR code's content is not kept. Text that a stamp or a pen mark runs across is located too
(``overprinted``): OCR reads it wrongly without saying so.

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
INK_CHROMA, INK_VALUE = 40, 120  # coloured ink: channels this far apart, the brightest at least this bright
DARK_PRINT = 160  # black print: no channel brighter than this
MARK_MIN_SIDE, MARK_MIN_SHARE = 40, 0.03  # a mark is at least this many px, and this share of the picture, on its short side
MARK_MAX_FILL = 0.35  # share of its box a mark may cover and still be drawn in strokes
STRUCK_INK = (0.03, 0.30)  # share of a word's box the mark's ink covers when it runs across the word
STRUCK_PRINT = 0.06  # share of a word's box that is black print


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


def _channels(img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Brightest channel, and how far the channels are apart (0 for grey, black and white)."""
    top, low = img.max(axis=2).astype(np.int16), img.min(axis=2).astype(np.int16)
    return top, top - low


def marks(img: np.ndarray) -> list[tuple[tuple[int, int, int, int], np.ndarray]]:
    """Stamps and pen marks: coloured ink drawn in strokes, larger than a line of text. Returns
    (x, y, width, height) and the ink pixels inside that box. A coloured cell, a highlight or a
    photograph is filled in, not drawn, and is not a mark."""
    h, w = img.shape[:2]
    top, chroma = _channels(img)
    ink = ((chroma >= INK_CHROMA) & (top >= INK_VALUE)).astype(np.uint8)
    # Ink has body. The colour fringes that screen fonts put on black letters, and hairlines
    # around form fields, are one pixel wide.
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    if not 0 < ink.mean() < 0.2:
        return []
    side = max(MARK_MIN_SIDE, round(MARK_MIN_SHARE * max(h, w)))
    k = max(3, round(max(h, w) / 300)) | 1
    n, labels, stats, _ = cv2.connectedComponentsWithStats(cv2.dilate(ink, np.ones((k, k), np.uint8)))
    out = []
    for i in range(1, n):
        x, y, cw, ch = (int(v) for v in stats[i][:4])
        if min(cw, ch) < side:
            continue
        strokes = ink[y:y + ch, x:x + cw] * (labels[y:y + ch, x:x + cw] == i).astype(np.uint8)
        if strokes.sum() <= MARK_MAX_FILL * cw * ch:
            out.append(((x, y, cw, ch), strokes))
    return out


def overprinted(img: np.ndarray, words: list[Box]) -> list[Box]:
    """Where a stamp or a pen mark runs across printed text that OCR did not read. ``words`` are
    the boxes of the words OCR read, in pixels of ``img`` (RGB).

    OCR reads a line under a stamp as far as it can and stops: a few letters joined to the word
    before ("Group CFOtokp" for a job title and an e-mail address), the rest not at all, and it
    reports what it did read with confidence. Two things together show it:

    - a word of black print with a little of the mark's ink running through the middle of it.
      The outline of a box passes around its words, not through them, and a word printed on a
      coloured cell or in coloured ink has no black print or far more ink;
    - further along the same ink, print on that line which no word OCR read accounts for. A
      call-out line across words that were all read changes nothing.

    The line is then boxed as far as the ink keeps running through it. The boxes are blanked in
    the masked copy, and the words in them are kept from the LLM text."""
    h, w = img.shape[:2]
    if min(h, w) < 60 or not words:
        return []
    found = marks(img)
    if not found:
        return []
    top, chroma = _channels(img)
    black = (chroma < INK_CHROMA) & (top < DARK_PRINT)
    boxes = [(max(int(b[0]), 0), max(int(b[1]), 0), min(int(round(b[2])), w), min(int(round(b[3])), h)) for b in words]
    out: list[Box] = []
    for (mx, my, mw, mh), strokes in found:
        for x0, y0, x1, y1 in boxes:
            ax0, ax1 = max(x0, mx), min(x1, mx + mw)
            # The middle of the line: an outline that touches the top or the foot of a word is not across it.
            line_h = y1 - y0
            cy0, cy1 = max(y0 + line_h // 4, my), min(y1 - line_h // 4, my + mh)
            if ax1 <= ax0 or cy1 <= cy0 or black[y0:y1, x0:x1].mean() < STRUCK_PRINT:
                continue
            band = strokes[cy0 - my:cy1 - my] > 0
            crossing = float(band[:, ax0 - mx:ax1 - mx].sum()) / ((x1 - x0) * (cy1 - cy0))
            if not STRUCK_INK[0] <= crossing <= STRUCK_INK[1]:
                continue
            cols = np.flatnonzero(band.any(axis=0))  # columns of the mark where its ink is on this line
            left = right = int(cols[(cols >= ax0 - mx) & (cols < ax1 - mx)][0])
            for c in cols[cols > right]:
                if c - right > 2 * line_h:
                    break
                right = int(c)
            for c in cols[cols < left][::-1]:
                if left - c > 2 * line_h:
                    break
                left = int(c)
            bx0, bx1 = max(min(x0, mx + left - 2 * line_h), 0), min(max(x1, mx + right + 2 * line_h), w)
            unread = black[cy0:cy1, bx0:bx1].any(axis=0)
            for ox0, oy0, ox1, oy1 in boxes:  # print inside a word OCR read is accounted for
                if min(oy1, y1) - max(oy0, y0) >= line_h / 2:
                    unread[max(ox0 - bx0, 0):max(ox1 - bx0, 0)] = False
            if unread.sum() >= line_h:
                out.append((float(bx0), float(max(y0 - 1, 0)), float(bx1), float(min(y1 + 1, h))))
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
