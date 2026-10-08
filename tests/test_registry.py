"""Protected content (optiv_pii_shield/registry.py): documents registered by fingerprint, and the
prompt guard refusing a prompt that copies from one."""
import json

import pytest
from fastapi.testclient import TestClient

from optiv_pii_shield.config import GuardPolicy
from optiv_pii_shield.guard import Guard
from optiv_pii_shield.registry import Registry
from server.api import app, session

NOTES = """Fab 3 weekly yield review, 14 March
- Line 4 yield fell from 91.2% to 86.7% after the recipe change on the second etch step
- The etch tool ET-07 is drifting and the measurement logs will be pulled before Thursday
- The customer shipment slips two weeks if yield stays under 88% through the end of the month
"""
COPIED = "Summarise this:\n- The etch tool ET-07 is drifting and the measurement logs will be pulled before Thursday"


@pytest.fixture
def reg(tmp_path):
    r = Registry(tmp_path / "registry.json")
    r.add("Fab 3 yield review", NOTES, "tester")
    return r


def test_a_document_is_kept_as_fingerprints_not_text(reg):
    stored = reg.path.read_text(encoding="utf-8")
    assert "yield" not in stored.replace("Fab 3 yield review", "") and "ET-07" not in stored and "drifting" not in stored
    doc = reg.list()[0]
    assert doc["name"] == "Fab 3 yield review" and doc["words"] == 59 and doc["fingerprints"] == 55 and doc["operator"] == "tester"
    assert set(json.loads(stored)) == {"format", "words_per_fingerprint", "salt", "documents"}


def test_copied_wording_is_recognised_whatever_its_case_and_layout(reg):
    hit = reg.match(COPIED)[0]
    assert hit["name"] == "Fab 3 yield review" and hit["longest"] == 16 and hit["lines"] == [[2, 2]]
    shouted = "THE ETCH TOOL et-07 IS DRIFTING,\nand the measurement logs... will be pulled before thursday!"
    assert reg.match(shouted)[0]["lines"] == [[1, 2]]


@pytest.mark.parametrize("text", [
    "An etch tool has drifted, so somebody is going to fetch the logs of the measurements later this week.",  # a paraphrase
    "the measurement logs will be pulled",  # five words in common: far too few
    "What is the capital of France, and why is it not Lyon given the history of the region?",
    "",
])
def test_other_text_does_not_overlap(reg, text):
    assert reg.match(text) == []


def test_registry_outlives_the_process_and_keeps_its_salt(reg):
    again = Registry(reg.path)
    assert again.list() == reg.list() and again.match(COPIED)[0]["words"] == 16
    other = Registry(reg.path.with_name("other.json"))
    other.add("same text, another registry", NOTES)
    assert other.prints[other.list()[0]["id"]].isdisjoint(reg.prints[reg.list()[0]["id"]])  # keyed: not comparable


def test_add_replace_remove_and_refuse(reg):
    first = reg.list()[0]["id"]
    reg.add("Fab 3 yield review (renamed)", NOTES.upper())  # the same words: replaces the entry
    assert [d["name"] for d in reg.list()] == ["Fab 3 yield review (renamed)"]
    with pytest.raises(ValueError):
        reg.add("too short", "only four words here")
    assert not reg.remove(first) and reg.remove(reg.list()[0]["id"]) and reg.list() == [] and reg.match(COPIED) == []
    assert Registry(reg.path).list() == []


def test_guard_refuses_a_prompt_that_copies_from_a_registered_document(reg):
    g = Guard(reg)
    r = g.check(COPIED)
    assert r["verdict"] == "blocked" and r["safe_text"] == "" and [b["rule"] for b in r["blocks"]] == ["protected"]
    assert "16 words in common" in r["blocks"][0]["detail"] and "line 2" in r["blocks"][0]["detail"]
    assert r["protected"][0]["name"] == "Fab 3 yield review"
    assert "Fab 3" not in str(g.state()["log"]) and g.state()["log"][0]["blocked_by"] == ["protected"]
    warned = Guard(reg).check(COPIED, policy=GuardPolicy(protected="warn"))
    assert warned["verdict"] != "blocked" and warned["warnings"][0]["rule"] == "protected" and "ET-07 is drifting" in warned["safe_text"]
    allowed = Guard(reg).check(COPIED, policy=GuardPolicy(protected="allow"))
    assert allowed["protected"] == [] and not allowed["warnings"]
    assert Guard(reg).check("What is the capital of France?")["verdict"] == "clean"
    assert Guard().check(COPIED)["verdict"] != "blocked"  # no registry, nothing to overlap


# ------------------------------------------------------------------------------------ server
def test_registry_over_http(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "registry", Registry(tmp_path / "registry.json"))
    monkeypatch.setattr(session.guard, "registry", session.registry)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/registry").json() == {"documents": [], "path": str(tmp_path / "registry.json")}
        assert client.post("/api/registry", data={"name": "x"}).status_code == 422
        r = client.post("/api/registry", data={"name": "Fab 3 yield review", "text": NOTES, "operator": "tester"},
                        files=[("files", ("spec.txt", ("Recipe R-118 holds the chamber at 4.2 mTorr for 38 seconds before the "
                                                       "overetch step begins and the endpoint is called.").encode(), "text/plain")),
                               ("files", ("tiny.txt", b"too short", "text/plain"))]).json()
        assert sorted(d["name"] for d in r["documents"]) == ["Fab 3 yield review", "spec.txt"]
        assert list(r["errors"]) == ["tiny.txt"] and "too short" in r["errors"]["tiny.txt"]
        assert not any(session.work.iterdir())  # the uploaded files are gone
        blocked = client.post("/api/guard/check", json={"text": "Why hold the chamber at 4.2 mTorr for 38 seconds before the "
                                                                "overetch step begins?"}).json()
        assert blocked["verdict"] == "blocked" and blocked["blocks"][0]["rule"] == "protected" and "spec.txt" in blocked["blocks"][0]["detail"]
        assert client.post("/api/registry", files=[("files", ("a.exe", b"MZ", "application/octet-stream"))]).status_code == 422
        doc = next(d for d in r["documents"] if d["name"] == "spec.txt")
        assert len(client.delete(f"/api/registry/{doc['id']}").json()["documents"]) == 1
        assert client.delete(f"/api/registry/{doc['id']}").status_code == 404
        assert client.delete("/api/session").status_code == 200  # the session goes, the registry stays
        assert len(client.get("/api/registry").json()["documents"]) == 1
        assert client.post("/api/guard/check", json={"text": COPIED, "policy": {"protected": "nope"}}).status_code == 422
        client.delete("/api/guard")
