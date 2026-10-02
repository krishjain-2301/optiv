"""Exceptions shared across the pipeline."""


class ModelMissing(RuntimeError):
    """A model the settings ask for (spaCy, GLiNER, the English OCR recogniser) is not installed.
    Never silently downgraded: a pipeline that quietly drops to rules only, or to a weaker model,
    misses PII without saying so."""


class RunCancelled(RuntimeError):
    """Raised by a caller's progress callback to stop a run. It ends the whole run: it is never
    recorded as one file's extraction error."""
