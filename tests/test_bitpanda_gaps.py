"""Bitpanda-Lückenprüfung (0.84.0, ab 0.85.0 ohne alte API) und Zuordnung von Sparplan-Einzahlungen mit Zeitversatz:
Import (Datum des Einzugs) ↔ API (Gutschrift Tage später) – nur bei gleichem Betrag, höchstens 7 Tagen und eindeutigem
Gegenstück, ohne Überkreuzung."""

# ruff: noqa: F811  (Fixtures aus test_bitpanda werden importiert und als Argumente verwendet)
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.datasources import bitpanda_gaps as G
from app.datasources import takeover as TK
from tests.helpers import tx
from tests.test_bitpanda import EUR_ID, U, api, client, ctx, master, oper, page, txn  # noqa: F401  (Fixtures)
from tests.test_takeover import holdings, ops, portfolio, started
from tests.test_takeover_gaps import _setup

D = Decimal
T0 = datetime(2024, 3, 1, 14, 45, tzinfo=UTC)


def cand(ts: datetime, qty: str, ref: str, asset: str = "EUR") -> tuple:
    return ts, (asset, D(qty)), ref


def test_lag_pairs_forward_unique_without_crossing():
    week = timedelta(days=7)
    olds = [cand(T0 + i * week, "50", f"o{i}") for i in range(4)]
    news = [cand(T0 + i * week + timedelta(days=3, hours=18), "50", f"n{i}") for i in range(4)]
    pairs = TK.lag_pairs(olds, news)
    assert [(o, n) for o, n, _lag in pairs] == [("o0", "n0"), ("o1", "n1"), ("o2", "n2"), ("o3", "n3")]
    assert TK.lag_text([lag for _o, _n, lag in pairs]) == "3,8 Tage"
    # API früher als der Import, mehr als 7 Tage, anderer Betrag oder anderes Asset: keine Zuordnung
    assert TK.lag_pairs([cand(T0, "50", "o")], [cand(T0 - timedelta(days=2), "50", "n")]) == []
    assert TK.lag_pairs([cand(T0, "50", "o")], [cand(T0 + timedelta(days=8), "50", "n")]) == []
    assert TK.lag_pairs([cand(T0, "50", "o")], [cand(T0 + timedelta(days=2), "50.01", "n")]) == []
    assert TK.lag_pairs([cand(T0, "50", "o")], [cand(T0 + timedelta(days=2), "50", "n", "USD")]) == []
    # mehrdeutig: zwei Import-Einzahlungen vor derselben Gutschrift – nur die nächste wird zugeordnet
    pairs = TK.lag_pairs([cand(T0, "50", "a"), cand(T0 + timedelta(days=1), "50", "b")],
                         [cand(T0 + timedelta(days=3), "50", "n")])
    assert [(o, n) for o, n, _lag in pairs] == [("b", "n")]
    # Versatz größer als der Abstand der Raten: Gutschrift liegt hinter der nächsten Rate → keine Zuordnung
    olds = [cand(T0 + i * timedelta(days=2), "50", f"o{i}") for i in range(3)]
    news = [cand(T0 + i * timedelta(days=2) + timedelta(days=5), "50", f"n{i}") for i in range(3)]
    assert [(o, n) for o, n, _lag in TK.lag_pairs(olds, news)] == [("o2", "n0")]


def _savings(api, config):
    """Konto wie in der Übernahme (500 € Einzahlung, Kauf, BTC-Auszahlung) plus drei wöchentliche Sparplan-
    Einzahlungen à 50 €: Import mit dem Datum des Einzugs (Freitag), API mit der Gutschrift 3,8 Tage später."""
    rows = [tx("K1", "2024-02-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "500"), value="500"),
            tx("K2", "2024-02-02T09:01:00Z", "buy", frm=("Bitpanda", "EUR", "250"), to=("Bitpanda", "BTC", "0.0055"),
               value="251.5"),
            tx("K3", "2024-02-12T12:00:00Z", "withdrawal", frm=("Bitpanda", "BTC", "0.001"), value="55")]
    rows += [tx(f"S{i}", (T0 + timedelta(days=7 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "deposit",
                to=("Bitpanda", "EUR", "50"), value="50") for i in range(3)]
    c, sid = started(config, api, rows=rows)
    plan = [oper(50 + i, "savings_plan",
                 txn(50 + i, "INCOMING", "50.00", EUR_ID,
                     (T0 + timedelta(days=7 * i + 3, hours=18)).strftime("%Y-%m-%dT%H:%M:%SZ"), ttype="deposit",
                     balance=str(300 + 50 * i), wallet=U(0xF0)))
            for i in range(3)]
    api.set_pages(page(ops(1, 3, 4) + plan))
    api.portfolio = portfolio("400", "0.00445")
    return c, sid


def test_savings_plan_deposits_with_lag_are_matched_in_takeover_and_gap_page(config, master, api):
    c, sid = _savings(api, config)
    try:
        assert G.analyse(ctx(c), sid) is None
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        assert a.ok, [(x.key, x.detail) for x in a.blockers]
        assert sorted(t.tx_id for t, _ln, _lag in a.lagged) == ["S0", "S1", "S2"] and a.lag_range == "3,8 Tage"
        assert not a.unmatched_old and not a.unmatched_new  # keine falschen „ohne Gegenstück“
        assert sum(1 for d in a.donors.values() if d.get("value_eur") == "50") == 3  # EUR-Werte aus dem Import
        page_ = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Sparplan-Einzahlungen mit späterem Datum der API" in page_ and "3,8 Tage" in page_

        rep = G.analyse(ctx(c), sid)
        assert rep["verdict"][0] == "good" and rep["gap_count"] == 0 and len(rep["lagged"]) == 3
        assert rep["import_last"] == datetime(2024, 3, 15, 14, 45, tzinfo=UTC) and rep["after_import"] == 1
        page_ = c.get(f"/settings/datasources/{sid}/luecken").text
        assert "API vollständig" in page_ and "3 zugeordnet" in page_ and "alten API" not in page_
        csv = c.get(f"/settings/datasources/{sid}/luecken.csv").text
        assert "Sparplan mit Versatz;2024-03-01T14:45:00Z;EUR;deposit;50;API: 2024-03-05T08:45:00Z (+3,8 Tage)" in csv

        assert TK.apply(ctx(c), sid, a.token)["errors"] == []
        assert holdings(c) == {"EUR": D(400), "BTC": D("0.00445")}
        assert TK.undo(ctx(c), sid)["errors"] == []
    finally:
        c.__exit__(None, None, None)


def test_gap_report_names_rest_and_import_rows(config, master, api):
    c, sid = _setup(api, config, "150")  # API fehlt eine Auszahlung über 200 €, der Import kennt nur 150
    try:
        assert "Kein gespeicherter Vollabruf" in c.get(f"/settings/datasources/{sid}/luecken").text
        TK.run_fetch(ctx(c), sid)
        rep = G.analyse(ctx(c), sid)
        eur = next(x for x in rep["assets"] if x["asset"] == "EUR")
        assert eur["gaps_total"] == D(-200) and eur["filled"] == D(-150) and eur["rest"] == D(-50)
        assert eur["state"] == "Lücken belegt" and rep["gap_count"] == 1
        assert rep["verdict"][0] == "crit" and "EUR" in rep["verdict"][1]
        k5 = [g for g in rep["import_only"] if g["asset"] == "EUR" and g["kind"].startswith("withdrawal")]
        assert k5 and k5[0]["fillers"] == 1 and k5[0]["sum"] == D(-150)
        page_ = c.get(f"/settings/datasources/{sid}/luecken").text
        assert "Lücken belegt: 1" in page_ and "Gegenprüfung" not in page_
        assert "Lücken der API prüfen" in c.get(f"/settings/datasources/{sid}/uebernahme").text
        csv = c.get(f"/settings/datasources/{sid}/luecken.csv").text
        assert "Lücke laut Saldo;;EUR;" in csv
        assert "Import ohne API;2024-02-15T10:00:00Z;EUR;withdrawal;-150;füllt Lücke der API" in csv
        assert c.post(f"/settings/datasources/{sid}/luecken/alt", headers={"X-CSRF-Token": c.token}).status_code \
            in (404, 405)  # Gegenprüfung mit der alten API entfernt
        assert c.get("/settings/datasources/99999/luecken").status_code == 404
    finally:
        c.__exit__(None, None, None)
