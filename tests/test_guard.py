"""The prompt guard (optiv_pii_shield/guard.py, /api/guard): a prompt comes back with its values
replaced, tokens hold from one prompt to the next, and an answer gets its values back."""
import pytest
from fastapi.testclient import TestClient

from optiv_pii_shield import Settings
from optiv_pii_shield.guard import Guard, GuardBusy, to_document
from server.api import app, session

PROMPT = """Summarise this for the audit committee.

Priya Raman (EMP-40718) reported that the vendor portal was still open for Rafael Mendoza-Kowalski.
Her e-mail is priya.raman@cadence-demo.example and her phone is (415) 555-0142.
SSN: 900-12-3456
"""
VALUES = ["Priya Raman", "EMP-40718", "Rafael Mendoza-Kowalski", "priya.raman@cadence-demo.example", "(415) 555-0142",
          "900-12-3456"]


def test_blocks_keep_their_place_in_the_prompt():
    text = "one\ntwo\n\n\nthree  \n"
    doc, offsets = to_document(text)
    assert [s.text for s in doc.spans] == ["one\ntwo", "three  "]
    assert all(text[offsets[s.id]:offsets[s.id] + len(s.text)] == s.text for s in doc.spans)
    assert [s.location for s in doc.spans] == ["line 1", "line 5"]


def test_prompt_is_redacted_in_its_own_shape():
    r = Guard().check(PROMPT)
    assert r["verdict"] == "redacted" and not any(v in r["safe_text"] for v in VALUES)
    assert r["safe_text"].startswith("Summarise this for the audit committee.\n\n[PERSON_001] (")
    assert r["safe_text"].count("\n") == PROMPT.count("\n")
    assert {f["text"] for f in r["findings"]} >= set(VALUES)
    assert all(PROMPT[f["start"]:f["end"]] == f["text"] and f["token"] in r["safe_text"] for f in r["findings"])
    assert r["bytes"] == len(PROMPT.encode()) and r["by_category"]["PERSON"] == 2


def test_clean_prompt_is_returned_unchanged():
    text = "What is the difference between a mutex and a semaphore?"
    r = Guard().check(text)
    assert r["verdict"] == "clean" and r["safe_text"] == text and not r["findings"]


def test_tokens_hold_across_prompts_and_known_people_are_found_again():
    g = Guard()
    first = g.check(PROMPT)
    token = {f["text"]: f["token"] for f in first["findings"]}
    later = g.check("Draft a reply to Raman, cc Mendoza-Kowalski, and quote priya.raman@cadence-demo.example.")
    assert "Raman" not in later["safe_text"] and "Mendoza" not in later["safe_text"]
    assert token["Priya Raman"] in later["safe_text"] and token["Rafael Mendoza-Kowalski"] in later["safe_text"]
    assert token["priya.raman@cadence-demo.example"] in later["safe_text"]
    assert g.state()["checks"] == 2 and g.state()["people"] == 2


def test_answer_gets_its_values_back_and_only_issued_tokens():
    g = Guard()
    token = {f["text"]: f["token"] for f in g.check(PROMPT)["findings"]}
    r = g.rehydrate(f"{token['Priya Raman']} should call {token['(415) 555-0142']} or [PERSON_999].", "me", "test")
    assert r["text"] == "Priya Raman should call (415) 555-0142 or [PERSON_999]."
    assert r["unknown"] == ["[PERSON_999]"] and len(r["restored"]) == 2
    assert Guard().rehydrate(token["Priya Raman"])["restored"] == []  # another conversation knows nothing


def test_log_holds_counts_and_never_text():
    g = Guard()
    g.check(PROMPT)
    g.rehydrate("[PERSON_001]", "me", "test")
    log = str(g.state()["log"])
    assert [e["event"] for e in g.state()["log"]] == ["rehydrate", "check"]
    assert not any(v in log for v in VALUES)
    g.reset()
    assert g.state() == {"checks": 0, "tokens": 0, "people": 0, "blocked": 0, "restored": 0, "log": []}
    assert g.rehydrate("[PERSON_001]")["restored"] == []


def test_another_profile_starts_a_new_conversation():
    g = Guard()
    g.check(PROMPT)
    s = Settings()
    s.profile = "gdpr"
    r = g.check("Priya Raman was born on 12 March 1985.", s)
    assert r["restarted"] and "Priya Raman" not in r["safe_text"]


def test_detector_in_use_is_reported_not_waited_for():
    g = Guard()
    with g.lock, pytest.raises(GuardBusy):
        g.check("Priya Raman", wait=0.05)


# ------------------------------------------------------------------------------------ server
@pytest.fixture(scope="module")
def client():
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def test_guard_over_http(client):
    assert client.get("/api/guard").json()["checks"] == 0
    r = client.post("/api/guard/check", json={"text": PROMPT})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["verdict"] == "redacted" and not any(v in body["safe_text"] for v in VALUES)
    person = next(f["token"] for f in body["findings"] if f["text"] == "Priya Raman")
    back = client.post("/api/guard/rehydrate", json={"text": f"Ask {person}.", "purpose": "answer"}).json()
    assert back["text"] == "Ask Priya Raman." and back["restored"] == [person]
    state = client.get("/api/guard").json()
    assert state["checks"] == 1 and state["restored"] == 1 and state["tokens"] >= 6
    assert client.delete("/api/guard").json()["tokens"] == 0
    assert client.post("/api/guard/rehydrate", json={"text": person}).json()["restored"] == []


def test_guard_refuses_what_it_should(client):
    assert client.post("/api/guard/check", json={"text": "   "}).status_code == 422
    assert client.post("/api/guard/check", json={"text": "x", "settings": {"profile": "nope"}}).status_code == 422
    assert client.post("/api/guard/check", json={"text": "x" * 100_001}).status_code == 422
    assert client.post("/api/guard/check", json={"text": PROMPT}, headers={"origin": "https://evil.example"}).status_code == 403
    with session.guard.lock:  # as while a scan holds the detector
        session.guard.check, real = (lambda *a, **k: (_ for _ in ()).throw(GuardBusy())), session.guard.check
        try:
            assert client.post("/api/guard/check", json={"text": PROMPT}).status_code == 409
        finally:
            session.guard.check = real


def test_deleting_the_session_forgets_the_conversation(client):
    client.post("/api/guard/check", json={"text": PROMPT})
    assert client.get("/api/guard").json()["tokens"] > 0
    assert client.delete("/api/session").json() == {"state": "idle"}
    assert client.get("/api/guard").json() == {"checks": 0, "tokens": 0, "people": 0, "blocked": 0, "restored": 0, "log": []}
