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
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:  # the server refuses any other Host
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


def test_only_this_machines_dashboard_is_answered(client):
    # DNS rebinding: another site's page reaches 127.0.0.1 but still sends its own name as Host
    assert client.get("/api/run", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/api/run", headers={"host": "localhost:8000"}).status_code == 200
    # a form on any web page can POST here; a request from another origin must change nothing
    assert client.post("/api/scan/cancel", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/scan", data={"synthetic": "true"}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.get("/api/scan").json()["state"] == "done"
    r = client.get("/api/run")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_masked_page_preview_and_integrity(client):
    run = client.get("/api/run").json()
    assert run["integrity"]["ok"] and run["integrity"]["audit_records"] > 50
    assert run["pipeline"]["verified_pages"] == 3 and run["pipeline"]["verify_on"]
    assert {o["name"] for o in run["outputs"]["safe"]} >= {"run_manifest.json"}
    doc = client.get("/api/run/doc", params={"file": "risk_policy_scanned.pdf"}).json()
    assert doc["masked_preview"]
    a = client.get("/api/run/page", params={"file": "risk_policy_scanned.pdf", "page": 1})
    b = client.get("/api/run/page", params={"file": "risk_policy_scanned.pdf", "page": 1, "masked": "true"})
    assert b.status_code == 200 and b.content[:4] == a.content[:4] and a.content != b.content


def test_review_rewrites_outputs_and_rehydrate_is_logged(client):
    run = client.get("/api/run").json()
    queued = next(q for q in run["review_queue"] if q["value"] == "tprm-office@cadence-demo.example")
    person = next(f for f in run["findings"] if f["entity_type"] == "PERSON" and f["decision"] == "redact" and " " in f["text"])
    body = {"operator": "reviewer-1",
            "decisions": [{"entity_type": queued["entity_type"], "value": queued["value"], "action": "reject"}],
            "additions": [{"text": "Vendor Tier", "entity_type": "VENDOR_ID"}]}
    assert client.post("/api/run/review", json={}).status_code == 422
    assert client.post("/api/run/review", json={"additions": [{"text": "x1", "entity_type": "NOPE"}]}).status_code == 422
    r = client.post("/api/run/review", json=body)
    assert r.status_code == 200 and r.json()["kind"] == "review"
    s, _ = wait(client, lambda s: s["state"] != "running")
    assert s["state"] == "done", s
    after = client.get("/api/run").json()
    assert not [f for f in after["findings"] if f["text"] == queued["value"]]
    assert [f for f in after["dropped"] if f["text"] == queued["value"] and f["review"] == "rejected"]
    added = [f for f in after["findings"] if f["review"] == "added"]
    assert added and all(f["text"] == "Vendor Tier" and f["layer"] == "L5 reviewer" for f in added)
    assert after["reviews"][-1]["operator"] == "reviewer-1" and after["integrity"]["ok"]
    red = client.get("/api/run/doc", params={"file": "tprm_training.docx"}).json()["redacted"]
    assert queued["value"] in red and "Vendor Tier" not in red
    # tokens back to values, and the audit log says who asked
    r = client.post("/api/run/rehydrate", json={"text": f"Ask {person['token']} or [PERSON_999].", "purpose": "test",
                                                "operator": "reviewer-1"}).json()
    assert r["restored"] == [person["token"]] and r["unknown"] == ["[PERSON_999]"] and "[PERSON_999]" in r["text"]
    assert person["token"] not in r["text"]
    log = client.get("/api/run/output", params={"name": "audit_log.jsonl"}).text.splitlines()
    last = json.loads(log[-1])
    assert last["event"] == "reidentification" and last["tokens"] == [person["token"]] and last["operator"] == "reviewer-1"
    assert client.get("/api/run").json()["integrity"]["ok"]


def test_uploads_are_capped(client, monkeypatch):
    import server.api as api

    monkeypatch.setattr(api, "MAX_UPLOAD", 1024)
    r = client.post("/api/scan", files=[("files", ("big.txt", b"x" * 5000, "text/plain"))])
    assert r.status_code == 413
    assert client.get("/api/scan").json()["state"] == "done"  # nothing was started


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
    r = client.post("/api/scan", data={"synthetic": "true", "settings": json.dumps({"profile": "nope"})})
    assert r.status_code == 422
