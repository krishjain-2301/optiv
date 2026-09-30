"""Extraction on the synthetic artifacts: provenance, structure and the known traps."""
import pytest
from lxml import etree

from pii_shield.extract import extract, sniff
from pii_shield.extract.ooxml import W, TextUnit, apply_replacements, build_unit


def test_apply_replacements_across_runs():
    p = etree.fromstring(f'<w:p xmlns:w="{W}"><w:r><w:t>Call Grace Wil</w:t></w:r><w:r><w:t>son today</w:t></w:r></w:p>')
    unit = build_unit(TextUnit(anchor="a", kind="paragraph", location=""), [p], f"{{{W}}}t")
    assert unit.text == "Call Grace Wilson today"
    s = unit.text.index("Grace")
    apply_replacements(unit, [(s, s + len("Grace Wilson"), "[PERSON_001]")])
    assert "".join(t.text for t in p.iter(f"{{{W}}}t")) == "Call [PERSON_001] today"


def test_sniff_ignores_extension(samples, tmp_path):
    fake = tmp_path / "really_a_pdf.docx"
    fake.write_bytes((samples / "risk_policy_scanned.pdf").read_bytes())
    assert sniff(fake) == "pdf"


@pytest.fixture(scope="module")
def docx_doc(samples, settings):
    return extract(samples / "tprm_training.docx", settings)


@pytest.fixture(scope="module")
def pptx_doc(samples, settings):
    return extract(samples / "org_pack.pptx", settings)


@pytest.fixture(scope="module")
def pdf_doc(samples, settings):
    return extract(samples / "risk_policy_scanned.pdf", settings)


def test_docx_cells_not_glued(docx_doc):
    cell = next(s for s in docx_doc.spans if s.kind == "table_cell" and "Tamika" in s.text)
    assert cell.text == "Tamika Oliver\nShimane Smith"
    assert cell.header == "Assigned reviewers"
    assert cell.table == (0, 1, 1)
    assert "OliverShimane" not in docx_doc.markdown


def test_docx_parts_covered(docx_doc):
    kinds = {s.kind for s in docx_doc.spans}
    assert {"heading", "paragraph", "table_cell", "header", "footer", "metadata", "image_ocr"} <= kinds
    meta = {s.text for s in docx_doc.spans if s.kind == "metadata"}
    assert {"Marcus Feld", "Ines Duarte"} <= meta


def test_docx_screenshot_ocr(docx_doc):
    img_text = " ".join(s.text for s in docx_doc.spans if s.kind == "image_ocr")
    assert "Daniel Brooks" in img_text
    assert docx_doc.images and docx_doc.images[0].ocr_status in ("read", "low_confidence")


def test_pptx_table_group_notes(pptx_doc):
    cells = [s for s in pptx_doc.spans if s.kind == "table_cell"]
    assert any(s.text == "+1 (555) 0114" and s.header == "Mobile" and s.page == 1 for s in cells)
    assert any("M. Vossberg CEO" in s.text for s in pptx_doc.spans)  # inside a group shape
    assert any(s.kind == "notes" and "Clara" in s.text for s in pptx_doc.spans)
    assert pptx_doc.structure["cells"] == 20


def test_pdf_scanned_pages_ocrd(pdf_doc):
    assert pdf_doc.ocr_pages == [1, 2, 3]
    assert all(s.source in ("ocr", "image_ocr", "native") for s in pdf_doc.spans)
    ocr_spans = [s for s in pdf_doc.spans if s.source != "native"]
    assert all(s.bbox and s.ocr_conf is not None and s.page for s in ocr_spans)


def test_pdf_table_cells_kept_apart(pdf_doc):
    assert pdf_doc.structure["tables"] >= 1
    cells = [s for s in pdf_doc.spans if s.kind == "table_cell" and s.page == 2]
    phone = [s for s in cells if "555-0193" in s.text]
    assert phone, "phone cell missing"
    assert phone[0].header and "phone" in phone[0].header.lower()
    assert "grace" not in phone[0].text.lower()  # no leakage from the neighbouring column


def test_pdf_screenshot_region_reread(pdf_doc):
    regions = [i for i in pdf_doc.images if i.page == 3]
    assert regions, "screenshot region not detected"
    text = " ".join(s.text for s in pdf_doc.spans if s.kind == "image_ocr" and s.page == 3)
    assert "ogrant@acmeco.example" in text.replace(" ", "")
