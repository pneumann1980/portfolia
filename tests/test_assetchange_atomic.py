"""AP1 – Token-Migrationen atomar und idempotent.

Fehlerinjektion an den Transaktionsgrenzen (Ziel-Asset, erste/zweite Buchung, Änderungsdatensatz): danach entspricht
der persistierte *und* der berechnete Zustand exakt dem Zustand vor der Operation. Dazu Verhältnisse (1:1, 1:1000,
Dezimal), mehrere Konten und Lots, Vorschau gegen geänderten Bestand, doppelte und gleichzeitige Ausführung,
Rückgängig/Wiederholen, nachträglich importierte historische Buchungen und die steuerliche Kennzeichnung.
Synthetische Daten in temporärer Datenbank.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.assetchange import service as S
from app.assetchange.service import asset_change_service
from app.main import build_app

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"
TABLES = ("journal_tx", "journal_log", "journal_asset", "asset_change", "csv_symbol", "price_daily")
MIG = {"kind": "migration", "date": "2024-09-04", "ratio": "1", "new_asset": "POL", "new_name": "POL (ex-MATIC)",
       "quote_source": "coingecko", "quote_id": "polygon-ecosystem-token"}


@pytest.fixture
def ctx(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(config, start_scheduler=False)) as c:
        tok = c.get("/settings") and c.cookies.get("portfolia_csrf")
        assert c.post("/actions/import/check", headers={"X-CSRF-Token": tok}).status_code == 200
        ctx = c.app.state.ctx
        ctx.client, ctx.token = c, tok
        _matic(ctx)
        yield ctx


def js(ctx):
    from app.journal.service import journal_service

    return journal_service(ctx)


def _matic(ctx) -> None:
    """MATIC mit zwei Lots auf „Börse X“ (unterschiedliche Anschaffung) und einem auf „Ledger“."""
    assert not js(ctx).save_asset({"asset_id": "MATIC", "name": "Polygon", "asset_class": "crypto",
                                   "quote_source": "coingecko", "quote_id": "matic-network"}).errors
    for acc, qty, eur, d in (("Börse X", "60", "30", "2023-03-01"), ("Börse X", "40", "28", "2024-05-01"),
                             ("Ledger", "50", "40", "2024-06-01")):
        res = js(ctx).save({"kind": "buy", "asset": "MATIC", "qty": qty, "amount": eur, "ccy": "EUR", "account": acc,
                            "date": d, "time": "10:00"})
        assert not res.errors, res.errors


def snapshot(ctx) -> dict:
    """Persistierter und berechneter Zustand (fachlich relevante Teile)."""
    db = ctx.db
    out = {t: db.q(f"SELECT * FROM {t} ORDER BY 1, 2") for t in TABLES}
    out = {t: [tuple(r) for r in rows] for t, rows in out.items()}
    ctx.invalidate_overlay()
    led = ctx.ledger()
    out["balances"] = sorted((k, v) for k, v in led.balances.items())
    out["lots"] = sorted((lot.asset, lot.account, lot.qty, lot.cost, lot.acq_date) for lot in led.lots)
    out["assets"] = sorted(ctx.portfolio().assets)
    return out


def bal(ctx, asset, account=None):
    led = ctx.ledger()
    if account:
        return led.balances.get((account, asset), Decimal(0))
    return led.holdings_by_asset().get(asset, Decimal(0))


# -- Erfolgreiche Umstellungen ------------------------------------------------------------------------------

@pytest.mark.parametrize(("ratio", "factor"), [("1", Decimal(1)), ("1:1000", Decimal(1000)),
                                               ("0,5", Decimal("0.5")), ("1.2345", Decimal("1.2345"))])
def test_ratios_keep_cost_basis_and_acquisition_dates(ctx, ratio, factor):
    led = ctx.ledger()
    cost_before = led.cost_basis("MATIC")
    dates_before = sorted((lot.account, lot.acq_date) for lot in led.lots_for("MATIC"))
    cid, p = asset_change_service(ctx).apply("MATIC", {**MIG, "ratio": ratio})
    assert cid is not None, p.errors
    assert bal(ctx, "MATIC") == 0
    assert bal(ctx, "POL", "Börse X") == 100 * factor and bal(ctx, "POL", "Ledger") == 50 * factor
    led = ctx.ledger()
    assert led.cost_basis("POL") == cost_before  # Kostenbasis unverändert
    assert sorted((lot.account, lot.acq_date) for lot in led.lots_for("POL")) == dates_before
    assert sum(lot.qty for lot in led.lots_for("POL")) == bal(ctx, "POL")  # Lots = Bestand
    assert all(lot.via for lot in led.lots_for("POL"))  # Herkunft aus Umstellung bleibt erkennbar


def test_partial_and_previously_migrated_holdings(ctx):
    """Teilweise bereits umgestellt (z. B. von der Börse gemeldet): nur der Restbestand wird umgestellt."""
    assert not js(ctx).save_asset({"asset_id": "POL", "name": "POL", "asset_class": "crypto",
                                   "quote_source": "coingecko", "quote_id": "polygon-ecosystem-token"}).errors
    res = js(ctx).save({"kind": "corporate", "tag": "migration", "account": "Ledger", "date": "2024-09-10",
                        "from_asset": "MATIC", "from_qty": "50", "to_asset": "POL", "to_qty": "50"})
    assert not res.errors
    p = asset_change_service(ctx).plan("MATIC", MIG)
    assert p.ok and [(r.account, r.qty_old) for r in p.rows] == [("Börse X", Decimal(100))]
    cid, _p = asset_change_service(ctx).apply("MATIC", MIG)
    assert cid is not None and bal(ctx, "MATIC") == 0 and bal(ctx, "POL") == 150


# -- Fehlerinjektion: nichts bleibt zurück --------------------------------------------------------------------

def _fail_on_call(monkeypatch, n: int, how: str = "errors"):
    from app.journal.service import JournalService, SaveResult

    real = JournalService.save
    calls = {"n": 0}

    def fake(self, data, tx_id=None):
        calls["n"] += 1
        if calls["n"] == n:
            if how == "raise":
                raise RuntimeError("Datenträger voll (simuliert)")
            return SaveResult(errors=["simulierter Fehler"])
        return real(self, data, tx_id)

    monkeypatch.setattr(JournalService, "save", fake)
    return calls


@pytest.mark.parametrize("how", ["errors", "raise"])
def test_failure_after_first_booking_rolls_back_everything(ctx, monkeypatch, how):
    before = snapshot(ctx)
    calls = _fail_on_call(monkeypatch, 2, how)
    cid, p = asset_change_service(ctx).apply("MATIC", MIG)
    assert cid is None and p.errors and calls["n"] == 2
    assert snapshot(ctx) == before  # auch das neu angelegte Asset POL und das Journal-Protokoll


def test_failure_creating_target_asset(ctx, monkeypatch):
    from app.journal.service import JournalService, SaveResult

    before = snapshot(ctx)
    monkeypatch.setattr(JournalService, "save_asset", lambda self, data, asset_id=None: SaveResult(errors=["kaputt"]))
    cid, p = asset_change_service(ctx).apply("MATIC", MIG)
    assert cid is None and "kaputt" in p.errors
    assert snapshot(ctx) == before


def test_failure_saving_change_record(ctx, monkeypatch):
    before = snapshot(ctx)
    real_now = S._now
    state = {"armed": True}

    def boom():
        if state["armed"]:
            raise OSError("simuliert: Schreiben des Änderungsdatensatzes")
        return real_now()

    monkeypatch.setattr(S, "_now", boom)
    cid, p = asset_change_service(ctx).apply("MATIC", MIG)
    assert cid is None and any("nichts geändert" in e for e in p.errors)
    state["armed"] = False
    assert snapshot(ctx) == before


# -- Wiederholung, Gleichzeitigkeit, veraltete Vorschau ----------------------------------------------------

def test_duplicate_execution_is_idempotent(ctx):
    svc = asset_change_service(ctx)
    assert svc.apply("MATIC", MIG)[0] is not None
    n = ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active' AND tag='migration'")
    cid, p = svc.apply("MATIC", MIG)
    assert cid is None and any("Kein Restbestand" in e for e in p.errors)
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active' AND tag='migration'") == n == 2
    assert bal(ctx, "POL") == 150


def test_concurrent_requests_book_once(ctx):
    results: list = []

    def run():
        try:
            results.append(asset_change_service(ctx).apply("MATIC", MIG)[0])
        finally:
            ctx.db.close_thread_conn()

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sum(1 for r in results if r is not None) == 1
    ctx.invalidate_overlay()
    assert ctx.db.scalar("SELECT COUNT(*) FROM asset_change WHERE kind='migration'") == 1
    assert bal(ctx, "POL") == 150 and bal(ctx, "MATIC") == 0


def test_stale_preview_is_rejected(ctx):
    svc = asset_change_service(ctx)
    fp = svc.plan("MATIC", MIG).fingerprint()
    assert not js(ctx).save({"kind": "buy", "asset": "MATIC", "qty": "5", "amount": "3", "ccy": "EUR",
                             "account": "Ledger", "date": "2024-07-01"}).errors
    before = snapshot(ctx)
    cid, p = svc.apply("MATIC", {**MIG, "fp": fp})
    assert cid is None and any("seit der Vorschau geändert" in e for e in p.errors)
    assert snapshot(ctx) == before
    cid, p = svc.apply("MATIC", {**MIG, "fp": svc.plan("MATIC", MIG).fingerprint()})
    assert cid is not None and bal(ctx, "POL", "Ledger") == 55


def test_web_preview_carries_fingerprint(ctx):
    c = ctx.client
    r = c.post("/changes/preview", data={**MIG, "asset": "MATIC", "csrf_token": ctx.token})
    assert r.status_code == 200 and 'name="fp"' in r.text


# -- Rückgängig, erneut, spätere Buchungen ----------------------------------------------------------------

def test_revert_removes_unused_created_asset_and_reapply(ctx):
    svc = asset_change_service(ctx)
    before = snapshot(ctx)
    cid, _p = svc.apply("MATIC", MIG)
    assert not svc.revert(cid)
    after = snapshot(ctx)
    assert after["balances"] == before["balances"] and after["lots"] == before["lots"]
    assert "POL" not in after["assets"]  # angelegtes, nicht mehr verwendetes Asset entfernt
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE tag='migration' AND status='active'") == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE tag='migration' AND status='reverted'") == 2
    assert svc.revert(cid)  # zweites Rückgängig: Fehlermeldung, keine Änderung
    cid2, _p = svc.apply("MATIC", MIG)
    assert cid2 is not None and bal(ctx, "POL") == 150


def test_revert_refused_when_new_holding_was_used(ctx):
    svc = asset_change_service(ctx)
    cid, _p = svc.apply("MATIC", MIG)
    assert not js(ctx).save({"kind": "sell", "asset": "POL", "qty": "80", "amount": "40", "ccy": "EUR",
                             "account": "Börse X", "date": "2025-01-10"}).errors
    before = snapshot(ctx)
    errors = svc.revert(cid)
    assert errors and "negativen Bestand" in errors[0]
    assert snapshot(ctx) == before


def test_late_historical_booking_after_migration(ctx):
    """Nachträglich importierter Kauf vor dem Stichtag: Restbestand wird erkannt und separat umgestellt."""
    svc = asset_change_service(ctx)
    svc.apply("MATIC", MIG)
    assert not js(ctx).save({"kind": "buy", "asset": "MATIC", "qty": "7", "amount": "4", "ccy": "EUR",
                             "account": "Ledger", "date": "2024-08-01"}).errors
    assert bal(ctx, "MATIC", "Ledger") == 7
    p = svc.plan("MATIC", MIG)
    assert p.ok and [(r.account, r.qty_old) for r in p.rows] == [("Ledger", Decimal(7))]
    assert any("Bereits am" in i for i in p.info)
    # gebucht nach der letzten Bewegung des Kontos (der ersten Umstellung), nicht davor
    first = ctx.db.q1("SELECT ts_utc FROM journal_tx WHERE tag='migration' AND to_account='Ledger'")
    assert p.rows[0].when.isoformat() > "2024-09-04"
    assert first is not None
    assert svc.apply("MATIC", MIG)[0] is not None and bal(ctx, "MATIC") == 0 and bal(ctx, "POL") == 157


# -- Steuer: technische Fortführung ≠ steuerliche Neutralität -------------------------------------------------

def test_tax_report_flags_disposals_from_migrated_lots(ctx):
    svc = asset_change_service(ctx)
    svc.apply("MATIC", MIG)
    assert not js(ctx).save({"kind": "sell", "asset": "POL", "qty": "60", "amount": "90", "ccy": "EUR",
                             "account": "Börse X", "date": "2025-02-01"}).errors
    from app.tax.service import tax_service

    _pack, _inp, res = tax_service(ctx).compute(2025)
    issue = next(i for i in res.issues if i.code == "conversion_unclear")
    assert "MATIC → POL" in issue.text and issue.count == 1
    rows = [r for s in res.sections for t in s.tables for r in t.rows if r.get("asset_id") == "POL"]
    assert rows and rows[0]["acq"] == date(2023, 3, 1)  # Anschaffungsdatum des ursprünglichen Lots
    assert "über Umstellung" in rows[0]["origin"]


def test_rename_failure_leaves_no_alias_or_record(ctx, monkeypatch):
    from app.jobs import tasks

    tasks.refresh_prices(ctx, force=True)
    tasks.backfill(ctx)
    before = snapshot(ctx)

    def boom(self, *a, **kw):
        raise OSError("simuliert: Kursübernahme")

    monkeypatch.setattr(S.AssetChangeService, "_carry_prices", boom)
    cid, p = asset_change_service(ctx).apply("WKN:918422", {"kind": "rename", "date": "2026-01-02",
                                                            "quote_source": "yahoo", "quote_id": "NVDX.DE",
                                                            "new_ticker": "NVDX"})
    assert cid is None and p.errors
    assert snapshot(ctx) == before
    assert ctx.portfolio().assets["WKN:918422"].quote_id == "NVDA"
