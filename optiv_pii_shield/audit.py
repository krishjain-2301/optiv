"""Tamper-evident audit log, run manifest and their verification.

* **Audit log** (``audit_log.jsonl``): append-only, one JSON record per line. Every record carries
  the hash of the record before it and its own hash, so a changed, removed or reordered line
  breaks the chain from that point on. A run, a reviewer's decisions and every re-identification
  are appended to the same chain; nothing is rewritten.
* **Manifest** (``run_manifest.json``): who ran what on which inputs with which settings, models
  and versions, and the SHA-256 of every output. Signed with Ed25519 when a signing key is
  configured (``PII_SHIELD_SIGNING_KEY`` = path of a PEM private key from ``keygen``).
* ``verify_run`` recomputes all of it.

The chain shows that the log was not edited after the fact; only the signature shows who wrote
it. Without a signing key the manifest still pins every output by digest, but anyone able to
rewrite the folder could rewrite the manifest too.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from . import modelstore

AUDIT_FORMAT = "pii-shield-audit/1"
MANIFEST_FORMAT = "pii-shield-manifest/1"
SIGNING_KEY_ENV = "PII_SHIELD_SIGNING_KEY"
PACKAGES = ("presidio-analyzer", "spacy", "en_core_web_lg", "pymupdf", "python-docx", "python-pptx", "openpyxl",
            "rapidocr-onnxruntime", "onnxruntime", "opencv-python", "phonenumbers", "cryptography", "gliner")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _link(prev: str, record: dict) -> str:
    return hashlib.sha256(prev.encode("ascii") + canonical(record)).hexdigest()


def genesis(run_id: str) -> str:
    return hashlib.sha256(f"{AUDIT_FORMAT}:{run_id}".encode("utf-8")).hexdigest()


class AuditLog:
    """Appends hash-chained records to a file. Reopening an existing log continues its chain."""

    def __init__(self, path: str | Path, run_id: str):
        self.path, self.run_id = Path(path), run_id
        self.head = genesis(run_id)
        self.count = 0
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.head = json.loads(line)["hash"]
                    self.count += 1

    def append(self, records: list[dict]) -> str:
        with open(self.path, "a", encoding="utf-8") as fh:
            for rec in records:
                rec = {"run_id": self.run_id, "seq": self.count, **rec}
                rec["prev"] = self.head
                self.head = rec["hash"] = _link(self.head, {k: v for k, v in rec.items() if k != "hash"})
                self.count += 1
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return self.head


def verify_chain(path: str | Path) -> tuple[bool, int, str]:
    """(intact, records read, head hash or the reason the chain is broken)."""
    lines = [l for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        return False, 0, "the audit log is empty"
    head = None
    for n, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            return False, n, f"line {n + 1} is not JSON"
        if head is None:
            head = genesis(rec.get("run_id", ""))
        if rec.get("seq") != n or rec.get("prev") != head:
            return False, n, f"line {n + 1} does not follow the record before it (removed, reordered or inserted)"
        head = _link(head, {k: v for k, v in rec.items() if k != "hash"})
        if rec.get("hash") != head:
            return False, n, f"line {n + 1} was changed after it was written"
    return True, len(lines), head


# ---------------------------------------------------------------------------------- signing
def keygen(out_dir: str | Path) -> tuple[Path, Path]:
    """Write a new Ed25519 key pair: pii_shield_signing.key (keep private) and .pub (hand out)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    priv, pub = out / "pii_shield_signing.key", out / "pii_shield_signing.pub"
    if priv.exists():
        raise FileExistsError(f"{priv} already exists; refusing to overwrite a signing key")
    priv.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    pub.write_text(public_text(key.public_key()), encoding="ascii")
    return priv, pub


def public_text(public_key) -> str:
    from cryptography.hazmat.primitives import serialization

    return base64.b64encode(public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode("ascii")


def _load_signing_key():
    path = os.environ.get(SIGNING_KEY_ENV)
    if not path:
        return None
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def sign(manifest: dict) -> dict | None:
    key = _load_signing_key()
    if key is None:
        return None
    return {"alg": "Ed25519", "public_key": public_text(key.public_key()),
            "value": base64.b64encode(key.sign(canonical(manifest))).decode("ascii")}


# --------------------------------------------------------------------------------- manifest
def versions() -> dict:
    out = {"python": sys.version.split()[0], "platform": platform.platform()}
    for name in PACKAGES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return out


def settings_record(settings) -> dict:
    """Settings as recorded: secrets are reduced to whether they were set."""
    from dataclasses import fields

    rec = {}
    for f in fields(settings):
        v = getattr(settings, f.name)
        rec[f.name] = ("set" if v else "not set") if f.name in ("vault_passphrase", "token_key") else v
    rec["allow_list"] = f"{len(settings.allow_list)} entries, sha256 {hashlib.sha256(canonical(sorted(settings.allow_list))).hexdigest()[:16]}"
    rec["extra_deny_list"] = f"{len(settings.extra_deny_list)} entries"  # names: not written out
    return rec


def file_record(path: Path) -> dict:
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": modelstore.sha256_file(path)}


def write_manifest(out_dir: Path, body: dict, outputs: list[Path]) -> Path:
    """``body`` holds the run's own fields; the output digests and the signature are added here."""
    path = out_dir / "run_manifest.json"
    manifest = {"format": MANIFEST_FORMAT, **body,
                "outputs": [file_record(p) for p in sorted(set(outputs)) if p.exists() and p != path]}
    manifest["signature"] = sign(manifest)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def verify_run(out_dir: str | Path, public_key: str | None = None) -> dict:
    """Check a run folder against its manifest. Returns {"ok", "signed", "problems": [...], ...}."""
    out = Path(out_dir)
    problems: list[str] = []
    mpath = out / "run_manifest.json"
    if not mpath.exists():
        return {"ok": False, "signed": False, "problems": ["run_manifest.json is missing"]}
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    sig = manifest.pop("signature", None)
    signed = False
    if sig:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(sig["public_key"])).verify(
                base64.b64decode(sig["value"]), canonical(manifest))
            signed = True
        except (InvalidSignature, ValueError, KeyError):
            problems.append("the manifest's signature does not match its content")
        if public_key and sig.get("public_key") != public_key.strip():
            signed = False
            problems.append("the manifest is signed with a different key than the one given")
    elif public_key:
        problems.append("the manifest is not signed")
    for rec in manifest.get("outputs", []):
        p = out / rec["name"]
        if not p.exists():
            problems.append(f"{rec['name']}: listed in the manifest but missing")
        elif rec["name"] != "audit_log.jsonl" and modelstore.sha256_file(p) != rec["sha256"]:
            problems.append(f"{rec['name']}: content differs from the manifest")
    chain = {"records": 0, "head": None}
    apath = out / "audit_log.jsonl"
    if apath.exists():
        ok, n, head = verify_chain(apath)
        chain = {"records": n, "head": head if ok else None}
        if not ok:
            problems.append(f"audit log: {head}")
        else:
            want = manifest.get("audit", {})
            lines = [l for l in apath.read_text(encoding="utf-8").splitlines() if l.strip()]
            if want.get("records", 0) > n:
                problems.append("audit log: shorter than when the manifest was written (records removed from the end)")
            elif want.get("records") and json.loads(lines[want["records"] - 1])["hash"] != want.get("head"):
                problems.append("audit log: not the log this manifest was written for")
    else:
        problems.append("audit_log.jsonl is missing")
    return {"ok": not problems, "signed": signed, "problems": problems, "run_id": manifest.get("run_id"),
            "operator": manifest.get("operator"), "outputs": len(manifest.get("outputs", [])), "audit": chain,
            "public_key": (sig or {}).get("public_key")}
