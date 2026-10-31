"""„Heute“ als Kalendertag (Europe/Berlin): Wertpapiere nur bei Handel heute (sonst ±0, dafür „letzter Handelstag“),
Krypto seit Mitternacht (ersatzweise 24 h), manuelle Kurse nur bei Eintrag heute, heutige Käufe/Verkäufe ab ihrem Kurs,
Prozentwert der Summe auf den Wert um 0:00 Uhr."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.analytics.valuation import _day, _today_flows, value_positions
from app.ledger.engine import run_ledger
from app.ledger.models import AssetInfo, Portfolio
from app.prices.models import PriceInfo, Quote
from app.prices.service import PriceService
from app.prices.store import PriceStore
from app.settings_store import Settings
from app.util.timeutil import local_tz, to_local_date
from app.web.fmt import trading_day
from tests.helpers import portfolio, tx

STOCK = AssetInfo("WKN:1", "Muster AG", "security", "yahoo", "MUS.DE")
US = AssetInfo("WKN:2", "Example Corp", "security", "yahoo", "EXMP")
COIN = AssetInfo("C#1", "Coin", "crypto", "coingecko", "coin")
SAT = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)  # Samstag
FRI_CLOSE = datetime(2026, 10, 30, 16, 35, tzinfo=UTC)  # Xetra-Schluss Freitag (17:35 Uhr in Berlin)


def svc(db) -> PriceService:
    return PriceService(db, Settings(db), PriceStore(db), yahoo=None, coingecko=None, ecb=None)


def berlin(y, m, d, h, mi=0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=local_tz())


def latest(s, asset, now) -> PriceInfo:
    return s.latest_eur_many([asset], Portfolio(None, [], {asset.asset_id: asset}, {}), now=now)[asset.asset_id]


# ---- Wertpapiere ------------------------------------------------------------------------------------------

def test_stock_on_weekend_is_flat_today_and_shows_the_last_trading_day(db):
    s = svc(db)
    s.store.upsert_quotes([Quote("yahoo:MUS.DE", 110.0, "EUR", FRI_CLOSE, "yahoo", prev_close=100.0)])
    pi = latest(s, STOCK, SAT)
    assert pi.day_ref_eur == pytest.approx(110.0) and not pi.traded_today and pi.day_basis == "sitzung"
    assert pi.session_date == to_local_date(FRI_CLOSE) and pi.prev_close_eur == pytest.approx(100.0)
    assert trading_day(pi.session_date) == "Fr., 30.10."
    friday_evening = latest(s, STOCK, datetime(2026, 10, 30, 19, 0, tzinfo=UTC))
    assert friday_evening.traded_today and friday_evening.day_ref_eur == pytest.approx(100.0)
    monday_early = latest(s, STOCK, berlin(2026, 11, 2, 8, 0))
    assert not monday_early.traded_today and monday_early.day_ref_eur == pytest.approx(110.0)


def test_us_stock_counts_only_after_the_us_session_started(db):
    s = svc(db)
    s.store.upsert_quotes([Quote("yahoo:EXMP", 52.0, "EUR", berlin(2026, 11, 2, 22, 0), "yahoo", prev_close=50.0)])
    tue_morning = latest(s, US, berlin(2026, 11, 3, 10, 0))
    assert not tue_morning.traded_today and tue_morning.day_ref_eur == pytest.approx(52.0)
    s.store.upsert_quotes([Quote("yahoo:EXMP", 53.0, "EUR", berlin(2026, 11, 3, 15, 45), "yahoo", prev_close=52.0)])
    tue_afternoon = latest(s, US, berlin(2026, 11, 3, 16, 0))
    assert tue_afternoon.traded_today and tue_afternoon.day_ref_eur == pytest.approx(52.0)


def test_quote_without_trading_time_is_not_counted_as_traded_today(db):
    s = svc(db)
    s.store.upsert_quotes([Quote("yahoo:MUS.DE", 110.0, "EUR", None, "yahoo", prev_close=100.0)])  # fast_info
    pi = latest(s, STOCK, datetime.now(UTC))
    assert pi.session_date is None and not pi.traded_today and pi.day_ref_eur == pytest.approx(110.0)


# ---- Krypto und manuelle Kurse ------------------------------------------------------------------------------

def test_crypto_since_midnight_with_24h_fallback(db):
    s = svc(db)
    now = berlin(2026, 10, 31, 10, 0)
    s.store.upsert_quotes([Quote("cg:coin", 50.0, "EUR", berlin(2026, 10, 30, 23, 50), "coingecko", prev_close=45.0)])
    s.store.upsert_quotes([Quote("cg:coin", 55.0, "EUR", now - timedelta(minutes=5), "coingecko", prev_close=40.0)])
    pi = latest(s, COIN, now)
    assert pi.day_basis == "mitternacht" and pi.day_ref_eur == pytest.approx(50.0)
    db.x("DELETE FROM quote_intraday WHERE ts<?", ((now - timedelta(hours=9)).astimezone(UTC).isoformat()[:19],))
    s.store.upsert_quotes([Quote("cg:coin", 49.0, "EUR", berlin(2026, 10, 30, 20, 0), "coingecko")])  # zu alt
    s.store.upsert_quotes([Quote("cg:coin", 55.0, "EUR", now - timedelta(minutes=5), "coingecko", prev_close=40.0)])
    pi = latest(s, COIN, now)
    assert pi.day_basis == "24h" and pi.day_ref_eur == pytest.approx(40.0)


def test_manual_price_counts_only_when_entered_today(db):
    from app.prices.fallback import FallbackPrices
    from app.util.timeutil import today_local

    today = today_local()
    for entered, ref in ((today, 2.0), (today - timedelta(days=1), 2.5)):
        a = AssetInfo("M#1", "Manuell", "crypto", "manual")
        pf = Portfolio(None, [], {"M#1": a}, {}, manual_prices={"M#1": [(entered - timedelta(days=1), 2.0),
                                                                        (entered, 2.5)]})
        pi = PriceService._fallback_info(a, pf, FallbackPrices(pf, None), today, has_series=False, day=today)
        assert pi.day_ref_eur == pytest.approx(ref), entered


# ---- Bewertung: Käufe/Verkäufe heute, Prozentbezug ------------------------------------------------------------

def test_day_change_counts_todays_trades_from_their_price():
    assert _day(2, 110, 100, []) == (pytest.approx(20), pytest.approx(200))
    assert _day(3, 110, 100, [(1, 105.0)])[0] == pytest.approx(2 * 10 + (110 - 105))  # Kauf heute zu 105
    assert _day(1, 110, 100, [(-1, 108.0)])[0] == pytest.approx(1 * 10 + (108 - 100))  # Verkauf heute zu 108
    assert _day(1.9, 110, 100, [(-0.1, None)])[0] == pytest.approx(1.9 * 10)  # Gebühr ohne Wert: kein Beitrag


def _valuation(rows, prices, today):
    pf = portfolio(rows)
    return value_positions(pf, run_ledger(pf), prices, invested=0.0, today=today)


def quote(price, ref, prev=None):
    return PriceInfo(price, price, "EUR", SAT, "yahoo", "quote", False, prev, day_ref_eur=ref, day_basis="sitzung")


def test_portfolio_total_and_percent_refer_to_midnight_with_deposit_and_buy_today():
    today = to_local_date(datetime.now(UTC))
    d = today.isoformat()
    rows = [tx("E1", "2026-01-02T10:00:00Z", "deposit", to=("Depot", "EUR", "1000"), value="1000"),
            tx("B1", "2026-01-03T10:00:00Z", "buy", frm=("Depot", "EUR", "1000"), to=("Depot", "WKN:A0B1C2", "10"),
               value="1000"),
            tx("E2", f"{d}T08:00:00Z", "deposit", to=("Depot", "EUR", "500"), value="500"),
            tx("B2", f"{d}T09:00:00Z", "buy", frm=("Depot", "EUR", "210"), to=("Depot", "WKN:A0B1C2", "2"),
               value="210")]
    prices = {"WKN:A0B1C2": quote(110.0, 100.0), "EUR": PriceInfo(1.0, 1.0, "EUR", SAT, "fiat", "fiat", False, 1.0)}
    v = _valuation(rows, prices, today)
    p = v.by_id()["WKN:A0B1C2"]
    assert p.day_change == pytest.approx(10 * 10 + 2 * (110 - 105))  # alte 10 Stück + Kauf heute ab 105
    # Bezug: 10 × 100 (Aktie um 0:00) + 0 € Bargeld um 0:00 – Einzahlung und Kauf heute zählen nicht
    assert v.day_change_pct == pytest.approx(110 / 1000)
    flows = _today_flows(portfolio(rows), None, today)
    assert flows["EUR"] == [(500.0, 500.0), (-210.0, 210.0)] and flows["WKN:A0B1C2"] == [(2.0, 210.0)]


def test_session_summary_for_securities_without_trading_today():
    today = to_local_date(SAT)
    rows = [tx("B1", "2026-01-03T10:00:00Z", "deposit", to=("Depot", "WKN:A0B1C2", "10"), value="1000")]
    pi = quote(110.0, 110.0, prev=100.0)
    pi.session_date = to_local_date(FRI_CLOSE)
    v = _valuation(rows, {"WKN:A0B1C2": pi}, today)
    p = v.by_id()["WKN:A0B1C2"]
    assert p.day_change == pytest.approx(0) and p.session_change == pytest.approx(100.0)
    assert v.session["change"] == pytest.approx(100.0) and v.session["pct"] == pytest.approx(0.1)
    assert v.session["date"] == to_local_date(FRI_CLOSE) and v.session["same_date"]
    pi.traded_today = True  # heute gehandelt → keine Zeile „letzter Handelstag“
    assert _valuation(rows, {"WKN:A0B1C2": pi}, today).session is None


# ---- Übersicht ---------------------------------------------------------------------------------------------

def test_dashboard_shows_last_trading_day_for_securities_without_trading_today(tmp_path):
    from app.config import Config, Secrets
    from tests.helpers import ASSETS
    from tests.test_diagnosis import client_with_import

    imp = tmp_path / "import"
    imp.mkdir()
    cfg = Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                 startup_jobs=False, log_format="text", secrets=Secrets())
    c = client_with_import(cfg, [tx("D1", "2025-01-02T10:00:00Z", "deposit", to=("Depot", "WKN:A0B1C2", "10"),
                                    value="1000")], ASSETS)
    try:
        ctx = c.app.state.ctx
        last_session = datetime.now(UTC) - timedelta(days=3)  # sicher vor heute
        ctx.store.upsert_quotes([Quote("demo:yahoo:MUS.DE", 110.0, "EUR", last_session, "demo", prev_close=100.0)])
        ctx.invalidate_data()
        p = ctx.valuation().by_id()["WKN:A0B1C2"]
        assert p.day_change == pytest.approx(0) and p.session_change == pytest.approx(100.0)
        html = c.get("/").text
        label = trading_day(to_local_date(last_session))
        assert f"Wertpapiere am letzten Handelstag ({label})" in html and "seit 0:00 Uhr" in html
        assert html.count("Wertpapiere am letzten Handelstag") >= 2  # Kopfzeile und Top-Bewegungen
    finally:
        c.__exit__(None, None, None)
