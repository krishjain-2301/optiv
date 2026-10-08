"""Prompt guard: text typed or pasted for an LLM is checked before it is sent.

The pipeline handles files; this handles the chat box. A prompt goes through the same detection
layers, tokens and leak gate, and comes back with every value replaced. The mapping from token to
value stays in memory here, so the model's answer can be given its values back afterwards.

Replacing values is not always enough. A policy (``config.GuardPolicy``) refuses the whole prompt
when it is source code, carries a classification marking, overlaps a registered confidential
document, holds a value of a category the organisation never lets out, or is larger than a limit. A refused prompt yields no text to send.

One ``Guard`` is one conversation: a person or value gets the same token in every prompt, and a
person redacted once is looked for in every later prompt, by surname alone too.

No prompt or answer is kept. Every check and every restoration is recorded, as counts and never
as text, in a hash-chained file that outlives the conversation (``activity`` reads it back).
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from . import audit
from .config import ORG, GuardPolicy, Settings
from .detect import code, markings
from .detect.propagation import PersonIndex
from .models import Document, Span
from .pipeline import get_detector, locate_missed
from .redact.leakcheck import build_needles, find, scrub_text
from .redact.text import LIVE, apply, merged_ranges
from .redact.tokens import TokenVault, rehydrate

NAME = "prompt"
MAX_LOG = 200
RECORD_ID = "prompt-guard"
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


def _lines(blocks: list[list[int]]) -> str:
    return ", ".join(f"line {a}" if a == b else f"lines {a} to {b}" for a, b in blocks)


class Guard:
    def __init__(self, registry=None, record_path: str | Path | None = None) -> None:
        self.registry = registry  # registry.Registry of protected documents, or None
        # Where every check and restoration is recorded for good (hash-chained, see ``activity``); None: nowhere.
        self.record_path = Path(record_path) if record_path is not None else None
        self._record_lock = threading.Lock()
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
        """To the conversation's log, and to the record that outlives it (never text: counts only)."""
        self.log.append(entry)
        del self.log[:-MAX_LOG]
        if self.record_path is not None:
            with self._record_lock:
                self.record_path.parent.mkdir(parents=True, exist_ok=True)
                audit.AuditLog(self.record_path, RECORD_ID).append([{k: v for k, v in entry.items() if k != "id"}])

    # ------------------------------------------------------------------------------- check
    def check(self, text: str, settings: Settings | None = None, policy: GuardPolicy | None = None,
              wait: float | None = None) -> dict:
        """The prompt with every value replaced, or refused, and what was found. ``wait`` is how
        long to wait for the detector before giving up with GuardBusy (None: as long as it takes)."""
        if not self.lock.acquire(timeout=-1 if wait is None else wait):
            raise GuardBusy("the detector is busy")
        try:
            return self._check(text, settings or Settings(), policy or GuardPolicy.default())
        finally:
            self.lock.release()

    def _check(self, text: str, settings: Settings, policy: GuardPolicy) -> dict:
        t0 = time.perf_counter()
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        size = len(text.encode("utf-8"))
        blocks: list[dict] = []  # why the prompt is refused
        warnings: list[dict] = []  # what the sender is told and left to judge
        components: list[str] = []
        found = {"detected": False, "lines": 0, "code_lines": 0, "share": 0.0, "languages": [], "blocks": []}
        marks: list[dict] = []
        overlaps: list[dict] = []
        findings, doc, offsets = {NAME: []}, None, {}

        too_large = bool(policy.max_bytes) and size > policy.max_bytes
        if too_large:  # refused unread
            blocks.append({"rule": "size", "detail": f"{size:,} bytes; the limit is {policy.max_bytes:,}"})
        else:
            found = code.analyse(text)
            marks = markings.find(text, ORG["markings"])
            overlaps = self.registry.match(text) if self.registry is not None and policy.protected != "allow" else []
            doc, offsets = to_document(text)
            # People already redacted in this conversation are looked for again, in any form.
            s = copy.copy(settings)
            s.extra_deny_list = list(settings.extra_deny_list) + sorted(self.persons.origin)
            detector = get_detector(s)
            findings = detector.detect_all({NAME: doc})
            components = detector.components
            if found["detected"] and policy.source_code != "allow":
                what = {"rule": "source_code", "detail": f"source code at {_lines(found['blocks'])}"
                        + (f" ({', '.join(found['languages'])})" if found["languages"] else "")}
                (blocks if policy.source_code == "block" else warnings).append(what)
            if marks and policy.markings != "allow":
                what = {"rule": "marking", "detail": "; ".join(f"“{m['text']}” on line {m['line']}" for m in marks)}
                (blocks if policy.markings == "block" else warnings).append(what)
            for hit in overlaps:
                what = {"rule": "protected", "detail": f"{hit['words']:,} words in common with the registered document "
                        f"“{hit['name']}” at {_lines(hit['lines'])}"}
                (blocks if policy.protected == "block" else warnings).append(what)
            held = Counter(f.entity_type for f in findings[NAME] if f.decision in LIVE)
            for category in policy.block_categories:
                if held.get(category):
                    blocks.append({"rule": "category", "category": category,
                                   "detail": f"{held[category]} value(s) of a category that must not be sent"})

        live = [f for f in findings[NAME] if f.decision in LIVE]
        safe, scrubbed, restarted = "", 0, False
        if not blocks:
            scheme = (settings.token_key, settings.profile)
            restarted = self.vault is not None and scheme != self.scheme
            if restarted:  # tokens of another scheme would collide with the new ones
                self.persons, self.vault = PersonIndex(), None
            for name in detector.person_index.origin:
                self.persons.add(name, NAME)
            if self.vault is None:
                self.vault, self.scheme = TokenVault(self.persons, settings.token_key, settings.profile), scheme
            docs = {NAME: doc}
            self.vault.assign_all(docs, findings)
            needles = build_needles(self.vault, findings)
            locate_missed(docs, findings, needles, self.vault)
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
            "verdict": "blocked" if blocks else "redacted" if live or scrubbed else "clean",
            "blocked_by": [b["rule"] for b in blocks], "warned": [w["rule"] for w in warnings],
            "chars": len(text), "bytes": size, "code_lines": found["code_lines"],
            "values": len({(f.entity_type, f.text.lower()) for f in live}), "findings": len(live),
            "review": sum(f.decision == "review" for f in live), "profile": settings.profile,
            "by_category": dict(Counter(f.entity_type for f in live)),
        }
        self._record(entry)
        shown = []
        for f in sorted(live, key=lambda f: offsets[f.span_id] + f.start):
            a = offsets[f.span_id] + f.start
            shown.append({"entity_type": f.entity_type, "text": f.text, "token": None if blocks else f.token, "score": f.score,
                          "decision": f.decision, "layer": f.layer, "reasons": f.reasons, "start": a,
                          "end": a + f.end - f.start, "line": text.count("\n", 0, a) + 1})
        return {**entry, "safe_text": safe, "findings": shown, "code": found, "markings": marks, "protected": overlaps, "blocks": blocks,
                "warnings": warnings, "policy": asdict(policy), "scrubbed": scrubbed, "restarted": restarted,
                "dropped": sum(f.decision == "drop" for f in findings[NAME]), "components": components,
                "elapsed": round(time.perf_counter() - t0, 3)}

    # --------------------------------------------------------------------------- rehydrate
    def _repair(self, text: str) -> tuple[str, list[str]]:
        """A model often rewrites a token: "[Person_001]", "PERSON 001", "[PERSON-001]". Each such
        form of a token this conversation issued is put back into shape, so that it can be restored."""
        repaired: list[str] = []
        for token in sorted(self.vault.values if self.vault is not None else [], key=len, reverse=True):
            parts = [re.escape(p) for p in token[1:-1].split("_") if p]
            if len(parts) < 2 or not re.search(r"\d", parts[-1]):
                continue  # "[CARD]": no number, so a loose form would be an ordinary word
            inner = r"[ \t_-]*".join(parts)
            loose = re.compile(r"\[[ \t]*" + inner + r"[ \t]*\]"  # in brackets, however it is spaced or cased
                               r"|(?<![A-Za-z0-9_\[])" + inner + r"(?![A-Za-z0-9]|[_-][A-Za-z0-9]|[ \t]*\])",  # or without them
                               re.IGNORECASE)

            def fix(m: re.Match, token=token) -> str:
                if m.group() != token and token not in repaired:
                    repaired.append(token)
                return token

            text = loose.sub(fix, text)
        return text, repaired

    def _inspect(self, answer: str, settings: Settings) -> dict:
        """What an answer holds besides tokens: values this conversation had replaced (the model should
        never have seen them), and values that came from nowhere in it (the model produced them)."""
        known: dict[str, str] = {}
        for token, forms in (self.vault.values if self.vault is not None else {}).items():
            for form in forms:
                known[re.sub(r"\s+", " ", form.strip()).lower()] = token
        echoed: list[str] = []
        if self.vault is not None:
            for _, _, matched in find(answer, build_needles(self.vault)):
                token = known.get(re.sub(r"\s+", " ", matched.strip()).lower())
                if token and token not in echoed:
                    echoed.append(token)
        doc, offsets = to_document(answer)
        s = copy.copy(settings)
        s.extra_deny_list = list(settings.extra_deny_list) + sorted(self.persons.origin)
        produced = []
        for f in get_detector(s).detect_all({NAME: doc})[NAME]:
            if f.decision not in LIVE or re.sub(r"\s+", " ", f.text.strip()).lower() in known:
                continue
            a = offsets[f.span_id] + f.start
            produced.append({"entity_type": f.entity_type, "text": f.text, "score": f.score, "decision": f.decision,
                             "line": answer.count("\n", 0, a) + 1, "reasons": f.reasons})
        return {"echoed": echoed, "produced": produced}

    def rehydrate(self, text: str, operator: str | None = None, purpose: str = "", settings: Settings | None = None,
                  inspect: bool = True, wait: float | None = None) -> dict:
        """Values back into an answer that holds tokens, and a look at what else the answer holds.

        Only tokens this conversation issued are restored; one the model wrote differently is put
        back into shape first. With ``inspect`` the answer also goes through detection: it is
        reported, never changed. If the detector is in use the answer is restored uninspected."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        fixed, repaired = self._repair(text)
        out, restored, unknown = rehydrate(fixed, self.vault.values if self.vault is not None else {})
        found: dict = {"echoed": [], "produced": []}
        inspected = False
        if inspect and self.lock.acquire(timeout=-1 if wait is None else wait):
            try:
                found, inspected = self._inspect(fixed, settings or Settings()), True
            finally:
                self.lock.release()
        self._record({"event": "rehydrate", "timestamp": audit.now(), "operator": operator or Settings().operator,
                      "tokens": restored, "count": len(restored), "unknown": len(unknown), "repaired": len(repaired),
                      "echoed": len(found["echoed"]), "produced": len(found["produced"]), "inspected": inspected,
                      "purpose": purpose})
        return {"text": out, "restored": restored, "unknown": unknown, "repaired": repaired, "inspected": inspected, **found}

    def state(self) -> dict:
        v = self.vault
        return {"checks": self.checks, "tokens": len(v.values) if v is not None else 0,
                "people": len(v.person_no) if v is not None else 0,
                "blocked": sum(e.get("verdict") == "blocked" for e in self.log),
                "restored": sum(e["event"] == "rehydrate" for e in self.log), "log": self.log[::-1]}


# ------------------------------------------------------------------------------ the record
def default_record_path() -> Path:
    return Path(os.environ.get("PII_SHIELD_GUARD_LOG") or Path.home() / ".pii_shield" / "guard_log.jsonl")


def activity(path: str | Path, recent: int = 200) -> dict:
    """What the guard did, over all conversations: totals, per operator, per day, per rule and per
    category, the latest events, and whether the record is intact (audit.verify_chain)."""
    path = Path(path)
    events: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass  # verify_chain below says which line
    ok, records, head = audit.verify_chain(path) if events else (True, 0, "")
    checks = [e for e in events if e.get("event") == "check"]
    restores = [e for e in events if e.get("event") == "rehydrate"]

    def tally(rows: list[dict]) -> dict:
        c = Counter(e.get("verdict") for e in rows)
        return {"checks": len(rows), "blocked": c["blocked"], "redacted": c["redacted"], "clean": c["clean"],
                "values": sum(e.get("values", 0) for e in rows if e.get("verdict") == "redacted")}

    operators: dict[str, dict] = {}
    for name in sorted({e.get("operator") or "unknown" for e in events}):
        mine = [e for e in checks if (e.get("operator") or "unknown") == name]
        back = [e for e in restores if (e.get("operator") or "unknown") == name]
        operators[name] = {**tally(mine), "restored": len(back), "tokens_restored": sum(e.get("count", 0) for e in back),
                           "last": max((e.get("timestamp", "") for e in mine + back), default="")}
    days: dict[str, list[dict]] = {}
    for e in checks:
        days.setdefault(e.get("timestamp", "")[:10], []).append(e)
    rules, categories, held = Counter(), Counter(), Counter()
    for e in checks:
        rules.update(e.get("blocked_by") or [])
        (held if e.get("verdict") == "blocked" else categories).update(e.get("by_category") or {})
    return {
        "path": str(path), "integrity": {"ok": ok, "records": records, "detail": "" if ok else head},
        "first": events[0].get("timestamp", "") if events else "", "last": events[-1].get("timestamp", "") if events else "",
        "totals": {**tally(checks), "restored": len(restores), "tokens_restored": sum(e.get("count", 0) for e in restores),
                   "echoed": sum(e.get("echoed", 0) for e in restores), "produced": sum(e.get("produced", 0) for e in restores),
                   "operators": len(operators), "code_lines_stopped": sum(e.get("code_lines", 0) for e in checks if e.get("verdict") == "blocked")},
        "by_operator": [{"operator": k, **v} for k, v in operators.items()],
        "by_day": [{"day": d, **tally(rows)} for d, rows in sorted(days.items())],
        "by_rule": dict(rules), "replaced_by_category": dict(categories), "stopped_by_category": dict(held),
        "recent": [{k: v for k, v in e.items() if k not in ("hash", "prev", "run_id")} for e in events[-recent:]][::-1],
    }
