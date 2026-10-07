"""One-time download of the model files into ./models, each checked against its pinned SHA-256
(optiv_pii_shield/modelstore.py): the English PP-OCR recognition model (about 9 MB) and the YuNet
face detector (about 230 KB).

Only model weights are downloaded; no document data ever leaves the machine. After this, every run
is fully offline.

    python scripts/fetch_models.py
    python scripts/fetch_models.py --gliner     # also cache the optional GLiNER-PII weights (about 1.8 GB)

GLiNER needs `pip install gliner` first. Its weights go to the Hugging Face cache at a pinned
revision; the pipeline then loads them from that cache only and never contacts the hub during a run.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optiv_pii_shield import modelstore  # noqa: E402


def fetch_gliner() -> int:
    from optiv_pii_shield import Settings

    try:
        from gliner import GLiNER
    except ImportError:
        print("GLiNER is not installed: run `pip install gliner` first")
        return 1
    s = Settings()
    print(f"caching {s.gliner_model} @ {s.gliner_revision}")
    GLiNER.from_pretrained(s.gliner_model, revision=s.gliner_revision)
    GLiNER.from_pretrained(s.gliner_model, revision=s.gliner_revision, local_files_only=True)  # the way the pipeline loads it
    print(f"cached {s.gliner_model}")
    return 0


def fetch(model: modelstore.ModelFile) -> bool:
    dest = modelstore.MODELS_DIR / model.name
    if dest.exists() and modelstore.sha256_file(dest) == model.sha256:
        print(f"already present, digest ok: {dest}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {model.url}")
    tmp = dest.with_suffix(".part")
    urllib.request.urlretrieve(model.url, tmp)
    got = modelstore.sha256_file(tmp)
    if got != model.sha256:
        tmp.unlink()
        print(f"ERROR: {model.name}: SHA-256 {got} does not match the pinned {model.sha256}; not installed")
        return False
    tmp.replace(dest)
    print(f"saved {dest} ({dest.stat().st_size / 1e6:.1f} MB), digest ok")
    return True


def main() -> int:
    ok = all([fetch(m) for m in modelstore.ALL])
    if not ok:
        return 1
    return fetch_gliner() if "--gliner" in sys.argv[1:] else 0


if __name__ == "__main__":
    sys.exit(main())
