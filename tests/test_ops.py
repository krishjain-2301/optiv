"""Operational guarantees: models are not silently downgraded, the vault is never stored in
plaintext, working folders are removed."""
import json
import os
import time

import pytest

from optiv_pii_shield import workspace
from optiv_pii_shield.config import Settings
from optiv_pii_shield.detect import Detector, ModelMissing
from optiv_pii_shield.redact.tokens import TokenVault, decrypt


def test_missing_spacy_model_is_an_error_not_a_fallback():
    with pytest.raises(ModelMissing):
        Detector(Settings(spacy_model="en_core_web_doesnotexist"))


def test_missing_model_stops_run_before_extraction(tmp_path, monkeypatch):
    import optiv_pii_shield.pipeline as pl

    def boom(*a, **k):
        raise AssertionError("extraction must not start without the models")

    monkeypatch.setattr(pl, "extract", boom)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ModelMissing):
        pl.run([tmp_path / "a.txt"], Settings(spacy_model="en_core_web_doesnotexist"), tmp_path / "out")


def test_vault_is_encrypted_and_round_trips(tmp_path):
    v = TokenVault()
    v.values["[PERSON_001]"].add("Priya Raman")
    path = v.save(tmp_path / "vault.json", "correct horse battery staple")
    raw = path.read_text(encoding="utf-8")
    assert "Priya" not in raw and "PERSON_001" not in raw
    env = json.loads(raw)
    assert env["cipher"] == "AES-256-GCM" and env["kdf"]["name"] == "scrypt"
    assert decrypt(env, "correct horse battery staple")["tokens"]["[PERSON_001]"] == ["Priya Raman"]
    with pytest.raises(ValueError):
        decrypt(env, "wrong")
    with pytest.raises(ValueError):
        v.save(tmp_path / "v2.json", "")


def test_cli_vault_round_trip(tmp_path, monkeypatch, capsys):
    from optiv_pii_shield.cli import main

    v = TokenVault()
    v.values["[EMAIL_001]"].add("kofi@example.com")
    v.save(tmp_path / "v.json", "pw-123456")
    monkeypatch.setenv("PII_SHIELD_VAULT_KEY", "pw-123456")
    assert main(["vault-open", str(tmp_path / "v.json")]) == 0
    assert "kofi@example.com" in capsys.readouterr().out
    monkeypatch.setenv("PII_SHIELD_VAULT_KEY", "nope")
    assert main(["vault-open", str(tmp_path / "v.json")]) == 4


def test_workspace_empty_and_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace.tempfile, "gettempdir", lambda: str(tmp_path))
    old, current = workspace.new_session(), workspace.new_session()
    for d in (old, current):
        (d / "in").mkdir()
        (d / "in" / "upload.pdf").write_bytes(b"%PDF")
        os.chmod(d / "in" / "upload.pdf", 0o444)  # read-only files must not block removal
    past = time.time() - 5 * 3600
    for p in [old, *old.rglob("*")]:
        os.utime(p, (past, past))
    assert workspace.sweep_stale(max_age_hours=2, keep=current) == 1
    assert not old.exists() and current.exists()
    workspace.empty(current)
    assert current.exists() and not list(current.iterdir())


def test_org_config_from_yaml(tmp_path):
    from optiv_pii_shield.config import load_org_config

    p = tmp_path / "org.yaml"
    p.write_text("allow_list: [Contoso]\nid_patterns:\n  - {name: badge, entity: EMPLOYEE_ID, pattern: 'BDG-[0-9]{5}', score: 0.9}\n",
                 encoding="utf-8")
    cfg = load_org_config(p)
    assert cfg["allow_list"] == ["Contoso"] and cfg["deny_list"] == [] and cfg["id_patterns"][0]["name"] == "badge"
    p.write_text("id_patterns:\n  - {name: broken, pattern: 'x'}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_org_config(p)


def test_bundled_org_rules_loaded():
    from optiv_pii_shield.detect.rules import RULES

    assert {"cadence_person_id", "cadence_vendor_id"} <= {r.name for r in RULES}


def test_missing_ocr_model_is_an_error(monkeypatch, tmp_path):
    from optiv_pii_shield.extract import ocr

    monkeypatch.setenv("PII_SHIELD_REC_MODEL", str(tmp_path / "missing.onnx"))
    ocr.get_engine.cache_clear()
    try:
        with pytest.raises(ModelMissing):
            ocr.get_engine("rapidocr")
    finally:
        ocr.get_engine.cache_clear()
