"""Protected content: confidential documents registered by fingerprint.

A trade secret has no shape a rule can match. What can be done offline is to recognise text that
was *copied* from a document the organisation has declared confidential. A registered document is
reduced to fingerprints: a keyed hash of every run of five consecutive words. The document itself
is not kept. A prompt that shares enough consecutive words with a registered document overlaps
it, and the prompt guard refuses it by default (``GuardPolicy.protected``).

What this is not: it does not recognise a paraphrase, a translation or a summary, only copied
wording. And a fingerprint is not the text, but someone who holds both this file and a candidate
text can test whether the candidate was registered.

The registry is a JSON file that outlives the server: $PII_SHIELD_REGISTRY, else
``~/.pii_shield/registry.json``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path

from . import audit

K = 5  # words per fingerprint
MIN_RUN = 10  # this many consecutive words in common is an overlap
MIN_COVERED = 30  # so is this many words in common, wherever they sit
MIN_WORDS = 10  # a shorter text cannot be registered: it would have next to no fingerprints
SIZE = 8  # bytes per fingerprint
WORD = re.compile(r"[^\W_]+")


def default_path() -> Path:
    return Path(os.environ.get("PII_SHIELD_REGISTRY") or Path.home() / ".pii_shield" / "registry.json")


def _words(text: str) -> list[tuple[str, int]]:
    """(word in lower case, where it starts) for every word: punctuation, case and layout do not count."""
    return [(m.group().lower(), m.start()) for m in WORD.finditer(text)]


class Registry:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_path()
        self.lock = threading.Lock()
        self.salt = os.urandom(16)
        self.docs: dict[str, dict] = {}  # id -> {name, words, registered, operator, digest}
        self.prints: dict[str, frozenset[bytes]] = {}  # id -> fingerprints
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.salt = base64.b64decode(data["salt"])
            for doc in data["documents"]:
                raw = base64.b64decode(doc.pop("fingerprints"))
                self.prints[doc["id"]] = frozenset(raw[i:i + SIZE] for i in range(0, len(raw), SIZE))
                self.docs[doc["id"]] = doc

    def _save(self) -> None:
        data = {"format": "pii-shield-registry/1", "words_per_fingerprint": K, "salt": base64.b64encode(self.salt).decode("ascii"),
                "documents": [{**doc, "fingerprints": base64.b64encode(b"".join(sorted(self.prints[i]))).decode("ascii")}
                              for i, doc in self.docs.items()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)

    def _prints(self, words: list[str]) -> list[bytes]:
        """One fingerprint per run of K words, in order: prints[i] covers words[i:i+K]."""
        return [hashlib.blake2b(" ".join(words[i:i + K]).encode("utf-8"), key=self.salt, digest_size=SIZE).digest()
                for i in range(len(words) - K + 1)]

    # ---------------------------------------------------------------------------- manage
    def add(self, name: str, text: str, operator: str = "") -> dict:
        """Register ``text`` under ``name``. The same content registered again replaces the entry."""
        words = [w for w, _ in _words(text)]
        if len(words) < MIN_WORDS:
            raise ValueError(f"too short to register: {len(words)} word(s), at least {MIN_WORDS} are needed")
        digest = hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()
        with self.lock:
            for old in [i for i, d in self.docs.items() if d["digest"] == digest]:
                del self.docs[old], self.prints[old]
            doc = {"id": uuid.uuid4().hex[:12], "name": name.strip() or "untitled", "words": len(words),
                   "registered": audit.now(), "operator": operator, "digest": digest}
            self.prints[doc["id"]] = frozenset(self._prints(words))
            self.docs[doc["id"]] = doc
            self._save()
        return self._public(doc)

    def remove(self, doc_id: str) -> bool:
        with self.lock:
            if doc_id not in self.docs:
                return False
            del self.docs[doc_id], self.prints[doc_id]
            self._save()
        return True

    def _public(self, doc: dict) -> dict:
        return {**{k: doc[k] for k in ("id", "name", "words", "registered", "operator")}, "fingerprints": len(self.prints[doc["id"]])}

    def list(self) -> list[dict]:
        return [self._public(d) for d in sorted(self.docs.values(), key=lambda d: d["registered"], reverse=True)]

    # ----------------------------------------------------------------------------- match
    def match(self, text: str) -> list[dict]:
        """Registered documents ``text`` overlaps, most words in common first:
        [{"id", "name", "words": in common, "longest": consecutive, "share": of the text, "lines": [[a, b], ...]}]"""
        if not self.docs:
            return []
        found = _words(text)
        prints = self._prints([w for w, _ in found])
        out = []
        for doc_id, known in self.prints.items():
            covered = [False] * len(found)
            for i, p in enumerate(prints):
                if p in known:
                    covered[i:i + K] = [True] * K
            total = sum(covered)
            longest = run = 0
            for c in covered:
                run = run + 1 if c else 0
                longest = max(longest, run)
            if longest < MIN_RUN and total < MIN_COVERED:
                continue
            lines: list[list[int]] = []
            for (_, start), c in zip(found, covered):
                if not c:
                    continue
                no = text.count("\n", 0, start) + 1
                if lines and no <= lines[-1][1] + 1:
                    lines[-1][1] = no
                else:
                    lines.append([no, no])
            out.append({"id": doc_id, "name": self.docs[doc_id]["name"], "words": total, "longest": longest,
                        "share": round(total / len(found), 3), "lines": lines})
        return sorted(out, key=lambda h: -h["words"])
