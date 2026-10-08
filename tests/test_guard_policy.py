"""The prompt guard's policy: when a prompt is refused instead of having its values replaced
(config.GuardPolicy), classification markings (detect/markings.py) and confidential terms."""
import pytest
from fastapi.testclient import TestClient

from optiv_pii_shield import Settings
from optiv_pii_shield.config import ORG, GuardPolicy, load_org_config
from optiv_pii_shield.detect import markings
from optiv_pii_shield.guard import Guard
from server.api import app

CODE = "def f(x):\n    y = x + 1\n    return y * 2\n\nprint(f(3))\n"
PROSE = "Priya Raman approved the Q3 budget."


# ------------------------------------------------------------------------------ markings
@pytest.mark.parametrize("text, marking", [
    ("CONFIDENTIAL", "CONFIDENTIAL"),
    ("Confidential", "Confidential"),
    ("Classification: Restricted", "Restricted"),
    ("[Confidential] Q3 plan for the board", "Confidential"),
    ("Cadence - Internal Use Only", "Internal Use Only"),
    ("this is strictly confidential, please summarise it", "strictly confidential"),
    ("DO NOT DISTRIBUTE", "DO NOT DISTRIBUTE"),
    ("Minutes of the meeting\nRESTRICTED\nAttendees: three", "RESTRICTED"),
])
def test_markings_are_found(text, marking):
    assert [m["text"] for m in markings.find(text, ORG["markings"])] == [marking]


@pytest.mark.parametrize("text", [
    "The report was confidential until last year, when it was published.",
    "Access is restricted to administrators.",
    "export AWS_SECRET_ACCESS_KEY=abc",
    "Proprietary algorithms are common in this field of research and in industry today.",
    "What is the capital of France?",
])
def test_ordinary_words_are_not_markings(text):
    assert markings.find(text, ORG["markings"]) == []


def test_marking_line_is_reported():
    found = markings.find("Hello\n\nInternal Use Only\nQ3 plan", ORG["markings"])
    assert found == [{"line": 3, "text": "Internal Use Only", "why": "a classification phrase"}]
    assert markings.find("CONFIDENTIAL", []) == []


# -------------------------------------------------------------------------------- policy
def test_default_policy_is_the_organisations():
    p = GuardPolicy.default()
    assert (p.source_code, p.markings, p.block_categories, p.max_bytes, p.protected) == ("block", "block", [], 0, "block")
    with pytest.raises(ValueError):
        GuardPolicy(source_code="maybe")
    with pytest.raises(ValueError):
        GuardPolicy(max_bytes=-1)


def test_source_code_is_blocked_by_default_and_yields_no_text():
    g = Guard()
    r = g.check(CODE)
    assert r["verdict"] == "blocked" and r["safe_text"] == "" and [b["rule"] for b in r["blocks"]] == ["source_code"]
    assert "lines 1 to 5" in r["blocks"][0]["detail"] and "Python" in r["blocks"][0]["detail"]
    assert g.state()["blocked"] == 1 and g.state()["log"][0]["blocked_by"] == ["source_code"]


def test_warn_and_allow_let_code_through_with_values_replaced():
    code = CODE + 'owner = "Priya Raman"\n'
    warned = Guard().check(code, policy=GuardPolicy(source_code="warn"))
    assert warned["verdict"] == "redacted" and [w["rule"] for w in warned["warnings"]] == ["source_code"] and not warned["blocks"]
    assert "Priya Raman" not in warned["safe_text"] and "return y * 2" in warned["safe_text"]
    allowed = Guard().check(code, policy=GuardPolicy(source_code="allow"))
    assert allowed["verdict"] == "redacted" and not allowed["warnings"] and allowed["code"]["detected"]


def test_marking_blocks_and_can_be_downgraded():
    text = "CONFIDENTIAL\n\n" + PROSE
    r = Guard().check(text)
    assert r["verdict"] == "blocked" and r["blocks"][0]["rule"] == "marking" and r["markings"][0]["line"] == 1
    r = Guard().check(text, policy=GuardPolicy(markings="warn"))
    assert r["verdict"] == "redacted" and r["warnings"][0]["rule"] == "marking" and "Priya Raman" not in r["safe_text"]


def test_blocked_category_refuses_the_prompt():
    text = "Refund card 4111 1111 1111 1111 for Priya Raman."
    r = Guard().check(text, policy=GuardPolicy(block_categories=["CREDIT_CARD"]))
    assert r["verdict"] == "blocked" and r["blocks"] == [{"rule": "category", "category": "CREDIT_CARD",
                                                           "detail": "1 value(s) of a category that must not be sent"}]
    assert Guard().check(text)["verdict"] == "redacted"
    assert Guard().check(PROSE, policy=GuardPolicy(block_categories=["CREDIT_CARD"]))["verdict"] == "redacted"


def test_size_limit_refuses_unread():
    r = Guard().check("Priya Raman " * 200, policy=GuardPolicy(max_bytes=1024))
    assert r["verdict"] == "blocked" and r["blocks"][0]["rule"] == "size" and r["findings"] == [] and not r["components"]
    assert Guard().check(PROSE, policy=GuardPolicy(max_bytes=1024))["verdict"] == "redacted"


def test_a_blocked_prompt_issues_no_tokens_and_remembers_nobody():
    g = Guard()
    r = g.check("CONFIDENTIAL\n\n" + PROSE)
    assert [(f["text"], f["token"]) for f in r["findings"]] == [("Priya Raman", None)]
    assert g.state()["tokens"] == 0 and g.state()["people"] == 0
    assert g.rehydrate("[PERSON_001]")["restored"] == []
    assert "Priya Raman" not in str(g.state()["log"])


def test_several_reasons_are_all_given():
    r = Guard().check("INTERNAL USE ONLY\n" + CODE, policy=GuardPolicy(max_bytes=0))
    assert sorted(b["rule"] for b in r["blocks"]) == ["marking", "source_code"]


# ------------------------------------------------------------------- confidential terms
def test_confidential_terms_are_replaced_in_any_case():
    s = Settings()
    s.confidential_terms = ["Project Falcon", "Bluebird"]
    r = Guard().check("Project Falcon ships in May; ask Priya Raman about PROJECT  FALCON and bluebird; falconry is a hobby.", s)
    assert r["verdict"] == "redacted" and r["by_category"]["CONFIDENTIAL_TERM"] == 3
    assert r["safe_text"] == "[TERM_U001] ships in May; ask [PERSON_001] about [TERM_U001] and [TERM_U002]; falconry is a hobby."


def test_confidential_term_can_refuse_the_prompt():
    s = Settings()
    s.confidential_terms = ["Bluebird"]
    r = Guard().check("When does Bluebird launch?", s, GuardPolicy(block_categories=["CONFIDENTIAL_TERM"]))
    assert r["verdict"] == "blocked" and r["blocks"][0]["category"] == "CONFIDENTIAL_TERM"
    plain = Guard().check("When does Bluebird launch?", policy=GuardPolicy(block_categories=["CONFIDENTIAL_TERM"]))
    assert plain["verdict"] != "blocked" and "CONFIDENTIAL_TERM" not in plain["by_category"]  # not a term unless configured


def test_org_config_carries_terms_markings_and_policy(tmp_path):
    p = tmp_path / "org.yaml"
    p.write_text("confidential_terms: [Bluebird]\nmarkings: [Eyes Only]\nguard_policy: {source_code: warn, max_bytes: 4096}\n",
                 encoding="utf-8")
    org = load_org_config(p)
    assert org["confidential_terms"] == ["Bluebird"] and org["markings"] == ["Eyes Only"]
    assert GuardPolicy(**org["guard_policy"]).source_code == "warn"
    p.write_text("guard_policy: {sourcecode: warn}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_org_config(p)
    assert "Confidential" in ORG["markings"] and ORG["confidential_terms"] == []  # the bundled configuration


# ------------------------------------------------------------------------------------ server
@pytest.fixture(scope="module")
def client():
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def test_policy_over_http(client):
    meta = client.get("/api/settings").json()
    assert meta["guard_policy"] == {"source_code": "block", "markings": "block", "block_categories": [], "max_bytes": 0,
                                    "protected": "block"}
    assert "Confidential" in meta["markings"] and "CONFIDENTIAL_TERM" in meta["entities"]
    r = client.post("/api/guard/check", json={"text": CODE}).json()
    assert r["verdict"] == "blocked" and r["safe_text"] == ""
    r = client.post("/api/guard/check", json={"text": CODE, "policy": {"source_code": "allow"}}).json()
    assert r["verdict"] == "clean" and r["safe_text"] == CODE
    r = client.post("/api/guard/check", json={"text": "Ask about Bluebird.", "settings": {"confidential_terms": ["Bluebird"]}}).json()
    assert r["safe_text"] == "Ask about [TERM_U001]."
    assert client.get("/api/guard").json()["blocked"] == 1
    assert client.post("/api/guard/check", json={"text": "x", "policy": {"source_code": "maybe"}}).status_code == 422
    assert client.post("/api/guard/check", json={"text": "x", "policy": {"block_categories": ["NOPE"]}}).status_code == 422
    assert client.post("/api/guard/check", json={"text": "x", "policy": {"max_bytes": -5}}).status_code == 422
    client.delete("/api/guard")
