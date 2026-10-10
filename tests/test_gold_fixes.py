"""What the gold-label test of 2026-10-10 found, as tests. Rules only (no NER model), made-up values."""
import cv2
import docx
import numpy as np

from optiv_pii_shield.config import Settings
from optiv_pii_shield.detect import Detector
from optiv_pii_shield.detect.propagation import PersonIndex, propagate
from optiv_pii_shield.detect.rules import IMAGE_RULES, run_rules
from optiv_pii_shield.detect.vocab import corpus_vocabulary, is_code_word
from optiv_pii_shield.extract.common import mostly_unreadable
from optiv_pii_shield.extract.docx import extract_docx
from optiv_pii_shield.extract.visual import overprinted
from optiv_pii_shield.models import Document, Span, Visual, Word


def detect(spans):
    doc = Document(file="t", path="t", file_type="text", pages=1, spans=spans)
    return [f for f in Detector(Settings(use_spacy=False)).detect_all({"t": doc})["t"] if f.decision != "drop"]


def para(i, text, **kw):
    kw.setdefault("kind", "paragraph")
    return Span(id=f"s{i}", file="t", text=text, page=1, location=f"p{i}", **kw)


def rule_hits(text, rules=None):
    return {(r.entity_type, text[r.start:r.end]) for r in run_rules(text, None, rules)}


# ------------------------------------------------------------------ words taken for people
def test_form_fields_in_a_cell_are_not_speakers():
    cell = "Question: 1.6\nOperator: Equal To\nResponse: Yes\nQuestion: 2.1\nResponse: No"
    spans = [para(0, cell, kind="table_cell", header="Conditions"), para(1, "Each question has one response.")]
    assert not [f for f in detect(spans) if f.entity_type == "PERSON"]


def test_a_repeated_label_in_prose_is_still_a_speaker():
    found = detect([para(0, "Okpara: we should escalate today.\nVarga: agreed, I will call them.\nOkpara: thank you.")])
    assert {f.text for f in found if f.entity_type == "PERSON"} == {"Okpara"}


def test_column_called_name_does_not_make_its_values_people():
    spans = [para(0, "High Risk Response", kind="table_cell", header="SLA name"),
             para(1, "A high risk response is due in five days; every response is logged.")]
    assert not [f for f in detect(spans) if f.entity_type == "PERSON"]


def test_a_committee_in_an_approver_column_is_not_a_person():
    spans = [para(0, "Divisional Risk Committee", kind="table_cell", header="Approved by"),
             para(1, "Board Risk Cttee", kind="table_cell", header="Approver"),
             para(2, "Ngozi Okpara", kind="table_cell", header="Approver")]
    assert {f.text for f in detect(spans) if f.entity_type == "PERSON"} == {"Ngozi Okpara"}


def test_vocabulary_is_not_learned_from_addresses():
    doc = Document(file="t", path="t", file_type="text", spans=[
        para(0, "Write to n.okpara@example.org or n.okpara@example.org about the report and the report owner.")])
    vocab = corpus_vocabulary({"t": doc})
    assert "report" in vocab and "okpara" not in vocab


def test_code_words():
    assert is_code_word("ServPro") and is_code_word("TierContains")
    assert not is_code_word("McDonald") and not is_code_word("SmithDaniel") and not is_code_word("Okpara")


# ------------------------------------------------------------------------ names left readable
def test_given_name_in_front_of_initial_and_surname():
    found = detect([para(0, "Name: N. D. Okpara"), para(1, "The audit lead, Chidinma D. Okpara (lead), reports to the board.")])
    assert "Chidinma D. Okpara" in {f.text for f in found}


def test_name_run_together_with_the_word_before():
    idx = PersonIndex()
    idx.add("Yelena Voronin", "t")
    span = Span(id="s", file="f", text="ChairYelena A. Voronin,Group CFO / OwnerVoronin", kind="image_ocr", source="image_ocr")
    hits = {h.text for h in propagate({"f": Document(file="f", path="f", file_type="pdf", spans=[span])}, idx)}
    assert {"Yelena", "Voronin"} <= hits


def test_title_and_surname():
    found = detect([para(0, '"Dear Ms Varga-Lindt, thank you for calling us on 15 April."'), para(1, "Varga-Lindt called again.")])
    assert [f.text for f in found if f.entity_type == "PERSON"] == ["Varga-Lindt", "Varga-Lindt"]


def test_initials_and_misread_surname_match_a_confirmed_person():
    idx = PersonIndex()
    idx.add("Fumio L. Kittelsen", "t")
    span = Span(id="s", file="f", text="F.L.Kittelsn / C.A kittels / R.T.Zubiri", kind="image_ocr", source="image_ocr")
    hits = {h.text for h in propagate({"f": Document(file="f", path="f", file_type="pdf", spans=[span])}, idx)}
    assert "F.L.Kittelsn" in hits and not any("Zubiri" in h for h in hits)


def test_cell_listing_people_one_per_line():
    cell = "Daniel Okpara\nSarah Varga\nTK Moreau\nJohn Lindt"
    found = detect([para(0, cell, kind="table_cell", header="Users")])
    assert "TK Moreau" in {f.text for f in found if f.entity_type == "PERSON"}


def test_word_like_given_name_alone_on_a_line_of_a_screenshot():
    found = detect([Span(id="s0", file="t", text="Mark Lund", kind="image_ocr", source="image_ocr", page=1, location="i"),
                    para(1, "Mark Lund")])
    assert [(f.span_id, f.decision) for f in found if f.entity_type == "PERSON"] == [("s0", "review")]


# --------------------------------------------------------------- values broken or cut short
def test_last_four_digits_of_a_social_security_number():
    assert ("US_SSN", "4417") in rule_hits("we hold the last four digits of your social security number (4417) and your date of birth")


def test_address_with_apartment_and_with_glued_state():
    t = "his home address at 77 Birchfield Lane, Apartment 9B, Austin, TX 78701."
    assert ("ADDRESS", "77 Birchfield Lane, Apartment 9B, Austin, TX 78701") in rule_hits(t)
    assert ("ADDRESS", "310 Larkspur Crescent,Round Rock,TX78664") in rule_hits("to 310 Larkspur Crescent,Round Rock,TX78664.")


def test_id_broken_after_a_hyphen():
    assert ("VENDOR_ID", "VEN-\nKD-6031") in rule_hits("VEN-\nKD-6031")
    spans = [para(0, "next-day reporting, T. Okpara (VEN-"), para(1, "+63 917 555 0142"), para(2, "KD-6031) was notified")]
    assert ("s2", "KD-6031") in {(f.span_id, f.text) for f in detect(spans)}


def test_date_of_birth_after_a_label_written_as_one_word():
    assert ("DATE_OF_BIRTH", "14 March 1982") in {(e, v) for e, v in rule_hits("DATEOFBIRTH\n14 March 1982")}


def test_number_of_a_known_id_inside_another_code():
    found = detect([para(0, "Employee ID: EMP-47216"), para(1, "PHOTOGRAPH ON FILE / CFG-47216-0091-2026 / ZIP 47216")])
    assert ("s1", "CFG-47216-0091-2026") in {(f.span_id, f.text) for f in found}
    assert not [f for f in found if f.span_id == "s1" and f.text == "47216"]


# ------------------------------------------------------------------------------- pictures
def test_damaged_values_in_screenshot_text():
    t = "user-t.vmokparaexarnpleorg.con pnone=+1-212-555-e147resultapproved / src_ip-s2.i60.14.8geo-London / target-EMP 47216 / EMP-4B216"
    got = rule_hits(t, IMAGE_RULES)
    assert {("EMAIL_ADDRESS", "user-t.vmokparaexarnpleorg.con"), ("PHONE_NUMBER", "+1-212-555-e147"), ("IP_ADDRESS", "s2.i60.14.8"),
            ("EMPLOYEE_ID", "EMP 47216"), ("EMPLOYEE_ID", "EMP-4B216")} <= got
    assert any(e == "EMAIL_ADDRESS" for e, _ in rule_hits("to-vendor.helpdsknorthwindco.exarnpleip_riag", IMAGE_RULES))
    assert ("PERSON", "T.R.Moreau") in rule_hits("High / Ineffective / T.R.Moreau / Left 28/02/2026", IMAGE_RULES)


def test_picture_read_mostly_below_the_floor_counts_as_unreadable():
    def span(confs):
        return Span(id="s", file="f", text="x", kind="image_ocr", source="image_ocr",
                    words=[Word(text="word", start=0, end=4, conf=c, bbox=(0, 0, 1, 1)) for c in confs])

    s = Settings()
    assert mostly_unreadable([span([0.65] * 7 + [0.95] * 3)], s)
    assert not mostly_unreadable([span([0.65] * 3 + [0.95] * 7)], s)
    assert not mostly_unreadable([span([0.65] * 2)], s)  # a logo with two words is no screenshot


def stamped_form():
    """Two lines of black print; a red stamp frame runs across the second. An outlined box holds the first."""
    img = np.full((400, 900, 3), 255, np.uint8)
    cv2.putText(img, "Reviewed", (60, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
    cv2.rectangle(img, (30, 50), (300, 130), (40, 90, 200), 3)  # a diagram box around its label
    cv2.putText(img, "t.okpara@example.org", (60, 260), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
    cv2.rectangle(img, (150, 170), (600, 250), (200, 60, 60), 4)  # the stamp: its lower edge crosses the address
    return img, {"label": (60, 72, 215, 108), "left": (60, 232, 240, 268), "right": (640, 232, 760, 268)}


def test_text_under_a_stamp_is_boxed_and_text_inside_an_outline_is_not():
    img, words = stamped_form()
    boxes = overprinted(img, list(words.values()))  # OCR stopped at "t.okpara": the rest of the line is unread
    assert len(boxes) == 1
    x0, y0, x1, y1 = boxes[0]
    assert x0 <= 150 and x1 >= 600 and y0 <= 232 and y1 >= 268  # the whole line, as far as the ink runs through it
    assert not overprinted(img, [words["label"]])  # an outline goes around its label
    assert not overprinted(img, [words["label"], (60, 232, 440, 268)])  # the whole line was read: nothing is hidden
    assert not overprinted(np.full((400, 900, 3), 255, np.uint8), list(words.values()))


def test_a_word_under_a_stamp_is_masked_however_sure_ocr_was():
    text = "Group CRO t.okpara"
    span = Span(id="s0", file="t", text=text, kind="image_ocr", source="image_ocr", page=1, location="i", image_ref="img1",
                words=[Word(text="Group", start=0, end=5, conf=0.99, bbox=(10, 10, 60, 30)),
                       Word(text="CRO", start=6, end=9, conf=0.99, bbox=(70, 10, 110, 30)),
                       Word(text="t.okpara", start=10, end=18, conf=0.99, bbox=(120, 10, 220, 30))])
    doc = Document(file="t", path="t", file_type="image", pages=1, spans=[span])
    doc.visuals.append(Visual("overprint", (65, 8, 400, 32), 1, "img1"))
    found = [f for f in Detector(Settings(use_spacy=False)).detect_all({"t": doc})["t"] if f.decision != "drop"]
    assert {f.text for f in found} == {"CRO", "t.okpara"}
    assert {f.recognizer for f in found if f.text == "CRO"} == {"failclosed:overprint"}


# ---------------------------------------------------------------------------- extraction
def test_docx_line_breaks_inside_a_cell_keep_names_apart(tmp_path):
    d = docx.Document()
    table = d.add_table(rows=1, cols=1)
    run = table.cell(0, 0).paragraphs[0].add_run("Tamara Olsen")
    for name in ("Shen Smith", "Chris Lund"):
        run = table.cell(0, 0).paragraphs[0].add_run()
        run.add_break()
        run.add_text(name)
    path = tmp_path / "cell.docx"
    d.save(path)
    cells = [s.text for s in extract_docx(path, Settings(ocr_embedded_images=False)).spans if s.kind == "table_cell"]
    assert cells == ["Tamara Olsen\nShen Smith\nChris Lund"]
