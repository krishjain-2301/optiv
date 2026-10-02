"""Unit tests for validators, rules, name handling and structure (no models, no files)."""
from pii_shield.detect import validators as v
from pii_shield.detect.names import looks_like_name, name_variants, trim_to_name
from pii_shield.detect.rules import run_rules
from pii_shield.detect.structure import header_category, structure_findings
from pii_shield.models import Span


def hits(text, entity=None):
    return [(r.entity_type, text[r.start:r.end], r.score) for r in run_rules(text) if entity in (None, r.entity_type)]


def best(text, entity):
    hs = [h for h in hits(text, entity)]
    return max(hs, key=lambda h: h[2]) if hs else None


# ------------------------------------------------------------------------ validators
def test_luhn():
    assert v.luhn_ok("4111 1111 1111 1111")
    assert not v.luhn_ok("4111 1111 1111 1112")


def test_pesel_checksum_and_partial():
    assert v.check_pesel("44051401359")[0] > 0  # valid published example
    assert v.check_pesel("890412xxxxx")[0] > 0  # masked, but birth date visible
    assert v.check_pesel("891332xxxxx")[0] < 0  # month 13 impossible


def test_pan_holder_type():
    assert v.check_pan("ABCPM1234F")[0] > 0
    assert v.check_pan("ABCZM1234F")[0] < 0


def test_ssn_test_range_is_lowered_not_vetoed():
    delta, _ = v.check_ssn("900-12-3456")
    assert -0.2 < delta < 0


def test_iban():
    assert v.check_iban("GB82 WEST 1234 5698 7654 32")[0] > 0
    assert v.check_iban("GB82 WEST 1234 5698 7654 33")[0] < 0


# ----------------------------------------------------------------------------- rules
def test_email_example_domain():
    assert best("mail m.vossberg@cadence.example now", "EMAIL_ADDRESS")[1] == "m.vossberg@cadence.example"


def test_ocr_damaged_email_still_flagged():
    h = best("wilsong@acmeco-p com Active", "EMAIL_ADDRESS")
    assert h and h[2] >= 0.35


def test_nonstandard_555_phone():
    h = best("Mobile: +1 (555) 0114", "PHONE_NUMBER")
    assert h and h[1] == "+1 (555) 0114" and h[2] >= 0.6


def test_international_phone_formats():
    for p in ["+44 7700 900418", "+48 22 555 01 87", "+91 98200 55512", "+81 3-5550-1234", "+1 (212) 555-0193"]:
        h = best(f"Phone {p}", "PHONE_NUMBER")
        assert h and h[1] == p, p


def test_cadence_ids():
    assert best("owner EMP-40718", "EMPLOYEE_ID")[1] == "EMP-40718"
    assert best("DIR-2291 signs", "EMPLOYEE_ID")[1] == "DIR-2291"
    assert best("vendor MER-IN-0042", "VENDOR_ID")[1] == "MER-IN-0042"


def test_ssn_test_range_kept_with_label():
    assert best("SSN: 900-12-3456", "US_SSN")[2] >= 0.6


def test_passport_needs_context():
    assert best("Passport No: K4829175", "PASSPORT")[2] >= 0.5
    assert best("Ref K4829175 closed", "PASSPORT")[2] < 0.35


def test_dob_needs_context_approval_date_is_not_dob():
    assert best("Date of birth: 12/04/1985", "DATE_OF_BIRTH")[2] >= 0.35
    h = best("Policy approved on 14/03/2024 by the Risk Committee.", "DATE_OF_BIRTH")
    assert h is None or h[2] < 0.35


def test_card_last_four():
    assert best("Corporate card ending 2218 is assigned", "CREDIT_CARD")[1] == "2218"


def test_card_luhn():
    assert best("card 4111 1111 1111 1111", "CREDIT_CARD")[2] >= 0.6


def test_address():
    h = best("Home address: 14 Harrow Lane, Leeds LS6 2QT", "ADDRESS")
    assert h and h[1].startswith("14 Harrow Lane")


def test_policy_numbers_not_phone():
    h = best("See section 16.5 of policy version 2024", "PHONE_NUMBER")
    assert h is None or h[2] < 0.35


# ----------------------------------------------------------------------------- names
def test_name_shapes():
    assert looks_like_name("Rafael Mendoza-Kowalski")
    assert looks_like_name("M. Vossberg")
    assert not looks_like_name("Vendor Tier")
    assert not looks_like_name("QRC_BYOD")
    assert not looks_like_name("Acme Payroll Ltd")


def test_trim_role_suffix():
    text = "M. Vossberg CEO"
    s, e = trim_to_name(text)
    assert text[s:e] == "M. Vossberg"


def test_variants():
    vs = name_variants("Rafael Mendoza-Kowalski")
    assert {"Rafael", "Mendoza-Kowalski", "R. Mendoza-Kowalski"} <= set(vs)


# ------------------------------------------------------------------------- structure
def test_header_category():
    assert header_category("E-mail") == "EMAIL_ADDRESS"
    assert header_category("Assigned reviewers") == "PERSON"
    assert header_category("Vendor name") is None
    assert header_category("Mobile") == "PHONE_NUMBER"


def test_table_header_flags_each_line():
    span = Span(id="s1", file="f", text="Tamika Oliver\nShimane Smith", kind="table_cell", header="Assigned reviewers")
    got = {f.text for f in structure_findings(span, set())}
    assert got == {"Tamika Oliver", "Shimane Smith"}


def test_label_line():
    span = Span(id="s1", file="f", text="Full name: Rafael Mendoza-Kowalski\nTIN: 12-3456789", kind="ocr_block")
    got = {(f.entity_type, f.text) for f in structure_findings(span, set())}
    assert ("PERSON", "Rafael Mendoza-Kowalski") in got
    assert ("TAX_ID", "12-3456789") in got


def test_role_mailbox_goes_to_review_band():
    assert best("contact tprm-office@cadence-demo.example", "EMAIL_ADDRESS")[2] < 0.6
    assert best("contact kofi.mensah@cadence-demo.example", "EMAIL_ADDRESS")[2] >= 0.9


def test_damaged_email_fallback_does_not_extend_clean_email():
    got = hits("ogrant@acmeco.example Active\nDisabled", "EMAIL_ADDRESS")
    assert {h[1] for h in got} == {"ogrant@acmeco.example"}


def test_resolver_keeps_second_name_in_cell():
    from pii_shield.config import Settings
    from pii_shield.detect.resolver import finalise
    from pii_shield.models import Document, Finding

    span = Span(id="s1", file="f", text="Tamika Oliver\nShimane Smith", kind="table_cell", header="Assigned reviewers")
    doc = Document(file="f", path="f", file_type="docx", spans=[span])
    ner = Finding("s1", "f", 0, 21, span.text[:21], "PERSON", 0.85, "ner:spacy", "L2 ner", ["ner"])
    found = [ner] + structure_findings(span, set())
    out = finalise({"f": found}, {"f": doc}, Settings())["f"]
    assert {f.text for f in out if f.decision != "drop"} == {"Tamika Oliver", "Shimane Smith"}


# ------------------------------------------------------------------ screenshot fail-closed
def test_image_text_emails_without_at():
    from pii_shield.detect.rules import IMAGE_RULES

    got = {r.entity_type: None for r in run_rules("x", None, IMAGE_RULES)}
    assert got == {}
    text = "Emily Martinez emlly.martinezmaomeccrp.com Auditor / john.davis / policy.docx"
    found = {text[r.start:r.end] for r in run_rules(text, None, IMAGE_RULES)}
    assert {"emlly.martinezmaomeccrp.com", "john.davis"} <= found
    assert "policy.docx" not in found


def test_image_rules_only_apply_to_image_spans():
    from pii_shield.config import Settings
    from pii_shield.detect import Detector

    det = Detector(Settings(use_spacy=False))
    body = Span(id="b", file="f", text="Contact via john.davis today", kind="paragraph", source="native")
    shot = Span(id="i", file="f", text="Contact via john.davis today", kind="image_ocr", source="image_ocr")
    assert not [f for f in det.detect_span(body) if f.entity_type == "EMAIL_ADDRESS"]
    assert [f.text for f in det.detect_span(shot) if f.entity_type == "EMAIL_ADDRESS"] == ["john.davis"]


def test_fuzzy_ocr_name_maps_to_confirmed_person():
    from pii_shield.detect.propagation import PersonIndex, propagate
    from pii_shield.models import Document

    idx = PersonIndex()
    idx.add("John Davis", "table")
    span = Span(id="i", file="f", text="Jahn Davis  john.davis@acme.com", kind="image_ocr", source="image_ocr")
    hits = propagate({"f": Document(file="f", path="f", file_type="pdf", spans=[span])}, idx)
    # The surname variant "Davis" also matches exactly; the resolver merges it into the fuzzy hit.
    assert ("Jahn Davis", "propagation:fuzzy-ocr") in [(h.text, h.recognizer) for h in hits]
    assert idx.canonical("Jahn Davis") == "John Davis"  # same token as the real spelling
    assert idx.fuzzy("Tom Browning") is None


def test_low_confidence_screenshot_identifier_is_masked():
    from pii_shield.config import Settings
    from pii_shield.detect import fail_closed_findings
    from pii_shield.models import Document, Word

    text = "Member martinezmaomeccrp.com Active"
    words = [Word(t, text.index(t), text.index(t) + len(t), (0, 0, 1, 1), 0.7) for t in text.split()]
    span = Span(id="i", file="f", text=text, kind="image_ocr", source="image_ocr", words=words)
    doc = Document(file="f", path="f", file_type="pdf", spans=[span])
    out = fail_closed_findings(doc, [], Settings())
    assert [f.text for f in out] == ["martinezmaomeccrp.com"]  # 0.70 < 0.80 image floor; "Member" is not identifier-like


def test_ids_glued_by_ocr_still_match():
    assert best("ref-ISS-2820-81120ner-EMP-41877sV-HIGH", "EMPLOYEE_ID")[1] == "EMP-41877"
    assert best("EMP-41077IAM Engineer", "EMPLOYEE_ID")[1] == "EMP-41077"
    assert best("IAM Engineer IIEMP-41077,", "EMPLOYEE_ID")[1] == "EMP-41077"  # fail closed on glued capitals


def test_ocr_glue_around_phones_and_ids():
    assert best("Whitfield+1(212)555-0147", "PHONE_NUMBER")[1] == "+1(212)555-0147"
    assert best("Group General CounselEMP-37012,", "EMPLOYEE_ID")[1] == "EMP-37012"


def test_identifier_split_from_context_is_still_found():
    from pii_shield.config import Settings
    from pii_shield.detect import Detector

    det = Detector(Settings(use_spacy=False))
    span = Span(id="s", file="f", text="555-0108", kind="image_ocr", source="image_ocr")
    hits = det.detect_span(span, prefix="+1 (212)Noted\n")
    assert any(f.entity_type == "PHONE_NUMBER" and f.text == "555-0108" for f in hits)


def test_phone_ocr_variants():
    from pii_shield.detect.rules import IMAGE_RULES

    assert best("D.Kulkarni+918045550182 / Moderate", "PHONE_NUMBER")[1] == "+918045550182"
    t = "(mobile+I917555-0164)."
    assert [t[r.start:r.end] for r in run_rules(t, None, IMAGE_RULES) if r.entity_type == "PHONE_NUMBER"] == ["+I917555-0164"]


# ------------------------------------------------------- names: any script, any letter case
def _redacted(texts, settings=None):
    """Run the full detector + resolver + propagation over plain paragraphs; return redacted text."""
    from pii_shield.config import Settings
    from pii_shield.detect import Detector
    from pii_shield.models import Document
    from pii_shield.redact.text import redacted_span_texts
    from pii_shield.redact.tokens import TokenVault

    s = settings or Settings(use_spacy=False)
    spans = [Span(id=f"s{i}", file="t", text=t, kind="paragraph", page=1, location=f"p{i}") for i, t in enumerate(texts)]
    doc = Document(file="t", path="t", file_type="text", pages=1, spans=spans)
    det = Detector(s)
    res = det.detect_all({"t": doc})
    TokenVault(det.person_index).assign_all({"t": doc}, res)
    red = redacted_span_texts(doc, res["t"], s)
    return [red.get(sp.id, sp.text) for sp in spans]


def test_unicode_name_shapes():
    for n in ["Hans Müller", "José García", "Łukasz Nowak", "Zoë O'Brien-Ábalos", "Siobhán Ní Bhriain"]:
        assert looks_like_name(n), n
    assert looks_like_name("PRIYA RAMAN")
    assert not looks_like_name("RAMAN")  # a lone capitalised word is an acronym far more often
    assert not looks_like_name("rahul verma")  # lowercase needs other evidence ...
    assert looks_like_name("rahul verma", cases=("title", "upper", "lower"))  # ... such as a Name column
    assert not looks_like_name("Priya RAMAN")


def test_trim_keeps_name_like_stopwords():
    for n in ["Larry Page", "April Smith", "Grace West", "Tom Low"]:
        s, e = trim_to_name(n)
        assert n[s:e] == n, n
    t = "Page 3 Larry Ellison"
    s, e = trim_to_name(t)
    assert t[s:e] == "Larry Ellison"
    t = "Risk Owner April"
    assert trim_to_name(t) is None or t[slice(*trim_to_name(t))] == "April"


def test_gazetteer_any_case():
    from pii_shield.detect.names import gazetteer_names

    t = "follow up with rahul verma, PRIYA RAMAN and Łukasz Nowak today"
    got = {t[a:b] for a, b, _, _ in gazetteer_names(t)}
    assert got == {"rahul verma", "PRIYA RAMAN", "Łukasz Nowak"}
    assert not gazetteer_names("the rahul approved")  # given name alone is not enough


def test_names_in_any_case_are_redacted_rules_only():
    out = _redacted(["Escalate to Łukasz Nowak.", "PRIYA RAMAN approved; follow up with rahul verma."])
    assert "Łukasz" not in out[0] and "Nowak" not in out[0]
    assert "PRIYA" not in out[1] and "rahul" not in out[1] and "verma" not in out[1]


def test_propagation_ignores_case_for_full_names():
    out = _redacted(["Name: Hans Müller", "HANS MÜLLER signed; later hans müller resigned. Müller left."])
    assert "Müller" not in out[1] and "MÜLLER" not in out[1] and "müller" not in out[1], out[1]


def test_single_word_variant_does_not_spread_in_lowercase():
    from pii_shield.detect.propagation import PersonIndex, propagate
    from pii_shield.models import Document

    idx = PersonIndex()
    idx.add("Larry Page", "t")
    span = Span(id="s", file="f", text="See page 4. Larry signed. LARRY PAGE agreed.", kind="paragraph")
    hits = {h.text for h in propagate({"f": Document(file="f", path="f", file_type="text", spans=[span])}, idx)}
    assert "page" not in hits and "Page" not in hits
    assert {"Larry", "LARRY PAGE"} <= hits


# ------------------------------------------------------- coverage: no keyword nearby
def test_national_landlines_without_keyword():
    for p in ["020 7946 0958", "0161 496 0000", "030 12345678", "022 2345 6789", "98200 55512", "(212) 555-0193"]:
        h = best(f"Reach them on {p} after nine.", "PHONE_NUMBER")
        assert h and h[1] == p and h[2] >= 0.6, (p, h)
    h = best("Biuro: 22 555 01 87 w godzinach pracy", "PHONE_NUMBER")  # PL: 9 digits, review band or better
    assert h and h[2] >= 0.35, h


def test_dates_years_and_versions_are_not_phones():
    for t in ["Signed 12-04-1985 in Leeds", "Valid until 14.03.2024", "Revenue 2024 1234 units", "release 1.2.3.4"]:
        h = best(t, "PHONE_NUMBER")
        assert h is None or h[2] < 0.35, (t, h)


def test_aadhaar():
    assert v.verhoeff_ok("234123412346")
    assert best("UID 2341 2341 2346 on file", "IN_AADHAAR")[2] >= 0.6
    assert best("ref 2341 2341 2346", "IN_AADHAAR")[2] >= 0.6  # valid check digit: no keyword needed
    h = best("Aadhaar 2345 6789 0123", "IN_AADHAAR")  # check digit fails, but labelled
    assert h and h[2] >= 0.35


def test_gstin():
    assert v.check_gstin("27AAPFU0939F1ZV")[0] > 0
    h = best("Supplier 27AAPFU0939F1ZV registered", "TAX_ID")
    assert h and h[1] == "27AAPFU0939F1ZV" and h[2] >= 0.6


def test_ip_addresses():
    assert best("login from 203.0.113.42 failed", "IP_ADDRESS")[2] >= 0.6
    assert best("server 10.24.8.17", "IP_ADDRESS")[2] >= 0.6
    assert best("peer 2001:db8:85a3::8a2e:370:7334 dropped", "IP_ADDRESS")[1] == "2001:db8:85a3::8a2e:370:7334"
    h = best("meeting at 12:30:45 today", "IP_ADDRESS")
    assert h is None or h[2] < 0.35


def test_credentials():
    for t, val in [("key sk-live-4f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c", "sk-live-4f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c"),
                   ("AKIAIOSFODNN7EXAMPLE was rotated", "AKIAIOSFODNN7EXAMPLE"),
                   ("password: Hunter2!Hunter2", "Hunter2!Hunter2"),
                   ("token ghp_" + "a1B2" * 9, "ghp_" + "a1B2" * 9)]:
        h = best(t, "CREDENTIAL")
        assert h and h[1] == val and h[2] >= 0.6, (t, h)


def test_card_length_number_failing_luhn_is_reviewed_not_dropped():
    h = best("Reimbursed to 4111 1111 1111 1112 on Friday.", "CREDIT_CARD")
    assert h and 0.35 <= h[2], h


def test_labelled_out_of_range_aadhaar_still_caught():
    assert best("Aadhaar 1416 1090 6499", "IN_AADHAAR")[2] >= 0.35
    h = best("ref 1416 1090 6499", "IN_AADHAAR")
    assert h is None or h[2] < 0.35
