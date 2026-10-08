"""The Samsung replay (optiv_pii_shield/replay.py): the three incidents and two contrast cases
under a size cap and under the prompt guard."""
import pytest
from fastapi.testclient import TestClient

from optiv_pii_shield import replay
from optiv_pii_shield.config import GuardPolicy
from server.api import app, session
from server.session import Job


@pytest.fixture(scope="module")
def result():
    return replay.run()


def by_id(result, sid):
    return next(x for x in result["scenarios"] if x["id"] == sid)


def test_the_cap_is_right_once_and_the_guard_every_time(result):
    assert result["cap_bytes"] == 1024 and result["summary"] == {
        "scenarios": 5, "cap_right": 1, "guard_right": 5, "cap_leaks": 3, "guard_leaks": 0,
        "cap_refused_harmless": 1, "guard_refused_harmless": 0}


def test_incidents_one_and_two_are_refused_as_source_code(result):
    one, two = by_id(result, "incident-1"), by_id(result, "incident-2")
    assert one["bytes"] > 1024 and one["cap"]["verdict"] == "blocked"  # a whole file: the one case the cap stops
    assert two["bytes"] < 1024 and two["cap"] == {"verdict": "allowed", "right": False,
                                                  "outcome": f"Sent as written: {two['bytes']:,} bytes fits under the cap"}
    for x in (one, two):
        assert x["guard"]["verdict"] == "blocked" and x["guard"]["safe_text"] == ""
        assert [b["rule"] for b in x["guard"]["blocks"]] == ["source_code"] and x["guard"]["languages"] == ["Python"]
    assert one["guard"]["by_category"]["CREDENTIAL"] == 1  # the database password was seen as well


def test_incident_three_goes_out_without_the_people_or_the_codename(result):
    x = by_id(result, "incident-3")
    safe = x["guard"]["safe_text"]
    assert x["cap"]["verdict"] == "allowed" and x["guard"]["verdict"] == "redacted"
    for value in ("Dana", "Whitlock", "Marcus", "Oyelaran", "Ines", "Carvalho", "Halcyon", "555-0177", "@northfab.example"):
        assert value not in safe
    assert "86.7%" in safe and "ET-07" in safe  # what was discussed is still there: stated on the page
    assert x["guard"]["by_category"]["CONFIDENTIAL_TERM"] == 2


def test_a_short_secret_fits_under_the_cap_and_a_long_harmless_prompt_does_not(result):
    short, long = by_id(result, "short-secret"), by_id(result, "long-harmless")
    assert short["bytes"] < 300 and short["cap"]["verdict"] == "allowed" and short["guard"]["verdict"] == "redacted"
    assert "Tr1ton" not in short["guard"]["safe_text"] and "password is [SECRET_U001]. Why" in short["guard"]["safe_text"]
    assert long["bytes"] > 1024 and long["cap"]["verdict"] == "blocked" and not long["sensitive"]
    assert long["guard"]["verdict"] == "clean" and long["guard"]["safe_text"] == long["prompt"]


def test_cap_and_policy_can_be_varied():
    wide = replay.run(cap=100_000)
    assert wide["summary"]["cap_leaks"] == 4 and wide["summary"]["cap_refused_harmless"] == 0
    lax = replay.run(policy=GuardPolicy(source_code="allow"))
    one = by_id(lax, "incident-1")
    assert one["guard"]["verdict"] == "redacted" and "Wafer!Lot42" not in one["guard"]["safe_text"]
    assert "def load_wafer(lot_id, wafer_no):" in one["guard"]["safe_text"]


def test_replay_over_http_leaves_the_conversation_alone():
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        before = client.get("/api/guard").json()
        r = client.get("/api/guard/replay", params={"cap": 2048})
        assert r.status_code == 200 and r.json()["cap_bytes"] == 2048 and len(r.json()["scenarios"]) == 5
        assert client.get("/api/guard").json() == before
        assert client.get("/api/guard/replay", params={"cap": 0}).status_code == 422
        session.job, before = Job(files=["x.pdf"]), session.job  # as while a scan holds the detector
        try:
            assert client.get("/api/guard/replay").status_code == 409
        finally:
            session.job = before
