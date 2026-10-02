"""Normalised document model shared by extraction, detection, redaction and reporting.

Every piece of text pulled out of a file becomes a ``Span`` that remembers exactly where it
came from (file, page/slide, element, table cell, bounding box, OCR confidence). Detection
produces ``Finding`` objects that point back into a span by character offsets, so every
flagged value can be traced to its source and every redaction can be drawn on the original.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

BBox = tuple[float, float, float, float]  # x0, y0, x1, y1 in the unit of the page (pt, px or EMU)


@dataclass
class Word:
    """A word inside a span with its character range and (optional) position on the page."""

    text: str
    start: int
    end: int
    bbox: Optional[BBox] = None
    conf: Optional[float] = None


@dataclass
class Span:
    id: str
    file: str
    text: str
    kind: str  # heading | paragraph | table_cell | text_box | header | footer | comment | notes | metadata | shape | ocr_block | image_ocr
    page: Optional[int] = None  # 1-based page or slide number
    location: str = ""  # human-readable position, e.g. "table 2, row 3, col 1"
    source: str = "native"  # native | ocr | image_ocr
    bbox: Optional[BBox] = None
    ocr_conf: Optional[float] = None
    table: Optional[tuple[int, int, int]] = None  # (table index, row, col), 0-based
    header: Optional[str] = None  # column header or field label that describes this span
    image_ref: Optional[str] = None  # id of the embedded image this span was read from
    anchor: Optional[str] = None  # stable key of the source element, used to write redactions back
    words: list[Word] = field(default_factory=list)
    level: Optional[int] = None  # heading level

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("words")
        return d


@dataclass
class ImageRef:
    """An embedded image (or an image region inside a scanned page)."""

    id: str
    file: str
    page: Optional[int]
    location: str
    width: int = 0
    height: int = 0
    ocr_status: str = "pending"  # read | low_confidence | unreadable | skipped
    ocr_conf: Optional[float] = None
    part_name: Optional[str] = None  # OOXML part name, used to rewrite the image when masking
    bbox: Optional[BBox] = None  # region on the page (scanned PDF regions)


@dataclass
class Document:
    file: str
    path: str
    file_type: str  # pdf | docx | pptx | image | text
    pages: int = 0
    spans: list[Span] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    markdown: str = ""
    ocr_pages: list[int] = field(default_factory=list)
    structure: dict = field(default_factory=dict)  # counts of headings, tables, rows, cells, ...
    warnings: list[str] = field(default_factory=list)
    gate: dict = field(default_factory=dict)  # leak-gate outcome: masked written/withheld, values scrubbed
    page_sizes: dict[int, tuple[float, float]] = field(default_factory=dict)
    page_images: dict[int, bytes] = field(default_factory=dict)  # rendered PNGs for scanned pages

    def span(self, span_id: str) -> Span:
        return self._index()[span_id]

    def _index(self) -> dict[str, Span]:
        if getattr(self, "_span_index", None) is None or len(self._span_index) != len(self.spans):
            self._span_index = {s.id: s for s in self.spans}
        return self._span_index


@dataclass
class Finding:
    span_id: str
    file: str
    start: int
    end: int
    text: str
    entity_type: str
    score: float
    recognizer: str
    layer: str  # L1 rules | L2 ner | L3 structure | L4 propagation | L0 fail-closed
    reasons: list[str] = field(default_factory=list)
    token: Optional[str] = None
    decision: str = "redact"  # redact | review | drop
    page: Optional[int] = None
    location: str = ""
    kind: str = ""
    source: str = ""
    context_type: str = ""  # table | labelled | narrative | image | metadata

    def to_dict(self) -> dict:
        return asdict(self)
