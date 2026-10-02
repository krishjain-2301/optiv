"""Working folders for uploads and outputs, and their removal.

Uploaded originals, extracted text and full registers are personal data; they must not pile up
in the system temp folder. Every UI session gets one folder under a single root; it is emptied
before each run and on request, and folders left behind by sessions that ended (browser closed,
server killed) are removed the next time the app starts.
"""
from __future__ import annotations

import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

ROOT_NAME = "pii_shield_work"
STALE_AFTER_HOURS = 2.0


def root() -> Path:
    r = Path(tempfile.gettempdir()) / ROOT_NAME
    r.mkdir(mode=0o700, exist_ok=True)
    return r


def new_session() -> Path:
    return Path(tempfile.mkdtemp(prefix="session_", dir=root()))


def _onerror(func, path, _exc):
    # Windows: read-only files (e.g. extracted from zips) cannot be removed until made writable.
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def wipe(path: Path) -> None:
    """Remove a folder and everything in it. Missing folders are fine."""
    if not path or not Path(path).exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_onerror)
    else:
        shutil.rmtree(path, onerror=_onerror)


def empty(path: Path) -> Path:
    """Remove the contents of a session folder, keep the folder."""
    path = Path(path)
    if path.exists():
        for child in path.iterdir():
            wipe(child) if child.is_dir() else child.unlink(missing_ok=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def sweep_stale(max_age_hours: float = STALE_AFTER_HOURS, keep: Path | None = None) -> int:
    """Delete session folders not touched for ``max_age_hours``. Returns how many were removed."""
    cutoff = time.time() - max_age_hours * 3600
    n = 0
    for d in root().iterdir():
        if not d.is_dir() or (keep is not None and d.resolve() == Path(keep).resolve()):
            continue
        newest = max([p.stat().st_mtime for p in d.rglob("*")] + [d.stat().st_mtime])
        if newest < cutoff:
            wipe(d)
            n += 1
    return n
