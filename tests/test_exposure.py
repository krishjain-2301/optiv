"""Exposure score and residual risk."""
import json

from pii_shield.config import SENSITIVITY
from pii_shield.exposure import file_exposure
from pii_shield.models import Document, Finding, ImageRef, Span


def _doc(findings_spec, words_per_page=100, pages=2):
    spans = [Span(id=f"s{p}", file="f", text=" ".join(["word"] * words_per_page), kind="paragraph", page=p)
             for p in range(1, pages + 1)]
    doc = Document(file="f", path="f", file_type="pdf", pages=pages, spans=spans)
    fs = [Finding(f"s{page}", "f", 0, 4, text, cat, 0.9, "r", "L1 rules", token=f"[{cat}_{i}]", decision=dec, page=page,
                  source="native")
          for i, (cat, text, page, dec) in enumerate(findings_spec)]
    return doc, fs


def test_weighted_score_density_and_pages():
    doc, fs = _doc([("PERSON", "Ann Lee", 1, "redact"), ("EMAIL_ADDRESS", "a@b.co", 1, "redact"),
                    ("US_SSN", "123-45-6789", 2, "review"), ("PERSON", "Bob", 2, "drop")])
    e = file_exposure(doc, fs)
    expected = SENSITIVITY["PERSON"] + SENSITIVITY["EMAIL_ADDRESS"] + SENSITIVITY["US_SSN"]  # drop not counted
    assert e["score"] == expected
    assert e["per_1k_words"] == round(expected / 200 * 1000, 2)
    assert e["by_page"] == {"1": SENSITIVITY["PERSON"] + SENSITIVITY["EMAIL_ADDRESS"], "2": SENSITIVITY["US_SSN"]}
    assert e["rating"] == "critical"  # an SSN is present


def test_rating_without_identifiers_follows_density():
    doc, fs = _doc([("PERSON", "Ann Lee", 1, "redact")], words_per_page=1000)
    assert file_exposure(doc, fs)["rating"] == "low"
    doc, fs = _doc([("PERSON", f"P{i}", 1, "redact") for i in range(20)], words_per_page=100)
    assert file_exposure(doc, fs)["rating"] == "high"
    doc, fs = _doc([])
    assert file_exposure(doc, fs)["rating"] == "none"


def test_residual_parts():
    doc, fs = _doc([("PERSON", "Ann Lee", 1, "redact"), ("PHONE_NUMBER", "555 0101 222", 1, "redact")])
    doc.images.append(ImageRef(id="i", file="f", page=1, location="x", ocr_status="unreadable"))
    doc.gate.update(masked="written", llm_text_scrubbed=2, masked_scrubbed=0)
    r = file_exposure(doc, fs)["residual"]
    assert r["known"] == {"values_left_in_outputs": 0, "masked_copy": "written", "llm_text_values_caught_by_gate": 2,
                          "masked_values_caught_by_gate": 0}
    assert r["unreadable"]["images_withheld"] == 1
    est = r["estimated_missed"]
    assert est["by_category"]["PERSON"] > est["by_category"].get("PHONE_NUMBER", 0) > 0
    assert "held-out" in est["basis"]


def test_summary_has_exposure_and_ranking(run_result):
    s = json.loads((run_result.out_dir / "summary.json").read_text())
    assert [r["file"] for r in s["exposure_ranking"]] and all("exposure" in f for f in s["files"])
    for f in s["files"]:
        assert f["exposure"]["residual"]["known"]["masked_copy"] == "written"
        assert f["exposure"]["by_page"]
