"""Ticker-/Token-Änderungen: Erkennung (Register, CoinGecko-Katalog, Anbieter-Bestände, Kursstillstand), Umstellung
mit Buchungen je Konto (Restbestand, nach der letzten Bewegung, idempotent, rückgängig), Umbenennung als Overlay mit
verketteter Kurshistorie. Synthetische Daten (Beispiel-ZIP, Demo-Kurse), keine Netzabrufe.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.assetchange import detect as D
from app.assetchange.service import asset_change_service
from app.main import build_app
from app.util.timeutil import today_local

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"


@pytest.fixture
def client(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.token = c.get("/settings") and c.cookies.get("portfolia_csrf")
        assert c.post("/actions/import/check", headers={"X-CSRF-Token": c.token}).status_code == 200
        from app.jobs import tasks

        tasks.refresh_prices(c.app.state.ctx, force=True)
        tasks.backfill(c.app.state.ctx)
        yield c


def post(c, url, data=None, **kw):
    return c.post(url, data={**(data or {}), "csrf_token": c.token}, follow_redirects=False, **kw)


def ctx(c):
    return c.app.state.ctx


def js(c):
    from app.journal.service import journal_service

    return journal_service(ctx(c))


def _matic(c) -> None:
    """MATIC (CoinGecko matic-network) auf zwei Konten, ein Teil nach dem Stichtag noch bewegt."""
    assert not js(c).save_asset({"asset_id": "MATIC", "name": "Polygon", "asset_class": "crypto",
                                 "quote_source": "coingecko", "quote_id": "matic-network"}).errors
    for acc, qty, d in (("Börse X", "100", "2024-05-01"), ("Ledger", "50", "2024-06-01")):
        res = js(c).save({"kind": "buy", "asset": "MATIC", "qty": qty, "price": "0,50", "ccy": "EUR", "account": acc,
                          "date": d, "time": "10:00"})
        assert not res.errors, res.errors
    res = js(c).save({"kind": "sell", "asset": "MATIC", "qty": "10", "price": "0,40", "ccy": "EUR",
                      "account": "Börse X", "date": "2024-10-01", "time": "09:00"})
    assert not res.errors, res.errors


def held(c, asset, account=None):
    led = ctx(c).ledger()
    if account:
        return led.balances.get((account, asset), Decimal(0))
    return led.holdings_by_asset().get(asset, Decimal(0))


# -- Umstellung MATIC → POL ---------------------------------------------------------------------------------

def test_known_migration_hint_preview_apply_and_revert(client):
    _matic(client)
    hs = [h for h in D.detect(ctx(client)) if h.old_asset == "MATIC"]
    assert hs and hs[0].confidence == "hoch" and hs[0].ratio == 1 and hs[0].effective == date(2024, 9, 4)
    assert hs[0].new_symbol == "POL" and hs[0].quote_id == "polygon-ecosystem-token"
    page = client.get("/changes").text
    assert "MATIC → POL" in page and "Prüfen" in page
    assert "Mögliche Token-Umstellung: MATIC → POL" in client.get("/").text
    form = client.get(f"/changes/new?{hs[0].query()}").text
    assert 'value="polygon-ecosystem-token"' in form and 'value="2024-09-04"' in form

    data = {"asset": "MATIC", "hint": hs[0].key, "kind": "migration", "date": "2024-09-04", "ratio": "1",
            "new_asset": "POL", "new_name": "POL (ex-MATIC)", "quote_source": "coingecko",
            "quote_id": "polygon-ecosystem-token"}
    before = ctx(client).db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active'")
    prev = post(client, "/changes/preview", data)
    assert prev.status_code == 200 and "Übernehmen" in prev.text
    assert "01.10.2024" in prev.text  # Börse X: nach dem Verkauf am 01.10. gebucht, nicht am Stichtag
    assert ctx(client).db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active'") == before  # nur Vorschau

    r = post(client, "/changes/apply", data)
    assert r.status_code == 303, r.text
    assert held(client, "MATIC") == 0 and held(client, "POL") == 140
    assert held(client, "POL", "Börse X") == 90 and held(client, "POL", "Ledger") == 50
    rows = ctx(client).db.q("SELECT * FROM journal_tx WHERE status='active' AND type='corporate_action' "
                            "AND tag='migration'")
    assert len(rows) == 2 and {r["to_asset"] for r in rows} == {"POL"}
    lots = ctx(client).ledger().lots_for("POL", "Ledger")
    assert lots and lots[0].acq_date == date(2024, 6, 1)  # Anschaffungsdatum bleibt (keine Veräußerung)
    pol = ctx(client).portfolio().assets["POL"]
    assert pol.quote_source == "coingecko" and pol.quote_id == "polygon-ecosystem-token"
    assert not [h for h in D.detect(ctx(client)) if h.old_asset == "MATIC"]  # kein Restbestand mehr
    # erneut anwenden: nichts mehr umzustellen (keine doppelten Buchungen)
    again = post(client, "/changes/preview", data)
    assert again.status_code == 400 and "Kein Restbestand" in again.text

    cid = ctx(client).db.scalar("SELECT id FROM asset_change WHERE kind='migration'")
    assert post(client, f"/changes/{cid}/revert").headers["location"].startswith(f"/changes?confirm={cid}")
    assert post(client, f"/changes/{cid}/revert", {"confirm": "1"}).status_code == 303
    assert held(client, "MATIC") == 140 and held(client, "POL") == 0
    assert ctx(client).db.scalar("SELECT status FROM asset_change WHERE id=?", (cid,)) == "reverted"


def test_migration_into_existing_asset_with_ratio(client):
    """Umstellung auf ein vorhandenes Asset mit Verhältnis 1:1000 (z. B. Redenominierung)."""
    svc = asset_change_service(ctx(client))
    p = svc.plan("SUI", {"kind": "migration", "date": (today_local() - timedelta(days=1)).isoformat(),
                         "ratio": "1:1000", "new_asset": "SOL"})
    assert p.ok and p.ratio == 1000 and p.new_exists
    q = held(client, "SUI")
    cid, p = svc.apply("SUI", {"kind": "migration", "date": (today_local() - timedelta(days=1)).isoformat(),
                               "ratio": "1:1000", "new_asset": "SOL"})
    assert cid is not None and held(client, "SUI") == 0
    assert sum(r.qty_new for r in p.rows) == q * 1000
    bad = svc.plan("SUI", {"kind": "migration", "date": "2999-01-01", "new_asset": "SOL"})
    assert any("Zukunft" in e for e in bad.errors)
    same = svc.plan("BTC", {"kind": "migration", "date": "2024-01-01", "new_asset": "BTC"})
    assert any("gleich" in e for e in same.errors)


# -- Umbenennung ---------------------------------------------------------------------------------------------

def test_rename_switches_quote_keeps_bookings_and_carries_history(client):
    c = ctx(client)
    a = c.portfolio().assets["WKN:918422"]
    old_series = c.prices.series_for(a)
    n_old = c.db.scalar("SELECT COUNT(*) FROM price_daily WHERE series=?", (old_series,))
    assert n_old > 100
    q_before = held(client, "WKN:918422")
    tx_before = len(c.portfolio().txs)
    eff = today_local() - timedelta(days=10)
    data = {"asset": "WKN:918422", "kind": "rename", "date": eff.isoformat(), "quote_source": "yahoo",
            "quote_id": "nvdx.de", "new_name": "NVIDIA (neu)", "new_ticker": "NVDX"}
    prev = post(client, "/changes/preview", data)
    assert prev.status_code == 200 and "NVDX.DE" in prev.text and "bleiben unverändert" in prev.text
    assert post(client, "/changes/apply", data).status_code == 303
    b = c.portfolio().assets["WKN:918422"]
    assert (b.quote_source, b.quote_id, b.name) == ("yahoo", "NVDX.DE", "NVIDIA (neu)")
    assert "NVDX" in b.aliases and b.extra["ticker_change"]["old_quote"] == "yahoo:NVDA"
    assert held(client, "WKN:918422") == q_before and len(c.portfolio().txs) == tx_before
    new_series = c.prices.series_for(b)
    carried = c.db.q("SELECT date, source FROM price_daily WHERE series=? AND source LIKE 'prev:%' ORDER BY date",
                     (new_series,))
    assert carried and carried[-1]["date"] <= eff.isoformat() and carried[0]["source"] == f"prev:{old_series}"
    assert c.db.scalar("SELECT asset_id FROM csv_symbol WHERE symbol='NVDX'") == "WKN:918422"
    detail = client.get("/asset/WKN:918422").text
    assert "Umbenannt am" in detail and "yahoo:NVDA" in detail

    from app.fullexport import collect

    assert any(b'"asset_changes"' in v and b"NVDX.DE" in v for v in collect(c, set()).values())  # im Gesamtexport
    cid = c.db.scalar("SELECT id FROM asset_change WHERE kind='rename'")
    assert not asset_change_service(c).revert(cid)
    a2 = c.portfolio().assets["WKN:918422"]
    assert (a2.quote_id, a2.name) == ("NVDA", a.name)
    assert not c.db.scalar("SELECT COUNT(*) FROM price_daily WHERE source LIKE 'prev:%'")
    assert c.db.scalar("SELECT COUNT(*) FROM csv_symbol WHERE symbol='NVDX'") == 0


def test_rename_validation(client):
    svc = asset_change_service(ctx(client))
    p = svc.plan("BTC", {"kind": "rename", "date": "2024-01-01"})
    assert any("Keine Änderung" in e for e in p.errors)
    p = svc.plan("BTC", {"kind": "rename", "date": "2024-01-01", "quote_source": "yahoo", "quote_id": "../x"})
    assert any("Yahoo-Symbol" in e for e in p.errors)
    p = svc.plan("BTC", {"kind": "rename", "date": "2024-01-01", "new_ticker": "ETH"})
    assert p.ok and any("bereits ein Asset" in w for w in p.warnings)
    assert post(client, "/changes/preview", {"asset": "EUR", "kind": "rename", "date": "2024-01-01"}
                ).status_code == 404


# -- weitere Erkennungssignale ------------------------------------------------------------------------------

def _catalog(c, coins):
    from app.prices.sources import catalog_path

    p = catalog_path(c)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(json.dumps({"fetched_at": datetime.now(UTC).isoformat(), "coins": coins}).encode()))


def test_catalog_old_marker_suggests_successor_with_unknown_ratio(client):
    c = ctx(client)
    assert not js(client).save_asset({"asset_id": "AERGO", "name": "Aergo", "asset_class": "crypto",
                                      "quote_source": "coingecko", "quote_id": "aergo"}).errors
    assert not js(client).save({"kind": "buy", "asset": "AERGO", "qty": "20", "price": "1", "ccy": "EUR",
                                "account": "Börse X", "date": "2024-03-01"}).errors
    _catalog(c, [{"id": "aergo", "symbol": "aergo", "name": "Aergo [OLD]"},
                 {"id": "aergo-2", "symbol": "aergo", "name": "Aergo"},
                 {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}])
    h = next(h for h in D.detect(c) if h.old_asset == "AERGO")
    assert h.kind == "migration" and h.quote_id == "aergo-2" and h.ratio is None and h.confidence == "mittel"
    form = client.get(f"/changes/new?{h.query()}").text
    assert "Verhältnis prüfen" in form
    # ausblenden und wieder anzeigen
    post(client, "/changes/dismiss", {"hint": h.key, "asset": "AERGO"})
    assert not [x for x in D.detect(c) if x.old_asset == "AERGO"]
    post(client, "/changes/undismiss", {"hint": h.key})
    assert [x for x in D.detect(c) if x.old_asset == "AERGO"]


def test_observed_balances_confirm_migration(client, monkeypatch):
    _matic(client)
    c = ctx(client)
    ds = SimpleNamespace(id=1, account="Ledger")
    items = [{"asset_id": "MATIC", "explained": Decimal(50), "observed": Decimal(0), "diff": Decimal(-50),
              "state": "diff"},
             {"asset_id": "BTC", "explained": Decimal(0), "observed": Decimal(50), "diff": Decimal(50),
              "state": "diff"}]
    fake = SimpleNamespace(list=lambda: [ds], balances=lambda sid: [1], holdings=lambda d: {"items": items})
    import app.datasources.service as S

    monkeypatch.setattr(S, "datasource_service", lambda _ctx: fake)
    h = next(h for h in D.detect(c) if h.old_asset == "MATIC")
    # Register sagt POL; der Anbieter meldet mehr BTC – passt nicht zum Register-Nachfolger, keine Bestätigung
    assert h.confidence == "hoch" and len(h.reasons) == 1
    assert not js(client).save_asset({"asset_id": "POL", "name": "POL", "asset_class": "crypto",
                                      "quote_source": "coingecko", "quote_id": "polygon-ecosystem-token"}).errors
    items[1]["asset_id"] = "POL"
    h = next(h for h in D.detect(c) if h.old_asset == "MATIC")
    assert h.new_asset == "POL" and any("Anbieter meldet 0 MATIC" in r for r in h.reasons)


def test_stale_market_price_hint(client):
    c = ctx(client)
    assert not js(client).save_asset({"asset_id": "OLDX", "name": "Alte Aktie", "asset_class": "security",
                                      "quote_source": "yahoo", "quote_id": "OLDX"}).errors
    assert not js(client).save({"kind": "buy", "asset": "OLDX", "qty": "3", "price": "10", "ccy": "EUR",
                                "account": "Depot A", "date": "2024-03-01"}).errors
    series = c.prices.series_for(c.portfolio().assets["OLDX"])
    c.db.x("DELETE FROM price_daily WHERE series=?", (series,))
    last = today_local() - timedelta(days=45)
    c.db.x("INSERT INTO price_daily(series, date, close, ccy, source, fetched_at) VALUES (?,?,?,?,?,?)",
           (series, last.isoformat(), 10.0, "EUR", "demo", datetime.now(UTC).isoformat()))
    h = next(h for h in D.detect(c) if h.old_asset == "OLDX")
    assert h.kind == "unknown" and h.confidence == "niedrig" and last.strftime("%d.%m.%Y") in h.reasons[0]
