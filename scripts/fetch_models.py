"""One-time download of the English PP-OCR recognition model (about 9 MB) into ./models.

Only model weights are downloaded; no document data ever leaves the machine. After this, OCR runs
fully offline. Override the location with the PII_SHIELD_REC_MODEL environment variable.

    python scripts/fetch_models.py
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

URL = "https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv3/en_PP-OCRv3_rec_infer.onnx"
DEST = Path(__file__).resolve().parents[1] / "models" / "en_PP-OCRv3_rec_infer.onnx"


def main() -> int:
    if DEST.exists() and DEST.stat().st_size > 1_000_000:
        print(f"already present: {DEST}")
        return 0
    DEST.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {URL}")
    tmp = DEST.with_suffix(".part")
    urllib.request.urlretrieve(URL, tmp)
    tmp.replace(DEST)
    print(f"saved {DEST} ({DEST.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
