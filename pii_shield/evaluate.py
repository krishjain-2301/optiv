"""Measure, don't claim.

Gold-label CSV format (one row per PII *instance*):

    file,page,text,category,context_type,note
    risk_policy.pdf,3,Priya Raman,PERSON,narrative,
    org_pack.pptx,1,+1 (555) 0114,PHONE_NUMBER,table,

``page`` may be blank for formats without pages (DOCX) or document properties. ``context_type``
is one of native | labelled | table | narrative | image | metadata and drives the per-source
breakdown (E.6 prompts 2 and 3).

Metrics:
* recall      = gold instances matched one-to-one by a live finding (redact or review) in the same file/page
* category recall = the matching finding also has the right category
* precision   = live findings whose text corresponds to *some* gold value in that file
* leak check  = gold values still present verbatim in the redacted text sent to the LLM
* structure retention for DOCX/PPTX from the raw XML, and text similarity for OCR'd PDFs
  against an optional hand transcription.
"""
from __future__ import annotations

import csv
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lxml import etree
from rapidfuzz import fuzz

from .models import Document, Finding

GOLD_FIELDS = ["file", "page", "text", "category", "context_type", "note"]
EQUIVALENT = {
    # categories that count as the same thing for scoring
    "EMPLOYEE_ID": {"EMPLOYEE_ID", "VENDOR_ID", "NATIONAL_ID"},
    "TAX_ID": {"TAX_ID", "US_SSN", "NATIONAL_ID"},
    "NATIONAL_ID": {"NATIONAL_ID", "PL_PESEL", "US_SSN", "IN_PAN", "PASSPORT", "TAX_ID"},
}


@dataclass
class GoldItem:
    file: str
    page: Optional[int]
    text: str
    category: str
    context_type: str = ""
    note: str = ""
    matched_by: Optional[Finding] = None


def load_gold(path: str | Path) -> list[GoldItem]:
    items = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            page = row.get("page", "").strip()
            items.append(GoldItem(row["file"].strip(), int(page) if page.isdigit() else None, row["text"],
                                  row["category"].strip().upper(), row.get("context_type", "").strip(),
                                  row.get("note", "")))
    return items


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9@]", "", s.lower())


def _match_score(gold: GoldItem, f: Finding) -> float:
    g, t = norm(gold.text), norm(f.text)
    if not g or not t:
        return 0.0
    if g == t:
        return 1.0
    if g in t:  # finding is wider than gold (e.g. whole address line); accept if not wildly wider
        return 0.9 if len(t) <= 3 * len(g) + 10 else 0.5
    if t in g and len(t) >= 0.6 * len(g):
        return 0.8
    r = fuzz.ratio(g, t) / 100
    return r if r >= 0.85 else 0.0


def _same_place(gold: GoldItem, f: Finding) -> bool:
    if gold.file != f.file:
        return False
    return gold.page is None or f.page is None or gold.page == f.page


def _cat_ok(gold: str, found: str) -> bool:
    return gold == found or found in EQUIVALENT.get(gold, set()) or gold in EQUIVALENT.get(found, set())


@dataclass
class EvalResult:
    total_gold: int
    recalled: int
    category_correct: int
    live_findings: int
    true_positive_findings: int
    auto_findings: int = 0  # decision == redact (excludes the review band)
    auto_true_positive: int = 0
    by_category: dict = field(default_factory=dict)
    by_context: dict = field(default_factory=dict)
    by_file: dict = field(default_factory=dict)
    missed: list = field(default_factory=list)
    false_positives: list = field(default_factory=list)
    leaks: list = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.recalled / self.total_gold if self.total_gold else 0.0

    @property
    def category_recall(self) -> float:
        return self.category_correct / self.total_gold if self.total_gold else 0.0

    @property
    def precision(self) -> float:
        return self.true_positive_findings / self.live_findings if self.live_findings else 0.0

    @property
    def precision_auto(self) -> float:
        """Precision of automatic redactions only (review-band hits are for a human to confirm)."""
        return self.auto_true_positive / self.auto_findings if self.auto_findings else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def as_dict(self) -> dict:
        return {
            "gold_instances": self.total_gold, "recall": round(self.recall, 4),
            "category_recall": round(self.category_recall, 4), "precision": round(self.precision, 4),
            "precision_auto_redact": round(self.precision_auto, 4),
            "f1": round(self.f1, 4), "live_findings": self.live_findings,
            "by_category": self.by_category, "by_context": self.by_context, "by_file": self.by_file,
            "missed": self.missed, "false_positives": self.false_positives, "leaks": self.leaks,
        }


def evaluate(gold: list[GoldItem], findings: dict[str, list[Finding]],
             redacted: Optional[dict[str, str]] = None) -> EvalResult:
    live = [f for fs in findings.values() for f in fs if f.decision in ("redact", "review")]
    files = set(findings)
    gold = [g for g in gold if g.file in files]
    for g in gold:
        g.matched_by = None

    # One-to-one assignment, best matches first.
    pairs = []
    for gi, g in enumerate(gold):
        for fi, f in enumerate(live):
            if _same_place(g, f):
                s = _match_score(g, f)
                if s > 0:
                    pairs.append((s, _cat_ok(g.category, f.entity_type), gi, fi))
    pairs.sort(key=lambda p: (-p[0], not p[1]))
    used_g, used_f = set(), set()
    for s, _, gi, fi in pairs:
        if gi in used_g or fi in used_f:
            continue
        gold[gi].matched_by = live[fi]
        used_g.add(gi)
        used_f.add(fi)

    # Precision: a finding is correct if it corresponds to any gold value in its file (repeats count).
    gold_by_file = defaultdict(list)
    for g in gold:
        gold_by_file[g.file].append(g)
    tp, fps, auto, auto_tp = 0, [], 0, 0
    for f in live:
        ok = any(_match_score(g, f) > 0 for g in gold_by_file[f.file])
        auto += f.decision == "redact"
        auto_tp += ok and f.decision == "redact"
        if ok:
            tp += 1
        else:
            fps.append({"file": f.file, "page": f.page, "text": f.text, "entity_type": f.entity_type,
                        "score": f.score, "decision": f.decision, "layer": f.layer, "location": f.location})

    def bucket(key_fn):
        agg = defaultdict(lambda: {"gold": 0, "recalled": 0, "category_correct": 0})
        for g in gold:
            b = agg[key_fn(g)]
            b["gold"] += 1
            if g.matched_by is not None:
                b["recalled"] += 1
                b["category_correct"] += _cat_ok(g.category, g.matched_by.entity_type)
        return {k: {**v, "recall": round(v["recalled"] / v["gold"], 4)} for k, v in sorted(agg.items())}

    leaks = []
    if redacted:
        for g in gold:
            text = redacted.get(g.file, "")
            if len(g.text) >= 3 and re.search(r"(?<![\w])" + re.escape(g.text) + r"(?![\w])", text):
                leaks.append({"file": g.file, "page": g.page, "text": g.text, "category": g.category})

    return EvalResult(
        total_gold=len(gold),
        recalled=sum(g.matched_by is not None for g in gold),
        category_correct=sum(g.matched_by is not None and _cat_ok(g.category, g.matched_by.entity_type) for g in gold),
        live_findings=len(live), true_positive_findings=tp, auto_findings=auto, auto_true_positive=auto_tp,
        by_category=bucket(lambda g: g.category), by_context=bucket(lambda g: g.context_type or "unspecified"),
        by_file=bucket(lambda g: g.file),
        missed=[{"file": g.file, "page": g.page, "text": g.text, "category": g.category, "context_type": g.context_type}
                for g in gold if g.matched_by is None],
        false_positives=fps, leaks=leaks,
    )


# --------------------------------------------------------------------- structure retention
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _texts(el, tag) -> str:
    return "".join(t.text or "" for t in el.iter(tag)).strip()


def _own_text(el) -> str:
    """Text of a WordprocessingML element excluding anything inside an anchored text box."""
    return "".join(t.text or "" for t in el.iter(f"{W}t")
                   if not any(a.tag == f"{W}txbxContent" for a in t.iterancestors() if a is not el)).strip()


def reference_structure(path: str | Path) -> dict:
    """Counts read straight from the package XML, independently of the extractor."""
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if "word/document.xml" in names:
            # Content-bearing elements only: an empty layout table or a paragraph whose only text
            # belongs to an anchored text box carries nothing to retain (text boxes are counted apart).
            root = etree.fromstring(zf.read("word/document.xml"))
            body = root.find(f"{W}body")
            own = lambda el: _own_text(el)  # noqa: E731
            paras = [p for p in body.iter(f"{W}p") if not any(a.tag in (f"{W}tc", f"{W}txbxContent") for a in p.iterancestors())]
            headings = [p for p in paras if (p.find(f"{W}pPr/{W}pStyle") is not None and
                        re.match(r"(heading|title)", p.find(f"{W}pPr/{W}pStyle").get(f"{W}val", ""), re.I))]
            tables = [t for t in body.iter(f"{W}tbl") if own(t)]
            rows = [tr for t in tables for tr in t.findall(f"{W}tr") if own(tr)]
            cells = [tc for t in tables for tc in t.iter(f"{W}tc") if own(tc)]
            return {"headings": sum(bool(own(p)) for p in headings),
                    "paragraphs": sum(bool(own(p)) for p in paras) - sum(bool(own(p)) for p in headings),
                    "tables": len(tables), "rows": len(rows), "cells": len(cells)}
        if "ppt/presentation.xml" in names:
            slides = [n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)]
            tables = cells = rows = 0
            for s in slides:
                root = etree.fromstring(zf.read(s))
                for t in root.iter(f"{A}tbl"):
                    tables += 1
                    rows += len(t.findall(f"{A}tr"))
                    cells += sum(bool(_texts(tc, f"{A}t")) for tc in t.iter(f"{A}tc"))
            return {"slides": len(slides), "tables": tables, "rows": rows, "cells": cells}
    return {}


def structure_retention(doc: Document, transcription: Optional[str] = None) -> dict:
    """Share of structural elements (DOCX/PPTX) or text (OCR'd PDF vs transcription) retained."""
    if doc.file_type in ("docx", "pptx"):
        ref = reference_structure(doc.path)
        detail = {}
        for k, v in ref.items():
            got = doc.structure.get(k, 0)
            detail[k] = {"reference": v, "extracted": got, "retained": round(min(got, v) / v, 4) if v else 1.0}
        scores = [d["retained"] for d in detail.values() if d["reference"]]
        return {"method": "element counts vs raw XML", "score": round(sum(scores) / len(scores), 4) if scores else None,
                "detail": detail}
    if transcription:
        ratio = fuzz.token_sort_ratio(_plain(doc.markdown), _plain(transcription)) / 100
        return {"method": "token similarity vs hand transcription", "score": round(ratio, 4)}
    return {"method": "no reference available", "score": None, "detail": doc.structure}


def _plain(md: str) -> str:
    md = re.sub(r"<!--.*?-->", " ", md)
    md = re.sub(r"[|#>*`\-]+", " ", md)
    return " ".join(md.split()).lower()


def gold_template(findings: dict[str, list[Finding]], path: str | Path) -> Path:
    """Bootstrap a gold file from a run; a human then corrects it (adds misses, deletes FPs)."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=GOLD_FIELDS)
        w.writeheader()
        for fs in findings.values():
            for f in fs:
                if f.decision in ("redact", "review"):
                    w.writerow({"file": f.file, "page": f.page or "", "text": f.text, "category": f.entity_type,
                                "context_type": f.context_type, "note": "auto - verify"})
    return Path(path)
