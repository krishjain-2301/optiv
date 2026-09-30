"""Walkers over DOCX and PPTX that yield every piece of text as a ``TextUnit``.

Extraction and redaction share these walkers: extraction turns units into spans, redaction
re-walks the same file, finds units by ``anchor`` and rewrites the underlying ``<w:t>`` /
``<a:t>`` nodes in place. Editing the text nodes (rather than paragraph text) keeps run
formatting, hyperlinks and content controls intact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_HEAD_STYLE = re.compile(r"^(heading|title)\s*(\d*)", re.I)


@dataclass
class TextUnit:
    anchor: str
    kind: str
    location: str
    page: Optional[int] = None
    table: Optional[tuple[int, int, int]] = None
    header: Optional[str] = None
    bbox: Optional[tuple] = None
    level: Optional[int] = None
    part: str = "body"  # body | header | footer | footnote | endnote | comment
    nodes: list[tuple[object, int, int]] = field(default_factory=list)  # (element, start, end)
    text: str = ""


def build_unit(unit: TextUnit, paragraphs: list, t_tag: str, skip_tag: Optional[str] = None) -> TextUnit:
    """Concatenate the text nodes of ``paragraphs`` (joined by newlines) and remember their offsets."""
    parts: list[str] = []
    pos = 0
    for i, p in enumerate(paragraphs):
        if i:
            parts.append("\n")
            pos += 1
        for t in p.iter(t_tag):
            if skip_tag is not None and _has_ancestor(t, skip_tag, stop=p):
                continue
            txt = t.text or ""
            unit.nodes.append((t, pos, pos + len(txt)))
            parts.append(txt)
            pos += len(txt)
    unit.text = "".join(parts)
    return unit


def _has_ancestor(el, tag: str, stop) -> bool:
    cur = el.getparent()
    while cur is not None and cur is not stop:
        if cur.tag == tag:
            return True
        cur = cur.getparent()
    return False


def apply_replacements(unit: TextUnit, reps: list[tuple[int, int, str]]) -> None:
    """Replace [start, end) ranges of the unit's text. The replacement lands in the node where the
    range starts; the rest of the range is removed from the following nodes."""
    reps = sorted(reps)
    full = unit.text
    for el, s, e in unit.nodes:
        out, pos = [], s
        for rs, re_, new in reps:
            if re_ <= s or rs >= e:
                if s <= rs < e and rs == re_:  # zero-width insert
                    out.append(full[pos:rs] + new)
                    pos = rs
                continue
            out.append(full[pos:max(rs, s)])
            if s <= rs < e:
                out.append(new)
            pos = min(re_, e)
        out.append(full[pos:e])
        new_text = "".join(out)
        if new_text != (el.text or ""):
            el.text = new_text
            if el.tag == f"{{{W}}}t":
                el.set(XML_SPACE, "preserve")


# ---------------------------------------------------------------------------------- DOCX
DOCX_TEXT_PARTS = re.compile(r"^/word/(header\d*|footer\d*|footnotes|endnotes|comments)\.xml$")


def docx_units(document) -> Iterator[TextUnit]:
    """Body paragraphs and tables in reading order, then text boxes, headers/footers, notes, comments."""
    body = document.element.body
    counters = {"p": 0, "tbl": 0}
    yield from _docx_block(body, "body", counters)
    yield from _docx_textboxes(body, "body", "")
    for part in document.part.package.iter_parts():
        m = DOCX_TEXT_PARTS.match(str(part.partname))
        if not m:
            continue
        root = xml_root(part)
        if root is None:
            continue
        kind = re.sub(r"\d+$", "", m.group(1)).rstrip("s") if m.group(1) != "comments" else "comment"
        c = {"p": 0, "tbl": 0}
        for unit in _docx_block(root, str(part.partname), c):
            # Headers/footers often lay text out in a small table: keep the cell (column header
            # context) but record where it lives, so it is not counted as body structure.
            unit.part = kind
            if unit.kind != "table_cell":
                unit.kind = kind
            unit.location = f"{m.group(1)}: {unit.location}"
            yield unit
        for unit in _docx_textboxes(root, str(part.partname), f"{m.group(1)}: "):
            unit.part = kind
            yield unit


def _docx_textboxes(root, prefix: str, loc_prefix: str) -> Iterator[TextUnit]:
    """Text boxes (body, headers and footers). Word stores each one twice (DrawingML + VML
    fallback); both copies are yielded so both get redacted, and the fallback is labelled."""
    for i, txbx in enumerate(root.iter(f"{{{W}}}txbxContent")):
        fallback = _has_ancestor(txbx, "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback", stop=root)
        paras = [p for p in txbx.iter(f"{{{W}}}p")]
        loc = f"{loc_prefix}text box {i + 1}" + (" (VML fallback copy)" if fallback else "")
        unit = build_unit(TextUnit(anchor=f"{prefix}/txbx[{i}]", kind="text_box", location=loc), paras, f"{{{W}}}t")
        if unit.text.strip():
            yield unit


def _docx_block(container, prefix: str, counters: dict, depth: int = 0) -> Iterator[TextUnit]:
    for child in container:
        tag = etree.QName(child).localname
        if tag == "p":
            counters["p"] += 1
            unit = TextUnit(anchor=f"{prefix}/p[{counters['p']}]", kind="paragraph",
                            location=f"paragraph {counters['p']}")
            style = child.find(f"{{{W}}}pPr/{{{W}}}pStyle")
            if style is not None:
                m = _HEAD_STYLE.match(style.get(f"{{{W}}}val", ""))
                if m:
                    unit.kind, unit.level = "heading", int(m.group(2) or 1)
            build_unit(unit, [child], f"{{{W}}}t", skip_tag=f"{{{W}}}txbxContent")
            if unit.text.strip():
                yield unit
        elif tag == "tbl":
            counters["tbl"] += 1
            yield from _docx_table(child, prefix, counters)
        elif tag in ("sdt", "customXml", "ins", "smartTag"):
            content = child.find(f"{{{W}}}sdtContent") if tag == "sdt" else child
            if content is not None:
                yield from _docx_block(content, prefix, counters, depth)
        elif tag == "body" or tag in ("hdr", "ftr", "footnotes", "endnotes", "comments", "footnote", "endnote", "comment"):
            yield from _docx_block(child, prefix, counters, depth)


def _docx_table(tbl, prefix: str, counters: dict) -> Iterator[TextUnit]:
    tindex = counters["tbl"]
    rows = tbl.findall(f"{{{W}}}tr")
    header: list[str] = []
    for ri, tr in enumerate(rows):
        ci = 0
        for tc in tr.findall(f"{{{W}}}tc"):
            span_el = tc.find(f"{{{W}}}tcPr/{{{W}}}gridSpan")
            width = int(span_el.get(f"{{{W}}}val")) if span_el is not None else 1
            vmerge = tc.find(f"{{{W}}}tcPr/{{{W}}}vMerge")
            continued = vmerge is not None and vmerge.get(f"{{{W}}}val") in (None, "continue")
            paras = [p for p in tc if etree.QName(p).localname == "p"]
            unit = TextUnit(anchor=f"{prefix}/tbl[{tindex}]/r[{ri}]/c[{ci}]", kind="table_cell",
                            location=f"table {tindex}, row {ri + 1}, col {ci + 1}", table=(tindex - 1, ri, ci))
            build_unit(unit, paras, f"{{{W}}}t", skip_tag=f"{{{W}}}txbxContent")
            if ri == 0:
                header.extend([unit.text.strip()] * width)
            elif ci < len(header):
                unit.header = header[ci] or None
            if unit.text.strip() and not continued:
                yield unit
            for nested in tc.findall(f"{{{W}}}tbl"):
                counters["tbl"] += 1
                yield from _docx_table(nested, prefix, counters)
            ci += width


def xml_root(part):
    """Parsed XML root of a package part (python-docx / python-pptx expose some parts only as blobs)."""
    el = getattr(part, "_element", None)
    if el is not None:
        return el
    cached = getattr(part, "_pii_root", None)
    if cached is not None:
        return cached  # parse once, so every walker edits the same tree
    try:
        root = etree.fromstring(part.blob)
    except Exception:
        return None
    part._pii_root = root  # kept so redaction can serialise it back
    return root


def xml_text_units(part, prefix: str, kind: str = "metadata", only: Optional[set[str]] = None) -> Iterator[TextUnit]:
    """Every element carrying text in an XML part (customXml items, custom properties), or only the
    elements named in ``only``. Pure numbers and booleans are skipped: they cannot identify anyone."""
    root = xml_root(part)
    if root is None:
        return
    for i, el in enumerate(root.iter()):
        if not isinstance(el.tag, str) or not (el.text or "").strip():
            continue
        name = etree.QName(el).localname
        if only is not None and name not in only:
            continue
        if re.fullmatch(r"[\d.:TZ+-]+|true|false", el.text.strip(), re.I):
            continue
        unit = TextUnit(anchor=f"{prefix}#{i}", kind=kind, location=f"{prefix}: <{name}>", header=name,
                        nodes=[(el, 0, len(el.text))], text=el.text)
        yield unit


PACKAGE_XML_PARTS = re.compile(r"^/(docProps/(core|app|custom)\.xml|customXml/item\d+\.xml)$")
# Standard properties that are free text (can hold a name); counters, dates and app names are not.
PROPERTY_FIELDS = {
    "/docProps/core.xml": {"creator", "lastModifiedBy", "title", "subject", "keywords", "description", "category",
                           "contentStatus", "identifier"},
    "/docProps/app.xml": {"Company", "Manager", "HyperlinkBase"},
}


def package_xml_units(package) -> Iterator[TextUnit]:
    """Document properties (author, last modified by, manager), custom properties and customXml stores."""
    for part in package.iter_parts():
        name = str(part.partname)
        if PACKAGE_XML_PARTS.match(name):
            yield from xml_text_units(part, name.lstrip("/"), only=PROPERTY_FIELDS.get(name))


class AttrNode:
    """Lets an XML attribute be edited through the same ``.text`` interface as a text node."""

    tag = "attr"

    def __init__(self, el, attr: str):
        self.el, self.attr = el, attr

    @property
    def text(self) -> str:
        return self.el.get(self.attr, "")

    @text.setter
    def text(self, value: str) -> None:
        self.el.set(self.attr, value)


PERSON_ATTRS = {f"{{{W}}}author", f"{{{W}}}initials", "name", "initials", "author"}
PERSON_ATTR_ELEMENTS = {"comment", "ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange", "cmAuthor",
                        "author", "person"}


def author_attribute_units(package) -> Iterator[TextUnit]:
    """Names hidden in attributes: comment and tracked-change authors, PowerPoint comment authors."""
    found: dict[str, list] = {}
    for part in package.iter_parts():
        if not str(part.partname).endswith(".xml") or str(part.partname).startswith("/customXml"):
            continue
        root = xml_root(part)
        if root is None:
            continue
        for el in root.iter():
            if not isinstance(el.tag, str) or etree.QName(el).localname not in PERSON_ATTR_ELEMENTS:
                continue
            for attr, val in el.attrib.items():
                if attr in PERSON_ATTRS and val.strip():
                    found.setdefault(val, []).append(AttrNode(el, attr))
    for i, (val, nodes) in enumerate(sorted(found.items())):
        yield TextUnit(anchor=f"attr#{i}:{val}", kind="metadata", location="comment / tracked-change author",
                       header="author", nodes=[(n, 0, len(val)) for n in nodes], text=val)


def image_parts(package) -> Iterator[object]:
    seen = set()
    for part in package.iter_parts():
        if str(getattr(part, "content_type", "")).startswith("image/") and part.partname not in seen:
            seen.add(part.partname)
            yield part


def flush_blob_parts(package) -> None:
    for part in package.iter_parts():
        root = getattr(part, "_pii_root", None)
        if root is not None:
            part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


# ---------------------------------------------------------------------------------- PPTX
def pptx_units(prs) -> Iterator[TextUnit]:
    for sno, slide in enumerate(prs.slides, start=1):
        yield from _pptx_shapes(slide.shapes, sno, f"slide[{sno}]")
        if slide.has_notes_slide:
            paras = list(slide.notes_slide.notes_text_frame._txBody.iter(f"{{{A}}}p")) if slide.notes_slide.notes_text_frame else []
            unit = build_unit(TextUnit(anchor=f"slide[{sno}]/notes", kind="notes", page=sno,
                                       location=f"slide {sno}, speaker notes"), paras, f"{{{A}}}t")
            if unit.text.strip():
                yield unit


def _pptx_shapes(shapes, sno: int, prefix: str) -> Iterator[TextUnit]:
    for shape in shapes:
        sid = f"{prefix}/sp[{shape.shape_id}]"
        name = shape.name
        bbox = _emu_box(shape)
        if _stype(shape) == 6:  # group
            yield from _pptx_shapes(shape.shapes, sno, sid)
            continue
        if getattr(shape, "has_table", False) and shape.has_table:
            tbl = shape.table
            header = [c.text.strip() for c in tbl.rows[0].cells] if len(tbl.rows) else []
            for ri, row in enumerate(tbl.rows):
                for ci, cell in enumerate(row.cells):
                    if cell.is_spanned:
                        continue
                    unit = TextUnit(anchor=f"{sid}/r[{ri}]/c[{ci}]", kind="table_cell", page=sno,
                                    location=f"slide {sno}, table '{name}', row {ri + 1}, col {ci + 1}",
                                    table=(shape.shape_id, ri, ci), bbox=bbox,
                                    header=(header[ci] or None) if ri > 0 and ci < len(header) else None)
                    build_unit(unit, list(cell._tc.iter(f"{{{A}}}p")), f"{{{A}}}t")
                    if unit.text.strip():
                        yield unit
            continue
        if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
            kind = "shape"
            if shape.is_placeholder:
                ph = str(shape.placeholder_format.type)
                kind = "heading" if "TITLE" in ph else "paragraph"
            unit = TextUnit(anchor=sid, kind=kind, page=sno, location=f"slide {sno}, shape '{name}'", bbox=bbox,
                            level=1 if kind == "heading" else None)
            build_unit(unit, list(shape.text_frame._txBody.iter(f"{{{A}}}p")), f"{{{A}}}t")
            if unit.text.strip():
                yield unit


def _stype(shape):
    """python-pptx raises on some unrecognised shapes; treat those as plain shapes."""
    try:
        return shape.shape_type
    except Exception:
        return "GroupShape" == shape.__class__.__name__ and 6 or None


def _emu_box(shape) -> Optional[tuple]:
    try:
        return (shape.left, shape.top, shape.left + shape.width, shape.top + shape.height)
    except Exception:
        return None


def iter_pictures(shapes, prefix: str = "") -> Iterator[tuple[object, str]]:
    for shape in shapes:
        if _stype(shape) == 6:
            yield from iter_pictures(shape.shapes, f"{prefix}/sp[{shape.shape_id}]")
        elif _stype(shape) == 13 or shape.__class__.__name__ == "Picture":
            yield shape, f"{prefix}/sp[{shape.shape_id}]"
