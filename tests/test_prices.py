"""Kursprovider und Kursdienst: Split-Bereinigung, Parsing, Budget, Veraltung, Bewertungs-Fallbacks."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd
import pytest

from app.ledger.models import AssetInfo, Portfolio
from app.prices.coingecko import BudgetExceeded, CoinGeckoProvider
from app.prices.ecb import EcbProvider
from app.prices.models import Bar, Quote
from app.prices.service import PriceService
from app.prices.store import PriceStore
from app.prices.yahoo import YahooProvider
from app.settings_store import Settings
from app.util.http import Quota, RateLimiter
from app.util.timeutil import today_local


def test_yahoo_bars_undo_split_adjustment():
    # Yahoo liefert split-adjustierte Schlusskurse; Split 10:1 am 2024-06-10
    idx = pd.to_datetime(["2024-06-06", "2024-06-07", "2024-06-10", "2024-06-11"]).tz_localize("America/New_York")
    df = pd.DataFrame({"Open": [120.0, 121.0, 122.0, 123.0], "High": [121.0, 122.0, 123.0, 124.0],
                       "Low": [119.0, 120.0, 121.0, 122.0], "Close": [120.5, 121.5, 122.5, 123.5],
                       "Volume": [1, 1, 1, 1], "Stock Splits": [0.0, 0.0, 10.0, 0.0]}, index=idx)
    bars = YahooProvider.bars_from_df(df, "USD")
    assert [b.close for b in bars] == [1205.0, 1215.0, 122.5, 123.5]
    assert [b.split_factor for b in bars] == [10.0, 10.0, 1.0, 1.0]
    assert bars[0].date == date(2024, 6, 6)


def test_yahoo_v7_parse_normalizes_pence():
    q, info = YahooProvider.parse_v7({"symbol": "VOD.L", "regularMarketPrice": 7250, "currency": "GBp",
                                      "regularMarketPreviousClose": 7000, "regularMarketTime": 1727366400,
                                      "marketCap": 10, "fullExchangeName": "LSE"})
    assert q.ccy == "GBP" and q.price == pytest.approx(72.5) and q.prev_close == pytest.approx(70.0)
    assert q.series == "yahoo:VOD.L" and info["fullExchangeName"] == "LSE"


def test_coingecko_market_chart_maps_midnight_to_previous_day():
    t0 = datetime(2026, 9, 20, tzinfo=UTC)
    data = {"prices": [[t0.timestamp() * 1000, 100.0], [(t0 + timedelta(days=1)).timestamp() * 1000, 110.0],
                       [datetime.now(UTC).timestamp() * 1000, 999.0]]}
    bars = CoinGeckoProvider.parse_market_chart(data)
    assert [(b.date, b.close) for b in bars] == [(date(2026, 9, 19), 100.0), (date(2026, 9, 20), 110.0)]


def test_coingecko_quotes_one_call_and_budget(db):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["x-cg-demo-api-key"] == "demo-key-123"
        assert "qty" not in str(request.url) and "value" not in str(request.url)
        return httpx.Response(200, json={
            "bitcoin": {"eur": 60000.5, "eur_24h_change": 1.5, "last_updated_at": 1727366400},
            "kaspa": {"eur": 0.12}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    quota = Quota(db, "coingecko", "month")
    cg = CoinGeckoProvider(client, "demo-key-123", "demo", limiter=RateLimiter(0), quota=quota, monthly_limit=2)
    quotes, errors = cg.quotes(["bitcoin", "kaspa", "missing"])
    assert len(calls) == 1 and set(quotes) == {"bitcoin", "kaspa"} and errors == ["missing: kein Kurs"]
    assert quotes["bitcoin"].price == 60000.5 and quota.used()[0] == 1
    cg.quotes(["bitcoin"])
    with pytest.raises(BudgetExceeded):
        cg.quotes(["bitcoin"])


def test_ecb_frankfurter_and_zip_fallback(monkeypatch):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("eurofxref-hist.csv", "Date,USD,GBP,\n2026-09-18,1.1000,0.8500,\n2026-09-17,1.0900,N/A,\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if "frankfurter" in request.url.host:
            return httpx.Response(503)
        return httpx.Response(200, content=buf.getvalue())

    ecb = EcbProvider(httpx.Client(transport=httpx.MockTransport(handler)), "https://api.frankfurter.dev/v1", None,
                      "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip")
    from app.util.http import request_with_retry

    def no_sleep(*a, **kw):
        kw["sleep"] = lambda s: None
        return request_with_retry(*a, **kw)

    monkeypatch.setattr("app.prices.ecb.request_with_retry", no_sleep)
    out = ecb.history(["USD", "GBP"], date(2026, 9, 1), date(2026, 9, 30))
    assert [(b.date, b.close) for b in out["USD"]] == [(date(2026, 9, 17), 1.09), (date(2026, 9, 18), 1.1)]
    assert [b.close for b in out["GBP"]] == [0.85]


def _service(db) -> PriceService:
    return PriceService(db, Settings(db), PriceStore(db), yahoo=None, coingecko=None, ecb=None)


def test_staleness_rules(db):
    svc = _service(db)
    crypto = AssetInfo("BTC", "Bitcoin", "crypto")
    stock = AssetInfo("X", "X", "security")
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)  # Montag
    assert svc.is_stale(crypto, now - timedelta(minutes=90), now)
    assert not svc.is_stale(crypto, now - timedelta(minutes=30), now)
    friday_close = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
    assert not svc.is_stale(stock, friday_close, datetime(2026, 9, 27, 12, 0, tzinfo=UTC))  # Sonntag
    assert svc.is_stale(stock, friday_close, now)  # Montagmittag: Handelstag vergangen
    assert not svc.is_stale(stock, friday_close, datetime(2026, 9, 28, 7, 0, tzinfo=UTC))  # Montagmorgen vor Handel


def test_latest_eur_fallback_chain_and_fx(db):
    svc = _service(db)
    store = svc.store
    usd = AssetInfo("WKN:1", "US Corp", "security", "yahoo", "USC")
    eur_daily = AssetInfo("WKN:2", "Old", "security", "yahoo", "OLD.DE")
    manual = AssetInfo("M#1", "Manual", "crypto", "manual")
    none = AssetInfo("N#1", "None", "crypto", "none")
    store.upsert_quotes([Quote("yahoo:USC", 110.0, "USD", datetime.now(UTC), "yahoo", prev_close=100.0),
                         Quote("fx:yahoo:USD", 1.10, None, datetime.now(UTC), "yahoo")])
    store.upsert_daily("yahoo:OLD.DE", [Bar(date(2026, 9, 1), 50.0)], "yahoo", "EUR")
    recent = today_local() - timedelta(days=3)
    pf = Portfolio(None, [], {a.asset_id: a for a in (usd, eur_daily, manual, none)}, {},
                   manual_prices={"M#1": [(recent, 2.0)]})
    out = svc.latest_eur_many([usd, eur_daily, manual, none], pf)
    assert out["WKN:1"].price_eur == pytest.approx(100.0)
    assert out["WKN:1"].prev_close_eur == pytest.approx(90.909, rel=1e-3)
    assert out["WKN:2"].kind == "daily" and out["WKN:2"].stale and out["WKN:2"].price_eur == 50.0
    assert out["M#1"].kind == "manual" and out["M#1"].price_eur == 2.0
    assert out["N#1"].kind == "unvalued" and out["N#1"].price_eur == 0.0 and not out["N#1"].valued


def test_fallback_prices_expire_after_max_age(db):
    """Manuelle und Transaktionskurse gelten nach dem letzten Kurspunkt nur begrenzt (Krypto 30 Tage)."""
    from tests.helpers import ASSETS, portfolio, tx
    svc = _service(db)
    today = today_local()
    extra = [{"asset_id": a, "name": a, "asset_class": "crypto", "quote_source": "none"} for a in ("OLD", "NEW")]
    extra.append({"asset_id": "WARR", "name": "Optionsschein", "asset_class": "security", "quote_source": "manual"})
    pf = portfolio([
        tx("t1", (today - timedelta(days=10)).isoformat(), "trade", frm=("Ex", "EUR", 50), to=("Ex", "NEW", 1000),
           value=50),
        tx("t2", (today - timedelta(days=10)).isoformat(), "trade", frm=("Ex", "EUR", 150), to=("Ex", "NEW", 1000),
           value=150),
        tx("t3", "2024-11-01", "trade", frm=("Ex", "EUR", 100), to=("Ex", "OLD", 1000), value=100),
        tx("t4", (today - timedelta(days=9)).isoformat(), "trade", frm=("Ex", "EUR", "0.5"), to=("Ex", "OLD", 1),
           value="0.5"),  # Staub unter 1 €: kein Kurspunkt
    ], assets=ASSETS + extra)
    pf.manual_prices = {"OLD": [(date(2025, 10, 30), 0.5)], "WARR": [(today - timedelta(days=200), 1.35)]}
    by_id = {a: pf.asset(a) for a in ("OLD", "NEW", "WARR")}
    out = svc.latest_eur_many(by_id.values(), pf)
    assert out["NEW"].kind == "tx" and out["NEW"].price_eur == pytest.approx(0.1)  # Tagesmittel 200 € / 2000
    assert "Transaktionskurs" in out["NEW"].note
    assert out["OLD"].kind == "unvalued" and not out["OLD"].valued
    assert "30.10.2025" in out["OLD"].note and "älter als 30 Tage" in out["OLD"].note
    assert out["WARR"].kind == "manual" and out["WARR"].price_eur == pytest.approx(1.35)  # Wertpapier: 365 Tage
    svc.settings.set("prices.fallback_max_age_crypto_days", 0)  # 0 = unbegrenzt
    out = svc.latest_eur_many(by_id.values(), pf)
    assert out["OLD"].kind == "manual" and out["OLD"].price_eur == pytest.approx(0.5)


def test_coingecko_budget_throttle(db):
    svc = _service(db)
    q = svc.cg_quota
    svc.settings.set("prices.coingecko_monthly_limit", 100)
    q.add(79)
    assert svc.crypto_due()[0] is True
    q.add(1)
    assert svc.cg_budget()["throttled"] is True
    svc.db.set_state("last_crypto_update", (datetime.now(UTC) - timedelta(minutes=12)).isoformat())
    due, why = svc.crypto_due()
    assert due is False and "gedrosselt" in why
    q.add(20)
    assert svc.crypto_due() == (False, "Monatskontingent erschöpft")


def test_eod_close_advances_history_so_no_daily_refetch(db, monkeypatch):
    from app.ledger.engine import run_ledger
    from app.util.timeutil import today_local
    from tests.helpers import portfolio, tx

    svc = _service(db)
    today = today_local()
    pf = portfolio([tx("b", "2026-01-05", "buy", frm=("Börse", "EUR", 100), to=("Börse", "BTC", "0.001"),
                       value=100)])
    led = run_ledger(pf)
    s = svc.series_for(pf.asset("BTC"))
    old = (today - timedelta(days=3)).isoformat()
    svc.store.set_meta(s, history_from="2025-12-29", history_to=old, last_history_fetch="2026-01-01T00:00:00Z",
                       history_status="ok")
    svc.store.upsert_quotes([Quote(s, 61000.0, "EUR", datetime.now(UTC), "coingecko")])
    assert svc._needs(s, date(2025, 12, 29), today, False)[0]  # ohne Tagesschluss: Abruf nötig
    assert svc.write_eod_closes(pf, led) == 1
    assert svc.store.meta(s)["history_to"] == today.isoformat()
    assert not svc._needs(s, date(2025, 12, 29), today, False)[0]
