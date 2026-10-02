"""Detection ensemble, orchestrated by Microsoft Presidio's AnalyzerEngine.

L1 rules + checksums + context   (RuleRecognizer, inside Presidio)
L2 NER: spaCy, optional GLiNER   (inside Presidio)
L3 structure: headers and labels (structure.py)
L4 propagation of confirmed people across all files (propagation.py)
L0 fail-closed: identifier-like text that OCR read with low confidence (this module)
Resolver: trim, merge, score agreement, route to redact / review / drop (resolver.py)
"""
from __future__ import annotations

import logging
import re

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.predefined_recognizers import SpacyRecognizer

from ..config import Settings
from ..models import Document, Finding, Span
from .ner import build_nlp_engine, try_gliner
from .names import upper_runs
from .propagation import build_index, propagate
from .resolver import finalise
from .rules import IMAGE_RULES, RuleRecognizer, run_rules
from .structure import structure_findings

log = logging.getLogger(__name__)
OUTPUT_ENTITIES = [
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "EMPLOYEE_ID", "VENDOR_ID", "US_SSN", "PASSPORT", "IN_PAN",
    "PL_PESEL", "TAX_ID", "NATIONAL_ID", "CREDIT_CARD", "IBAN_CODE", "DATE_OF_BIRTH", "ADDRESS", "IN_AADHAAR",
    "IP_ADDRESS", "CREDENTIAL",
]


class Detector:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.analyzer: AnalyzerEngine | None = None
        self.components: list[str] = ["L1 rules (PiiShieldRules)"]
        if self.settings.use_spacy:
            try:
                nlp_engine, model = build_nlp_engine(self.settings.spacy_model)
            except RuntimeError as exc:
                log.warning("%s; running rules only", exc)
                nlp_engine = None
            if nlp_engine is not None:
                registry = RecognizerRegistry(supported_languages=["en"])
                registry.add_recognizer(RuleRecognizer())
                registry.add_recognizer(SpacyRecognizer(supported_entities=["PERSON"]))
                self.components.append(f"L2 spaCy NER ({model})")
                if self.settings.use_gliner:
                    g = try_gliner(self.settings.gliner_model, self.settings.gliner_threshold)
                    if g is not None:
                        registry.add_recognizer(g)
                        self.components.append(f"L2 GLiNER ({self.settings.gliner_model})")
                self.analyzer = AnalyzerEngine(registry=registry, nlp_engine=nlp_engine, supported_languages=["en"])
                supported = set(self.analyzer.get_supported_entities("en"))
                self.analyzer_entities = [e for e in OUTPUT_ENTITIES if e in supported]
        self.allow = {a.lower() for a in self.settings.allow_list}

    # ---------------------------------------------------------------------------- per span
    def detect_span(self, span: Span, prefix: str = "") -> list[Finding]:
        if not span.text.strip():
            return []
        text = prefix + span.text
        off = len(prefix)
        # Short document properties ("Marcus Feld", "Draft") are fragments where NER is unreliable;
        # the structure layer (field name -> category) and the rules cover them. Free-text
        # properties ("Drafted for Priya Raman", description, comments) are prose and get NER too.
        prose = span.kind != "metadata" or len(span.text.split()) >= 3
        use_ner = prose and any(c.isupper() for c in span.text)
        if self.analyzer is not None and use_ner:
            raw = self.analyzer.analyze(text=text, language="en", entities=self.analyzer_entities, score_threshold=0.0)
            raw = list(raw) + self._recased_ner(text)
        else:
            raw = run_rules(text, OUTPUT_ENTITIES)
        if span.source == "image_ocr":
            raw = list(raw) + run_rules(text, OUTPUT_ENTITIES, IMAGE_RULES)
        if off:
            # A match that starts in the neighbouring context is dropped below, on the assumption the
            # neighbour detects it. When it spans the join ("(212)Noted" + newline + "555-0108") neither
            # side sees it whole, so the span's own text is also scanned alone; the resolver merges
            # duplicates.
            raw = list(raw) + [_shift(r, off) for r in run_rules(span.text, OUTPUT_ENTITIES)]
        out = []
        for r in raw:
            if r.start < off:
                continue  # starts in the neighbouring text: that span detects it on its own
            start, end = r.start - off, r.end - off
            meta = r.recognition_metadata or {}
            name = meta.get(r.RECOGNIZER_NAME_KEY, "unknown")
            if name == "SpacyRecognizer":
                name, layer, reasons = "ner:spacy", "L2 ner", [f"spaCy NER labelled PERSON (score {r.score:.2f})"]
            else:
                layer, reasons = meta.get("layer", "L2 ner"), list(meta.get("reasons", []))
            out.append(Finding(span_id=span.id, file=span.file, start=start, end=end, text=span.text[start:end],
                               entity_type=r.entity_type, score=round(r.score, 3), recognizer=name, layer=layer,
                               reasons=reasons))
        out.extend(structure_findings(span, self.allow))
        return out

    def _recased_ner(self, text: str) -> list:
        """NER models read capitals as acronyms, so "PRIYA RAMAN" is invisible to them. Runs of
        ALL-CAPS words are title-cased (same length, so offsets still hold) and NER runs again;
        only PERSON hits inside those runs are kept."""
        runs = upper_runs(text)
        if not runs:
            return []
        chars = list(text)
        for a, b in runs:
            chars[a:b] = list(text[a:b].title())
        recased = "".join(chars)
        if len(recased) != len(text):
            return []
        hits = self.analyzer.analyze(text=recased, language="en", entities=["PERSON"], score_threshold=0.0)
        out = []
        for r in hits:
            meta = r.recognition_metadata or {}
            if meta.get(r.RECOGNIZER_NAME_KEY) != "SpacyRecognizer" or not any(a <= r.start and r.end <= b for a, b in runs):
                continue
            meta[r.RECOGNIZER_NAME_KEY] = "ner:spacy-recased"
            meta["layer"] = "L2 ner"
            meta["reasons"] = [f"spaCy NER labelled PERSON on case-restored text (score {r.score:.2f})"]
            out.append(r)
        return out

    def detect_document(self, doc: Document) -> list[Finding]:
        findings: list[Finding] = []
        prev: Span | None = None
        for span in doc.spans:
            findings.extend(self.detect_span(span, self._prefix(span, prev)))
            prev = span
        return findings

    def _prefix(self, span: Span, prev: Span | None) -> str:
        """Neighbouring text given to recognisers so labels in a header or previous line count as context."""
        if span.kind == "table_cell" and span.header:
            return f"{span.header}: "
        if span.kind == "metadata" and span.header:
            return f"{span.header}: "
        if prev is not None and prev.page == span.page and prev.kind != "metadata":
            tail = prev.text[-self.settings.context_window:]
            return tail.split(" ", 1)[-1] + "\n" if " " in tail else tail + "\n"
        return ""

    # ----------------------------------------------------------------------- whole corpus
    def detect_all(self, docs: dict[str, Document]) -> dict[str, list[Finding]]:
        s = self.settings
        first = {f: self.detect_document(d) for f, d in docs.items()}
        resolved = finalise(first, docs, s)
        if s.propagate_persons:
            idx = build_index(resolved, s.extra_deny_list)
            extra = propagate(docs, idx)
            for f in extra:
                first[f.file].append(f)
            resolved = finalise(first, docs, s)
            self.person_index = idx
        else:
            self.person_index = build_index(resolved, s.extra_deny_list)
        for file, doc in docs.items():
            resolved[file].extend(fail_closed_findings(doc, resolved[file], s))
        return resolved


def _shift(r, off: int):
    r.start += off
    r.end += off
    return r


IDENT_TEXT = re.compile(r"\d|@")
IDENT_IMAGE = re.compile(r"\d|@|[A-Za-z]{2,}[._][A-Za-z]{2,}")


def fail_closed_findings(doc: Document, findings: list[Finding], s: Settings) -> list[Finding]:
    """L0: OCR words read with low confidence that look like identifiers are masked and queued for
    review, so an unreadable identifier never reaches the LLM. Text read from screenshots gets a
    stricter floor (low_conf_image_ocr) and a wider notion of "identifier-like": besides digits and
    "@", dotted tokens such as damaged e-mails or domains ("martinezmaomeccrp.com")."""
    covered: dict[str, list[tuple[int, int]]] = {}
    for f in findings:
        if f.decision != "drop":
            covered.setdefault(f.span_id, []).append((f.start, f.end))
    out = []
    for span in doc.spans:
        if span.source == "native":
            continue
        image = span.source == "image_ocr"
        floor = s.low_conf_image_ocr if image else s.low_conf_ocr
        ident = IDENT_IMAGE if image else IDENT_TEXT
        for w in span.words:
            if w.conf is None or w.conf >= floor:
                continue
            if not ident.search(w.text) or len(w.text) < 4:
                continue
            if any(a < w.end and b > w.start for a, b in covered.get(span.id, [])):
                continue
            out.append(Finding(span_id=span.id, file=span.file, start=w.start, end=w.end, text=w.text,
                               entity_type="LOW_CONFIDENCE_OCR", score=0.5, recognizer="failclosed:ocr",
                               layer="L0 fail-closed", decision="review",
                               reasons=[f"OCR confidence {w.conf:.2f} < {floor:.2f} on identifier-like "
                                        f"{'screenshot ' if image else ''}text"],
                               page=span.page, location=span.location, kind=span.kind, source=span.source,
                               context_type="image" if span.source == "image_ocr" else "narrative"))
    return out


__all__ = ["Detector", "OUTPUT_ENTITIES"]
