"""Command line.

    python -m optiv_pii_shield run samples/*.pdf samples/*.docx --out out/
    python -m optiv_pii_shield run samples/synthetic/* --out out/ --gold samples/synthetic/gold_labels.csv
    python -m optiv_pii_shield gold-template samples/*.pptx --out gold_draft.csv
    $env:PII_SHIELD_VAULT_KEY = "..." ; python -m optiv_pii_shield run ...      # also save the encrypted vault
    python -m optiv_pii_shield vault-open out/token_vault.SENSITIVE.enc.json
    python -m optiv_pii_shield rehydrate out/token_vault.SENSITIVE.enc.json answer.txt   # tokens -> values
    python -m optiv_pii_shield keygen keys/          # Ed25519 pair; PII_SHIELD_SIGNING_KEY = the .key file
    python -m optiv_pii_shield verify-run out/       # manifest, output digests, audit chain
"""
from __future__ import annotations

import argparse
import getpass
import glob
import json
import logging
import os
import sys
from pathlib import Path

from . import audit
from .config import PROFILES, Settings
from .detect import ModelMissing
from .evaluate import evaluate, gold_template, load_gold, structure_retention
from .pipeline import run

VAULT_ENV = "PII_SHIELD_VAULT_KEY"
TOKEN_ENV = "PII_SHIELD_TOKEN_KEY"


def _expand(patterns: list[str], gold: str | None = None) -> list[Path]:
    """Files to scan. A glob such as ``samples/*`` also matches the gold-label CSV beside the
    samples: the file given as --gold and files named like gold labels are left out. Any other
    CSV is data and is scanned."""
    files: list[Path] = []
    skip = Path(gold).resolve() if gold else None
    for p in patterns:
        for m in glob.glob(p) or [p]:
            m = Path(m)
            if not m.is_file() or m.resolve() == skip or (m.suffix.lower() == ".csv" and "gold" in m.name.lower()):
                continue
            files.append(m)
    return files


def _settings(a) -> Settings:
    s = Settings()
    s.ocr_engine = a.ocr
    s.use_gliner = a.gliner
    s.spacy_model = a.spacy_model
    if a.no_images:
        s.ocr_embedded_images = False
    s.profile = getattr(a, "profile", "default")
    s.verify_outputs = not getattr(a, "no_verify", False)
    s.blank_textless_images = not getattr(a, "keep_unread_images", False)
    if getattr(a, "no_faces", False):
        s.detect_faces = s.detect_qr = False
    if getattr(a, "operator", None):
        s.operator = a.operator
    s.vault_passphrase = os.environ.get(getattr(a, "vault_key_env", VAULT_ENV)) or None
    s.token_key = os.environ.get(getattr(a, "token_key_env", TOKEN_ENV)) or None
    return s


def _open_vault(a) -> dict | None:
    from .redact.tokens import decrypt

    passphrase = os.environ.get(a.vault_key_env) or getpass.getpass("vault passphrase: ")
    try:
        return decrypt(json.loads(Path(a.vault).read_text(encoding="utf-8")), passphrase)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return None


def _log_reidentification(vault_path: Path, operator: str, how: str, tokens: list[str], purpose: str) -> None:
    """Re-identification is the one step that turns safe text back into personal data: who did
    it, when, for which tokens and why is appended to the run's audit chain."""
    log_path = vault_path.parent / "audit_log.jsonl"
    run_id = "unknown"
    if log_path.exists():
        first = log_path.read_text(encoding="utf-8").split("\n", 1)[0]
        run_id = json.loads(first).get("run_id", run_id) if first.strip() else run_id
    audit.AuditLog(log_path, run_id).append([{
        "event": "reidentification", "timestamp": audit.now(), "operator": operator, "how": how,
        "tokens": sorted(tokens), "count": len(tokens), "purpose": purpose}])


def _vault_open(a) -> int:
    data = _open_vault(a)
    if data is None:
        return 4
    _log_reidentification(Path(a.vault), a.operator or Settings().operator, "vault-open (whole vault)",
                          list(data["tokens"]), a.purpose)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def _rehydrate(a) -> int:
    from .redact.tokens import rehydrate

    data = _open_vault(a)
    if data is None:
        return 4
    text = sys.stdin.read() if a.text == "-" else Path(a.text).read_text(encoding="utf-8")
    out, restored, unknown = rehydrate(text, data["tokens"])
    _log_reidentification(Path(a.vault), a.operator or Settings().operator, "rehydrate", restored, a.purpose)
    sys.stdout.reconfigure(encoding="utf-8")
    print(out)
    print(f"{len(restored)} token(s) restored" + (f"; not restored: {', '.join(unknown)}" if unknown else ""), file=sys.stderr)
    return 0


def _verify_run(a) -> int:
    key = Path(a.pubkey).read_text(encoding="ascii") if a.pubkey else None
    r = audit.verify_run(a.folder, key)
    print(f"run {r.get('run_id')}  operator {r.get('operator')}  outputs {r.get('outputs')}  "
          f"audit records {r.get('audit', {}).get('records')}")
    print("signature: " + ("valid" + (" (key given)" if key else f" (key in the manifest: {r.get('public_key')})")
                           if r["signed"] else "none or invalid"))
    for p in r["problems"]:
        print(f"  PROBLEM {p}")
    print("OK: outputs and audit log match the manifest" if r["ok"] else "FAILED")
    return 0 if r["ok"] else 5


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="optiv_pii_shield", description="Offline PII detection and redaction")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (("vault-open", "decrypt a token vault and print it (authorised re-identification)"),
                       ("rehydrate", "put original values back into a text that holds tokens (an LLM answer)")):
        v = sub.add_parser(name, help=text)
        v.add_argument("vault")
        if name == "rehydrate":
            v.add_argument("text", help="text file holding tokens, or - for standard input")
        v.add_argument("--vault-key-env", default=VAULT_ENV, help="environment variable holding the passphrase")
        v.add_argument("--operator", help="who is re-identifying (default: the OS user)")
        v.add_argument("--purpose", default="", help="why; recorded in the audit log")
    k = sub.add_parser("keygen", help="create an Ed25519 key pair for signing run manifests")
    k.add_argument("out", nargs="?", default=".")
    vr = sub.add_parser("verify-run", help="check a run folder against its manifest and audit chain")
    vr.add_argument("folder")
    vr.add_argument("--pubkey", help="file holding the public key the manifest must be signed with")
    for name in ("run", "gold-template"):
        p = sub.add_parser(name)
        p.add_argument("files", nargs="+")
        p.add_argument("--out", default="out" if name == "run" else "gold_draft.csv")
        p.add_argument("--ocr", default="auto", choices=["auto", "rapidocr", "tesseract"])
        p.add_argument("--gliner", action="store_true", help="add the GLiNER-PII model (needs `pip install gliner`)")
        p.add_argument("--spacy-model", default="en_core_web_lg")
        p.add_argument("--no-images", action="store_true", help="skip OCR of embedded images (they are blanked instead)")
        p.add_argument("-v", "--verbose", action="store_true")
        if name == "run":
            p.add_argument("--gold", help="gold-label CSV: prints recall / precision / leaks")
            p.add_argument("--transcriptions", help="folder of <stem>.txt hand transcriptions for OCR'd PDFs")
            p.add_argument("--profile", default="default", choices=sorted(PROFILES), help="redaction profile")
            p.add_argument("--no-verify", action="store_true", help="skip the re-OCR verification of masked copies")
            p.add_argument("--keep-unread-images", action="store_true",
                           help="leave pictures without readable text in the masked copies")
            p.add_argument("--no-faces", action="store_true", help="do not look for faces and QR codes")
            p.add_argument("--operator", help="who runs this (default: the OS user); recorded in the manifest")
            p.add_argument("--vault-key-env", default=VAULT_ENV,
                           help=f"environment variable holding the vault passphrase (default {VAULT_ENV}); "
                                "unset = the token vault is not saved")
            p.add_argument("--token-key-env", default=TOKEN_ENV,
                           help=f"environment variable holding the token key (default {TOKEN_ENV}); "
                                "set = tokens are the same in every run")
    a = ap.parse_args(argv)
    if a.cmd == "vault-open":
        return _vault_open(a)
    if a.cmd == "rehydrate":
        return _rehydrate(a)
    if a.cmd == "keygen":
        priv, pub = audit.keygen(a.out)
        print(f"private key: {priv}  (set {audit.SIGNING_KEY_ENV} to this path; keep it secret)\npublic key:  {pub}")
        return 0
    if a.cmd == "verify-run":
        return _verify_run(a)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    files = _expand(a.files, getattr(a, "gold", None))
    if not files:
        print("no input files", file=sys.stderr)
        return 2
    progress = lambda msg, frac: print(f"[{frac:4.0%}] {msg}", file=sys.stderr)  # noqa: E731

    try:
        if a.cmd == "gold-template":
            res = run(files, _settings(a), None, progress)
            print(f"wrote {gold_template(res.findings, a.out)}")
            return 0
        res = run(files, _settings(a), a.out, progress)
    except ModelMissing as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    print(f"\nrun {res.run_id}  components: {', '.join(res.components)}")
    for f, doc in res.docs.items():
        live = [x for x in res.findings[f] if x.decision != "drop"]
        review = sum(x.decision == "review" for x in live)
        ocr = f", OCR pages {doc.ocr_pages}" if doc.ocr_pages else ""
        print(f"  {f}: {len(doc.spans)} spans, {len(live)} findings ({review} for review){ocr}")
        for w in doc.warnings:
            print(f"    ! {w}")
    for f, err in res.errors.items():
        print(f"  ERROR {f}: {err}")

    if a.gold:
        ev = evaluate(load_gold(a.gold), res.findings, res.redacted)
        print(f"\nrecall {ev.recall:.1%}  category-recall {ev.category_recall:.1%}  precision {ev.precision:.1%} (auto-redact {ev.precision_auto:.1%})  "
              f"F1 {ev.f1:.3f}  leaks {len(ev.leaks)}  ({ev.recalled}/{ev.total_gold} gold instances)")
        for ctx, m in ev.by_context.items():
            print(f"  {ctx:<12} recall {m['recall']:.1%} ({m['recalled']}/{m['gold']})")
        for m in ev.missed:
            print(f"  MISSED {m['file']} p{m['page']} [{m['category']}] {m['text']!r}")
        structure = {}
        for f, doc in res.docs.items():
            tr = None
            if a.transcriptions:
                tp = Path(a.transcriptions) / f"{Path(f).stem}.txt"
                tr = tp.read_text(encoding="utf-8") if tp.exists() else None
            structure[f] = structure_retention(doc, tr)
        out = Path(a.out) / "evaluation.json"
        out.write_text(json.dumps({"metrics": ev.as_dict(), "structure_retention": structure}, indent=2, default=str),
                       encoding="utf-8")
        print(f"  evaluation written to {out}")
    print(f"\noutputs in {Path(a.out).resolve()}")
    return 0 if not res.errors else 1


if __name__ == "__main__":
    sys.exit(main())
