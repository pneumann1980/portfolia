"""Export/Restore nach Financial Integrity (Phase 10) – synthetische Daten, temporäre Instanzen.

Geprüft: Derivat-Buchungen (Funding, Liquidation) überstehen Gesamtexport und Neueinrichtung mit Kennzeichen, Menge
und Wert; Ledger-Ursprung bleibt „derivative“ (nicht § 23), belegte Lot-Herkunft und steuerliche Einordnung zeigen
danach dasselbe Bild. Sparplan-Schätzungen sind keine Buchungen (nicht im Export, nach dem Umzug neu geschätzt) und
werden nie als belegter Kauf eingeordnet.
"""

from __future__ import annotations

import os
import shutil
import time
from decimal import Decimal

import pytest

from app.diagnosis import cockpit as CP
from app.jobs import tasks
from app.journal.service import journal_service
from app.ledger import provenance as PV
from tests.test_full_export import MASTER, SAMPLE, _client, _other, _place, _prepare


def snapshot(ctx) -> dict:
    ctx.invalidate_data()
    pf = ctx.recorded_portfolio()
    led = ctx.ledger()
    pv = PV.build(ctx)
    return {
        "deriv": sorted((t.type, t.tag, t.from_asset, str(t.from_qty), t.to_asset, str(t.to_qty), str(t.value_eur))
                        for t in pf.txs if t.tag in ("funding", "liquidation")),
        "bybit": sorted((k, v) for k, v in led.balances.items() if k[0] == "Bybit"),
        "deriv_disposals": sorted((d.asset, d.date, d.qty) for d in led.disposals if d.kind == "derivative"),
        # Sparplan-Schätzungen sind keine Buchungen und werden neu geschätzt – verglichen wird der belegte Teil
        "provenance": sorted((a.asset, tuple(sorted((k, q) for k, q in a.by_class.items() if k != "geschaetzt")))
                             for a in pv.assets),
        # Anschaffungs-Dimension zählt Schätzungen mit (s. o.) – verglichen wird die steuerliche Einordnung
        "cockpit": [(d.key, d.state, d.figure) for d in CP.build(ctx).dims if d.key == "einordnung"],
    }


@pytest.fixture
def source(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    shutil.copy(SAMPLE, config.import_dir / "beispiel.zip")
    os.utime(config.import_dir / "beispiel.zip", (time.time() - 7200, time.time() - 7200))
    with _client(config) as c:
        ctx = c.app.state.ctx
        tasks.import_check(ctx, "test")
        _prepare(c)
        js = journal_service(ctx)
        assert not js.save_asset({"asset_id": "USDT", "name": "Tether", "asset_class": "crypto",
                                  "quote_source": "none", "quote_id": ""}).errors
        base = {"kind": "expert", "time": "10:00", "note": "synthetisch"}
        for data in (
                {"type": "deposit", "tag": "", "date": "2024-04-30", "to_account": "Bybit", "to_asset": "USDT",
                 "to_qty": "1000", "value_eur": "920"},
                {"type": "deposit", "tag": "funding", "date": "2024-05-01", "to_account": "Bybit", "to_asset": "USDT",
                 "to_qty": "1,25", "value_eur": "1,15"},
                {"type": "withdrawal", "tag": "liquidation", "date": "2024-05-02", "from_account": "Bybit",
                 "from_asset": "USDT", "from_qty": "400", "value_eur": "368"}):
            res = js.save({**base, **data})
            assert not res.errors, res.errors
        body = c.get("/journal/export.zip").content
        ctx.invalidate_data()
        estimated = {t.tx_id for t in ctx.portfolio().txs if t.flag == "estimated"}
        classes = {k for a in PV.build(ctx).assets for k in a.by_class}
        assert not estimated or "geschaetzt" in classes  # Schätzungen nie als belegter Kauf
        yield {"zip": body, "state": snapshot(ctx), "config": config, "estimated": estimated}


def test_derivatives_survive_export_and_restore(source, tmp_path):
    a = source["state"]
    assert len(a["deriv"]) == 2 and dict(a["bybit"])[("Bybit", "USDT")] == Decimal("601.25")
    assert a["deriv_disposals"], "Liquidation muss als Derivat-Abgang erscheinen"
    cfg = _other(source["config"], tmp_path, "b")
    _place(cfg, source["zip"], "portfolia-export.zip")
    with _client(cfg) as c:
        ctx = c.app.state.ctx
        out = tasks.import_check(ctx, "test")
        assert out.status == "imported"
        assert snapshot(ctx) == a
