"""Masked copies of the original files, preserving page count, layout and formatting.

* PDF: true redaction (text and image pixels under the box are removed), token printed in the box.
* DOCX / PPTX: text nodes rewritten in place with tokens; PII in embedded screenshots painted over;
  images that could not be read are blanked (fail closed).
* All formats: author, last-modified-by and other personal document properties are cleared.
"""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
from lxml import etree
from PIL import Image, ImageDraw, ImageFont

from ..config import Settings
from ..extract.ooxml import (W, apply_replacements, author_attribute_units, docx_units, flush_blob_parts,
                             package_xml_units, pptx_units)
from ..models import Document, Finding, ImageRef, Span
from .text import LIVE, merged_ranges

WITHHOLD = ("unreadable", "low_confidence")
PERSONAL_CORE_PROPS = ("author", "last_modified_by")


def finding_boxes(span: Span, f: Finding) -> list[tuple[float, float, float, float]]:
    """Boxes covering the finding: union of its words' boxes, one box per visual line."""
    words = [w for w in span.words if w.bbox and w.start < f.end and w.end > f.start]
    if not words:
        return [span.bbox] if span.bbox else []
    lines: list[list] = []
    for w in sorted(words, key=lambda w: (w.bbox[1], w.bbox[0])):
        if lines and abs(lines[-1][-1].bbox[1] - w.bbox[1]) < (w.bbox[3] - w.bbox[1]) * 0.5:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [(min(w.bbox[0] for w in ln), min(w.bbox[1] for w in ln), max(w.bbox[2] for w in ln), max(w.bbox[3] for w in ln))
            for ln in lines]


def pad(b: tuple, frac: float = 0.3) -> tuple:
    """Grow a box sideways by a share of its height: OCR positions are approximate, so masks err on
    the side of covering a neighbouring character rather than exposing one."""
    h = b[3] - b[1]
    return (b[0] - h * frac, b[1] - h * 0.08, b[2] + h * frac, b[3] + h * 0.08)


def _live(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.decision in LIVE]


# -------------------------------------------------------------------------------------- PDF
def mask_pdf(doc: Document, findings: list[Finding], out: Path, settings: Settings) -> Path:
    pdf = fitz.open(doc.path)
    for f in _live(findings):
        span = doc.span(f.span_id)
        if span.page is None:
            continue  # metadata: cleared below
        page = pdf[span.page - 1]
        boxes = finding_boxes(span, f)
        if span.source == "native" and not any(w.bbox for w in span.words):
            boxes = [tuple(r) for r in page.search_for(f.text, clip=fitz.Rect(span.bbox) if span.bbox else None)] or boxes
        for b in boxes:
            r = fitz.Rect(pad(b))
            page.add_redact_annot(r, text=f.token or "", fill=(0, 0, 0), text_color=(1, 1, 1),
                                  fontsize=max(3.5, min(9, r.height * 0.6)), align=fitz.TEXT_ALIGN_LEFT)
    for img in doc.images:
        if img.bbox and img.page and (img.ocr_status in WITHHOLD or img.ocr_status == "no_text"):
            page = pdf[img.page - 1]
            page.add_redact_annot(fitz.Rect(img.bbox), text="[IMAGE WITHHELD]", fill=(0.2, 0.2, 0.2),
                                  text_color=(1, 1, 1), fontsize=9)
    for page in pdf:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
    pdf.set_metadata({k: "" for k in ("author", "creator", "producer", "title", "subject", "keywords")})
    pdf.del_xml_metadata()
    pdf.save(out, garbage=4, deflate=True)
    return out


# ------------------------------------------------------------------------------- DOCX / PPTX
def _ranges_by_anchor(doc: Document, findings: list[Finding]) -> dict[str, list[tuple[int, int, str]]]:
    by_span: dict[str, list[Finding]] = {}
    for f in _live(findings):
        by_span.setdefault(f.span_id, []).append(f)
    out = {}
    for sid, fs in by_span.items():
        span = doc.span(sid)
        if span.anchor:
            out[span.anchor] = merged_ranges(fs)
    return out


def _rewrite_units(units, ranges: dict[str, list]) -> int:
    n = 0
    for u in units:
        if u.anchor in ranges:
            apply_replacements(u, ranges[u.anchor])
            n += 1
    return n


def _paint_image(blob: bytes, boxes: list, withhold: bool) -> bytes | None:
    try:
        img = Image.open(io.BytesIO(blob))
        fmt = img.format or "PNG"
        img = img.convert("RGB")
    except Exception:
        return None
    d = ImageDraw.Draw(img)
    if withhold:
        d.rectangle([0, 0, img.width, img.height], fill=(60, 60, 60))
        try:
            fnt = ImageFont.truetype("arial.ttf", max(12, img.height // 12))
        except OSError:
            fnt = ImageFont.load_default()
        d.text((10, 10), "IMAGE WITHHELD", fill="white", font=fnt)
    for b in boxes:
        d.rectangle(list(pad(b)), fill="black")
    buf = io.BytesIO()
    img.save(buf, format="JPEG" if fmt.upper() in ("JPEG", "JPG") else "PNG", quality=92)
    return buf.getvalue()


def _mask_images(doc: Document, findings: list[Finding], package) -> dict[str, str]:
    """Paint PII boxes onto embedded images. Returns part names that must be removed (unpaintable)."""
    boxes: dict[str, list] = {}
    for f in _live(findings):
        span = doc.span(f.span_id)
        if span.image_ref:
            boxes.setdefault(span.image_ref, []).extend(finding_boxes(span, f))
    refs: dict[str, ImageRef] = {i.id: i for i in doc.images}
    by_part: dict[str, tuple[list, bool]] = {}
    for rid, ref in refs.items():
        if not ref.part_name:
            continue
        withhold = ref.ocr_status in WITHHOLD
        b, w = by_part.get(ref.part_name, ([], False))
        by_part[ref.part_name] = (b + boxes.get(rid, []), w or withhold)
    remove: dict[str, str] = {}
    parts = {str(p.partname): p for p in package.iter_parts()}
    for name, (bx, withhold) in by_part.items():
        if not bx and not withhold:
            continue
        part = parts.get(name)
        if part is None:
            continue
        new = _paint_image(part.blob, bx, withhold)
        if new is None:
            remove[name] = "unreadable"
        else:
            part._blob = new
    for ref in refs.values():
        if ref.part_name and ref.ocr_status == "unreadable":
            remove[ref.part_name] = "unreadable"
    return remove


def _remove_pictures(package, part_names: dict[str, str]) -> int:
    """Replace references to images we could not read or paint (EMF/WMF) with a text marker."""
    if not part_names:
        return 0
    n = 0
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    for part in package.iter_parts():
        el = getattr(part, "_element", None)
        rels = getattr(part, "rels", None)
        if el is None or not rels:
            continue
        rids = {rid for rid, rel in rels.items() if not rel.is_external and str(rel.target_part.partname) in part_names}
        if not rids:
            continue
        for blip in list(el.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip",
                                 "{urn:schemas-microsoft-com:vml}imagedata")):
            rid = blip.get(f"{{{R}}}embed") or blip.get(f"{{{R}}}id")
            if rid not in rids:
                continue
            # Walk up to the drawing container (DOCX run content or PPTX picture shape).
            node = blip
            while node is not None and etree.QName(node).localname not in ("drawing", "pict", "pic"):
                node = node.getparent()
            if node is None:
                continue
            parent = node.getparent()
            if etree.QName(node).localname in ("drawing", "pict"):
                t = etree.SubElement(parent, f"{{{W}}}t")
                t.text = "[IMAGE WITHHELD]"
                parent.replace(node, t)
            elif etree.QName(parent).localname in ("spTree", "grpSp"):
                parent.remove(node)
            n += 1
    return n


def _clear_core(core) -> None:
    for prop in PERSONAL_CORE_PROPS:
        setattr(core, prop, "")


def mask_docx(doc: Document, findings: list[Finding], out: Path, settings: Settings) -> Path:
    import docx

    d = docx.Document(doc.path)
    pkg = d.part.package
    ranges = _ranges_by_anchor(doc, findings)
    _rewrite_units(list(docx_units(d)) + list(package_xml_units(pkg)) + list(author_attribute_units(pkg)), ranges)
    _remove_pictures(pkg, _mask_images(doc, findings, pkg))
    _clear_core(d.core_properties)
    flush_blob_parts(pkg)
    d.save(str(out))
    return out


def mask_pptx(doc: Document, findings: list[Finding], out: Path, settings: Settings) -> Path:
    from pptx import Presentation

    prs = Presentation(doc.path)
    pkg = prs.part.package
    ranges = _ranges_by_anchor(doc, findings)
    _rewrite_units(list(pptx_units(prs)) + list(package_xml_units(pkg)) + list(author_attribute_units(pkg)), ranges)
    _remove_pictures(pkg, _mask_images(doc, findings, pkg))
    _clear_core(prs.core_properties)
    flush_blob_parts(pkg)
    prs.save(str(out))
    return out


def mask_image(doc: Document, findings: list[Finding], out: Path, settings: Settings) -> Path:
    boxes = [b for f in _live(findings) for b in finding_boxes(doc.span(f.span_id), f)]
    withhold = any(i.ocr_status in WITHHOLD for i in doc.images)
    new = _paint_image(Path(doc.path).read_bytes(), boxes, withhold)
    out = out.with_suffix(".png") if new is None or not out.suffix else out
    out.write_bytes(new or b"")
    return out


def write_masked(doc: Document, findings: list[Finding], out_dir: Path, settings: Settings) -> Path | None:
    stem = Path(doc.file).stem
    fn = {"pdf": mask_pdf, "docx": mask_docx, "pptx": mask_pptx, "image": mask_image}.get(doc.file_type)
    if fn is None:
        return None
    ext = Path(doc.file).suffix or f".{doc.file_type}"
    return fn(doc, findings, out_dir / f"{stem}.masked{ext}", settings)
