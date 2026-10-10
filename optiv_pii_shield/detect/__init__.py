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
from functools import lru_cache

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.predefined_recognizers import SpacyRecognizer

from ..config import ORG, Settings
from ..models import Document, Finding, Span
from ..errors import ModelMissing
from .ner import build_nlp_engine, load_gliner
from .names import upper_runs
from .propagation import build_index, propagate, propagate_ids
from .resolver import finalise
from .rules import IMAGE_RULES, RULES, RuleRecognizer, run_rules
from .structure import name_list_findings, structure_findings
from .vocab import corpus_vocabulary

PROPAGATION_ROUNDS = 3
ID_RULES = [r for r in RULES if r.name in {p["name"] for p in ORG["id_patterns"]}]
ID_ENTITIES = sorted({r.entity for r in ID_RULES})
BROKEN_ID = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5}-)\s*$")
BROKEN_ID_REACH = 6  # text blocks: the cells of the same table row come in between
NOT_NAME_POS = {"VERB", "AUX", "ADJ", "ADV", "ADP", "DET", "PRON", "SCONJ", "CCONJ", "INTJ", "PART"}

log = logging.getLogger(__name__)
OUTPUT_ENTITIES = [
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "EMPLOYEE_ID", "VENDOR_ID", "US_SSN", "PASSPORT", "IN_PAN",
    "PL_PESEL", "TAX_ID", "NATIONAL_ID", "CREDIT_CARD", "IBAN_CODE", "DATE_OF_BIRTH", "ADDRESS", "IN_AADHAAR",
    "IP_ADDRESS", "CREDENTIAL", "BANK_ACCOUNT", "UPI_ID", "DRIVING_LICENCE", "IN_VOTER_ID", "UK_NINO", "HEALTH_DATA",
    "CONFIDENTIAL_TERM",
]


@lru_cache(maxsize=8)
def _terms_pattern(terms: tuple[str, ...]) -> re.Pattern | None:
    alts = sorted({t.strip() for t in terms if len(t.strip()) >= 3}, key=len, reverse=True)
    if not alts:
        return None
    return re.compile(r"(?<![^\W_])(?:" + "|".join(r"\s+".join(re.escape(w) for w in a.split()) for a in alts) + r")(?![^\W_])",
                      re.IGNORECASE)


def term_findings(span: Span, terms: list[str]) -> list[Finding]:
    """The organisation's confidential terms (Settings.confidential_terms), wherever they are written."""
    rx = _terms_pattern(tuple(terms))
    if rx is None:
        return []
    return [Finding(span_id=span.id, file=span.file, start=m.start(), end=m.end(), text=m.group(),
                    entity_type="CONFIDENTIAL_TERM", score=0.95, recognizer="org:term", layer="L1 rules",
                    reasons=["listed as a confidential term of the organisation"]) for m in rx.finditer(span.text)]


class Detector:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.analyzer: AnalyzerEngine | None = None
        self.components: list[str] = ["L1 rules (PiiShieldRules)"]
        if self.settings.use_spacy:
            # Raises ModelMissing: running without the requested NER model is not a degraded mode
            # we allow silently. Rules-only runs must be asked for (use_spacy=False).
            nlp_engine, model = build_nlp_engine(self.settings.spacy_model)
            registry = RecognizerRegistry(supported_languages=["en"])
            registry.add_recognizer(RuleRecognizer())
            registry.add_recognizer(SpacyRecognizer(supported_entities=["PERSON"]))
            self.components.append(f"L2 spaCy NER ({model})")
            if self.settings.use_gliner:
                registry.add_recognizer(load_gliner(self.settings.gliner_model, self.settings.gliner_threshold,
                                                        self.settings.gliner_revision))
                self.components.append(f"L2 GLiNER ({self.settings.gliner_model})")
            self.analyzer = AnalyzerEngine(registry=registry, nlp_engine=nlp_engine, supported_languages=["en"])
            supported = set(self.analyzer.get_supported_entities("en"))
            self.analyzer_entities = [e for e in OUTPUT_ENTITIES if e in supported]
        else:
            log.warning("NER disabled by settings (use_spacy=False): rules, structure and propagation only")
            self.components.append("NER disabled (use_spacy=False)")
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
        not_names: set[int] = set()
        if self.analyzer is not None and use_ner:
            parsed = self.analyzer.nlp_engine.process_text(text, "en")
            raw = self.analyzer.analyze(text=text, language="en", entities=self.analyzer_entities, score_threshold=0.0,
                                        nlp_artifacts=parsed)
            raw = list(raw) + self._recased_ner(text)
            # The same model that guessed PERSON also tags parts of speech: a lone word it reads as
            # a verb or an adjective ("Navigate to Settings") is not a name.
            not_names = {t.idx for t in parsed.tokens if t.pos_ in NOT_NAME_POS}
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
                # Starts in the neighbouring text: that span detects it on its own. Except a value
                # broken after a hyphen at the end of the line before ("(VEN-" / "KD-6031)"): neither
                # span holds it whole, so the half in this one is kept.
                if not (r.end > off and r.entity_type != "PERSON" and text[r.start:off].endswith("-\n")):
                    continue
                r.start = off
            start, end = r.start - off, r.end - off
            meta = r.recognition_metadata or {}
            name = meta.get(r.RECOGNIZER_NAME_KEY, "unknown")
            decision = "redact"  # the default; the resolver routes by score
            if name == "SpacyRecognizer":
                name, layer, reasons = "ner:spacy", "L2 ner", [f"spaCy NER labelled PERSON (score {r.score:.2f})"]
                if r.start in not_names and len(text[r.start:r.end].split()) == 1:
                    decision = "drop"
                    reasons.append("a single word the model itself tags as a verb, adjective or function word")
            else:
                layer, reasons = meta.get("layer", "L2 ner"), list(meta.get("reasons", []))
            out.append(Finding(span_id=span.id, file=span.file, start=start, end=end, text=span.text[start:end],
                               entity_type=r.entity_type, score=round(r.score, 3), recognizer=name, layer=layer,
                               reasons=reasons, decision=decision))
        out.extend(structure_findings(span, self.allow))
        out.extend(term_findings(span, self.settings.confidential_terms))
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

    def detect_document(self, doc: Document, on_span=None) -> list[Finding]:
        """``on_span(done)`` is called every 20 text elements (GLiNER takes a while per element)."""
        findings: list[Finding] = []
        prev: Span | None = None
        for i, span in enumerate(doc.spans, 1):
            findings.extend(self.detect_span(span, self._prefix(span, prev)))
            prev = span
            if on_span is not None and (i % 20 == 0 or i == len(doc.spans)):
                on_span(i)
        findings.extend(self._broken_ids(doc))
        return findings

    def _broken_ids(self, doc: Document) -> list[Finding]:
        """An ID broken after its prefix ("... Okpara (VEN-"), whose second half OCR read as a block
        of its own a few lines down ("KD-6031)"). Joined, the two halves match one of the
        organisation's ID formats; the second half is masked where it stands."""
        out = []
        for i, span in enumerate(doc.spans):
            m = BROKEN_ID.search(span.text)
            if not m or not ID_ENTITIES:
                continue
            head = m.group(1)
            for nxt in doc.spans[i + 1:i + 1 + BROKEN_ID_REACH]:
                if nxt.page != span.page:
                    break
                hit = next((r for r in run_rules(head + nxt.text, ID_ENTITIES, ID_RULES) if r.start == 0 and r.end > len(head)), None)
                if hit:
                    end = hit.end - len(head)
                    out.append(Finding(span_id=nxt.id, file=nxt.file, start=0, end=end, text=nxt.text[:end],
                                       entity_type=hit.entity_type, score=round(hit.score, 3), recognizer="rule:broken_id",
                                       layer="L1 rules",
                                       reasons=[f"second half of an ID broken after '{head}' at {span.location}"]))
                    break
        return out

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
    def detect_all(self, docs: dict[str, Document], on_progress=None) -> dict[str, list[Finding]]:
        """``on_progress(file, done, total)`` counts text elements over all files."""
        s = self.settings
        total = sum(len(d.spans) for d in docs.values())
        first, base = {}, 0
        for f, d in docs.items():
            report = (lambda n, f=f, base=base: on_progress(f, base + n, total)) if on_progress else None
            first[f] = self.detect_document(d, report)
            base += len(d.spans)
        vocab = corpus_vocabulary(docs)
        resolved = finalise(first, docs, s, vocab)
        if s.propagate_persons:
            # A name completed in one round ("Okpara" -> "Ngozi C. Okpara") is a new person to
            # look for in the next, so propagation repeats until it learns nothing new.
            raw, known = {f: list(fs) for f, fs in first.items()}, set()
            for _ in range(PROPAGATION_ROUNDS):
                idx = build_index(resolved, s.extra_deny_list)
                if set(idx.canon) <= known:
                    break
                known = set(idx.canon)
                first = {f: list(fs) for f, fs in raw.items()}
                for f in propagate(docs, idx, vocab) + name_list_findings(docs, resolved, self.allow):
                    first[f.file].append(f)
                resolved = finalise(first, docs, s, vocab)
            for f in propagate_ids(docs, resolved):
                first[f.file].append(f)
            resolved = finalise(first, docs, s, vocab)
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


__all__ = ["Detector", "ModelMissing", "OUTPUT_ENTITIES"]
