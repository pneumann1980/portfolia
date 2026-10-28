"""Kommandozeile ``python -m app <befehl>`` (vorher ungetestet, 0 % Abdeckung)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import main

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"


def test_help_and_unknown_command(capsys):
    main([])
    assert "hash-password" in capsys.readouterr().out
    main(["--help"])
    assert "validate" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        main(["gibt-es-nicht"])
    assert e.value.code == 1


def test_hash_password_with_argument_and_interactive(monkeypatch, capsys):
    from app.web.security import verify_password

    main(["hash-password", "geheim-123"])
    h = capsys.readouterr().out.strip()
    assert verify_password("geheim-123", h) and not verify_password("falsch", h)
    answers = iter(["abc-passwort", "abc-passwort"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    main(["hash-password"])
    assert verify_password("abc-passwort", capsys.readouterr().out.strip())
    answers = iter(["eins", "zwei"])
    with pytest.raises(SystemExit) as e:
        main(["hash-password"])
    assert e.value.code == 1 and "stimmen nicht überein" in capsys.readouterr().err


def test_validate_ok_and_broken_file(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        main(["validate", str(SAMPLE)])
    out = capsys.readouterr().out
    assert e.value.code == 0 and out.rstrip().splitlines()[-1].startswith("OK:")
    bad = tmp_path / "kaputt.zip"
    bad.write_bytes(b"das ist keine zip-datei")
    with pytest.raises(SystemExit) as e:
        main(["validate", str(bad)])
    assert e.value.code == 2
    assert json.loads(capsys.readouterr().out.split("\n}", 1)[0] + "\n}")  # Bericht ist gültiges JSON


def test_sample_zip_roundtrip(tmp_path, capsys):
    target = tmp_path / "bsp.zip"
    main(["sample-zip", str(target)])
    assert target.exists() and "Beispiel-Import geschrieben" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        main(["validate", str(target)])
    assert e.value.code == 0


def test_master_key_prints_key_and_hint_on_stderr(capsys):
    main(["master-key"])
    cap = capsys.readouterr()
    assert len(cap.out.strip()) >= 32 and "PORTFOLIA_MASTER_KEY_FILE" in cap.err


def test_backup_and_credentials_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IMPORT_DIR", str(tmp_path / "import"))
    (tmp_path / "import").mkdir()
    main(["backup"])
    assert "Sicherung erstellt:" in capsys.readouterr().out
    assert list((tmp_path / "data" / "backups").glob("*"))
    main(["credentials", "status"])
    out = capsys.readouterr().out
    assert "Master-Key:" in out and "Gespeicherte API-Keys: 0" in out
