"""Source code and the credentials inside it (detect/code.py, detect/secrets.py, the L1 secret rules,
and what the prompt guard reports about them)."""
import pytest

from optiv_pii_shield.config import GuardPolicy
from optiv_pii_shield.detect import secrets as sec
from optiv_pii_shield.detect.code import analyse
from optiv_pii_shield.detect.rules import run_rules
from optiv_pii_shield.guard import Guard


def secrets_in(text):
    return {text[r.start:r.end] for r in run_rules(text) if r.entity_type == "CREDENTIAL" and r.score >= 0.35}


# ------------------------------------------------------------------------------- secrets
@pytest.mark.parametrize("line, value", [
    ('db_password = "S3cr3t!Pass#2024"', "S3cr3t!Pass#2024"),
    ("DB_PASSWORD=S3cr3tPass2024", "S3cr3tPass2024"),
    ("export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    ("config[\"apiKey\"] = 'k9Zq2Lm8Xw4Tn6Vb1Rc3'", "k9Zq2Lm8Xw4Tn6Vb1Rc3"),
    ('"client_secret": "a8f3K2m9Qz7Lp4Xw",', "a8f3K2m9Qz7Lp4Xw"),
    ("DATABASE_URL=postgres://app_user:Pr0d!Pass99@db.internal.example:5432/orders", "Pr0d!Pass99"),
    ('conn = "mongodb+srv://admin:hunter2secret@cluster0.mongodb.net/test"', "hunter2secret"),
    ("Authorization: Bearer abcDEF123456ghiJKL789012mnoPQR", "abcDEF123456ghiJKL789012mnoPQR"),
    ('headers = {"X-API-Key": "9f8e7d6c5b4a39281706f5e4d3c2b1a0"}', "9f8e7d6c5b4a39281706f5e4d3c2b1a0"),
    ("token glpat-AbCdEfGhIjKlMnOpQrSt12 was pushed", "glpat-AbCdEfGhIjKlMnOpQrSt12"),
    ('signature = "Zm9vYmFyMTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1OT1A="', "Zm9vYmFyMTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1OT1A="),
])
def test_secrets_in_code_and_config(line, value):
    assert secrets_in(line) == {value}


@pytest.mark.parametrize("line", [
    'password = os.environ["DB_PASSWORD"]',
    'password = "<your-password>"',
    'password_field = "pwd_input"',
    'max_tokens = "4096abcd"',
    'tokenizer = "bert-base-uncased"',
    "token = get_token()",
    'api_key = "[SECRET_U001]"',  # the guard's own token, checked a second time
    'content_type = "application/x-www-form-urlencoded"',
    "The password policy requires twelve characters.",
    "Please reset my token if it has expired.",
])
def test_names_and_placeholders_that_are_not_secrets(line):
    assert secrets_in(line) == set()


def test_url_password_is_not_read_as_an_email():
    hits = [(r.entity_type, r.score) for r in run_rules("postgres://app_user:Pr0d!Pass99@db.internal.example:5432/x")]
    assert ("CREDENTIAL", 1.0) in hits and not [h for h in hits if h[0] == "EMAIL_ADDRESS"]
    assert [r.entity_type for r in run_rules("write to ops@example.com")] == ["EMAIL_ADDRESS"]


def test_private_key_with_and_without_its_end_line():
    body = "\n".join(["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj"] * 4)
    whole = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
    assert whole in secrets_in(whole)  # the body alone matches too; the resolver merges the two
    cut = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{body}"
    assert any(s.startswith("-----BEGIN OPENSSH") for s in secrets_in(cut))
    assert secrets_in(body + "\n")  # the body alone, pasted without its BEGIN line


def test_secret_names():
    assert sec.words("AWS_SECRET_ACCESS_KEY") == ["aws", "secret", "access", "key"] and sec.words("dbPassword") == ["db", "password"]
    assert all(sec.is_secret_name(n) for n in ("dbPassword", "API_KEY", "client.secret", "signing-key", "passwd"))
    assert not any(sec.is_secret_name(n) for n in ("password_file", "token_url", "max_tokens", "tokenizer", "keyboard", "primary_key_id"))
    assert sec.entropy("aaaaaaaa") == 0 and sec.entropy("abcdefgh") == 3


# ---------------------------------------------------------------------------------- code
PYTHON = '''Can you fix this bug? It returns None for some wafers.

def load_measurements(db, wafer_id):
    rows = db.query("SELECT * FROM measurements WHERE wafer_id = %s", wafer_id)
    result = []
    for row in rows:
        if row.status == "ok":
            result.append(row.value)
    return result
'''
CODE = {
    "Python": PYTHON,
    "JavaScript / TypeScript": 'const app = express();\napp.get("/users/:id", async (req, res) => {\n  res.json(await db.users.findOne({ id: req.params.id }));\n});\n',
    "Java / C#": "public class YieldCalculator {\n    public double compute(int good, int total) {\n        return (double) good / total;\n    }\n}\n",
    "SQL": "SELECT e.name, d.name\nFROM employees e\nINNER JOIN departments d ON e.dept_id = d.id\nWHERE e.salary > 100000;\n",
    "Configuration": "DB_HOST=db.internal.example\nDB_USER=app_user\nDB_PASSWORD=S3cr3tPass2024\nDEBUG=false\n",
    "Shell": "$ git clone https://github.com/acme/tool.git\n$ cd tool\n$ pip install -r requirements.txt\n",
}
PROSE = [
    "Summarise this incident for the audit committee in five bullet points.\n\nPriya Raman reported that the vendor portal was "
    "still open three weeks\nafter the contract ended. Raman escalated it to the access team the same day.\n",
    "Meeting notes, 3 October\nAttendees: Priya Raman, Tom Lee\nAgenda:\n- Yield on line 4: dropped to 87% after the recipe change\n"
    "- Action: Rafael to roll back by Friday\nNext meeting: 10 October\n",
    "Hi team,\n\nRevenue = 4.2M, up 8% on Q2; costs were flat.\nLet me know if you have questions (I am out on Friday).\n\nThanks,\nPriya\n",
    "What does print(len(x)) do in Python?",
    "Things to buy:\n1. Milk\n2. Eggs; bread; butter\n3. Coffee (two bags)\n",
]


@pytest.mark.parametrize("language", CODE)
def test_code_is_recognised(language):
    r = analyse(CODE[language])
    assert r["detected"] and r["languages"][0] == language and r["code_lines"] >= 3


@pytest.mark.parametrize("text", PROSE)
def test_prose_is_not_code(text):
    assert analyse(text) == {"detected": False, "lines": sum(bool(ln.strip()) for ln in text.split("\n")), "code_lines": 0,
                             "share": 0.0, "languages": [], "blocks": []}


def test_code_block_is_located_and_a_fence_is_always_code():
    r = analyse(PYTHON)
    assert r["blocks"] == [[3, 9]] and r["lines"] == 8 and r["code_lines"] == 7
    fenced = analyse("Why does this fail?\n\n```\nx <- c(1, 2, 3)\nmean(x)\n```\n")
    assert fenced["detected"] and fenced["blocks"] == [[4, 5]]


# --------------------------------------------------------------------------------- guard
STRIPE = "sk_" + "live_" + "4f9a8b7c6d5e4f3a2b1c0d9e"  # made up; split so no scanner reads it as a live key
FLASK = '''# Author: Priya Raman <priya.raman@cadence-demo.example>
from flask import Flask

app = Flask(__name__)
DB_URL = "postgres://app_user:Pr0d!Pass99@db.internal.example:5432/orders"
STRIPE_KEY = "STRIPE_VALUE"
api_token = os.environ["API_TOKEN"]

def get_order(order_id):
    conn = psycopg2.connect(DB_URL)
    return {"order": conn.fetch(order_id), "support": "+1 (415) 555-0142"}
'''.replace("STRIPE_VALUE", STRIPE)


def test_guard_redacts_inside_code_and_leaves_the_code_working():
    warn = GuardPolicy(source_code="warn")  # the default policy refuses code outright (test_guard_policy.py)
    r = Guard().check(FLASK, policy=warn)
    assert r["code"]["detected"] and r["code"]["languages"] == ["Python"] and r["code_lines"] == r["code"]["code_lines"] > 5
    safe = r["safe_text"]
    for value in ("Priya Raman", "priya.raman@cadence-demo.example", "Pr0d!Pass99", STRIPE, "555-0142"):
        assert value not in safe
    # quotes, the URL around the password and every line of code are still there
    assert 'DB_URL = "postgres://app_user:[SECRET_U001]@db.internal.example:5432/orders"' in safe
    assert 'STRIPE_KEY = "[SECRET_U002]"' in safe and 'api_token = os.environ["API_TOKEN"]' in safe
    assert safe.count("\n") == FLASK.count("\n") and "def get_order(order_id):" in safe
    again = Guard().check(safe, policy=warn)  # the safe text holds nothing further: tokens are not secrets
    assert again["safe_text"] == safe


def test_guard_reports_no_code_for_prose():
    r = Guard().check(PROSE[0])
    assert not r["code"]["detected"] and r["code_lines"] == 0
