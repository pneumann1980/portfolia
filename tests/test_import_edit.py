"""Buchungen des kuratierten Imports bearbeiten und löschen (Überlagerung; die Import-Datei bleibt unverändert)."""

from __future__ import annotations

import io
import os
import time
import zipfile
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests.helpers import ASSETS, tx

ROWS = [
    tx("IMP-1", "2025-03-10T10:15:37Z", "deposit", to=("Börse", "EUR", "1000")),
    tx("IMP-2", "2025-03-11T09:00:05Z", "buy", frm=("Börse", "EUR", "500"), to=("Börse", "BTC", "0.01"), value="500"),
    tx("IMP-3", "2025-03-12", "buy", frm=("Börse", "EUR", "100"), to=("Börse", "ETH", "0.05"), value="100"),
]


def _zip(config, rows, name="imp.zip", age=3600):
    dst = config.import_dir / name
    build_zip(dst, transactions=rows, assets=ASSETS, generated_at="2025-04-01T00:00:00Z", valuation_date="2025-03-31")
    old = time.time() - age
    os.utime(dst, (old, old))
    return dst


@pytest.fixture
def client(config):
    _zip(config, ROWS)
    with TestClient(build_app(config, start_scheduler=False)) as c:
        tasks.import_check(c.app.state.ctx, "test")
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def post(c, url, **data):
    return c.post(url, data={"csrf_token": c.token, **data}, follow_redirects=False)


def ctx(c):
    return c.app.state.ctx


def txs(c):
    return {t.tx_id: t for t in ctx(c).portfolio().txs}


def form(c, tx_id):
    page = c.get(f"/journal/{tx_id}/edit").text
    assert "kuratierten Import" in page and 'name="from_qty"' in page
    return {k: v for k, v in _inputs(page).items() if k != "csrf_token"}


def _inputs(page: str) -> dict[str, str]:
    import re

    out = {}
    for m in re.finditer(r'<input type="[^"]+" name="([a-z_]+)" value="([^"]*)"', page):
        out[m.group(1)] = m.group(2)
    for m in re.finditer(r'<select name="([a-z_]+)">(.*?)</select>', page, re.S):
        sel = re.search(r'<option value="([^"]*)" selected', m.group(2))
        out[m.group(1)] = sel.group(1) if sel else ""
    return out


def test_edit_import_transaction_keeps_file_and_counts(client):
    c = client
    data = form(c, "IMP-2")
    assert data["time"] == "10:00:05"  # lokale Zeit mit Sekunden (kein stiller Verlust)
    r = post(c, "/journal/IMP-2/edit", **data)
    assert r.status_code == 303
    assert ctx(c).db.scalar("SELECT COUNT(*) FROM tx_override") == 0  # unverändert gespeichert → keine Änderung
    data["to_qty"] = "0.012"
    data["value_eur"] = "510"
    r = post(c, "/journal/IMP-2/edit", **data)
    assert r.status_code == 303, r.text[:500]
    t = txs(c)["IMP-2"]
    assert t.to_qty == Decimal("0.012") and t.value_eur == Decimal("510") and t.origin == "import"
    assert t.ts.second == 5
    assert ctx(c).ledger().holdings_by_asset()["BTC"] == Decimal("0.012")
    page = c.get("/journal").text
    assert "bearbeitet" in page and "/journal/IMP-2/revert" in page
    # Gesamtexport enthält die wirksame Fassung
    z = zipfile.ZipFile(io.BytesIO(c.get("/journal/export.zip").content))
    rows = z.read("transactions.csv").decode()
    assert "IMP-2" in rows and "0.012" in rows and "510" in rows
    # Import-Datei unverändert, Protokoll vorhanden
    assert ctx(c).db.scalar("SELECT COUNT(*) FROM journal_log WHERE action='import_edit' AND ref='IMP-2'") == 1
    r = post(c, "/journal/IMP-2/revert")
    assert r.status_code == 303 and txs(c)["IMP-2"].to_qty == Decimal("0.01")


def test_invalid_edit_is_rejected(client):
    c = client
    data = form(c, "IMP-2")
    data["to_asset"] = "GIBTESNICHT"
    r = post(c, "/journal/IMP-2/edit", **data)
    assert r.status_code == 400 and ctx(c).db.scalar("SELECT COUNT(*) FROM tx_override") == 0


def test_delete_and_restore_import_transaction(client):
    c = client
    data = form(c, "IMP-3")
    data["note"] = "korrigiert"
    post(c, "/journal/IMP-3/edit", **data)
    r = post(c, "/journal/IMP-3/delete")
    assert r.status_code == 303 and "IMP-3" not in txs(c)
    assert "ETH" not in ctx(c).ledger().holdings_by_asset()
    page = c.get("/journal").text
    assert "Gelöschte Buchungen" in page and "aus dem Import" in page
    r = post(c, "/journal/IMP-3/restore")
    assert r.status_code == 303 and txs(c)["IMP-3"].note == "korrigiert"  # bearbeitete Fassung bleibt


def test_override_survives_new_import_and_flags_changes(client, config):
    c = client
    data = form(c, "IMP-2")
    data["value_eur"] = "505"
    post(c, "/journal/IMP-2/edit", **data)
    post(c, "/journal/IMP-3/delete")
    # neuer Import: IMP-2 geändert, IMP-3 entfällt
    rows = [ROWS[0], tx("IMP-2", "2025-03-11T09:00:05Z", "buy", frm=("Börse", "EUR", "500"),
                        to=("Börse", "BTC", "0.011"), value="500")]
    _zip(config, rows, name="imp2.zip", age=60)
    assert tasks.import_check(ctx(c), "test").status == "imported"
    t = txs(c)["IMP-2"]
    assert t.value_eur == Decimal("505")  # eigene Änderung gilt weiter
    states = ctx(c).effective_base()[1]
    assert states["IMP-2"].status == "changed" and states["IMP-3"].status == "orphan"
    page = c.get("/journal").text
    assert "Änderungen an Import-Buchungen prüfen" in page
    post(c, "/journal/IMP-3/revert")
    assert "IMP-3" not in ctx(c).effective_base()[1]


def test_deleted_import_id_does_not_revive_journal_duplicate(client):
    c = client
    db = ctx(c).db
    # Journal-Buchung mit derselben ID wie eine Import-Buchung (z. B. nach Übernahme eines Gesamtexports)
    db.x("INSERT INTO journal_tx(tx_id, source, status, ts_utc, date_only, type, to_account, to_asset, to_qty, "
         "created_at, updated_at) VALUES ('IMP-1', 'manual', 'active', '2025-03-10T10:15:37Z', 0, 'deposit', "
         "'Börse', 'EUR', '1000', '2025-03-10T10:15:37Z', '2025-03-10T10:15:37Z')")
    ctx(c).invalidate_overlay()
    assert sum(1 for t in ctx(c).portfolio().txs if t.tx_id == "IMP-1") == 1
    post(c, "/journal/IMP-1/delete")
    assert "IMP-1" not in txs(c)


def test_import_rows_have_actions(client):
    page = client.get("/journal").text
    assert "/journal/IMP-1/edit" in page and "/journal/IMP-1/delete" in page


def test_migration_9_adds_tables_and_keeps_data(tmp_path):
    from app.db import Database

    d = Database(tmp_path / "app.sqlite")
    d.migrate(target=8)
    d.x("INSERT INTO settings(key, value_json, updated_at) VALUES ('ui.default_range', '\"3J\"', 'x')")
    d.migrate(target=9)
    assert d.scalar("PRAGMA user_version") == 9
    tables = {r["name"] for r in d.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tx_override", "import_extra"} <= tables
    assert d.scalar("SELECT value_json FROM settings WHERE key='ui.default_range'") == '"3J"'
