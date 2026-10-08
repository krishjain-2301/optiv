"""Extraction router: sniff the real type, dispatch to the right extractor."""
from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..models import Document
from ..modelstore import sha256_file
from ..render import render_markdown
from .sniff import sniff


def extract(path: str | Path, settings: Settings | None = None, on_page=None) -> Document:
    """``on_page(done, total)`` reports progress through a PDF's pages (other formats read at once)."""
    settings = settings or Settings()
    doc = _extract(Path(path), settings, on_page)
    doc.sha256 = sha256_file(path)
    doc.markdown = render_markdown(doc)
    return doc


def _extract(path: Path, settings: Settings, on_page=None) -> Document:
    path = Path(path)
    ftype = sniff(path)
    if ftype == "pdf":
        from .pdf import extract_pdf

        return extract_pdf(path, settings, on_page)
    if ftype == "docx":
        from .docx import extract_docx

        return extract_docx(path, settings)
    if ftype == "pptx":
        from .pptx import extract_pptx

        return extract_pptx(path, settings)
    if ftype == "xlsx":
        from .xlsx import extract_xlsx

        return extract_xlsx(path, settings)
    if ftype == "image":
        from .image import extract_image

        return extract_image(path, settings)
    if ftype in ("text", "csv", "eml"):
        from . import plain

        return {"text": plain.extract_text, "csv": plain.extract_csv, "eml": plain.extract_eml}[ftype](path, settings)
    if ftype == "transcript":
        from .transcript import extract_transcript

        return extract_transcript(path, settings)
    if ftype == "legacy":
        raise ValueError(f"{path.name}: legacy Office / Outlook binary format (.doc, .xls, .ppt, .msg) is not read; "
                         "save it as DOCX, XLSX, PPTX or EML and scan that. Nothing is passed downstream.")
    raise ValueError(f"{path.name}: unsupported file type ({ftype}); refusing to pass it downstream")


__all__ = ["extract", "sniff"]
