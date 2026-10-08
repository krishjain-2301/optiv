"""The guard's record of what it did (feature: activity) and its look at the answer that comes back
(tokens the model rewrote, values it should not have, values it made up)."""
import json

import pytest
from fastapi.testclient import TestClient

from optiv_pii_shield import Settings, audit
from optiv_pii_shield.guard import Guard, activity
from server.api import app, session

PROMPT = "Priya Raman (EMP-40718) wrote to rafael.mendoza@cadence-demo.example about card 4111 1111 1111 1111."
CODE = "def f(x):\n    return x + 1\n\nprint(f(2))\nvalue = f(3)\n"


# --------------------------------------------------------------------------- the answer
@pytest.fixture(scope="module")
def conversation():
    g = Guard()
    token = {f["entity_type"]: f["token"] for f in g.check(PROMPT)["findings"]}
    return g, token


@pytest.mark.parametrize("written", ["[Person_001]", "[PERSON 001]", "[person-001]", "PERSON_001", "Person 001", "[ PERSON_001 ]"])
def test_a_token_the_model_rewrote_is_recognised(conversation, written):
    g, _ = conversation
    r = g.rehydrate(f"Ask {written}, please.", inspect=False)
    assert r["text"] == "Ask Priya Raman, please." and r["repaired"] == ["[PERSON_001]"] and r["restored"] == ["[PERSON_001]"]


def test_exact_tokens_are_not_reported_as_repaired_and_others_are_left(conversation):
    g, token = conversation
    r = g.rehydrate(f"{token['PERSON']} and {token['EMAIL_ADDRESS']}; not [PERSON_0011], PERSON_001x or [PERSON_077].", inspect=False)
    assert r["repaired"] == [] and r["unknown"] == ["[PERSON_0011]", "[PERSON_077]"]
    assert r["text"].startswith("Priya Raman and rafael.mendoza@cadence-demo.example; not [PERSON_0011], PERSON_001x")
    assert Guard().rehydrate("PERSON 001 and [Person_001]", inspect=False)["repaired"] == []  # nothing issued, nothing to repair


def test_the_answer_is_inspected_for_what_else_it_holds(conversation):
    g, token = conversation
    answer = (f"{token['PERSON']} should write again. As Priya Raman said, card {token['CREDIT_CARD']} is fine.\n"
              "Otherwise call Tom Baker on (212) 555-0199 and use password: Zx9!kLm2#Qw to log in.")
    r = g.rehydrate(answer)
    assert r["inspected"] and r["echoed"] == ["[PERSON_001]"]  # her name was in the answer although only the token was sent
    assert {(p["entity_type"], p["text"], p["line"]) for p in r["produced"]} == {
        ("PERSON", "Tom Baker", 2), ("PHONE_NUMBER", "(212) 555-0199", 2), ("CREDENTIAL", "Zx9!kLm2#Qw", 2)}
    assert "Tom Baker on (212) 555-0199" in r["text"] and "4111 1111 1111 1111" in r["text"]  # reported, never changed
    clean = g.rehydrate(f"{token['PERSON']} approved it on Friday.")
    assert clean["inspected"] and clean["echoed"] == [] and clean["produced"] == []


def test_the_look_at_the_answer_gives_way_to_a_scan(conversation):
    g, token = conversation
    with g.lock:  # as while a scan holds the detector
        r = g.rehydrate(f"{token['PERSON']} and Tom Baker.", wait=0.05)
    assert not r["inspected"] and r["text"] == "Priya Raman and Tom Baker." and r["produced"] == []


# --------------------------------------------------------------------------- the record
@pytest.fixture
def recorded(tmp_path):
    path = tmp_path / "guard_log.jsonl"
    g = Guard(record_path=path)
    dana = Settings()
    dana.operator = "dana"
    token = g.check(PROMPT)["findings"][0]["token"]
    g.rehydrate(f"{token} and Tom Baker", "sam", "answer")
    g.check(CODE, dana)
    g.check("What is a mutex?", dana)
    g.check("INTERNAL USE ONLY\n" + PROMPT, dana)
    return g, path


def test_every_check_and_restoration_is_recorded_without_text(recorded):
    g, path = recorded
    lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    assert [(e["event"], e.get("verdict")) for e in lines] == [("check", "redacted"), ("rehydrate", None), ("check", "blocked"),
                                                               ("check", "clean"), ("check", "blocked")]
    raw = path.read_text(encoding="utf-8")
    assert not any(v in raw for v in ("Priya", "Raman", "EMP-40718", "cadence-demo", "4111", "Tom Baker", "def f"))
    assert audit.verify_chain(path)[:2] == (True, 5)


def test_activity_counts_by_operator_day_rule_and_category(recorded):
    g, path = recorded
    a = activity(path)
    assert a["integrity"] == {"ok": True, "records": 5, "detail": ""} and a["path"] == str(path)
    t = a["totals"]
    assert (t["checks"], t["blocked"], t["redacted"], t["clean"], t["restored"], t["operators"]) == (4, 2, 1, 1, 1, 3)
    assert t["values"] == 4 and t["tokens_restored"] == 1 and t["produced"] == 1 and t["code_lines_stopped"] == 4
    who = {o["operator"]: o for o in a["by_operator"]}
    assert (who["dana"]["checks"], who["dana"]["blocked"], who["dana"]["clean"]) == (3, 2, 1) and who["sam"]["restored"] == 1
    assert a["by_rule"] == {"source_code": 1, "marking": 1}
    assert a["replaced_by_category"]["PERSON"] == 1 and a["stopped_by_category"]["CREDIT_CARD"] == 1
    assert len(a["by_day"]) == 1 and a["by_day"][0]["checks"] == 4
    assert [e["event"] for e in a["recent"]][:2] == ["check", "check"] and "hash" not in a["recent"][0]


def test_the_record_outlives_the_conversation_and_shows_tampering(recorded):
    g, path = recorded
    g.reset()
    assert g.state()["log"] == [] and activity(path)["totals"]["checks"] == 4
    Guard(record_path=path).check("What is a semaphore?")  # another conversation continues the same chain
    assert activity(path)["integrity"] == {"ok": True, "records": 6, "detail": ""}
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")  # the blocked code prompt is removed
    broken = activity(path)
    assert not broken["integrity"]["ok"] and "line 3" in broken["integrity"]["detail"] and broken["totals"]["checks"] == 4
    empty = activity(path.with_name("none.jsonl"))
    assert empty["integrity"]["ok"] and empty["totals"]["checks"] == 0 and empty["recent"] == []


# ------------------------------------------------------------------------------------ server
def test_activity_and_answer_inspection_over_http(tmp_path, monkeypatch):
    monkeypatch.setattr(session.guard, "record_path", tmp_path / "guard_log.jsonl")
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/guard/activity").json()["totals"]["checks"] == 0
        token = client.post("/api/guard/check", json={"text": PROMPT, "settings": {"operator": "dana"}}).json()["findings"][0]["token"]
        client.post("/api/guard/check", json={"text": CODE, "settings": {"operator": "dana"}})
        r = client.post("/api/guard/rehydrate", json={"text": f"{token.lower()} and Tom Baker.", "operator": "sam"}).json()
        assert r["text"] == "Priya Raman and Tom Baker." and r["repaired"] == [token] and r["inspected"]
        assert [p["text"] for p in r["produced"]] == ["Tom Baker"]
        a = client.get("/api/guard/activity").json()
        assert a["integrity"]["ok"] and a["totals"]["checks"] == 2 and a["totals"]["blocked"] == 1 and a["totals"]["restored"] == 1
        assert {o["operator"] for o in a["by_operator"]} == {"dana", "sam"}
        client.delete("/api/guard")
        client.delete("/api/session")
        assert client.get("/api/guard/activity").json()["totals"]["checks"] == 2  # neither deletes the record
        assert client.post("/api/guard/rehydrate", json={"text": "x", "settings": {"profile": "nope"}}).status_code == 422
