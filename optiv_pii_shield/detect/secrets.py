"""Credentials in source code and configuration: helpers for the L1 rules.

A pattern alone cannot tell ``db_password = "S3cr3t!Pass"`` from ``password_field = "pwd"`` or
``token = os.environ["TOKEN"]``. These checks look at the name on the left (is it a secret's
name?) and at the value on the right (a literal, or a placeholder or a reference?).
"""
from __future__ import annotations

import math
import re
from collections import Counter

Result = tuple[float, str]

SECRET_WORDS = {"password", "passwd", "pwd", "passphrase", "secret", "secrets", "token", "apikey", "credential",
                "credentials", "authtoken", "accesskey", "secretkey", "privatekey"}
SECRET_PAIRS = {("api", "key"), ("access", "key"), ("private", "key"), ("signing", "key"), ("encryption", "key"),
                ("master", "key"), ("account", "key"), ("shared", "key"), ("auth", "key"), ("session", "key")}
# "password_file", "token_url", "max_tokens": the name mentions a secret but holds something else.
NOT_THE_SECRET = {"file", "path", "dir", "url", "uri", "name", "field", "header", "label", "prompt", "hint", "length",
                  "len", "min", "max", "policy", "expiry", "expires", "ttl", "type", "id", "env", "var", "param",
                  "placeholder", "regex", "pattern", "column", "input", "count", "limit", "endpoint", "required",
                  "enabled", "valid", "reset", "strength", "rules", "format", "prefix", "suffix"}
PLACEHOLDER = re.compile(
    r"^(?:<[^>]*>|\$\{?\w+\}?|\{\{.*\}\}|%\w+%|x{3,}|\*{3,}|\.{3,}|-+|none|null|nil|true|false|undefined|todo|tbd|n/?a|"
    r"changeme|change[_-]me|placeholder|redacted|example|sample|test|your[_ -].*|my[_ -]?(?:password|secret|token|key)|"
    r"password|secret|token|\[[A-Z][A-Z_]*(?:_[A-Z0-9*]+)*\])$", re.IGNORECASE)
REFERENCE = re.compile(r"os\.environ|getenv|process\.env|\$\(|\(\)$|^[A-Za-z_][\w.]*\[")


def words(identifier: str) -> list[str]:
    """"dbPassword" -> ["db", "password"]; "AWS_SECRET_ACCESS_KEY" -> ["aws", "secret", "access", "key"]."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", identifier)
    return [w.lower() for w in re.split(r"[\s_.\-]+", spaced) if w]


def is_secret_name(identifier: str) -> bool:
    ws = words(identifier)
    if any(w in NOT_THE_SECRET for w in ws):
        return False
    return any(w in SECRET_WORDS for w in ws) or any(pair in SECRET_PAIRS for pair in zip(ws, ws[1:]))


def named_secret(m: re.Match) -> bool:
    return is_secret_name(m.group("k"))


def entropy(s: str) -> float:
    """Shannon entropy in bits per character."""
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def _classes(v: str) -> int:
    return sum(bool(re.search(p, v)) for p in (r"\d", r"[a-z]", r"[A-Z]", r"[^A-Za-z0-9]"))


def check_secret_value(value: str) -> Result:
    v = value.strip()
    if PLACEHOLDER.match(v) or REFERENCE.search(v):
        return -0.5, "a placeholder or a reference, not a literal secret"
    if len(v) >= 8 and _classes(v) >= 3:
        return 0.2, "mixed character classes"
    if len(v) >= 12 and entropy(v) >= 3.5:
        return 0.15, f"high entropy ({entropy(v):.1f} bits/char)"
    return 0.0, ""


def mixed(m: re.Match) -> bool:
    """A random-looking string: long hex, or digits with both letter cases. Not a word or a path."""
    v = m.group("v")
    if re.fullmatch(r"[0-9a-fA-F]{32,}", v):
        return bool(re.search(r"\d", v) and re.search(r"[a-fA-F]", v))
    return _classes(re.sub(r"[^A-Za-z0-9]", "", v)) == 3


def check_entropy(value: str) -> Result:
    h = entropy(value)
    floor = 3.0 if re.fullmatch(r"[0-9a-fA-F]+", value) else 4.0
    if h < floor:
        return -0.3, f"low entropy ({h:.1f} bits/char)"
    return 0.2, f"random-looking string ({h:.1f} bits/char)"
