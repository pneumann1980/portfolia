"""Sparpläne: Erkennung, Schätzung seit Importstand, Freigabe/Anpassung, Export und Abgleich mit neuem Import."""

import csv
import io
import os
import shutil
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.importer.sample import ACCOUNTS, ASSETS, sample_transactions
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.ledger.engine import run_ledger
from app.main import build_app
from app.plans.detect import detect_plans, shift_weekend
from app.plans.service import plan_service
from app.util.timeutil import local_tz
from tests.helpers import portfolio, tx

D = Decimal
SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"
TODAY = date(2026, 9, 27)
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=local_tz())


def _buys(dates, asset="ETH", acc="Börse", amount=100, hour="07:30:00", fee=None):
    return [tx(f"{asset}{i}", f"{d.isoformat()}T{hour}Z", "buy", frm=(acc, "EUR", amount), to=(acc, asset, "0.05"),
               value=amount, fee=fee) for i, d in enumerate(dates)]


# -- Erkennung ----------------------------------------------------------------------------------------

def test_detect_monthly_with_weekend_shift_and_one_off_buy():
    months = [(2025, m) for m in range(8, 13)] + [(2026, m) for m in range(1, 9)]
    ds = [shift_weekend(date(y, m, 25), True) for y, m in months]
    rows = [*_buys(ds), tx("x1", "2026-03-10", "buy", frm=("Börse", "EUR", 5000), to=("Börse", "ETH", "2"),
                           value=5000)]
    (p,) = detect_plans(portfolio(rows), date(2026, 9, 19))
    assert (p.freq, p.days, p.status, p.confidence) == ("monthly", (25,), "active", "hoch")
    assert p.next_due == date(2026, 9, 25) and len(p.executions) == 13 and p.amount == D(100)
    assert "x1" not in {e.tx_id for e in p.executions}  # Einzelkauf gehört nicht zum Plan
    assert p.schedule(date(2026, 9, 19), date(2026, 12, 31)) == [date(2026, 9, 25), date(2026, 10, 26),
                                                                 date(2026, 11, 25), date(2026, 12, 25)]


def test_detect_weekly_semimonthly_quarterly():
    weekly = [date(2026, 3, 2) + timedelta(days=7 * i) for i in range(29)]
    (p,) = detect_plans(portfolio(_buys(weekly, asset="BTC")), date(2026, 9, 19))
    assert (p.freq, p.weekday, p.next_due) == ("weekly", 0, date(2026, 9, 21))
    semi = [shift_weekend(date(2026, m, d), True) for m in range(1, 10) for d in (2, 16)]
    (p,) = detect_plans(portfolio(_buys(semi, asset="SOL")), date(2026, 9, 19))
    assert (p.freq, p.days, p.next_due) == ("semimonthly", (2, 16), date(2026, 10, 2))
    quarterly = [date(2024, 7, 15), date(2024, 10, 15), date(2025, 1, 15), date(2025, 4, 15), date(2025, 7, 15)]
    (p,) = detect_plans(portfolio(_buys(quarterly, asset="ADA")), date(2025, 9, 1))
    assert (p.freq, p.days, p.confidence, p.next_due) == ("quarterly", (15,), "mittel", date(2025, 10, 15))


def test_detect_paused_ended_and_too_short():
    ended = [date(2026, m, 5) for m in range(1, 7)]
    (p,) = detect_plans(portfolio(_buys(ended, asset="DOT")), date(2026, 9, 19))
    assert p.status == "ended" and p.missed == 3
    paused = [date(2026, m, 5) for m in range(1, 9)]
    (p,) = detect_plans(portfolio(_buys(paused, asset="DOT")), date(2026, 9, 19))
    assert p.status == "paused"
    assert detect_plans(portfolio(_buys([date(2026, 7, 5), date(2026, 8, 5)])), date(2026, 9, 1)) == []


def test_detect_amount_change_and_irregular_amounts():
    ds = [date(2026, m, 1) for m in range(1, 9)]
    rows = _buys(ds[:6], amount=100) + [tx(f"n{i}", f"{d.isoformat()}T07:30:00Z", "buy", frm=("Börse", "EUR", 200),
                                           to=("Börse", "ETH", "0.1"), value=200) for i, d in enumerate(ds[6:])]
    (p,) = detect_plans(portfolio(rows), date(2026, 8, 20))
    assert p.amount == D(200)  # jüngste Sparrate gilt
    rows = [tx(f"r{i}", f"{d.isoformat()}T07:30:00Z", "buy", frm=("Börse", "EUR", a), to=("Börse", "ETH", "0.1"),
               value=a) for i, (d, a) in enumerate(zip(ds, [50, 300, 120, 900, 60, 400, 75, 800], strict=True))]
    assert detect_plans(portfolio(rows), date(2026, 8, 20)) == []  # keine stabile Sparrate


def test_detect_external_funding_and_fee():
    ds = [date(2026, m, 3) for m in range(1, 9)]
    rows = _buys(ds, acc="Neo", amount="99", fee=("EUR", 1, 1))
    rows += [tx(f"d{i}", d.isoformat(), "deposit", to=("Neo", "EUR", 100), value=100) for i, d in enumerate(ds)]
    (p,) = detect_plans(portfolio(rows), date(2026, 8, 20))
    assert p.funding == "external" and p.fee == D(1) and p.amount == D(99)


# -- Schätzung im Portfolio und Freigabe ---------------------------------------------------------------

@pytest.fixture
def client(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    app = build_app(config, start_scheduler=False)
    with TestClient(app) as c:
        ctx = app.state.ctx
        tasks.import_check(ctx, "test")  # inkl. Kurse/Historie (Demo) und Sparplan-Aktualisierung
        # Schätzungen auf festen Stichtag zurücksetzen (Tests unabhängig vom Ausführungsdatum)
        ctx.db.x("DELETE FROM tx_estimate")
        plan_service(ctx).update(today=TODAY, now=NOW)
        ctx.invalidate_overlay()
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        yield c


def _svc(c):
    return plan_service(c.app.state.ctx)


def test_estimates_appear_tagged_in_portfolio(client):
    ctx = client.app.state.ctx
    svc = _svc(client)
    res = svc.update(today=TODAY, now=NOW)
    pend = svc.estimates(("estimated",))
    assert {(e["asset_id"], e["due_date"]) for e in pend} == {("WKN:A0RPWH", "2026-09-25"), ("BTC", "2026-09-21")}
    assert res["active"] == 2
    assert svc.update(today=TODAY, now=NOW)["created"] == 0  # idempotent
    btc = next(e for e in pend if e["asset_id"] == "BTC")
    assert btc["value_d"] == D("25.00") and btc["fee_d"] == D("0.25") and btc["qty_d"] > 0
    assert btc["local"].strftime("%H:%M") == "08:00"  # übliche Ausführungszeit (06:00 UTC, Sommerzeit)
    base, pf = ctx.base_portfolio(), ctx.portfolio()
    assert len(pf.txs) == len(base.txs) + 2
    assert {t.flag for t in pf.txs if t.source == "sparplan"} == {"estimated"}
    base_btc = run_ledger(base, ctx.engine_options()).holdings_by_asset()["BTC"]
    assert ctx.ledger().holdings_by_asset()["BTC"] - base_btc == btc["qty_d"]
    assert "geschätzte Sparplan-Ausführungen im Portfolio" in client.get("/").text
    assert "geschätzt" in client.get("/positions").text
    assert "Zu prüfen" in client.get("/plans").text
    from app.tax.service import tax_service

    _, _, r = tax_service(ctx).compute(2026)
    assert any(i.code == "estimated_tx" for i in r.issues)


def test_before_execution_time_no_estimate(client):
    svc = _svc(client)
    client.app.state.ctx.db.x("DELETE FROM tx_estimate")  # Stand vor dem ersten Lauf
    early = datetime(2026, 9, 21, 5, 0, tzinfo=local_tz())
    svc.update(today=early.date(), now=early)
    assert not any(e["asset_id"] == "BTC" and e["due_date"] == "2026-09-21" for e in svc.estimates(("estimated",)))


def test_confirm_edit_dismiss_export(client):
    svc = _svc(client)
    svc.update(today=TODAY, now=NOW)
    pend = {e["asset_id"]: e for e in svc.estimates(("estimated",))}
    etf, btc = pend["WKN:A0RPWH"], pend["BTC"]
    # Anpassen mit Fehlern → 400 und Meldung
    r = client.post(f"/plans/tx/{etf['id']}/edit", data={"date": "2026-09-25", "time": "09:31", "qty": "0",
                                                          "price": "abc", "fee": "0", "confirm": "1"})
    assert r.status_code == 400 and "Kurs muss größer als 0 sein" in r.text and "Stückzahl" in r.text
    # Anpassen und freigeben (deutsche Zahlen)
    r = client.post(f"/plans/tx/{etf['id']}/edit", data={"date": "2026-09-25", "time": "09:31", "qty": "1,234",
                                                          "price": "121,55", "fee": "0", "confirm": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    row = next(e for e in svc.estimates(("confirmed",)) if e["id"] == etf["id"])
    assert row["qty_d"] == D("1.234") and row["price_d"] == D("121.55") and row["value_d"] == D("149.99")
    assert row["user_edited"] == 1 and row["local"].strftime("%H:%M") == "09:31"
    # Freigeben ohne Änderung → Markierung verschwindet
    assert client.post(f"/plans/tx/{btc['id']}/confirm", follow_redirects=False).status_code == 303
    assert svc.estimates(("estimated",)) == []
    ctx = client.app.state.ctx
    assert {t.flag for t in ctx.portfolio().txs if t.source == "sparplan"} == {"confirmed"}
    assert "geschätzte Sparplan" not in client.get("/").text
    # Bestätigte bleiben bei erneuter Aktualisierung erhalten
    svc.update(today=TODAY, now=NOW)
    assert len(svc.estimates(("confirmed",))) == 2
    # Export im transactions.csv-Format
    r = client.get("/plans/export.csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert r.headers["content-type"].startswith("text/csv") and len(rows) == 2
    assert {rw["to_asset"] for rw in rows} == {"WKN:A0RPWH", "BTC"} and all(rw["type"] == "buy" for rw in rows)
    # Verwerfen
    assert client.post(f"/plans/tx/{btc['id']}/dismiss", follow_redirects=False).status_code == 303
    assert len(svc.estimates(("confirmed",))) == 1
    assert client.post("/plans/tx/999999/confirm").status_code == 404


def test_price_plausibility_warning(client):
    svc = _svc(client)
    ctx = client.app.state.ctx
    pend = {e["asset_id"]: e for e in svc.estimates(("estimated",))}
    # Kurse aus dem Demo-Kursverlauf liegen im Rahmen des letzten Import-Kaufs → kein Hinweis
    assert all(e["price_warn"] is None for e in pend.values())
    assert "Kurs prüfen" not in client.get("/plans").text
    # z. B. falsche Kursreihe oder Split: Faktor > 2 ggü. letztem Kauf laut Import
    btc = pend["BTC"]
    ctx.db.x("UPDATE tx_estimate SET price_eur=? WHERE id=?", (str(btc["price_d"] * 7), btc["id"]))
    warn = next(e for e in svc.estimates(("estimated",)) if e["id"] == btc["id"])["price_warn"]
    assert warn is not None and warn["ratio"] > 2 and warn["date"] < TODAY
    page = client.get("/plans").text
    assert "Kurs prüfen" in page and "letzter Kauf laut Import" in page
    assert 'data-confirm="1 Schätzung hat einen auffälligen Kurs' in page
    # nach Anpassung durch den Nutzer gilt der Kurs als geprüft
    r = client.post(f"/plans/tx/{btc['id']}/edit", data={"date": "2026-09-21", "time": "08:00", "qty": "",
                                                          "amount": "25", "price": str(btc["price_d"] * 7),
                                                          "fee": "0,25", "confirm": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert next(e for e in svc.estimates(("estimated",)) if e["id"] == btc["id"])["price_warn"] is None


def test_plan_toggle_and_amount_override(client):
    svc = _svc(client)
    svc.update(today=TODAY, now=NOW)
    key = next(p["key"] for p in svc.plans() if p["asset_id"] == "BTC")
    client.post("/plans/plan/enabled", data={"key": key, "value": "off"})
    assert not any(e["asset_id"] == "BTC" for e in svc.estimates(("estimated",)))
    client.post("/plans/plan/enabled", data={"key": key, "value": "on"})
    svc.update(today=TODAY, now=NOW)
    btc = next(e for e in svc.estimates(("estimated",)) if e["asset_id"] == "BTC")
    client.post("/plans/plan/amount", data={"key": key, "amount": "50,00"})
    btc2 = next(e for e in svc.estimates(("estimated",)) if e["id"] == btc["id"])
    assert btc2["value_d"] == D("50.00") and btc2["qty_d"] > btc["qty_d"]
    assert client.post("/plans/plan/amount", data={"key": key, "amount": "-5"}).status_code == 400


def test_reconcile_with_next_import(client, config):
    ctx = client.app.state.ctx
    svc = _svc(client)
    svc.update(today=TODAY, now=NOW)
    etf = next(e for e in svc.estimates(("estimated",)) if e["asset_id"] == "WKN:A0RPWH")
    btc = next(e for e in svc.estimates(("estimated",)) if e["asset_id"] == "BTC")
    svc.confirm(etf["id"])  # freigegeben, aber im nächsten Import nicht enthalten
    # Neuer Import bis 05.10.: echte BTC-Ausführung (andere Stückzahl/Uhrzeit), ETF-Ausführung fehlt
    rows = [r for r in sample_transactions(until=date(2026, 10, 5))
            if not (r["to_asset"] == "WKN:A0RPWH" and r["datetime"].startswith("2026-09-25"))]
    for r in rows:
        if r["to_asset"] == "BTC" and r["datetime"].startswith("2026-09-21"):
            r["datetime"] = "2026-09-21T06:07:00Z"
            r["to_qty"] = str(D(r["to_qty"]) * D("1.01"))
    old = time.time() - 3600
    path = build_zip(config.import_dir / "neu.zip", transactions=rows, assets=ASSETS, holdings_check=[],
                     issues=[], accounts=ACCOUNTS, extra_tx_columns=["related_asset"],
                     generated_at="2026-10-05T20:00:00Z", valuation_date="2026-10-05")
    os.utime(path, (old + 10, old + 10))
    out = tasks.import_check(ctx, "test")
    assert out.status == "imported", out.message
    st = {e["id"]: e for e in svc.estimates(("estimated", "confirmed", "superseded", "missing", "dismissed"))}
    assert st[btc["id"]]["status"] == "superseded" and st[btc["id"]]["matched_tx_id"]
    assert st[etf["id"]]["status"] == "confirmed" and st[etf["id"]]["missing_import_id"] == out.import_id
    assert "fehlen im aktuellen Import" in client.get("/").text
    # keine Doppelzählung: überholte Schätzung ist nicht mehr im Portfolio
    assert btc["tx_id"] not in {t.tx_id for t in ctx.portfolio().txs}
