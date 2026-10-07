"""Layer L2: statistical / transformer NER, all running locally.

* spaCy (via Presidio's ``SpacyRecognizer``) is always on when a model is installed.
* GLiNER-PII is optional (``Settings.use_gliner``); it needs ``pip install gliner`` which pulls torch.
"""
from __future__ import annotations

import logging
import re

import spacy
from presidio_analyzer import AnalysisExplanation, EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider

from ..errors import ModelMissing

log = logging.getLogger(__name__)


def build_nlp_engine(model: str):
    """Load exactly the requested spaCy model."""
    if not spacy.util.is_package(model):
        raise ModelMissing(
            f"spaCy model '{model}' is not installed. Install it with `python -m spacy download {model}`, "
            f"or choose an installed model explicitly (--spacy-model). Refusing to run without NER."
        )
    conf = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": model}],
        "ner_model_configuration": {
            "labels_to_ignore": ["CARDINAL", "ORDINAL", "QUANTITY", "PERCENT", "MONEY", "TIME", "LAW",
                                 "WORK_OF_ART", "PRODUCT", "EVENT", "LANGUAGE", "FAC"],
        },
    }
    return NlpEngineProvider(nlp_configuration=conf).create_engine(), model


GLINER_LABELS = {
    "person": "PERSON",
    "email": "EMAIL_ADDRESS",
    "phone number": "PHONE_NUMBER",
    "street address": "ADDRESS",
    "date of birth": "DATE_OF_BIRTH",
    "passport number": "PASSPORT",
    "social security number": "US_SSN",
    "tax identification number": "TAX_ID",
    "national id number": "NATIONAL_ID",
    "credit card number": "CREDIT_CARD",
    "bank account number": "IBAN_CODE",
}


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def gliner_extent(etype: str, text: str, start: int, end: int) -> tuple[int, int] | None:
    """Where a GLiNER hit really is, or None when it cannot be a value of its category.

    The model also tags the label beside a value as the value ("E-mail", "Email" in a table header)
    and cuts e-mails short ("tprm-office@cadence-demo" without ".example"). A label word redacted as
    PII becomes a vault value, and the leak gate then refuses every file that still shows the word.
    So: an e-mail hit is moved onto the whole address it overlaps, identifiers and dates need digits,
    an address needs a number or a comma. Names are checked for shape by the resolver.
    """
    value = text[start:end]
    if etype == "PERSON":
        return start, end
    if etype == "EMAIL_ADDRESS":
        for m in EMAIL.finditer(text):
            if m.start() < end and m.end() > start:
                return m.start(), m.end()
        return None
    if etype == "ADDRESS":
        return (start, end) if re.search(r"[\d,]", value) else None
    return (start, end) if sum(c.isdigit() for c in value) >= 4 else None


class GlinerRecognizer(EntityRecognizer):
    """Zero-shot PII NER (knowledgator/gliner-pii-*). Scores are the model's own probabilities."""

    def __init__(self, model_name: str, threshold: float = 0.45, revision: str | None = None):
        self.model_name = model_name
        self.revision = revision
        self.threshold = threshold
        self.model = None
        super().__init__(supported_entities=sorted(set(GLINER_LABELS.values())), name="GLiNER",
                         supported_language="en")

    def load(self) -> None:
        from gliner import GLiNER  # optional dependency

        # Cached weights only: a run never contacts the model hub (scripts/fetch_models.py --gliner).
        self.model = GLiNER.from_pretrained(self.model_name, revision=self.revision, local_files_only=True)

    def analyze(self, text, entities, nlp_artifacts=None) -> list[RecognizerResult]:
        if not text.strip() or self.model is None:
            return []
        out = []
        for ent in self.model.predict_entities(text, list(GLINER_LABELS), threshold=self.threshold):
            etype = GLINER_LABELS[ent["label"]]
            if entities and etype not in entities:
                continue
            extent = gliner_extent(etype, text, ent["start"], ent["end"])
            if extent is None:
                continue
            reason = f"GLiNER label '{ent['label']}' p={ent['score']:.2f}"
            out.append(RecognizerResult(
                etype, extent[0], extent[1], float(ent["score"]),
                analysis_explanation=AnalysisExplanation("GLiNER", float(ent["score"]), textual_explanation=reason),
                recognition_metadata={RecognizerResult.RECOGNIZER_NAME_KEY: "ner:gliner",
                                      RecognizerResult.IS_SCORE_ENHANCED_BY_CONTEXT_KEY: True,
                                      "reasons": [reason], "layer": "L2 ner"},
            ))
        return out


def load_gliner(model_name: str, threshold: float, revision: str | None = None) -> GlinerRecognizer:
    """GLiNER was asked for, so it must load: a missing package or uncached weights is an error."""
    try:
        return GlinerRecognizer(model_name, threshold, revision)  # Presidio calls load() in __init__
    except Exception as exc:  # missing package, no weights cached and offline, ...
        raise ModelMissing(f"GLiNER model '{model_name}' could not be loaded ({exc}). "
                           "Install it (`pip install gliner`, then cache the weights once) or turn GLiNER off.") from exc
