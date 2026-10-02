"""Held-out set (scripts/make_heldout.py, seed "test"): Faker data in six locales with accents,
capitals, lowercase and OCR noise, never used to write rules. Leaks are measured on what the LLM
would receive, strictly (a surname left next to a token counts as a leak).

Structured identifiers must never leak. Names are held to a regression floor rather than zero,
because they do leak today: see the printed report and README "Known limits"."""
import pytest

from pii_shield.redact.text import redacted_span_texts

# Regression floors measured on the test seed; raise them when detection improves, never lower
# them to make a change pass.
MIN_PERSON_RECALL = 0.70
MIN_PERSON_RECALL_PLAIN = 0.85  # Title-case names in ordinary prose
ZERO_LEAK = {"PHONE_NUMBER", "EMAIL_ADDRESS", "IP_ADDRESS", "IBAN_CODE", "CREDIT_CARD", "IN_AADHAAR", "PL_PESEL",
             "US_SSN"}


@pytest.fixture(scope="module")
def heldout(tmp_path_factory, settings):
    import make_heldout as mh
    from pii_shield import run

    paras = mh.generate("test")
    tmp = tmp_path_factory.mktemp("heldout")
    paths = mh.write(paras, tmp / "in", "heldout_test")
    res = run([paths["txt"], paths["docx"]], settings, tmp / "out")
    scores = {}
    for fname, doc in res.docs.items():
        red = redacted_span_texts(doc, res.findings[fname], settings)
        texts = [red.get(sp.id, sp.text) for sp in doc.spans if sp.kind == "paragraph"]
        assert len(texts) == len(paras), fname
        scores[fname] = mh.score(paras, texts)
    return res, scores


def test_report(heldout, capsys):
    _, scores = heldout
    with capsys.disabled():
        for fname, sc in scores.items():
            print(f"\n  HELD-OUT {fname}: recall {sc['recall']:.3f} ({sc['gold'] - sc['leaked']}/{sc['gold']}), "
                  f"{sc['decoy_tokens']} token(s) in {sc['decoy_paragraphs']} decoy paragraphs")
            for k, b in sorted(sc["by_category"].items()):
                print(f"    {k:<14} {b['gold'] - b['leaked']}/{b['gold']}")
            for k, b in sorted(sc["by_variant"].items()):
                print(f"    variant {k:<6} {b['gold'] - b['leaked']}/{b['gold']}")


def test_no_structured_identifier_leaks(heldout):
    for fname, sc in heldout[1].items():
        leaked = [r for r in sc["failures"] if r["category"] in ZERO_LEAK]
        assert not leaked, (fname, leaked)


def test_person_leak_floor(heldout):
    for fname, sc in heldout[1].items():
        p = sc["by_category"]["PERSON"]
        assert 1 - p["leaked"] / p["gold"] >= MIN_PERSON_RECALL, (fname, p)
        plain = [r for r in sc["failures"] if r["category"] == "PERSON" and r["variant"] == "plain"]
        n_plain = sum(1 for _ in plain)
        assert 1 - n_plain / max(sc["by_variant"]["plain"]["gold"], 1) >= MIN_PERSON_RECALL_PLAIN, (fname, plain)


def test_decoys_stay_clean(heldout):
    for fname, sc in heldout[1].items():
        assert sc["decoy_tokens"] <= 1, (fname, [r for r in sc["failures"] if r["variant"] == "decoy"])


def test_heldout_masked_docx_passes_gate(heldout):
    res, _ = heldout
    assert not res.errors, res.errors
