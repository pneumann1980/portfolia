"""Sicherung in der App wiederherstellen: vormerken (doppelt bestätigt), beim nächsten Start vor dem Öffnen der
Datenbank austauschen, vorherigen Stand sichern; fehlerhafte Sicherung lässt den Stand unverändert."""

from __future__ import annotations

import gzip

import pytest
from fastapi.testclient import TestClient

from app import dbrestore as DR
from app.config import Config, Secrets
from app.jobs.maintenance import backup_now, list_backups
from app.main import build_app
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import client_with_import

BASE = "/quality/assistent"


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


@pytest.fixture
def started(cfg, monkeypatch):
    restarts: list[float] = []
    monkeypatch.setattr(DR, "restart_process", lambda delay_s=DR.RESTART_DELAY_S: restarts.append(delay_s))
    c = client_with_import(cfg, [tx("B1", "2025-01-02", "deposit", to=("Depot D", "EUR", "10"), value="10")], ASSETS)
    return c, restarts


def reopen(cfg) -> TestClient:
    c = TestClient(build_app(cfg, start_scheduler=False))
    c.__enter__()
    c.get("/settings")
    c.token = c.cookies.get("portfolia_csrf")
    return c


def test_restore_is_requested_with_double_confirmation_and_applied_on_start(cfg, started):
    c, restarts = started
    ctx = c.app.state.ctx
    name = backup_now(ctx, "manual")["file"]
    assert next(b for b in list_backups(ctx) if b["name"] == name)["label"] == "von Hand gesichert"
    ctx.db.set_state("test.marker", "nach der Sicherung")
    page = c.get(f"{BASE}/protokoll").text
    assert "Sicherung wiederherstellen" in page and name in page and DR.CONFIRM_WORD in page
    # ohne Wort bzw. ohne Häkchen: nichts vorgemerkt
    for form in ({"name": name, "ok": "1", "word": "ja"}, {"name": name, "word": DR.CONFIRM_WORD}):
        r = c.post(f"{BASE}/sicherung", data={"csrf_token": c.token, **form}, follow_redirects=False)
        assert "err=" in r.headers["location"] and DR.pending(cfg) is None
    r = c.post(f"{BASE}/sicherung", data={"csrf_token": c.token, "name": "../app.sqlite", "ok": "1",
                                          "word": DR.CONFIRM_WORD}, follow_redirects=False)
    assert "err=" in r.headers["location"] and DR.pending(cfg) is None  # nur Dateien des Sicherungsordners
    r = c.post(f"{BASE}/sicherung", data={"csrf_token": c.token, "name": name, "ok": "1", "word": DR.CONFIRM_WORD})
    assert r.status_code == 200 and "Portfolia startet neu" in r.text and 'data-restart-wait=' in r.text
    assert DR.pending(cfg)["file"] == name and restarts
    assert "ist vorgemerkt" in c.get(f"{BASE}/protokoll").text
    c.__exit__(None, None, None)
    # Neustart: Wiederherstellung vor dem Öffnen der Datenbank
    c2 = reopen(cfg)
    try:
        ctx2 = c2.app.state.ctx
        assert ctx2.db.get_state("test.marker") is None  # Stand der Sicherung
        res = DR.result(cfg)
        assert res["ok"] and res["file"] == name and DR.pending(cfg) is None
        before = next(b for b in list_backups(ctx2) if b["name"] == res["before"])
        assert before["label"].startswith("vor Wiederherstellung")
        page = c2.get(f"{BASE}/protokoll").text
        assert "Wiederhergestellt aus" in page and res["before"] in page
        c2.post(f"{BASE}/sicherung/ok", data={"csrf_token": c2.token})
        assert DR.result(cfg) is None
        # Wiederherstellung ist umkehrbar: den Stand davor zurückholen
        assert DR.request(ctx2, res["before"]) == []
    finally:
        c2.__exit__(None, None, None)
    c3 = reopen(cfg)
    try:
        assert c3.app.state.ctx.db.get_state("test.marker") == "nach der Sicherung"
    finally:
        c3.__exit__(None, None, None)


def test_broken_backup_leaves_the_current_state_unchanged(cfg, started):
    c, _r = started
    ctx = c.app.state.ctx
    ctx.db.set_state("test.marker", "aktuell")
    bad = cfg.backup_dir / "app-20250101-120000.sqlite.gz"
    with gzip.open(bad, "wb") as g:
        g.write(b"keine Datenbank")
    assert DR.request(ctx, bad.name) == []
    c.__exit__(None, None, None)
    c2 = reopen(cfg)
    try:
        assert c2.app.state.ctx.db.get_state("test.marker") == "aktuell"
        res = DR.result(cfg)
        assert not res["ok"] and res["error"] and DR.pending(cfg) is None
        assert "fehlgeschlagen" in c2.get(f"{BASE}/protokoll").text
    finally:
        c2.__exit__(None, None, None)


def test_pending_restore_can_be_cancelled(cfg, started):
    c, _r = started
    ctx = c.app.state.ctx
    name = backup_now(ctx, "manual")["file"]
    assert DR.request(ctx, name) == [] and DR.pending(cfg)
    r = c.post(f"{BASE}/sicherung/abbrechen", data={"csrf_token": c.token}, follow_redirects=False)
    assert "msg=" in r.headers["location"] and DR.pending(cfg) is None
    assert DR.apply_pending(cfg) is None  # nichts mehr vorgemerkt
    c.__exit__(None, None, None)
