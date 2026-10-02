"""The dashboard's API (server/api.py): a scan runs in the background, reports progress, can be
paused and cancelled, and its results come back as JSON without original values leaking into
the shareable downloads."""
import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from server.api import app, session


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def wait(client, until, timeout=180):
    seen = []
    t = time.time()
    while time.time() - t < timeout:
        s = client.get("/api/scan").json()
        seen.append(s)
        if until(s):
            return s, seen
        time.sleep(0.2)
    raise AssertionError(f"timed out; last status {seen[-1]}")


def test_idle_before_any_scan(client):
    assert client.get("/api/scan").json() == {"state": "idle"}
    assert client.get("/api/run").status_code == 404


def test_cancel_stops_the_scan_and_deletes_its_files(client):
    assert client.post("/api/scan", data={"synthetic": "true"}).json()["state"] == "running"
    assert client.post("/api/scan", data={"synthetic": "true"}).status_code == 409  # one scan at a time
    wait(client, lambda s: s["fraction"] > 0.05)
    client.post("/api/scan/cancel")
    s, _ = wait(client, lambda s: s["state"] not in ("running", "paused"))
    assert s["state"] == "cancelled"
    assert client.get("/api/run").status_code == 404
    assert not any(session.work.iterdir())


def test_pause_holds_progress_until_resumed(client):
    client.post("/api/scan", data={"synthetic": "true"})
    client.post("/api/scan/pause")
    s, _ = wait(client, lambda s: s["state"] == "paused")
    held = s["fraction"]
    time.sleep(1.0)
    assert client.get("/api/scan").json()["fraction"] == held
    client.post("/api/scan/resume")
    s, seen = wait(client, lambda s: s["state"] == "done")
    fractions = [x["fraction"] for x in seen]
    assert fractions == sorted(fractions) and s["fraction"] == 1.0


def test_run_payload(client):
    run = client.get("/api/run").json()
    assert {f["file"] for f in run["files"]} == {"org_pack.pptx", "risk_policy_scanned.pdf", "tprm_training.docx"}
    assert run["has_gold"] and not run["errors"]
    assert len(run["findings"]) > 50 and all(f["decision"] in ("redact", "review") for f in run["findings"])
    assert all(f["decision"] == "drop" for f in run["dropped"])
    assert run["pipeline"]["masked_written"] == 3
    assert {o["name"] for o in run["outputs"]["safe"]} >= {"summary.json", "audit_log.jsonl", "pii_exposure_register.csv"}
    assert all("SENSITIVE" in o["name"] for o in run["outputs"]["sensitive"])


def test_doc_detail_and_page_image(client):
    doc = client.get("/api/run/doc", params={"file": "risk_policy_scanned.pdf"}).json()
    assert doc["previewable"] and doc["pages"] == 3 and doc["context"]
    boxes = [b for page in doc["boxes"].values() for b in page]
    assert boxes and all(0 <= b["x"] <= 1 and 0 <= b["y"] <= 1 and b["w"] > 0 for b in boxes)
    for span in doc["context"]:
        for f in span["findings"]:
            assert 0 <= f["start"] < f["end"] <= len(span["text"])
    png = client.get("/api/run/page", params={"file": "risk_policy_scanned.pdf", "page": 1})
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/api/run/page", params={"file": "risk_policy_scanned.pdf", "page": 9}).status_code == 404
    assert client.get("/api/run/doc", params={"file": "nope.pdf"}).status_code == 404


def test_evaluation_uses_the_synthetic_gold_labels(client):
    ev = client.get("/api/run/evaluation").json()
    assert ev["has_gold"] and ev["scores"]["recall"] >= 0.95 and not ev["scores"]["leaks"]
    assert set(ev["retention"]) == {"org_pack.pptx", "risk_policy_scanned.pdf", "tprm_training.docx"}


def test_downloads_are_limited_to_listed_outputs(client):
    assert client.get("/api/run/output", params={"name": "summary.json"}).status_code == 200
    assert client.get("/api/run/output", params={"name": "../in/org_pack.pptx"}).status_code == 404
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/run/outputs.zip").content))
    names = z.namelist()
    assert names and not any("SENSITIVE" in n for n in names)
    gold = [f["text"] for f in client.get("/api/run").json()["findings"] if f["entity_type"] == "EMAIL_ADDRESS"]
    summary = json.dumps(json.loads(z.read("summary.json")))
    assert gold and not any(v in summary for v in gold)


def test_delete_session(client):
    assert client.delete("/api/session").json() == {"state": "idle"}
    assert client.get("/api/run").status_code == 404
    assert not any(session.work.iterdir())


def test_rejects_unsupported_uploads_and_bad_settings(client):
    r = client.post("/api/scan", files=[("files", ("notes.exe", b"MZ", "application/octet-stream"))])
    assert r.status_code == 422
    r = client.post("/api/scan", data={"synthetic": "true", "settings": json.dumps({"redact_threshold": 2})})
    assert r.status_code == 422
    assert client.get("/api/scan").json()["state"] == "idle"
