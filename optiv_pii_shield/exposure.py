"""Exposure score: how much personal data a file carries, where it sits, and what risk is left
after redaction.

* **exposure**: sum of sensitivity weights over every PII instance found (a passport number weighs
  more than a work e-mail), also per 1,000 words so files of different length compare, and per
  page/slide for a heatmap.
* **residual risk** after redaction, in three parts that are kept apart because they are known
  to different degrees:
    - *known residual*: original values still present in an output. Zero by construction (the leak
      gate scrubs the LLM text and refuses masked files), so this reports what the gate had to do.
    - *unreadable content*: images withheld and OCR words masked because they could not be read.
      Redacted, but nobody knows what they contained.
    - *estimated missed*: PII the detectors probably did not find, estimated from miss rates
      measured on the held-out set (scripts/make_heldout.py). An estimate, labelled as one.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .config import EXPOSURE_RATING, MISS_RATES, SENSITIVITY
from .models import Document, Finding

LIVE = ("redact", "review")


def word_count(doc: Document) -> int:
    return sum(len(s.text.split()) for s in doc.spans)


def weight(entity: str) -> float:
    return SENSITIVITY.get(entity, SENSITIVITY["_default"])


def miss_rate(f: Finding) -> float:
    ocr = f.source in ("ocr", "image_ocr")
    key = (f.entity_type, "ocr" if ocr else "native")
    return MISS_RATES.get(key, MISS_RATES.get((f.entity_type, "native"), MISS_RATES["_default"]))


def rating(score: float, density: float, top_weight: float) -> str:
    """critical: any government/financial identifier or credential; then by density."""
    r = EXPOSURE_RATING
    if top_weight >= r["critical_weight"]:
        return "critical"
    if density >= r["high_density"]:
        return "high"
    if density >= r["medium_density"]:
        return "medium"
    return "low" if score > 0 else "none"


def file_exposure(doc: Document, findings: list[Finding]) -> dict:
    live = [f for f in findings if f.decision in LIVE and f.entity_type != "LOW_CONFIDENCE_OCR"]
    words = word_count(doc)
    score = sum(weight(f.entity_type) for f in live)
    distinct: dict[str, Finding] = {}
    for f in live:
        distinct.setdefault(f.token or f"{f.entity_type}:{f.text.lower()}", f)
    distinct_score = sum(weight(f.entity_type) for f in distinct.values())
    by_page: dict[str, float] = defaultdict(float)
    for f in live:
        by_page[str(f.page) if f.page else "document"] += weight(f.entity_type)
    by_cat = Counter()
    for f in live:
        by_cat[f.entity_type] += weight(f.entity_type)
    density = round(score / words * 1000, 2) if words else 0.0
    top = max((weight(f.entity_type) for f in live), default=0.0)

    # residual risk
    est = defaultdict(float)
    for f in distinct.values():
        r = miss_rate(f)
        est[f.entity_type] += r / (1 - r) if r < 1 else 0  # found n, miss rate r -> about n*r/(1-r) missed
    est_score = sum(weight(c) * n for c, n in est.items())
    unread_images = sum(i.ocr_status in ("unreadable", "low_confidence") for i in doc.images)
    unread_words = sum(f.entity_type == "LOW_CONFIDENCE_OCR" and f.decision in LIVE for f in findings)
    gate = dict(doc.gate)
    return {
        "score": round(score, 1),
        "distinct_values_score": round(distinct_score, 1),
        "per_1k_words": density,
        "words": words,
        "rating": rating(score, density, top),
        "by_category": dict(by_cat.most_common()),
        "by_page": dict(sorted(by_page.items(), key=lambda kv: (kv[0] == "document", int(kv[0]) if kv[0].isdigit() else 0))),
        "residual": {
            "known": {
                "values_left_in_outputs": 0 if gate.get("masked") != "withheld" else None,
                "masked_copy": gate.get("masked", "not written"),
                "llm_text_values_caught_by_gate": gate.get("llm_text_scrubbed", 0),
                "masked_values_caught_by_gate": gate.get("masked_scrubbed", 0),
            },
            "unreadable": {"images_withheld": unread_images, "ocr_words_masked": unread_words},
            "estimated_missed": {
                "instances": round(sum(est.values()), 1),
                "by_category": {c: round(n, 2) for c, n in sorted(est.items()) if n >= 0.005},
                "score": round(est_score, 1),
                "per_1k_words": round(est_score / words * 1000, 2) if words else 0.0,
                "basis": MISS_RATES["_basis"],
            },
        },
    }
