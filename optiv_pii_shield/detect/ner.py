"""Layer L2: statistical / transformer NER, all running locally.

* spaCy (via Presidio's ``SpacyRecognizer``) is always on when a model is installed.
* GLiNER-PII is optional (``Settings.use_gliner``); it needs ``pip install gliner`` which pulls torch.
"""
from __future__ import annotations

import logging
from typing import Optional

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


class GlinerRecognizer(EntityRecognizer):
    """Zero-shot PII NER (knowledgator/gliner-pii-*). Scores are the model's own probabilities."""

    def __init__(self, model_name: str, threshold: float = 0.45):
        self.model_name = model_name
        self.threshold = threshold
        self.model = None
        super().__init__(supported_entities=sorted(set(GLINER_LABELS.values())), name="GLiNER",
                         supported_language="en")

    def load(self) -> None:
        from gliner import GLiNER  # optional dependency

        self.model = GLiNER.from_pretrained(self.model_name)

    def analyze(self, text, entities, nlp_artifacts=None) -> list[RecognizerResult]:
        if not text.strip() or self.model is None:
            return []
        out = []
        for ent in self.model.predict_entities(text, list(GLINER_LABELS), threshold=self.threshold):
            etype = GLINER_LABELS[ent["label"]]
            if entities and etype not in entities:
                continue
            reason = f"GLiNER label '{ent['label']}' p={ent['score']:.2f}"
            out.append(RecognizerResult(
                etype, ent["start"], ent["end"], float(ent["score"]),
                analysis_explanation=AnalysisExplanation("GLiNER", float(ent["score"]), textual_explanation=reason),
                recognition_metadata={RecognizerResult.RECOGNIZER_NAME_KEY: "ner:gliner",
                                      RecognizerResult.IS_SCORE_ENHANCED_BY_CONTEXT_KEY: True,
                                      "reasons": [reason], "layer": "L2 ner"},
            ))
        return out


def load_gliner(model_name: str, threshold: float) -> GlinerRecognizer:
    """GLiNER was asked for, so it must load: a missing package or uncached weights is an error."""
    try:
        return GlinerRecognizer(model_name, threshold)  # Presidio calls load() in __init__
    except Exception as exc:  # missing package, no weights cached and offline, ...
        raise ModelMissing(f"GLiNER model '{model_name}' could not be loaded ({exc}). "
                           "Install it (`pip install gliner`, then cache the weights once) or turn GLiNER off.") from exc
