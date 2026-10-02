"""One-time download of the English PP-OCR recognition model (about 9 MB) into ./models.

Only model weights are downloaded; no document data ever leaves the machine. After this, OCR runs
fully offline. Override the location with the PII_SHIELD_REC_MODEL environment variable.

    python scripts/fetch_models.py
    python scripts/fetch_models.py --gliner     # also cache the optional GLiNER-PII weights (about 1.8 GB)

GLiNER needs `pip install gliner` first. Its weights go to the Hugging Face cache; the pipeline then
loads them from that cache only and never contacts the hub during a run.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

URL = "https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv3/en_PP-OCRv3_rec_infer.onnx"
DEST = Path(__file__).resolve().parents[1] / "models" / "en_PP-OCRv3_rec_infer.onnx"


def fetch_gliner() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from optiv_pii_shield import Settings

    try:
        from gliner import GLiNER
    except ImportError:
        print("GLiNER is not installed: run `pip install gliner` first")
        return 1
    model = Settings().gliner_model
    print(f"caching {model}")
    GLiNER.from_pretrained(model)
    GLiNER.from_pretrained(model, local_files_only=True)  # the way the pipeline loads it
    print(f"cached {model}")
    return 0


def main() -> int:
    if DEST.exists() and DEST.stat().st_size > 1_000_000:
        print(f"already present: {DEST}")
    else:
        DEST.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {URL}")
        tmp = DEST.with_suffix(".part")
        urllib.request.urlretrieve(URL, tmp)
        tmp.replace(DEST)
        print(f"saved {DEST} ({DEST.stat().st_size / 1e6:.1f} MB)")
    return fetch_gliner() if "--gliner" in sys.argv[1:] else 0


if __name__ == "__main__":
    sys.exit(main())
