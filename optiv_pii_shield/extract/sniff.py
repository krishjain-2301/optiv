"""Identify the true file type from magic bytes, never from the extension.

Text has no magic bytes, so among files that decode as text the extension picks the reader
(.eml, .csv / .tsv, anything else is plain text); a wrong extension there only changes how the
same text is split up, never whether it is read.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*", b"BM")
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # .doc / .xls / .ppt / .msg
MAX_UNZIPPED = 2 * 1024 ** 3  # an Office file that unpacks to more than this is refused (zip bomb)
MAX_MEMBERS = 20_000


def sniff(path: str | Path) -> str:
    """Return one of: pdf, docx, pptx, xlsx, image, text, csv, eml, legacy, unknown."""
    path = Path(path)
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
        except zipfile.BadZipFile:
            return "unknown"
        if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_UNZIPPED:
            raise ValueError(f"{path.name}: the package unpacks to more than {MAX_UNZIPPED // 1024 ** 3} GB "
                             f"or {MAX_MEMBERS} parts; refused (resource limit)")
        names = {i.filename for i in infos}
        if "word/document.xml" in names:
            return "docx"
        if "ppt/presentation.xml" in names:
            return "pptx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "unknown"
    if head.startswith(OLE_MAGIC):
        return "legacy"
    if head.startswith(IMAGE_MAGIC) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    try:
        with open(path, "rb") as fh:
            fh.read(4096).decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.end < 4090:  # not just a multi-byte character cut at the end of the sample
            return "unknown"
    suffix = path.suffix.lower()
    return "eml" if suffix == ".eml" else "csv" if suffix in (".csv", ".tsv") else "text"
