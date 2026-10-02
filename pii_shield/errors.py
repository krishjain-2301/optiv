"""Exceptions shared across the pipeline."""


class ModelMissing(RuntimeError):
    """A model the settings ask for (spaCy, GLiNER, the English OCR recogniser) is not installed.
    Never silently downgraded: a pipeline that quietly drops to rules only, or to a weaker model,
    misses PII without saying so."""
