"""Identify the true file type from magic bytes, never from the extension."""
from __future__ import annotations

import zipfile
from pathlib import Path

IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*", b"BM")


def sniff(path: str | Path) -> str:
    """Return one of: pdf, docx, pptx, xlsx, image, text, unknown."""
    path = Path(path)
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            return "unknown"
        if "word/document.xml" in names:
            return "docx"
        if "ppt/presentation.xml" in names:
            return "pptx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "unknown"
    if head.startswith(IMAGE_MAGIC) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    try:
        path.read_bytes()[:4096].decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "unknown"
