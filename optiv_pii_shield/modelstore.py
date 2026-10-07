"""Model files the pipeline loads from ./models, each pinned to a SHA-256.

A model is code that reads every document: a swapped file could hide PII from the detectors or
exfiltrate nothing but still be wrong. So the download (scripts/fetch_models.py) and every load
check the digest, and a mismatch is an error, never a warning.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .errors import ModelMissing

log = logging.getLogger(__name__)
MODELS_DIR = Path(__file__).resolve().parents[1] / "models"


@dataclass(frozen=True)
class ModelFile:
    name: str
    url: str
    sha256: str
    env: str  # environment variable that points at another copy (not checked: the user's choice)
    what: str


OCR_EN = ModelFile(
    "en_PP-OCRv3_rec_infer.onnx",
    "https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv3/en_PP-OCRv3_rec_infer.onnx",
    "ef7abd8bd3629ae57ea2c28b425c1bd258a871b93fd2fe7c433946ade9b5d9ea",
    "PII_SHIELD_REC_MODEL", "English OCR recognition model")
FACE = ModelFile(
    "face_detection_yunet_2023mar.onnx",
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    "PII_SHIELD_FACE_MODEL", "face detection model (YuNet)")
ALL = (OCR_EN, FACE)

# The optional GLiNER weights live in the Hugging Face cache; this is the snapshot the held-out
# numbers were measured with.
GLINER_REVISION = "61726e0ad791dcab3e29339bbec3ad42ded65641"


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@lru_cache(maxsize=8)
def _verified(path: str, expected: str, mtime: float) -> bool:
    return sha256_file(path) == expected


def locate(model: ModelFile) -> Path:
    """Path of a model file, digest checked. Raises ModelMissing when absent or altered."""
    override = os.environ.get(model.env)
    if override:
        p = Path(override)
        if not p.exists():
            raise ModelMissing(f"{model.what}: ${model.env} points at {p}, which does not exist")
        log.warning("%s loaded from $%s (%s): its digest is not checked", model.what, model.env, p)
        return p
    p = MODELS_DIR / model.name
    if not p.exists():
        raise ModelMissing(f"{model.what} not found. Run `python scripts/fetch_models.py` "
                           f"(one-time download) or set {model.env}.")
    if not _verified(str(p), model.sha256, p.stat().st_mtime):
        raise ModelMissing(f"{model.what} at {p} does not match its pinned SHA-256. "
                           "Delete it and run `python scripts/fetch_models.py` again.")
    return p


def digests() -> dict[str, str]:
    """Digest of each model file present, for the run manifest."""
    out = {}
    for m in ALL:
        p = Path(os.environ.get(m.env) or MODELS_DIR / m.name)
        if p.exists():
            out[m.name] = sha256_file(p)
    return out
