"""AP6 – Export → Neueinrichtung → fachlicher Vergleich, Idempotenz, Fehler mitten in der Übernahme, beschädigte
Archive, Schema-Migrationen aus älteren Versionen. Synthetische Daten, temporäre Datenbanken.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from app import fullexport
from app.assetchange.service import asset_change_service
from app.db import MIGRATIONS, Database
from app.jobs import tasks
from app.journal.service import journal_service
from app.ledger.engine import run_ledger
from app.taxdata.service import tax_import_service
from app.watchlist.service import watchlist_service
from tests.test_full_export import MASTER, SAMPLE, SECRET, _client, _other, _place, _prepare

TAX_DOC = {"taxYear": 2024, "records": [
    {"asset": "MATIC", "quantity": "10", "disposalDate": "2024-12-01", "gainLoss": "1,50", "externalId": "x-1"},
    {"asset": "BTC", "quantity": "0.01", "disposalDate": "2024-11-02", "gainLoss": "-3"}]}


def state(ctx) -> dict:
    """Fachlicher Zustand: wirksame Buchungen, Bestände, Lots, Veräußerungen, Steuer, Kurse, App-Daten."""
    ctx.invalidate_data()
    pf = ctx.portfolio()
    led = run_ledger(ctx.recorded_portfolio(), ctx.engine_options())  # ohne geschätzte Sparplan-Buchungen
    db = ctx.db
    from app.tax.service import tax_service

    _pack, _inp, tax = tax_service(ctx).compute(2024)
    return {
        "txs": sorted((t.date, t.type, t.from_asset, str(t.from_qty), t.to_asset, str(t.to_qty), str(t.value_eur))
                      for t in pf.txs if t.flag != "estimated"),
        "balances": sorted((k, v) for k, v in led.balances.items() if v),
        "lots": sorted((lot.asset, lot.account, lot.qty, lot.cost, lot.acq_date) for lot in led.lots),
        "disposals": sorted((d.asset, d.date, d.qty, d.proceeds, d.cost) for d in led.disposals),
        "tax": (tax.data["crypto"]["net"], tax.data["crypto"]["count_tax"], tax.data["crypto"]["free_gain"]),
        "prices": sorted(tuple(r) for r in db.q("SELECT series, date, close, source FROM price_daily")),
        "tax_files": sorted(tuple(r) for r in db.q("SELECT tax_year, filename, sha256, status, records, matched "
                                                   "FROM tax_file")),
        "tax_records": sorted(tuple(r) for r in db.q("SELECT r.line, r.asset, r.quantity, r.gain_loss, "
                                                     "r.match_status FROM tax_record r JOIN tax_file f ON "
                                                     "f.id=r.file_id")),
        "watchlist": sorted(tuple(r) for r in db.q("SELECT quote_source, quote_id FROM watchlist_item")),
        "changes": sorted(tuple(r) for r in db.q("SELECT kind, old_asset, new_asset, ratio, status FROM "
                                                 "asset_change")),
        "settings": sorted((r["key"], r["value_json"]) for r in db.q("SELECT key, value_json FROM settings")),
        "sources": sorted((r["provider"], r["name"], r["account"]) for r in db.q("SELECT * FROM data_source")),
    }


@pytest.fixture
def source(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    import os
    import shutil
    import time

    shutil.copy(SAMPLE, config.import_dir / "beispiel.zip")
    os.utime(config.import_dir / "beispiel.zip", (time.time() - 7200, time.time() - 7200))
    with _client(config) as c:
        ctx = c.app.state.ctx
        tasks.import_check(ctx, "test")
        tasks.refresh_prices(ctx, force=True)
        tasks.backfill(ctx)
        _prepare(c)
        js = journal_service(ctx)
        assert not js.save_asset({"asset_id": "MATIC", "name": "Polygon", "asset_class": "crypto",
                                  "quote_source": "coingecko", "quote_id": "matic-network"}).errors
        for acc, qty, d in (("Börse X", "100", "2023-05-01"), ("Ledger", "50,125", "2024-06-01")):
            assert not js.save({"kind": "buy", "asset": "MATIC", "qty": qty, "amount": "40", "ccy": "EUR",
                                "account": acc, "date": d}).errors
        cid, p = asset_change_service(ctx).apply("MATIC", {"kind": "migration", "date": "2024-09-04", "ratio": "1",
                                                           "new_asset": "POL", "new_name": "POL",
                                                           "quote_source": "coingecko",
                                                           "quote_id": "polygon-ecosystem-token"})
        assert cid is not None, p.errors
        fid, _a = tax_import_service(ctx).register(json.dumps(TAX_DOC).encode(), "steuer-2024.json", "upload")
        assert tax_import_service(ctx).activate(fid).get("activated")
        wl = watchlist_service(ctx)
        entry, errs, _ = wl.resolve("security", "SAP.DE")
        assert not errs and wl.add(wl.default_id(), entry)[0]
        from app.datasources.service import datasource_service

        assert not datasource_service(ctx).create({"kind": "exchange", "provider": "bitpanda", "name": "BP",
                                                   "account": "Bitpanda", "sync_interval_min": "60",
                                                   "api_key": SECRET})[1]
        ctx.settings.set("prices.crypto_interval_min", 15)
        config.sources_path.write_text("# eigene Quellen\nsources: []\n", encoding="utf-8")
        body = c.get("/journal/export.zip").content
        assert SECRET.encode() not in body
        yield {"zip": body, "state": state(ctx), "config": config}


def test_roundtrip_restores_financial_state(source, tmp_path):
    cfg = _other(source["config"], tmp_path, "b")
    _place(cfg, source["zip"], "portfolia-export.zip")
    with _client(cfg) as c:
        ctx = c.app.state.ctx
        out = tasks.import_check(ctx, "test")
        assert out.status == "imported" and ctx.db.get_state(f"restore.{out.import_id}")["state"] == "applied"
        a, b = source["state"], state(ctx)
        for key in ("txs", "balances", "lots", "disposals", "tax", "tax_files", "tax_records", "watchlist",
                    "changes", "sources"):
            assert a[key] == b[key], key
        # Kurse: alle exportierten Werte unverändert; zusätzliche nur für Reihen, die die Quelle noch nicht abgerufen
        # hatte (der Start-Job der neuen Instanz lädt sie nach)
        assert set(a["prices"]) <= set(b["prices"])
        assert not {r[0] for r in set(b["prices"]) - set(a["prices"])} & {r[0] for r in a["prices"]}
        assert ("prices.crypto_interval_min", "15") in b["settings"]
        # Originaldatei der Steuerdaten liegt wieder vor
        f = ctx.db.q1("SELECT path FROM tax_file WHERE status='active'")
        from pathlib import Path

        assert f["path"] and Path(f["path"]).is_file()
        # Datenquelle ohne Zugangsdaten: eindeutig gemeldet, nie stillschweigend „ok“
        from app.datasources.service import datasource_service

        svc = datasource_service(ctx)
        ds = svc.list()[0]
        assert ctx.db.scalar("SELECT COUNT(*) FROM data_source_secret") == 0
        res = svc.check(int(ds.id))
        msg = json.dumps(res, ensure_ascii=False, default=str)
        assert "Kein API-Key hinterlegt" in msg
        # Umstellung von vor dem Umzug: Rückgängig wird sicher abgelehnt (Buchungen sind jetzt Import-Buchungen)
        cid = ctx.db.scalar("SELECT id FROM asset_change WHERE kind='migration'")
        assert asset_change_service(ctx).revert(cid)
        # zweite Übernahme: idempotent, nichts doppelt
        counts = fullexport.apply(ctx, out.import_id)
        assert counts["taxdata"] == 0 and counts["watchlist"] == 0 and counts["prices"] == 0
        assert counts["asset_changes"] == 0 and counts["datasources"] == 0
        assert state(ctx) == b


def test_failure_during_restore_changes_nothing(source, tmp_path, monkeypatch):
    cfg = _other(source["config"], tmp_path, "c")
    with _client(cfg) as c:
        ctx = c.app.state.ctx
        ctx.settings.set("ui.default_range", "3J")  # bestehende Installation → Übernahme erst auf Anfrage
        cfg.sources_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.sources_path.write_text("# alt\n", encoding="utf-8")
        _place(cfg, source["zip"], "portfolia-export.zip")
        out = tasks.import_check(ctx, "test")
        before = {t: ctx.db.q(f"SELECT * FROM {t}") for t in ("settings", "tax_file", "watchlist_item",
                                                               "asset_change", "data_source", "price_daily")}
        before = {t: [tuple(r) for r in rows] for t, rows in before.items()}

        def boom(*a, **kw):
            raise sqlite3.OperationalError("simuliert: Datenträger voll")

        monkeypatch.setattr(fullexport, "_taxdata", boom)
        with pytest.raises(sqlite3.OperationalError):
            fullexport.apply(ctx, out.import_id)
        after = {t: [tuple(r) for r in ctx.db.q(f"SELECT * FROM {t}")] for t in before}
        assert after == before
        assert cfg.sources_path.read_text(encoding="utf-8") == "# alt\n"  # Datei unverändert
        assert not list(cfg.sources_path.parent.glob(".*restore-*"))  # keine Reste
        monkeypatch.undo()
        counts = fullexport.apply(ctx, out.import_id)  # danach vollständig möglich
        assert counts["taxdata"] == 1 and cfg.sources_path.read_text(encoding="utf-8").startswith("# eigene")


@pytest.mark.parametrize("damage", ["truncated", "garbage"])
def test_damaged_archive_is_rejected_without_side_effects(source, tmp_path, damage):
    cfg = _other(source["config"], tmp_path, f"d-{damage}")
    body = source["zip"]
    data = body[: len(body) // 2] if damage == "truncated" else b"PK\x03\x04" + b"\x00" * 200
    _place(cfg, data, "kaputt.zip")
    with _client(cfg) as c:
        ctx = c.app.state.ctx
        out = tasks.import_check(ctx, "test")
        assert out.status != "imported"
        assert ctx.active_import_id() is None and not ctx.db.scalar("SELECT COUNT(*) FROM settings")
        assert not ctx.db.scalar("SELECT COUNT(*) FROM tax_file")


@pytest.mark.parametrize("version", [1, 4, 7, 10, 13, 15, 16])
def test_migration_from_older_schema_keeps_data(tmp_path, version):
    db = Database(tmp_path / f"v{version}.sqlite")
    db.migrate(target=version)
    assert db.scalar("PRAGMA user_version") == version
    db.x("INSERT INTO settings(key, value_json, updated_at) VALUES ('ui.default_range', '\"3J\"', '2024-01-01')")
    db.x("INSERT INTO price_daily(series, date, close, ccy, source, fetched_at) VALUES "
         "('cg:bitcoin', '2024-01-02', 40000.5, 'EUR', 'coingecko', '2024-01-02')")
    if version >= 4:
        db.x("INSERT INTO journal_tx(tx_id, source, status, ts_utc, date_only, type, to_account, to_asset, to_qty, "
             "value_eur, created_at, updated_at) VALUES ('PF-M-000001', 'manual', 'active', '2024-01-02T10:00:00Z', "
             "0, 'deposit', 'Wallet', 'BTC', '0.125', '5000', '2024-01-02', '2024-01-02')")
    db.migrate()
    assert db.scalar("PRAGMA user_version") == MIGRATIONS[-1][0]
    assert db.scalar("SELECT value_json FROM settings WHERE key='ui.default_range'") == '"3J"'
    assert db.scalar("SELECT close FROM price_daily WHERE series='cg:bitcoin'") == 40000.5
    if version >= 4:
        row = db.q1("SELECT * FROM journal_tx WHERE tx_id='PF-M-000001'")
        assert row["status"] == "active" and Decimal(row["to_qty"]) == Decimal("0.125")
    db.migrate()  # erneut: idempotent
    assert db.scalar("PRAGMA user_version") == MIGRATIONS[-1][0]
