"""Stable pseudonymous tokens, consistent across every file in a run.

Each person gets a number; their e-mail, phone and IDs reuse it when they can be linked (same
table row, or the e-mail's local part matches the name), so an LLM can still reason about
"who did what": ``[PERSON_007]`` and ``[EMAIL_007]`` are the same individual.

With a token key (``Settings.token_key``) the number is replaced by an HMAC-SHA256 of the value,
so the same person or value gets the same token in every run, and nobody without the key can
compute a token from a guessed value.

A redaction profile (``config.PROFILES``) can replace a category by something other than a token:
the category alone, the last four digits, or the year of a date.

The vault (token -> original value) is the only place the originals survive. It is written to
the output folder only when a passphrase is given, and then only encrypted (AES-256-GCM, key
derived with scrypt). Without a passphrase it is never written: the originals do not persist.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from ..config import PROFILES
from ..detect.names import name_tokens
from ..detect.propagation import PersonIndex
from ..models import Document, Finding

SHORT = {
    "PERSON": "PERSON", "EMAIL_ADDRESS": "EMAIL", "PHONE_NUMBER": "PHONE", "EMPLOYEE_ID": "EMP_ID",
    "VENDOR_ID": "VENDOR_ID", "US_SSN": "SSN", "PASSPORT": "PASSPORT", "IN_PAN": "PAN", "PL_PESEL": "PESEL",
    "TAX_ID": "TIN", "NATIONAL_ID": "NATIONAL_ID", "CREDIT_CARD": "CARD", "IBAN_CODE": "IBAN",
    "DATE_OF_BIRTH": "DOB", "ADDRESS": "ADDRESS", "LOW_CONFIDENCE_OCR": "UNREADABLE", "IN_AADHAAR": "AADHAAR",
    "IP_ADDRESS": "IP", "CREDENTIAL": "SECRET", "BANK_ACCOUNT": "BANK", "UPI_ID": "UPI", "DRIVING_LICENCE": "DL",
    "IN_VOTER_ID": "VOTER_ID", "UK_NINO": "NINO", "HEALTH_DATA": "HEALTH", "CONFIDENTIAL_TERM": "TERM",
}
LINKABLE = {"EMAIL_ADDRESS", "PHONE_NUMBER", "EMPLOYEE_ID", "US_SSN", "PASSPORT", "IN_PAN", "PL_PESEL", "TAX_ID",
            "DATE_OF_BIRTH", "ADDRESS", "NATIONAL_ID", "IN_AADHAAR", "BANK_ACCOUNT", "UPI_ID", "DRIVING_LICENCE",
            "IN_VOTER_ID", "UK_NINO", "HEALTH_DATA"}


def normalise(entity: str, value: str) -> str:
    v = value.strip()
    if entity in ("EMAIL_ADDRESS", "UPI_ID"):
        return re.sub(r"\s", "", v.lower())
    if entity in ("PHONE_NUMBER", "CREDIT_CARD", "US_SSN", "IN_AADHAAR") or (
            entity in ("TAX_ID", "BANK_ACCOUNT") and not re.search(r"[A-Za-z]", v)):
        return re.sub(r"\D", "", v)
    if entity == "PERSON":
        return " ".join(name_tokens(v)).lower()
    return re.sub(r"\s+", " ", v.upper())


class TokenVault:
    def __init__(self, persons: PersonIndex | None = None, key: str | None = None, profile: str = "default"):
        self.persons = persons or PersonIndex()
        self.key = key.encode("utf-8") if key else None  # set: tokens derive from the value, stable across runs
        self.actions: dict[str, str] = PROFILES.get(profile, PROFILES["default"])["actions"]
        self.person_no: dict[str, int | str] = {}  # canonical person -> number (or keyed id)
        self.tokens: dict[tuple[str, str], str] = {}  # (entity, normalised value) -> token
        self.values: dict[str, set[str]] = defaultdict(set)  # token -> original surface forms
        self.entity_of: dict[str, str] = {}  # token -> category
        self.linked: set[str] = set()  # tokens that carry a person's number
        self.counters: dict[str, int] = defaultdict(int)

    # ---------------------------------------------------------------------------------
    def _digest(self, what: str, n: int = 8) -> str:
        return hmac.new(self.key, what.encode("utf-8"), hashlib.sha256).hexdigest()[:n].upper()

    def _person_id(self, canonical: str) -> str:
        if canonical not in self.person_no:
            self.person_no[canonical] = self._digest("PERSON:" + canonical.lower()) if self.key else len(self.person_no) + 1
        n = self.person_no[canonical]
        return n if isinstance(n, str) else f"{n:03d}"

    def person_for_email(self, email: str) -> str | None:
        local = re.sub(r"[^a-z]", " ", email.split("@")[0].lower()).split()
        joined = "".join(local)
        if not joined:
            return None
        for canonical in self.person_no:
            toks = [t.lower().rstrip(".") for t in name_tokens(canonical)]
            if len(toks) < 2:
                continue
            first, last = toks[0], re.sub(r"[^a-z]", "", toks[-1])
            if not first or not last:
                continue
            forms = {first + last, first[0] + last, last + first[0], last + first, first + "." + last}
            if joined in {f.replace(".", "") for f in forms} or (last in joined and first[0] == joined[0]):
                return canonical
        return None

    def _new_token(self, entity: str, key: tuple[str, str], owner: str | None) -> str:
        short = SHORT.get(entity, entity)
        if self.key:
            # Derived from the value alone, so the same value gets the same token in every run.
            value_id = self._digest(f"{entity}:{key[1]}")
            if owner is not None:
                return f"[{short}_{self._person_id(owner)}_{value_id[:4]}]"
            return f"[{short}_U{value_id}]"
        if owner is not None:
            base = f"{short}_{self._person_id(owner)}"
            existing = [t for t in self.tokens.values() if t.startswith(f"[{base}")]
            return f"[{base}]" if not existing else f"[{base}_{len(existing) + 1}]"
        self.counters[short] += 1
        return f"[{short}_U{self.counters[short]:03d}]"

    def _replacement(self, entity: str, value: str, token: str) -> str:
        """What stands in for the value in outputs, by the profile's action for its category."""
        action = self.actions.get(entity, "token")
        short = SHORT.get(entity, entity)
        if action == "mask":
            return f"[{short}]"
        if action == "last4":
            d = re.sub(r"\D", "", value)
            return f"[{short}_****{d[-4:]}]" if len(d) >= 8 else f"[{short}]"
        if action == "year":
            years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", value)
            return f"[{short}_{years[-1]}]" if years else f"[{short}]"
        return token

    def assign(self, f: Finding, row_person: str | None = None) -> str:
        entity = f.entity_type
        key = (entity, normalise(entity, f.text))
        if entity == "PERSON":
            canonical = self.persons.canonical(f.text)
            key = (entity, canonical.lower())
            if key not in self.tokens:
                self.tokens[key] = f"[PERSON_{self._person_id(canonical)}]"
        elif key not in self.tokens:
            owner = None
            if entity in LINKABLE:
                owner = row_person or (self.person_for_email(f.text) if entity == "EMAIL_ADDRESS" else None)
            self.tokens[key] = self._new_token(entity, key, owner)
            if owner is not None:
                self.linked.add(self.tokens[key])
        token = self._replacement(entity, f.text, self.tokens[key])
        if token != self.tokens[key] and self.tokens[key] in self.linked:
            self.linked.add(token)
        self.values[token].add(f.text)
        self.entity_of[token] = entity
        f.token = token
        return token

    # ---------------------------------------------------------------------------------
    def assign_all(self, docs: dict[str, Document], findings: dict[str, list[Finding]]) -> None:
        """People first (so numbers follow first appearance), then everything else, linking by table row."""
        live = [f for fs in findings.values() for f in fs if f.decision in ("redact", "review")]
        for f in live:
            if f.entity_type == "PERSON":
                self.assign(f)
        row_owner: dict[tuple, str] = {}
        for f in live:
            if f.entity_type != "PERSON":
                continue
            span = docs[f.file].span(f.span_id)
            if span.table is not None:
                row_owner.setdefault((f.file, span.page, span.table[0], span.table[1]), self.persons.canonical(f.text))
        for f in live:
            if f.entity_type == "PERSON":
                continue
            span = docs[f.file].span(f.span_id)
            owner = None
            if span.table is not None:
                owner = row_owner.get((f.file, span.page, span.table[0], span.table[1]))
            self.assign(f, owner)

    def to_json(self) -> dict:
        return {
            "warning": "SENSITIVE: maps pseudonymous tokens back to original PII. Store encrypted, restrict access.",
            "tokens": {t: sorted(v) for t, v in sorted(self.values.items())},
        }

    def save(self, path: str | Path, passphrase: str) -> Path:
        """Write the vault encrypted (AES-256-GCM, key from scrypt). There is no plaintext mode."""
        if not passphrase:
            raise ValueError("a passphrase is required to save the token vault")
        path = Path(path)
        path.write_text(json.dumps(encrypt(json.dumps(self.to_json(), ensure_ascii=False).encode("utf-8"), passphrase),
                                   indent=2), encoding="utf-8")
        return path


# ------------------------------------------------------------------------------ rehydration
TOKEN = re.compile(r"\[[A-Z][A-Z_]*(?:_[A-Z0-9*]+)*\]")
NOT_VALUES = {"[IMAGE_TEXT_WITHHELD]", "[IMAGE WITHHELD]", "[EMBEDDED OBJECT WITHHELD]", "[REDACTED]"}


def rehydrate(text: str, mapping: dict) -> tuple[str, list[str], list[str]]:
    """Put original values back where an LLM answer (or any text) holds tokens.

    ``mapping`` is token -> original surface forms (the vault). A person token stands for every
    way the person was written ("Priya Raman", "Raman", "Priya's"); the longest form is used.
    Tokens that stand for several different values by design (a masked or generalised category:
    ``[CARD]``, ``[DOB_1985]``) cannot be restored and are left as they are.
    Returns (text, tokens restored, tokens seen that the vault does not hold or cannot restore).
    """
    restored: list[str] = []
    unknown: list[str] = []

    def put_back(m: re.Match) -> str:
        token = m.group()
        if token in NOT_VALUES:
            return token
        forms = sorted(mapping.get(token) or [], key=lambda v: (-len(v), v))
        ambiguous = not token.startswith("[PERSON_") and len({re.sub(r"\W", "", v).lower() for v in forms}) > 1
        if not forms or ambiguous:
            if token not in unknown:
                unknown.append(token)
            return token
        if token not in restored:
            restored.append(token)
        return forms[0]

    return TOKEN.sub(put_back, text), restored, unknown


# ------------------------------------------------------------------------------ vault crypto
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def encrypt(data: bytes, passphrase: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(passphrase, salt, **SCRYPT)).encrypt(nonce, data, VAULT_AAD)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return {"format": "pii-shield-vault/1", "cipher": "AES-256-GCM", "kdf": {"name": "scrypt", **SCRYPT, "salt": b64(salt)},
            "nonce": b64(nonce), "ciphertext": b64(ct),
            "warning": "SENSITIVE: maps pseudonymous tokens back to original PII. Restrict access."}


def decrypt(envelope: dict, passphrase: str) -> dict:
    """Open a saved vault. Raises ValueError on a wrong passphrase or a tampered file."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    kdf = envelope["kdf"]
    key = _key(passphrase, base64.b64decode(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"])
    try:
        data = AESGCM(key).decrypt(base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["ciphertext"]), VAULT_AAD)
    except InvalidTag:
        raise ValueError("wrong passphrase, or the vault file was modified") from None
    return json.loads(data)


VAULT_AAD = b"pii-shield-vault/1"
