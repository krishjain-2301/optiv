"""End-to-end: detection quality is *measured* against the planted gold labels, and the
redacted output and masked files are checked for leaks."""
import json
import zipfile

import pymupdf as fitz
import pytest

from pii_shield.evaluate import evaluate, load_gold, structure_retention

# Regression floor, not a claim. The measured numbers are printed by `test_report_metrics`.
MIN_RECALL = 0.90
MIN_PRECISION = 0.80


@pytest.fixture(scope="module")
def ev(run_result, samples):
    return evaluate(load_gold(samples / "gold_labels.csv"), run_result.findings, run_result.redacted)


def test_all_files_processed(run_result):
    assert not run_result.errors, run_result.errors
    assert len(run_result.docs) == 3


def test_report_metrics(ev, capsys):
    with capsys.disabled():
        print(f"\n  recall={ev.recall:.3f} category_recall={ev.category_recall:.3f} precision={ev.precision:.3f} precision_auto={ev.precision_auto:.3f} "
              f"leaks={len(ev.leaks)} gold={ev.total_gold}")
        for ctx, m in ev.by_context.items():
            print(f"    {ctx:<10} {m['recalled']}/{m['gold']}")
        for m in ev.missed:
            print(f"    MISSED {m}")
        for fp in ev.false_positives[:15]:
            print(f"    FP {fp}")


def test_recall_floor(ev):
    assert ev.recall >= MIN_RECALL, ev.missed


def test_precision_floor(ev):
    assert ev.precision >= MIN_PRECISION, ev.false_positives


def test_no_structured_id_leaks(ev):
    critical = {"US_SSN", "PASSPORT", "IN_PAN", "PL_PESEL", "TAX_ID", "EMPLOYEE_ID", "EMAIL_ADDRESS"}
    leaked = [l for l in ev.leaks if l["category"] in critical]
    assert not leaked, leaked


def test_every_finding_is_traceable(run_result):
    for fs in run_result.findings.values():
        for f in fs:
            if f.decision == "drop":
                continue
            assert f.reasons and f.layer and f.recognizer and f.location and f.token, f


def test_tokens_stable_across_files(run_result):
    toks = {}
    for fs in run_result.findings.values():
        for f in fs:
            if f.entity_type == "PERSON" and f.decision != "drop":
                toks.setdefault(run_result.vault.persons.canonical(f.text), set()).add(f.token)
    assert all(len(t) == 1 for t in toks.values()), {k: v for k, v in toks.items() if len(v) > 1}
    # possessive / first-name mention maps to the same person as the full name
    rafael = [f for f in run_result.findings["risk_policy_scanned.pdf"] if f.text.startswith("Rafael")]
    assert len({f.token for f in rafael}) == 1


def test_email_linked_to_person(run_result):
    f = next(f for f in run_result.findings["org_pack.pptx"] if f.text == "m.vossberg@cadence.example")
    p = next(f for f in run_result.findings["org_pack.pptx"] if f.text == "Martin Vossberg")
    assert f.token.split("_")[-1] == p.token.split("_")[-1]


def test_masked_pdf_preserves_pages_and_clears_metadata(run_result):
    out = run_result.out_dir / "risk_policy_scanned.masked.pdf"
    pdf = fitz.open(out)
    assert pdf.page_count == 3
    assert not pdf.metadata.get("author")


def test_masked_docx_pptx_have_no_pii(run_result, samples):
    gold = load_gold(samples / "gold_labels.csv")
    for name in ("tprm_training", "org_pack"):
        ext = "docx" if name == "tprm_training" else "pptx"
        with zipfile.ZipFile(run_result.out_dir / f"{name}.masked.{ext}") as zf:
            xml = " ".join(zf.read(n).decode("utf8", "ignore") for n in zf.namelist() if n.endswith(".xml"))
        leaked = [g.text for g in gold if g.file == f"{name}.{ext}" and g.context_type != "image" and g.text in xml]
        assert not leaked, leaked


def test_structure_retention(run_result):
    for f in ("tprm_training.docx", "org_pack.pptx"):
        r = structure_retention(run_result.docs[f])
        assert r["score"] is not None and r["score"] >= 0.8, r


def test_reports_written(run_result):
    out = run_result.out_dir
    for name in ("pii_exposure_register.csv", "pii_exposure_register.xlsx", "pii_exposure_register.SENSITIVE.csv",
                 "summary.json", "audit_log.jsonl"):
        assert (out / name).exists(), name
    # no passphrase in the test settings: the vault must not be on disk in any form
    assert not list(out.glob("token_vault*")), list(out.glob("token_vault*"))
    # the shareable register shows values partially masked only
    reg = (out / "pii_exposure_register.csv").read_text(encoding="utf-8")
    assert "Rafael Mendoza-Kowalski" not in reg and "R*********************i" in reg
    summary = json.loads((out / "summary.json").read_text())
    assert {f["file"] for f in summary["files"]} == set(run_result.docs)
    # audit log never holds raw values
    first = json.loads((out / "audit_log.jsonl").read_text().splitlines()[0])
    assert "value_masked" in first and "text" not in first
