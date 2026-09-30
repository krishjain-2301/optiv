import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="session")
def samples(tmp_path_factory) -> Path:
    import make_samples

    out = tmp_path_factory.mktemp("samples")
    make_samples.main(str(out))
    return out


@pytest.fixture(scope="session")
def settings():
    from pii_shield import Settings

    return Settings()


@pytest.fixture(scope="session")
def run_result(samples, settings, tmp_path_factory):
    from pii_shield import run

    out = tmp_path_factory.mktemp("out")
    files = sorted(p for p in samples.iterdir() if p.suffix in (".pdf", ".docx", ".pptx"))
    res = run(files, settings, out)
    res.out_dir = out
    return res
