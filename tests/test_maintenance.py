"""Wartung: Sicherungen (Integrität, gzip, Aufbewahrung), Aktion im Web, Anfragegröße."""

import gzip
import sqlite3

from fastapi.testclient import TestClient

from app.context import AppContext
from app.jobs import maintenance as M
from app.main import build_app


def test_backup_roundtrip_and_retention(config, monkeypatch):
    ctx = AppContext(config)
    ctx.startup()
    ctx.settings.set("backup.keep", 2)
    stamps = iter(["20260101-031500", "20260102-031500", "20260103-031500"])

    class FakeNow:
        def strftime(self, fmt):
            return next(stamps)

    monkeypatch.setattr(M, "now_local", lambda: FakeNow())
    for _ in range(3):
        res = M.backup_now(ctx)
        assert res["bytes"] > 0
    names = [b["name"] for b in M.list_backups(ctx)]
    assert names == ["app-20260103-031500.sqlite.gz", "app-20260102-031500.sqlite.gz"]  # älteste entfernt
    raw = gzip.decompress((config.backup_dir / names[0]).read_bytes())
    restored = config.data_dir / "restored.sqlite"
    restored.write_bytes(raw)
    con = sqlite3.connect(restored)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT value_json FROM settings WHERE key='backup.keep'").fetchone()[0] == "2"
    finally:
        con.close()
    assert not list(config.backup_dir.glob(".*tmp"))  # keine Zwischenstände liegen gelassen
    assert M.backup_path(ctx, "../app.sqlite") is None and M.backup_path(ctx, names[0]) is not None
    assert M.db_maintenance(ctx)["pages"] > 0


def test_backup_action_and_body_limit(config):
    app = build_app(config, start_scheduler=False)
    with TestClient(app) as c:
        c.get("/settings")
        token = c.cookies.get("portfolia_csrf")
        r = c.post("/actions/backup", headers={"X-CSRF-Token": token, "HX-Request": "true"})
        assert r.status_code == 200 and "Gesichert: app-" in r.text
        assert "app-" in c.get("/settings").text  # Liste der Sicherungen
        r = c.post("/settings/save", headers={"X-CSRF-Token": token, "Content-Length": "5000000"},
                   content=b"section=general")
        assert r.status_code == 413


def test_manual_price_refresh_cooldown(config):
    app = build_app(config, start_scheduler=False)
    with TestClient(app) as c:
        c.get("/settings")
        token = c.cookies.get("portfolia_csrf")
        h = {"X-CSRF-Token": token, "HX-Request": "true"}
        assert "Kurse aktualisiert" in c.post("/actions/prices/refresh", headers=h).text
        assert "warten" in c.post("/actions/prices/refresh", headers=h).text
