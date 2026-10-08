"""The one local session: its working folder, the scan in progress and the last result.

The server is a single-user tool on 127.0.0.1, so there is exactly one session. A scan runs in a
background thread; the pipeline's progress callback is where its state is published and where a
pause or a cancel takes effect (at the next page, batch of text elements or output file).
"""
from __future__ import annotations

import gc
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from optiv_pii_shield import RunResult, Settings, audit, run, workspace
from optiv_pii_shield.errors import ModelMissing, RunCancelled
from optiv_pii_shield.guard import Guard, GuardBusy
from optiv_pii_shield.pipeline import REVIEW_STAGES, STAGES, _unique, apply_review, stage_of
from optiv_pii_shield.redact.tokens import rehydrate
from optiv_pii_shield.review import Addition, Decision

log = logging.getLogger(__name__)
ACTIVE = ("running", "paused")


class Busy(RuntimeError):
    """A scan is already running."""


@dataclass
class Job:
    files: list[str]
    kind: str = "scan"  # scan | review (the redaction stage again, after a reviewer's decisions)
    state: str = "running"  # running | paused | done | failed | cancelled
    fraction: float = 0.0
    message: str = "Preparing files"
    error: str | None = None
    pause_requested: bool = False
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    _go: threading.Event = field(default_factory=threading.Event)
    _cancel: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self._go.set()

    # called by the pipeline thread
    def progress(self, message: str, fraction: float) -> None:
        self.message, self.fraction = message, min(max(fraction, 0.0), 1.0)
        if not self._go.is_set() and not self._cancel.is_set():
            self.state = "paused"
            self._go.wait()
            self.state = "running"
        if self._cancel.is_set():
            raise RunCancelled()

    # called by request handlers
    def pause(self) -> None:
        self.pause_requested = True
        self._go.clear()

    def resume(self) -> None:
        self.pause_requested = False
        self._go.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._go.set()

    @property
    def stages(self):
        return REVIEW_STAGES if self.kind == "review" else STAGES

    def status(self) -> dict:
        end = self.finished if self.finished is not None else time.monotonic()
        return {
            "state": self.state, "fraction": round(self.fraction, 4), "message": self.message, "error": self.error,
            "pause_requested": self.pause_requested and self.state == "running",
            "cancel_requested": self._cancel.is_set() and self.state in ACTIVE,
            "stage": stage_of(self.fraction, self.stages), "stages": [name for name, _ in self.stages],
            "elapsed": round(end - self.started, 1), "files": self.files, "kind": self.kind,
        }


class Session:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.work: Path | None = None
        self.job: Job | None = None
        self.result: RunResult | None = None
        self.settings: Settings | None = None
        self.gold: Path | None = None
        self.transcriptions: dict[str, str] = {}  # file -> hand transcription, for structure retention
        self.cache: dict = {}  # payloads derived from the result, dropped with it
        self.guard = Guard()  # the prompt guard's conversation: in memory only, independent of the run

    # ------------------------------------------------------------------------- lifecycle
    def open(self) -> None:
        workspace.sweep_stale()
        self.work = workspace.new_session()

    def close(self) -> None:
        if self.job is not None and self.job.state in ACTIVE:
            self.job.cancel()
        self.guard.reset()
        if self.work is not None:
            self._release()
            try:
                workspace.wipe(self.work)
            except OSError:
                log.warning("could not remove %s; it is swept at the next start", self.work)

    @property
    def out(self) -> Path:
        return self.work / "out"

    def _release(self) -> None:
        self.result, self.settings, self.gold = None, None, None
        self.transcriptions = {}
        self.cache.clear()

    def empty(self) -> None:
        """Delete everything in the working folder. Windows may hold a file for a moment after the
        thread that used it stopped, so this retries."""
        self._release()
        for _ in range(10):
            gc.collect()
            try:
                workspace.empty(self.work)
                return
            except PermissionError:
                time.sleep(0.3)
        log.warning("some files in %s are still in use; they are removed before the next run", self.work)

    # ----------------------------------------------------------------------------- scans
    def start(self, settings: Settings, uploads: list[tuple[str, bytes]] | None, gold: bytes | None) -> Job:
        """Start a scan of ``uploads`` (name, content), or of freshly generated synthetic samples
        when ``uploads`` is None. Everything from the previous run is deleted first."""
        with self.lock:
            if self.job is not None and self.job.state in ACTIVE:
                raise Busy("a scan is already running")
            self.job = job = Job(files=[n for n, _ in uploads] if uploads is not None else ["synthetic samples"])
        threading.Thread(target=self._work, args=(job, settings, uploads, gold), daemon=True, name="scan").start()
        return job

    def _prepare(self, uploads: list[tuple[str, bytes]] | None, gold: bytes | None) -> tuple[list[Path], Path | None]:
        self.empty()
        src = self.work / "in"
        src.mkdir(parents=True, exist_ok=True)
        if uploads is None:
            import make_samples

            make_samples.main(str(src))
            return sorted(p for p in src.iterdir() if p.suffix in (".pdf", ".docx", ".pptx")), src / "gold_labels.csv"
        paths = []
        for name, content in uploads:
            p = src / _unique(Path(name).name, {q.name for q in paths})  # two uploads may share a name
            p.write_bytes(content)
            paths.append(p)
        gold_path = None
        if gold is not None:
            gold_path = self.work / "gold.csv"
            gold_path.write_bytes(gold)
        return paths, gold_path

    def _work(self, job: Job, settings: Settings, uploads, gold) -> None:
        try:
            paths, gold_path = self._prepare(uploads, gold)
            job.files = [p.name for p in paths]
            job.progress("Preparing files", 0.0)
            with self.guard.lock:  # the detector is shared with the prompt guard
                res = run(paths, settings, self.out, progress=job.progress)
            with self.lock:
                self.result, self.settings, self.gold = res, settings, gold_path
                self.cache.clear()
            job.fraction, job.state = 1.0, "done"
        except RunCancelled:
            self.empty()
            job.message, job.state = "Cancelled. The scan's files were deleted.", "cancelled"
        except ModelMissing as exc:
            self.empty()
            job.error, job.state = str(exc), "failed"
        except Exception as exc:  # the scan thread must always end in a state the UI can show
            log.exception("scan failed")
            self.empty()
            job.error, job.state = f"{type(exc).__name__}: {exc}", "failed"
        finally:
            job.finished = time.monotonic()

    # ---------------------------------------------------------------------------- review
    def review(self, decisions: list[Decision], additions: list[Addition], operator: str | None) -> Job:
        """Apply a reviewer's decisions and write every output again, as a background job."""
        with self.lock:
            if self.job is not None and self.job.state in ACTIVE:
                raise Busy("a scan is already running")
            if self.result is None:
                raise Busy("there is no run to review")
            self.job = job = Job(files=list(self.result.docs), kind="review", message="Applying decisions")
        threading.Thread(target=self._review, args=(job, decisions, additions, operator), daemon=True, name="review").start()
        return job

    def _review(self, job: Job, decisions, additions, operator) -> None:
        try:
            apply_review(self.result, decisions, additions, operator, progress=job.progress)
            with self.lock:
                self.cache.clear()
            job.fraction, job.state = 1.0, "done"
        except RunCancelled:
            # Outputs were being rewritten: half of them reflect the decisions, half do not.
            self.empty()
            job.message, job.state = "Cancelled while rewriting the outputs. The run's files were deleted.", "cancelled"
        except Exception as exc:
            log.exception("review failed")
            self.empty()
            job.error, job.state = f"{type(exc).__name__}: {exc}", "failed"
        finally:
            job.finished = time.monotonic()

    def rehydrate(self, text: str, operator: str | None, purpose: str) -> dict:
        """Tokens back to values, from the vault held in memory for this run. Logged."""
        res = self.result
        out, restored, unknown = rehydrate(text, res.vault.values)
        audit.AuditLog(self.out / "audit_log.jsonl", res.run_id).append([{
            "event": "reidentification", "timestamp": audit.now(), "operator": operator or res.settings.operator,
            "how": "dashboard rehydrate", "tokens": sorted(restored), "count": len(restored), "purpose": purpose}])
        self.cache.pop("run", None)
        return {"text": out, "restored": restored, "unknown": unknown}

    def set_transcription(self, file: str, text: str) -> None:
        self.transcriptions[file] = text
        self.cache.pop("evaluation", None)

    def set_gold(self, content: bytes) -> None:
        self.gold = self.work / "gold.csv"
        self.gold.write_bytes(content)
        self.cache.pop("evaluation", None)

    def delete_files(self) -> None:
        with self.lock:
            if self.job is not None and self.job.state in ACTIVE:
                raise Busy("a scan is running; cancel it first")
            self.job = None
        self.guard.reset()
        self.empty()

    # ---------------------------------------------------------------------- prompt guard
    def check_prompt(self, text: str, settings: Settings) -> dict:
        """A prompt with its values replaced by tokens (optiv_pii_shield/guard.py). Refused while a
        scan is using the detector."""
        busy = "a scan is running; the prompt guard is free again when it has finished"
        if self.job is not None and self.job.kind == "scan" and self.job.state in ACTIVE:
            raise Busy(busy)
        try:
            return self.guard.check(text, settings, wait=10)
        except GuardBusy as exc:
            raise Busy(busy) from exc
