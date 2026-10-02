"""Extraction router: sniff the real type, dispatch to the right extractor."""
from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..models import Document, Span
from ..render import render_markdown
from .sniff import sniff


def extract(path: str | Path, settings: Settings | None = None, on_page=None) -> Document:
    """``on_page(done, total)`` reports progress through a PDF's pages (other formats read at once)."""
    settings = settings or Settings()
    doc = _extract(Path(path), settings, on_page)
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
    if ftype == "text":
        text = path.read_text(encoding="utf-8", errors="replace")
        doc = Document(file=path.name, path=str(path), file_type="text", pages=1)
        for i, para in enumerate(p for p in text.split("\n\n") if p.strip()):
            doc.spans.append(Span(id=f"{path.stem[:12]}-{i + 1:05d}", file=path.name, text=para, kind="paragraph",
                                  page=1, location=f"paragraph {i + 1}", anchor=f"p[{i}]"))
        doc.structure = {"paragraphs": len(doc.spans)}
        return doc
    raise ValueError(f"{path.name}: unsupported file type ({ftype}); refusing to pass it downstream")


__all__ = ["extract", "sniff"]
