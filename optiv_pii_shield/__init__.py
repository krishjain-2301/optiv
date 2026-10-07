"""PII Shield: offline, fail-closed PII detection and redaction for PDF, DOCX, PPTX, XLSX, images, e-mail, CSV and text.

    from optiv_pii_shield import run, Settings
    result = run(["policy.pdf", "training.docx"], Settings(), out_dir="out")
"""
from .config import Settings
from .pipeline import RunResult, run

__version__ = "0.3.0"
__all__ = ["run", "RunResult", "Settings", "__version__"]
