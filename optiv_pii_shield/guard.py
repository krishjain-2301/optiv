"""Prompt guard: text typed or pasted for an LLM is checked before it is sent.

The pipeline handles files; this handles the chat box. A prompt goes through the same detection
layers, tokens and leak gate, and comes back with every value replaced. The mapping from token to
value stays in memory here, so the model's answer can be given its values back afterwards.

One ``Guard`` is one conversation: a person or value gets the same token in every prompt, and a
person redacted once is looked for in every later prompt, by surname alone too.

Nothing is written to disk and no prompt is kept: the log holds counts, never text.
"""
from __future__ import annotations

import copy
import re
import threading
import time
from collections import Counter

from . import audit
from .config import Settings
from .detect.propagation import PersonIndex
from .models import Document, Span
from .pipeline import get_detector, locate_missed
from .redact.leakcheck import build_needles, scrub_text
from .redact.text import LIVE, apply, merged_ranges
from .redact.tokens import TokenVault, rehydrate

NAME = "prompt"
MAX_LOG = 200
BLOCK = re.compile(r"(?:[^\n]+\n?)+")  # lines up to a blank line


class GuardBusy(RuntimeError):
    """The detector is in use (a scan is running)."""


def to_document(text: str) -> tuple[Document, dict[str, int]]:
    """One span per block of lines, and where each block starts in ``text``."""
    doc = Document(file=NAME, path="", file_type="text", pages=1)
    offsets: dict[str, int] = {}
    for i, m in enumerate(BLOCK.finditer(text)):
        block = m.group().rstrip("\n")
        if not block.strip():
            continue
        span = Span(id=f"{NAME}-{i:04d}", file=NAME, text=block, kind="paragraph", page=1,
                    location=f"line {text.count(chr(10), 0, m.start()) + 1}", anchor=f"p[{i}]")
        doc.spans.append(span)
        offsets[span.id] = m.start()
    return doc, offsets


class Guard:
    def __init__(self) -> None:
        self.lock = threading.Lock()  # the detector is shared with scans: one user of it at a time
        self.reset()

    def reset(self) -> None:
        """Forget the conversation: its people, its tokens and their values, and the log."""
        self.persons = PersonIndex()
        self.vault: TokenVault | None = None
        self.scheme: tuple | None = None  # (token key, profile) the vault was built with
        self.log: list[dict] = []
        self.checks = 0

    def _record(self, entry: dict) -> None:
        self.log.append(entry)
        del self.log[:-MAX_LOG]

    # ------------------------------------------------------------------------------- check
    def check(self, text: str, settings: Settings | None = None, wait: float | None = None) -> dict:
        """The prompt with every value replaced, and what was found. ``wait`` is how long to wait
        for the detector before giving up with GuardBusy (None: as long as it takes)."""
        if not self.lock.acquire(timeout=-1 if wait is None else wait):
            raise GuardBusy("the detector is busy")
        try:
            return self._check(text, settings or Settings())
        finally:
            self.lock.release()

    def _check(self, text: str, settings: Settings) -> dict:
        t0 = time.perf_counter()
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        scheme = (settings.token_key, settings.profile)
        restarted = self.vault is not None and scheme != self.scheme
        if restarted:  # tokens of another scheme would collide with the new ones
            self.persons, self.vault = PersonIndex(), None
        doc, offsets = to_document(text)
        docs = {NAME: doc}

        # People already redacted in this conversation are looked for again, in any form.
        s = copy.copy(settings)
        s.extra_deny_list = list(settings.extra_deny_list) + sorted(self.persons.origin)
        detector = get_detector(s)
        findings = detector.detect_all(docs)
        for name in detector.person_index.origin:
            self.persons.add(name, NAME)

        if self.vault is None:
            self.vault, self.scheme = TokenVault(self.persons, settings.token_key, settings.profile), scheme
        vault = self.vault
        vault.assign_all(docs, findings)
        needles = build_needles(vault, findings)
        locate_missed(docs, findings, needles, vault)

        live = [f for f in findings[NAME] if f.decision in LIVE]
        by_span: dict[str, list] = {}
        for f in live:
            by_span.setdefault(f.span_id, []).append(f)
        pieces = [(offsets[sp.id], offsets[sp.id] + len(sp.text), apply(sp.text, merged_ranges(by_span[sp.id])))
                  for sp in doc.spans if sp.id in by_span]
        # Leak gate: any value of this prompt still readable in the result is replaced by its token.
        safe, scrubbed = scrub_text(apply(text, pieces), needles)

        self.checks += 1
        entry = {
            "event": "check", "id": self.checks, "timestamp": audit.now(), "operator": settings.operator,
            "verdict": "redacted" if live or scrubbed else "clean", "chars": len(text), "bytes": len(text.encode("utf-8")),
            "values": len({f.token for f in live}), "findings": len(live),
            "review": sum(f.decision == "review" for f in live), "profile": settings.profile,
            "by_category": dict(Counter(f.entity_type for f in live)),
        }
        self._record(entry)
        shown = []
        for f in sorted(live, key=lambda f: offsets[f.span_id] + f.start):
            a = offsets[f.span_id] + f.start
            shown.append({"entity_type": f.entity_type, "text": f.text, "token": f.token, "score": f.score,
                          "decision": f.decision, "layer": f.layer, "reasons": f.reasons, "start": a,
                          "end": a + f.end - f.start, "line": text.count("\n", 0, a) + 1})
        return {**entry, "safe_text": safe, "findings": shown, "scrubbed": scrubbed, "restarted": restarted,
                "dropped": sum(f.decision == "drop" for f in findings[NAME]), "components": detector.components,
                "elapsed": round(time.perf_counter() - t0, 3)}

    # --------------------------------------------------------------------------- rehydrate
    def rehydrate(self, text: str, operator: str | None = None, purpose: str = "") -> dict:
        """Values back into text that holds tokens. Only tokens this conversation issued are known."""
        out, restored, unknown = rehydrate(text, self.vault.values if self.vault is not None else {})
        self._record({"event": "rehydrate", "timestamp": audit.now(), "operator": operator or Settings().operator,
                      "tokens": restored, "count": len(restored), "unknown": len(unknown), "purpose": purpose})
        return {"text": out, "restored": restored, "unknown": unknown}

    def state(self) -> dict:
        v = self.vault
        return {"checks": self.checks, "tokens": len(v.values) if v is not None else 0,
                "people": len(v.person_no) if v is not None else 0,
                "restored": sum(e["event"] == "rehydrate" for e in self.log), "log": self.log[::-1]}
