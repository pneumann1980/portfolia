"""M24/AP1 – Wiederherstellung (Gesamtexport übernehmen) bei Fehlern und Abbrüchen.

SQLite-Transaktion und Dateisystem bilden keine gemeinsame Transaktion. Abgesichert wird deshalb jeder Übergang:
Prüfen → Dateien vorbereiten → Datenbank + Journal (eine Transaktion) → Dateien ersetzen → Journal schließen.
Fehler vor dem Commit lassen alles unverändert; Fehler bzw. Prozessabbrüche danach werden beim nächsten Start
(oder per „Fortsetzen“) deterministisch zu Ende geführt. Synthetische Daten, temporäre Verzeichnisse.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import fullexport
from app.config import Config, Secrets
from app.jobs import tasks
from app.taxdata.service import tax_import_service
from tests.test_full_export import SAMPLE, _client, _place, _prepare

ROOT = Path(__file__).resolve().parent.parent
TAX_DOC = {"taxYear": 2024, "records": [{"asset": "BTC", "quantity": "0.01", "disposalDate": "2024-11-02",
                                         "gainLoss": "-3"}]}
NEW_SOURCES = "# eigene Quellen\nsources: []\n"


def _cfg(base: Path) -> Config:
    (base / "import").mkdir(parents=True, exist_ok=True)
    return Config(data_dir=base / "data", import_dir=base / "import", demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


@pytest.fixture(scope="module")
def export_zip(tmp_path_factory) -> bytes:
    """Gesamtexport mit drei Dateien (News-Quellen, lokale Steuerregel, Steuerbericht) und Steuerdaten."""
    cfg = _cfg(tmp_path_factory.mktemp("quelle"))
    shutil.copy(SAMPLE, cfg.import_dir / "beispiel.zip")
    os.utime(cfg.import_dir / "beispiel.zip", (time.time() - 7200, time.time() - 7200))
    with _client(cfg) as c:
        ctx = c.app.state.ctx
        tasks.import_check(ctx, "test")
        _prepare(c)
        cfg.sources_path.write_text(NEW_SOURCES, encoding="utf-8")
        (cfg.tax_rules_dir / "de").mkdir(parents=True, exist_ok=True)
        (cfg.tax_rules_dir / "de" / "lokal.yaml").write_text("hinweis: lokal\n", encoding="utf-8")
        fid, _ = tax_import_service(ctx).register(json.dumps(TAX_DOC).encode(), "steuer-2024.json", "upload")
        assert tax_import_service(ctx).activate(fid).get("activated")
        ctx.settings.set("prices.crypto_interval_min", 15)
        return c.get("/journal/export.zip").content


@contextlib.contextmanager
def target(base: Path, body: bytes) -> Iterator[tuple]:
    """Bestehende Installation (Übernahme erst auf Anfrage) mit eigener sources.yaml und importiertem Export."""
    cfg = _cfg(base)
    with _client(cfg) as c:
        ctx = c.app.state.ctx
        ctx.settings.set("ui.default_range", "3J")
        cfg.sources_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.sources_path.write_text("# alt\n", encoding="utf-8")
        _place(cfg, body, "portfolia-export.zip")
        out = tasks.import_check(ctx, "test")
        assert out.status == "imported"
        ctx.test_client = c
        yield cfg, ctx, out.import_id


def snapshot(ctx) -> dict:
    db = ctx.db
    return {"settings": db.q("SELECT key, value_json FROM settings ORDER BY key"),
            "tax": db.q("SELECT sha256, status FROM tax_file ORDER BY id"),
            "records": db.scalar("SELECT COUNT(*) FROM tax_record"),
            "prices": db.scalar("SELECT COUNT(*) FROM price_daily"),
            "state": db.q("SELECT key, value FROM app_state WHERE key LIKE 'restore.%' ORDER BY key"),
            "log": db.scalar("SELECT COUNT(*) FROM journal_log WHERE action LIKE 'restore%'")}


def leftovers(cfg: Config) -> list[Path]:
    dirs = [cfg.sources_path.parent, cfg.tax_rules_dir, cfg.tax_data_dir / "uploads"]
    return sorted({p for d in dirs if d.exists() for p in d.rglob(".*restore-*")})


def _norm(snap: dict) -> dict:
    return {k: [tuple(r) for r in v] if isinstance(v, list) else v for k, v in snap.items()}


# ----------------------------------------------------------------------------------------------------------------------
# Fehler vor dem Datenbank-Commit: nichts geändert
# ----------------------------------------------------------------------------------------------------------------------

def test_insufficient_disk_space_changes_nothing(export_zip, tmp_path, monkeypatch):
    with target(tmp_path / "t", export_zip) as (cfg, ctx, iid):
        before = _norm(snapshot(ctx))
        real = shutil.disk_usage
        monkeypatch.setattr(fullexport.shutil, "disk_usage",
                            lambda p: real(p)._replace(free=1024))
        with pytest.raises(fullexport.RestoreError, match="Speicherplatz"):
            fullexport.apply(ctx, iid)
        assert _norm(snapshot(ctx)) == before and not leftovers(cfg)
        assert cfg.sources_path.read_text(encoding="utf-8") == "# alt\n"


def test_permission_error_while_staging_changes_nothing(export_zip, tmp_path, monkeypatch):
    with target(tmp_path / "t", export_zip) as (cfg, ctx, iid):
        before = _norm(snapshot(ctx))
        real, calls = fullexport._write_synced, []

        def write(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise PermissionError(13, "Permission denied")
            real(path, data)

        monkeypatch.setattr(fullexport, "_write_synced", write)
        with pytest.raises(PermissionError):
            fullexport.apply(ctx, iid)
        assert _norm(snapshot(ctx)) == before and not leftovers(cfg)


def test_failure_inside_db_transaction_rolls_back_and_cleans_up(export_zip, tmp_path, monkeypatch):
    with target(tmp_path / "t", export_zip) as (cfg, ctx, iid):
        before = _norm(snapshot(ctx))
        monkeypatch.setattr(fullexport, "_meta", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space")))
        with pytest.raises(OSError):
            fullexport.apply(ctx, iid)
        assert _norm(snapshot(ctx)) == before and not leftovers(cfg)
        assert fullexport.pending(ctx.db) is None


def test_contradictory_backup_is_rejected_before_any_change(export_zip, tmp_path):
    with target(tmp_path / "t", export_zip) as (cfg, ctx, iid):
        raw = ctx.db.q1("SELECT data FROM import_extra WHERE import_id=? AND name=?", (iid, fullexport.TAXDATA))
        files = json.loads(bytes(raw["data"]))
        files.append({**files[0], "sha256": "f" * 64, "filename": "zweite.json"})  # zweite aktive Datei für 2024
        ctx.db.x("UPDATE import_extra SET data=? WHERE import_id=? AND name=?",
                 (json.dumps(files).encode(), iid, fullexport.TAXDATA))
        before = _norm(snapshot(ctx))
        with pytest.raises(fullexport.RestoreError, match="widersprüchlich"):
            fullexport.apply(ctx, iid)
        assert _norm(snapshot(ctx)) == before and not leftovers(cfg)


# ----------------------------------------------------------------------------------------------------------------------
# Fehler nach dem Commit: unvollständig sichtbar, Fortsetzen deterministisch
# ----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_rename_failure_after_commit_is_resumable(export_zip, tmp_path, monkeypatch, fail_at):
    """fail_at 1: gleich nach dem Commit; 2/3: zwischen mehreren Umbenennungen."""
    with target(tmp_path / "t", export_zip) as (cfg, ctx, iid):
        real, n = Path.replace, [0]

        def replace(self, dst):
            n[0] += 1
            if n[0] == fail_at:
                raise OSError(30, "Read-only file system")
            return real(self, dst)

        monkeypatch.setattr(Path, "replace", replace)
        with pytest.raises(fullexport.RestoreIncomplete):
            fullexport.apply(ctx, iid)
        st = fullexport.status(ctx)
        assert st["state"] == "incomplete" and st["files_open"] == 3 and "Read-only" in st["error"]
        assert ctx.db.get_state(f"restore.{iid}")["state"] == "incomplete"  # nie „übernommen“, solange Dateien fehlen
        assert ctx.settings.get("prices.crypto_interval_min") == 15  # Datenbankteil gilt bereits
        monkeypatch.undo()
        assert fullexport.resume(ctx) is not None
        assert cfg.sources_path.read_text(encoding="utf-8") == NEW_SOURCES
        assert (cfg.tax_rules_dir / "de" / "lokal.yaml").read_text(encoding="utf-8") == "hinweis: lokal\n"
        f = ctx.db.q1("SELECT path FROM tax_file WHERE status='active'")
        assert Path(f["path"]).is_file()
        assert ctx.db.get_state(f"restore.{iid}")["state"] == "applied" and fullexport.pending(ctx.db) is None
        baks = list(cfg.sources_path.parent.glob("sources.yaml.bak-*"))
        assert len(baks) == 1 and baks[0].read_text(encoding="utf-8") == "# alt\n"  # bisherige Fassung gesichert
        assert not leftovers(cfg)
        assert fullexport.resume(ctx) is None  # nichts mehr offen


def test_resume_recreates_missing_temp_files_from_stored_extras(export_zip, tmp_path, monkeypatch):
    with target(tmp_path / "t", export_zip) as (cfg, ctx, iid):
        monkeypatch.setattr(Path, "replace", lambda self, dst: (_ for _ in ()).throw(OSError(5, "I/O error")))
        with pytest.raises(fullexport.RestoreIncomplete):
            fullexport.apply(ctx, iid)
        monkeypatch.undo()
        for p in leftovers(cfg):  # temporäre Dateien verloren (z. B. Aufräumen eines anderen Werkzeugs)
            p.unlink()
        fullexport.resume(ctx)
        assert cfg.sources_path.read_text(encoding="utf-8") == NEW_SOURCES
        assert ctx.db.get_state(f"restore.{iid}")["state"] == "applied"


def test_incomplete_restore_is_shown_and_resumable_in_ui(export_zip, tmp_path, monkeypatch):
    with target(tmp_path / "t", export_zip) as (cfg, ctx, iid):
        monkeypatch.setattr(Path, "replace", lambda self, dst: (_ for _ in ()).throw(OSError(13, "Permission denied")))
        with pytest.raises(fullexport.RestoreIncomplete):
            fullexport.apply(ctx, iid)
        monkeypatch.undo()
        c = ctx.test_client
        _prepare(c)
        assert "Wiederherstellung unvollständig" in c.get("/settings").text
        assert "Wiederherstellung unvollständig" in c.get("/").text
        r = c.post("/actions/restore/resume", data={}, follow_redirects=False)
        assert r.status_code == 303 and "restore_apply" in r.headers["location"]
        assert "Wiederherstellung unvollständig" not in c.get("/settings").text
        assert cfg.sources_path.read_text(encoding="utf-8") == NEW_SOURCES


def test_repeated_restore_is_idempotent(export_zip, tmp_path):
    with target(tmp_path / "t", export_zip) as (cfg, ctx, iid):
        first = fullexport.apply(ctx, iid)
        assert first["files"] == 3 and first["taxdata"] == 1
        txs = ctx.db.scalar("SELECT COUNT(*) FROM journal_tx")
        state = _norm(snapshot(ctx))
        again = fullexport.apply(ctx, iid)
        assert again["files"] == 0 and again["taxdata"] == 0 and again["prices"] == 0
        assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx") == txs
        s2 = _norm(snapshot(ctx))
        assert {k: v for k, v in s2.items() if k not in ("state", "log", "settings")} == \
            {k: v for k, v in state.items() if k not in ("state", "log", "settings")}
        # dieselbe Datei erneut in den Importordner: keine zweite Aktivierung, keine Doppelbuchungen
        _place(cfg, export_zip, "portfolia-export-kopie.zip")
        tasks.import_check(ctx, "test")
        assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx") == txs
        assert len(list(cfg.sources_path.parent.glob("sources.yaml.bak-*"))) == 1


# ----------------------------------------------------------------------------------------------------------------------
# Echte Prozessabbrüche (Kindprozess endet mit os._exit) und Neustart
# ----------------------------------------------------------------------------------------------------------------------

_CHILD = r"""
import os, sys
from pathlib import Path
from app import fullexport
from app.config import Config, Secrets
from app.context import AppContext

data, imp, iid, mode = sys.argv[1:5]
cfg = Config(data_dir=Path(data), import_dir=Path(imp), demo_mode=True, scheduler_enabled=False, startup_jobs=False,
             log_format="text", secrets=Secrets())
ctx = AppContext(cfg)
ctx.startup()
if mode == "rename":
    real, n = Path.replace, [0]
    def replace(self, dst):
        n[0] += 1
        if n[0] == 2:
            os._exit(9)  # Abbruch zwischen zwei Umbenennungen (nach dem Datenbank-Commit)
        return real(self, dst)
    Path.replace = replace
else:
    def boom(*a, **k):
        os._exit(9)  # Abbruch mitten in der Datenbank-Transaktion
    fullexport._taxdata = boom
fullexport.apply(ctx, int(iid))
os._exit(0)
"""


def _child(cfg: Config, iid: int, mode: str) -> int:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-c", _CHILD, str(cfg.data_dir), str(cfg.import_dir), str(iid), mode],
                          cwd=ROOT, env=env, timeout=120, capture_output=True).returncode


def test_process_abort_during_renames_is_completed_on_restart(export_zip, tmp_path):
    base = tmp_path / "t"
    with target(base, export_zip) as (cfg, _ctx, iid):
        pass
    assert _child(cfg, iid, "rename") == 9
    with _client(cfg) as c:  # Neustart: Wiederanlauf beim Start
        ctx = c.app.state.ctx
        assert fullexport.pending(ctx.db) is None
        assert ctx.db.get_state(f"restore.{iid}")["state"] == "applied"
        assert cfg.sources_path.read_text(encoding="utf-8") == NEW_SOURCES
        assert ctx.db.scalar("SELECT COUNT(*) FROM tax_file WHERE status='active'") == 1
        assert not leftovers(cfg)


def test_process_abort_inside_db_transaction_leaves_previous_state(export_zip, tmp_path):
    base = tmp_path / "t"
    with target(base, export_zip) as (cfg, ctx, iid):
        before = _norm(snapshot(ctx))
    assert _child(cfg, iid, "db") == 9
    assert leftovers(cfg)  # vorbereitete Dateien des abgebrochenen Laufs liegen noch da …
    with _client(cfg) as c:
        ctx = c.app.state.ctx
        assert not leftovers(cfg)  # … und werden beim Start entfernt
        assert _norm(snapshot(ctx)) == before  # SQLite hat die offene Transaktion verworfen
        assert cfg.sources_path.read_text(encoding="utf-8") == "# alt\n"
        counts = fullexport.apply(ctx, iid)  # danach regulär möglich
        assert counts["files"] == 3


def test_restore_journal_contains_no_secrets(export_zip, tmp_path, monkeypatch):
    with target(tmp_path / "t", export_zip) as (_cfg, ctx, iid):
        monkeypatch.setattr(Path, "replace", lambda self, dst: (_ for _ in ()).throw(OSError(5, "I/O error")))
        with pytest.raises(fullexport.RestoreIncomplete):
            fullexport.apply(ctx, iid)
        raw = json.dumps(fullexport.pending(ctx.db))
        assert "api_key" not in raw.lower() and "data" not in json.loads(raw)  # nur Pfade, Prüfsummen, Zähler
