"""Historie aus Ledger × Kursen und Performance darauf (End-to-End ohne Netzwerk)."""

from datetime import date, timedelta

import numpy as np
import pytest

from app.analytics import periods as P
from app.analytics.history import compute_history
from app.analytics.valuation import FlowValuer
from app.ledger.engine import run_ledger
from app.prices.models import Bar
from app.prices.store import PriceStore
from tests.helpers import portfolio, tx


def _series_for(a):
    return {"WKN:A0B1C2": "yahoo:MUS.DE", "WKN:US0001": "yahoo:EXMP", "BTC": "cg:bitcoin"}.get(a.asset_id)


def test_history_values_flows_and_ttwror(db):
    store = PriceStore(db)
    start = date(2024, 1, 1)
    # Kurs 100 → 110 → 121 (je +10 %) an drei Tagen
    store.upsert_daily("yahoo:MUS.DE", [Bar(start, 100.0), Bar(start + timedelta(1), 110.0),
                                        Bar(start + timedelta(2), 121.0)], "test", "EUR")
    pf = portfolio([
        tx("b1", "2024-01-01", "buy", frm=("Depot", "EUR", 1000), to=("Depot", "WKN:A0B1C2", 10), value=1000),
        tx("b2", "2024-01-02", "buy", frm=("Depot", "EUR", 1100), to=("Depot", "WKN:A0B1C2", 10), value=1100),
    ])
    led = run_ledger(pf)
    h = compute_history(pf, led, store, _series_for, FlowValuer(store, _series_for), end=start + timedelta(2))
    assert h.value.tolist() == pytest.approx([1000.0, 2200.0, 2420.0])
    assert h.invested.tolist() == pytest.approx([1000.0, 2100.0, 2100.0])
    s = P.total_series(h)
    a, b, inc = P.bounds(h, "MAX")
    m = P.metrics(h, s, a, b, inc)
    # Tag 0: 0 → 1000 (Zufluss 1000) = 0 %; Tag 1: (2200)/(1000+1100) − 1; Tag 2: +10 %
    expected = (2200 / 2100) * 1.10 - 1
    assert m["ttwror"] == pytest.approx(expected)
    assert m["gain"] == pytest.approx(2420 - 2100)
    # Positionssicht identisch (einziges Asset, Kauf = Zufluss in die Position)
    ms = P.metrics(h, P.group_series(h, ["WKN:A0B1C2"]), a, b, inc)
    assert ms["ttwror"] == pytest.approx(expected)


def test_history_estimates_before_first_price_and_marks_unvalued(db):
    store = PriceStore(db)
    store.upsert_daily("cg:bitcoin", [Bar(date(2024, 1, 10), 40000.0)], "test", "EUR")
    assets_extra = [{"asset_id": "NOPRICE", "name": "Ohne Kurs", "asset_class": "crypto", "quote_source": "none"}]
    from tests.helpers import ASSETS
    pf = portfolio([
        tx("b1", "2024-01-05", "buy", frm=("Ex", "EUR", 3500), to=("Ex", "BTC", "0.1"), value=3500),
        tx("d1", "2024-01-06", "deposit", tag="airdrop", to=("Ex", "NOPRICE", 5)),
    ], assets=ASSETS + assets_extra)
    led = run_ledger(pf)
    h = compute_history(pf, led, store, _series_for, FlowValuer(store, _series_for), end=date(2024, 1, 12))
    k = h.asset_index()["BTC"]
    # vor dem ersten Marktkurs: Transaktionskurs 35.000 €/BTC als Schätzung
    assert h.asset_price[k][0] == pytest.approx(35000.0)
    assert h.asset_price[k][5] == pytest.approx(40000.0)
    assert h.estimated_days["BTC"] == 5
    # Airdrop ohne Wert und ohne Kursquelle: unbewertet
    assert "NOPRICE" in h.unvalued_assets
    assert np.all(h.asset_value[h.asset_index()["NOPRICE"]] == 0)


def _no_source(*ids: str, cls: str = "crypto") -> list[dict[str, str]]:
    from tests.helpers import ASSETS
    return ASSETS + [{"asset_id": a, "name": a, "asset_class": cls, "quote_source": "none"} for a in ids]


def test_history_values_assets_without_source_by_transaction_prices(db):
    """Verkaufte Aktie ohne Kursquelle: zwischen Kauf und Verkauf mit Transaktionskursen, nicht 0 € (TTWROR)."""
    store = PriceStore(db)
    pf = portfolio([
        tx("d0", "2020-01-01", "deposit", to=("Depot", "EUR", 1000)),
        tx("b1", "2020-01-02", "buy", frm=("Depot", "EUR", 1000), to=("Depot", "OLD", 10), value=1000),
        tx("s1", "2021-06-30", "sell", frm=("Depot", "OLD", 10), to=("Depot", "EUR", 1500), value=1500),
    ], assets=_no_source("OLD", cls="security"))
    led = run_ledger(pf)
    h = compute_history(pf, led, store, _series_for, FlowValuer(store, _series_for), end=date(2021, 7, 2))
    k = h.asset_index()["OLD"]
    i_mid = h.index_of(date(2021, 1, 1))  # > 365 Tage nach dem Kauf: zwischen zwei Kurspunkten kein Ablauf
    assert h.asset_price[k][i_mid] == pytest.approx(100.0)
    assert h.asset_price[k][h.index_of(date(2021, 6, 30))] == pytest.approx(150.0)
    assert not h.unvalued_assets and not h.unvalued_past
    assert h.fallback_days["OLD"] == (date(2021, 6, 30) - date(2020, 1, 2)).days
    s = P.total_series(h)
    a, b, inc = P.bounds(h, "MAX")
    assert P.metrics(h, s, a, b, inc)["ttwror"] == pytest.approx(0.5)


def test_history_fallback_expires_like_current_valuation(db):
    """Token ohne Kursquelle: Transaktionskurs gilt nach dem letzten Kurspunkt höchstens 30 Tage (Krypto)."""
    store = PriceStore(db)
    pf = portfolio([
        tx("d0", "2024-01-01", "deposit", to=("Ex", "EUR", 100)),
        tx("b1", "2024-01-01", "trade", frm=("Ex", "EUR", 100), to=("Ex", "DEAD", 1000), value=100),
    ], assets=_no_source("DEAD"))
    led = run_ledger(pf)
    h = compute_history(pf, led, store, _series_for, FlowValuer(store, _series_for), end=date(2024, 3, 1))
    k = h.asset_index()["DEAD"]
    assert h.asset_price[k][h.index_of(date(2024, 1, 31))] == pytest.approx(0.1)
    assert h.asset_price[k][h.index_of(date(2024, 2, 1))] == 0.0
    assert h.unvalued_assets == ["DEAD"]
    # Wertberichtigung am Ablauftag: Totalverlust der Position (wie die aktuelle Bewertung mit 0 €)
    s = P.group_series(h, ["DEAD"])
    a, b, inc = P.bounds(h, "MAX")
    assert P.metrics(h, s, a, b, inc)["ttwror"] == pytest.approx(-1.0)


def test_history_fallback_is_split_aware(db):
    store = PriceStore(db)
    pf = portfolio([
        tx("d0", "2024-01-01", "deposit", to=("D", "EUR", 1000)),
        tx("b1", "2024-01-01", "buy", frm=("D", "EUR", 1000), to=("D", "SPL", 10), value=1000),
        tx("c1", "2024-02-01", "corporate_action", tag="split", frm=("D", "SPL", 10), to=("D", "SPL", 40)),
    ], assets=_no_source("SPL", cls="security"))
    led = run_ledger(pf)
    h = compute_history(pf, led, store, _series_for, FlowValuer(store, _series_for), end=date(2024, 2, 3))
    k = h.asset_index()["SPL"]
    assert h.asset_price[k][h.index_of(date(2024, 1, 31))] == pytest.approx(100.0)
    assert h.asset_price[k][h.index_of(date(2024, 2, 1))] == pytest.approx(25.0)
    assert h.value.tolist() == pytest.approx([1000.0] * h.n)


def test_token_transfer_without_value_is_valued_like_the_position(db):
    """Zugang eines Tokens ohne EUR-Wert und ohne Kursquelle: Zufluss = Positionswert, kein Scheingewinn."""
    store = PriceStore(db)
    pf = portfolio([
        tx("d1", "2024-01-01", "deposit", to=("W", "TOK", 1000)),
        tx("d2", "2024-01-05", "deposit", tag="airdrop", to=("W", "TOK", 10), value=5),
    ], assets=_no_source("TOK"))
    led = run_ledger(pf)
    h = compute_history(pf, led, store, _series_for, FlowValuer(store, _series_for), end=date(2024, 1, 6))
    assert h.value[0] == pytest.approx(500.0)  # vor dem ersten Kurspunkt: dieser als Schätzung
    assert h.inflow[0] == pytest.approx(500.0)
    s = P.total_series(h)
    a, b, inc = P.bounds(h, "MAX")
    assert P.metrics(h, s, a, b, inc)["ttwror"] == pytest.approx(5 / 500)  # nur der Airdrop-Ertrag


def test_usd_prices_are_converted_with_daily_fx(db):
    store = PriceStore(db)
    d0 = date(2024, 3, 1)
    store.upsert_daily("yahoo:EXMP", [Bar(d0, 110.0), Bar(d0 + timedelta(1), 110.0)], "test", "USD")
    store.upsert_daily("fx:ecb:USD", [Bar(d0, 1.10), Bar(d0 + timedelta(1), 1.00)], "ecb", None)
    pf = portfolio([tx("b1", "2024-03-01", "buy", frm=("D", "EUR", 1000), to=("D", "WKN:US0001", 10), value=1000)])
    led = run_ledger(pf)
    h = compute_history(pf, led, store, _series_for, FlowValuer(store, _series_for), end=d0 + timedelta(1))
    assert h.value.tolist() == pytest.approx([1000.0, 1100.0])
