"""M24/AP5.1 – Historische Bewertung: Referenzfälle mit vorab berechneten Sollwerten.

Grundsatz: Ein fehlender Kurs erzeugt keinen Verlust (Lücke wird neutral behandelt), ein tatsächlicher
wirtschaftlicher Verlust darf dadurch aber nicht verschwinden: Was während der Lücke verkauft wird oder bei
Rückkehr des Kurses anders bewertet ist, wird gegen den **letzten bekannten Wert** abgerechnet.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.analytics import periods as P
from app.analytics.history import compute_history
from app.analytics.valuation import FlowValuer
from app.ledger.engine import run_ledger
from app.prices.models import Bar
from app.prices.store import PriceStore
from tests.helpers import ASSETS, portfolio, tx

END = date(2024, 4, 30)


def _series(a):
    return {"BTC": "cg:bitcoin", "ETH": "cg:ethereum", "WKN:A0B1C2": "yahoo:MUS.DE"}.get(a.asset_id)


def _bars(store, series, start, end, price_of):
    d, bars = start, []
    while d <= end:
        p = price_of(d)
        if p is not None:
            bars.append(Bar(d, p))
        d += timedelta(days=1)
    store.upsert_daily(series, bars, "test", "EUR")


def _metrics(h, assets=None):
    a, b, inc = P.bounds(h, "MAX")
    s = P.total_series(h) if assets is None else P.group_series(h, assets)
    return P.metrics(h, s, a, b, inc)


def _hist(db, rows, end=END):
    store = PriceStore(db)
    _bars(store, "cg:bitcoin", date(2024, 1, 1), end, lambda d: 10000.0)  # Anker: konstanter Marktkurs
    return store, portfolio(rows)


def _dead(*extra):
    """BTC (konstanter Marktkurs, 1.000 €) + Token DEAD ohne Kursquelle: 1.000 Stück für 100 € (0,10 €) am
    01.01.; der Transaktionskurs gilt 30 Tage, danach kein Kurs (Lücke)."""
    rows = [tx("b0", "2024-01-01", "buy", frm=("Ex", "EUR", 1000), to=("Ex", "BTC", "0.1"), value=1000),
            tx("b1", "2024-01-01", "buy", frm=("Ex", "EUR", 100), to=("Ex", "DEAD", 1000), value=100), *extra]
    assets = [*ASSETS, {"asset_id": "DEAD", "name": "DEAD", "asset_class": "crypto", "quote_source": "none",
                        "quote_id": ""}]
    return rows, assets


def _hist_dead(db, *extra):
    store = PriceStore(db)
    _bars(store, "cg:bitcoin", date(2024, 1, 1), END, lambda d: 10000.0)
    rows, assets = _dead(*extra)
    pf = portfolio(rows, assets=assets)
    return compute_history(pf, run_ledger(pf), store, _series, FlowValuer(store, _series), end=END)


def test_token_listed_after_gap_shows_change_against_last_known_value(db):
    """DEAD erhält später eine Kursquelle; Marktkurse erst ab 01.04. (0,05 €). Wert vorher 100 € (Kauf), danach
    50 €. Soll: G/V −50 € – die Wertänderung über die Lücke hinweg bleibt sichtbar, keine Neutralisierung."""
    store = PriceStore(db)
    _bars(store, "cg:bitcoin", date(2024, 1, 1), END, lambda d: 10000.0)
    _bars(store, "cg:dead", date(2024, 4, 1), END, lambda d: 0.05)
    rows, assets = _dead()
    assets = [a if a["asset_id"] != "DEAD" else {**a, "quote_source": "coingecko", "quote_id": "dead"}
              for a in assets]
    pf = portfolio(rows, assets=assets)
    series = {"BTC": "cg:bitcoin", "DEAD": "cg:dead"}.get
    h = compute_history(pf, run_ledger(pf), store, lambda a: series(a.asset_id),
                        FlowValuer(store, lambda a: series(a.asset_id)), end=END)
    assert _metrics(h)["gain"] == pytest.approx(-50.0)
    assert _metrics(h, ["DEAD"])["gain"] == pytest.approx(-50.0)


def test_unpriced_withdrawal_and_deposit_during_gap_are_neutral(db):
    """Nach Ablauf des Ersatzkurses: Zugang (500 Stück, ohne Wert) am 01.03. und Abgang aller 1.500 Stück (ohne
    Wert, z. B. an externe Wallet) am 15.03. Beides sind externe Bewegungen ohne bekannten Wert – Soll: G/V 0 €,
    TTWROR 0 % (kein Scheinverlust, kein Scheingewinn)."""
    h = _hist_dead(db, tx("d2", "2024-03-01", "deposit", to=("Ex", "DEAD", 500)),
                   tx("w1", "2024-03-15", "withdrawal", frm=("Ex", "DEAD", 1500)))
    m = _metrics(h)
    assert m["valuation_gaps"] and m["gain"] == pytest.approx(0.0, abs=1e-9)
    assert m["ttwror"] == pytest.approx(0.0, abs=1e-12)


def test_market_priced_asset_gap_is_carried_forward_and_change_visible(db):
    """Assets mit Kursquelle: fehlende Tage übernehmen den letzten Kurs (geschätzt); Kursrückgang 1.000 → 800 € ist
    ein echter Verlust."""
    store, pf = _hist(db, [
        tx("b0", "2024-01-01", "buy", frm=("Ex", "EUR", 1000), to=("Ex", "BTC", "0.1"), value=1000),
        tx("b1", "2024-01-01", "buy", frm=("Ex", "EUR", 1000), to=("Ex", "ETH", "1"), value=1000)])
    _bars(store, "cg:ethereum", date(2024, 1, 1), END,
          lambda d: 1000.0 if d <= date(2024, 1, 31) else (800.0 if d >= date(2024, 4, 1) else None))
    h = compute_history(pf, run_ledger(pf), store, _series, FlowValuer(store, _series), end=END)
    m = _metrics(h)
    assert m["gain"] == pytest.approx(-200.0) and m["ttwror"] == pytest.approx(1800 / 2000 - 1)
    assert P.valuation_state(h, 0, h.n - 1)["state"] == "estimated"


def test_gap_without_return_or_sale_is_neutral(db):
    store, pf = _hist(db, [
        tx("b0", "2024-01-01", "buy", frm=("Ex", "EUR", 1000), to=("Ex", "BTC", "0.1"), value=1000),
        tx("b1", "2024-01-01", "buy", frm=("Ex", "EUR", 1000), to=("Ex", "ETH", "1"), value=1000)])
    _bars(store, "cg:ethereum", date(2024, 1, 1), date(2024, 1, 31), lambda d: 1000.0)
    h = compute_history(pf, run_ledger(pf), store, _series, FlowValuer(store, _series), end=END)
    m = _metrics(h)
    assert m["gain"] == pytest.approx(0.0) and m["ttwror"] == pytest.approx(0.0) and m["max_drawdown"] == 0.0


def test_token_never_valued_gets_first_price_without_artificial_gain(db):
    """Zugang ohne Wert und ohne je bekannten Kurs: erster Kurs ist Erstbewertung (neutral), danach zählt die
    Kursentwicklung normal."""
    store, pf = _hist(db, [
        tx("b0", "2024-01-01", "buy", frm=("Ex", "EUR", 1000), to=("Ex", "BTC", "0.1"), value=1000),
        tx("d1", "2024-01-10", "deposit", to=("Ex", "ETH", "1"))])
    _bars(store, "cg:ethereum", date(2024, 3, 1), END, lambda d: 900.0 if d < date(2024, 4, 1) else 990.0)
    h = compute_history(pf, run_ledger(pf), store, _series, FlowValuer(store, _series), end=END)
    m = _metrics(h)
    assert m["gain"] == pytest.approx(90.0)  # nur 900 → 990, keine 900 € „Gewinn“ aus der Erstbewertung


def test_split_reverse_split_and_total_loss_reference(db):
    store = PriceStore(db)
    _bars(store, "yahoo:MUS.DE", date(2024, 1, 1), END,
          lambda d: 100.0 if d < date(2024, 2, 1) else (25.0 if d < date(2024, 3, 1) else 50.0))
    pf = portfolio([
        tx("b", "2024-01-01", "buy", frm=("D", "EUR", 1000), to=("D", "WKN:A0B1C2", 10), value=1000),
        tx("s", "2024-02-01", "corporate_action", tag="split", frm=("D", "WKN:A0B1C2", 10),
           to=("D", "WKN:A0B1C2", 40)),
        tx("r", "2024-03-01", "corporate_action", tag="reverse_split", frm=("D", "WKN:A0B1C2", 40),
           to=("D", "WKN:A0B1C2", 20)),
        tx("l", "2024-04-15", "withdrawal", tag="lost", frm=("D", "WKN:A0B1C2", 20), value=0)], assets=ASSETS)
    h = compute_history(pf, run_ledger(pf), store, _series, FlowValuer(store, _series), end=END)
    i1, i2 = h.index_of(date(2024, 2, 10)), h.index_of(date(2024, 3, 10))
    assert h.value[i1] == pytest.approx(1000.0) and h.value[i2] == pytest.approx(1000.0)  # Splits wertneutral
    m = _metrics(h)
    assert m["gain"] == pytest.approx(-1000.0)  # Totalverlust ist ein echter Verlust
    assert m["max_drawdown"] == pytest.approx(-1.0)


def test_corrected_historic_price_changes_metrics_reproducibly(db):
    store, pf = _hist(db, [
        tx("b0", "2024-01-01", "buy", frm=("Ex", "EUR", 1000), to=("Ex", "ETH", "1"), value=1000)])
    _bars(store, "cg:ethereum", date(2024, 1, 1), END, lambda d: 1000.0 if d < date(2024, 3, 1) else 1500.0)
    h1 = compute_history(pf, run_ledger(pf), store, _series, FlowValuer(store, _series), end=END)
    assert _metrics(h1)["gain"] == pytest.approx(500.0)
    _bars(store, "cg:ethereum", date(2024, 3, 1), END, lambda d: 1200.0)  # Korrektur der Kursreihe
    h2 = compute_history(pf, run_ledger(pf), store, _series, FlowValuer(store, _series), end=END)
    h3 = compute_history(pf, run_ledger(pf), store, _series, FlowValuer(store, _series), end=END)
    assert _metrics(h2)["gain"] == pytest.approx(200.0) and _metrics(h2) == _metrics(h3)


def test_missing_fx_rate_does_not_create_loss(db):
    """USD-Kurs ohne Wechselkurs an einzelnen Tagen: Wechselkurs wird fortgeschrieben, kein Einbruch auf 0."""
    store = PriceStore(db)
    d0 = date(2024, 3, 1)
    store.upsert_daily("yahoo:EXMP", [Bar(d0 + timedelta(i), 110.0) for i in range(5)], "test", "USD")
    store.upsert_daily("fx:ecb:USD", [Bar(d0, 1.10), Bar(d0 + timedelta(4), 1.10)], "ecb", None)
    pf = portfolio([tx("b1", "2024-03-01", "buy", frm=("D", "EUR", 1000), to=("D", "WKN:US0001", 10), value=1000)])
    series = {"WKN:US0001": "yahoo:EXMP"}.get
    h = compute_history(pf, run_ledger(pf), store, lambda a: series(a.asset_id),
                        FlowValuer(store, lambda a: series(a.asset_id)), end=d0 + timedelta(4))
    assert h.value.tolist() == pytest.approx([1000.0] * 5)


def test_partially_unvalued_period_metrics_are_consistent(db):
    h = _hist_dead(db)  # DEAD nach 30 Tagen ohne Kurs, BTC konstant
    m = _metrics(h)
    assert P.valuation_state(h, 0, h.n - 1)["state"] == "incomplete"
    assert m["gain"] == pytest.approx(0.0) and m["ttwror"] == pytest.approx(0.0) and m["max_drawdown"] == 0.0
    assert m["irr"] is not None and abs(m["irr"]) < 1e-9  # geldgewichtet ebenso 0 %
