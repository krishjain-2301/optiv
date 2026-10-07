"""Human review: a reviewer confirms or rejects what the detectors found and adds what they missed.

Decisions are made per *value*, not per occurrence: "Cloud is not a person" or "Raman is a person"
holds everywhere the value appears. (Rejecting one occurrence while another stays redacted could
not be honoured anyway: the leak gate would put the token back.) After the decisions are applied
the redaction stage runs again, so every output reflects them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .detect.resolver import annotate
from .models import Document, Finding
from .redact.tokens import normalise

REVIEW_LAYER = "L5 reviewer"
LIVE = ("redact", "review")


@dataclass
class Decision:
    entity_type: str
    value: str
    action: str  # approve | reject


@dataclass
class Addition:
    text: str
    entity_type: str
    file: str | None = None  # None: every file


def value_key(entity: str, text: str) -> tuple[str, str]:
    return entity, normalise(entity, text)


def queue(findings: dict[str, list[Finding]]) -> list[dict]:
    """Distinct values awaiting a human: everything in the review band that nobody decided yet."""
    rows: dict[tuple[str, str], dict] = {}
    for fs in findings.values():
        for f in fs:
            if f.decision != "review" or f.review:
                continue
            r = rows.setdefault(value_key(f.entity_type, f.text), {
                "entity_type": f.entity_type, "value": f.text, "occurrences": 0, "files": set(), "score": f.score,
                "layer": f.layer, "location": f"{f.file}: {f.location}", "reasons": list(f.reasons)})
            r["occurrences"] += 1
            r["files"].add(f.file)
            r["score"] = max(r["score"], f.score)
    return sorted(({**r, "files": sorted(r["files"])} for r in rows.values()), key=lambda r: (-r["occurrences"], r["value"]))


def apply(docs: dict[str, Document], findings: dict[str, list[Finding]], decisions: list[Decision],
          additions: list[Addition], operator: str, persons=None) -> dict:
    """Change ``findings`` in place. Returns counts for the audit log."""
    by_key = {value_key(d.entity_type, d.value): d for d in decisions}
    approved = rejected = 0
    for fs in findings.values():
        for f in fs:
            d = by_key.get(value_key(f.entity_type, f.text))
            if d is None or f.decision not in LIVE:
                continue
            if d.action == "approve":
                f.decision, f.review = "redact", "approved"
                f.reasons.append(f"confirmed as personal data by reviewer {operator}")
                approved += 1
            elif d.action == "reject":
                f.decision, f.review, f.token = "drop", "rejected", None
                f.reasons.append(f"rejected by reviewer {operator}: not personal data")
                rejected += 1
    added = 0
    for a in additions:
        text = a.text.strip()
        if len(text) < 2:
            continue
        pattern = re.compile(r"(?<![^\W_])" + r"\s+".join(re.escape(t) for t in text.split()) + r"(?![^\W_])", re.IGNORECASE)
        for file, doc in docs.items():
            if a.file and a.file != file:
                continue
            taken: dict[str, list[tuple[int, int]]] = {}
            for f in findings[file]:
                if f.decision in LIVE:
                    taken.setdefault(f.span_id, []).append((f.start, f.end))
            for span in doc.spans:
                for m in pattern.finditer(span.text):
                    if any(s <= m.start() and m.end() <= e for s, e in taken.get(span.id, [])):
                        continue  # already redacted inside a finding
                    f = Finding(span_id=span.id, file=file, start=m.start(), end=m.end(), text=m.group(),
                                entity_type=a.entity_type, score=1.0, recognizer="reviewer:added", layer=REVIEW_LAYER,
                                reasons=[f"added by reviewer {operator}: the detectors had missed it"], decision="redact",
                                review="added")
                    findings[file].append(annotate(f, span))
                    added += 1
        if a.entity_type == "PERSON" and persons is not None:
            persons.add(text, f"reviewer {operator}")
    for fs in findings.values():
        fs.sort(key=lambda f: (f.page or 0, f.span_id, f.start))
    return {"approved": approved, "rejected": rejected, "added": added}
