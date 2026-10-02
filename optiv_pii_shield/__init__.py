"""PII Shield: offline, fail-closed PII detection and redaction for PDF, DOCX, PPTX and images.

    from optiv_pii_shield import run, Settings
    result = run(["policy.pdf", "training.docx"], Settings(), out_dir="out")
"""
from .config import Settings
from .pipeline import RunResult, run

__version__ = "0.1.0"
__all__ = ["run", "RunResult", "Settings", "__version__"]
