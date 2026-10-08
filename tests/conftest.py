import os
import sys
import tempfile
from pathlib import Path

import pytest

# The registry of protected documents is a file in the user's home: tests get one of their own.
_HOME = Path(tempfile.mkdtemp(prefix="pii-shield-test-"))
os.environ["PII_SHIELD_REGISTRY"] = str(_HOME / "registry.json")
os.environ["PII_SHIELD_GUARD_LOG"] = str(_HOME / "guard_log.jsonl")  # so is the guard's record

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
    from optiv_pii_shield import Settings

    return Settings()


class NoNetwork:
    """Blocks every outgoing connection and name lookup while active, and keeps what was tried.
    "Nothing leaves the machine" is then something a test shows, not something the README says."""

    def __enter__(self):
        import socket

        self.attempts: list[str] = []
        self._saved = {n: getattr(socket.socket, n) for n in ("connect", "connect_ex", "sendto")}
        self._lookup = (socket.getaddrinfo, socket.create_connection)

        def refuse(name):
            def blocked(sock, *args, **kwargs):
                self.attempts.append(f"{name} {args[:1]}")
                raise OSError("network access is blocked in this test")
            return blocked

        def no_lookup(*args, **kwargs):
            self.attempts.append(f"lookup {args[:2]}")
            raise OSError("network access is blocked in this test")

        for n in self._saved:
            setattr(socket.socket, n, refuse(n))
        socket.getaddrinfo, socket.create_connection = no_lookup, no_lookup
        return self

    def __exit__(self, *exc):
        import socket

        for n, fn in self._saved.items():
            setattr(socket.socket, n, fn)
        socket.getaddrinfo, socket.create_connection = self._lookup
        return False


@pytest.fixture(scope="session")
def run_result(samples, settings, tmp_path_factory):
    from optiv_pii_shield import run

    out = tmp_path_factory.mktemp("out")
    files = sorted(p for p in samples.iterdir() if p.suffix in (".pdf", ".docx", ".pptx"))
    with NoNetwork() as net:  # the whole run: models, OCR, detection, masking, verification, reports
        res = run(files, settings, out)
    res.out_dir = out
    res.network_attempts = net.attempts
    return res
