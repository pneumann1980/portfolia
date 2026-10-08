"""AP2 – Ledger-Invarianten (eigenschaftsbasiert mit festen Seeds) und metamorphe Tests.

Referenz für Bestände ist eine *unabhängige* Summe der Buchungsbeine (keine zweite FIFO-Logik): Bestand je Konto und
Asset = Σ Zugänge − Σ Abgänge − Σ Gebührenbeine. Geprüft werden außerdem: Lots = Bestand, Erhalt der Kostenbasis
(angeschafft = verbleibend + veräußert), keine Datenprobleme bei gedeckten Abgängen, Unabhängigkeit von der
Reihenfolge in der Quelle (auch bei identischen Zeitstempeln) sowie gezielte Sonderfälle.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.ledger.engine import EngineOptions, run_ledger
from tests.helpers import portfolio, tx

D = Decimal
Q = D("0.00000001")
ACCOUNTS = ("Börse", "Wallet")
ASSETS = ("BTC", "ETH", "KAS")


def buy(i, dt, asset, qty, eur, acc="Börse", fee=None):
    return tx(i, dt, "buy", frm=(acc, "EUR", eur), to=(acc, asset, qty), value=eur, fee=fee)


def sell(i, dt, asset, qty, eur, acc="Börse", fee=None):
    return tx(i, dt, "sell", frm=(acc, asset, qty), to=(acc, "EUR", eur), value=eur, fee=fee)


def generate(seed: int, n: int = 120) -> list[dict]:
    """Zufällige, aber stets gedeckte Buchungsfolge (Kauf, Verkauf, Tausch, Transfer mit/ohne Gebühr, Staking)."""
    rnd = random.Random(seed)
    bal: dict[tuple[str, str], Decimal] = defaultdict(lambda: D(0))
    rows = []
    minute = 0
    for i in range(n):
        minute += rnd.randint(1, 3 * 24 * 60)  # streng monoton: Generator und Ledger sehen dieselbe Reihenfolge
        dt = (datetime(2023, 1, 1, tzinfo=UTC) + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:00Z")
        acc = rnd.choice(ACCOUNTS)
        asset = rnd.choice(ASSETS)
        have = bal[(acc, asset)]
        op = rnd.choice(("buy", "buy", "sell", "trade", "transfer", "staking"))
        if op in ("sell", "trade", "transfer") and have <= D("0.001"):
            op = "buy"
        if op == "buy":
            q = D(rnd.randint(1, 5000)) / 1000
            fee = ("EUR", "", str(D(rnd.randint(0, 500)) / 100)) if rnd.random() < 0.3 else None
            rows.append(buy(f"t{i}", dt, asset, q, D(rnd.randint(100, 900000)) / 100, acc, fee))
            bal[(acc, asset)] += q
        elif op == "staking":
            q = D(rnd.randint(1, 900)) / 10000
            rows.append(tx(f"t{i}", dt, "deposit", tag="staking", to=(acc, asset, q), value=D(rnd.randint(1, 900))))
            bal[(acc, asset)] += q
        elif op == "sell":
            q = (have * D(rnd.randint(1, 100)) / 100).quantize(Q)
            rows.append(sell(f"t{i}", dt, asset, q, D(rnd.randint(100, 900000)) / 100, acc))
            bal[(acc, asset)] -= q
        elif op == "trade":
            q = (have * D(rnd.randint(1, 100)) / 100).quantize(Q)
            other = rnd.choice([a for a in ASSETS if a != asset])
            q2 = D(rnd.randint(1, 5000)) / 1000
            rows.append(tx(f"t{i}", dt, "trade", frm=(acc, asset, q), to=(acc, other, q2),
                           value=D(rnd.randint(100, 900000)) / 100))
            bal[(acc, asset)] -= q
            bal[(acc, other)] += q2
        else:  # Transfer zum anderen Konto, teils mit impliziter Netzwerkgebühr
            dst = next(a for a in ACCOUNTS if a != acc)
            q = (have * D(rnd.randint(1, 100)) / 100).quantize(Q)
            recv = q - (q / 100).quantize(Q) if rnd.random() < 0.4 else q
            rows.append(tx(f"t{i}", dt, "transfer", frm=(acc, asset, q), to=(dst, asset, recv)))
            bal[(acc, asset)] -= q
            bal[(dst, asset)] += recv
    return rows


def leg_balances(rows: list[dict]) -> dict[tuple[str, str], Decimal]:
    """Unabhängige Referenz: Summe der Buchungsbeine je Konto und Asset (ohne Fiat)."""
    out: dict[tuple[str, str], Decimal] = defaultdict(lambda: D(0))
    for r in rows:
        if r["from_asset"] and r["from_asset"] != "EUR":
            out[(r["from_account"], r["from_asset"])] -= D(r["from_qty"])
        if r["to_asset"] and r["to_asset"] != "EUR":
            out[(r["to_account"], r["to_asset"])] += D(r["to_qty"])
        if r["fee_asset"] and r["fee_asset"] != "EUR" and r["fee_qty"]:
            out[(r["from_account"] or r["to_account"], r["fee_asset"])] -= D(r["fee_qty"])
    return {k: v for k, v in out.items() if v}


def acquired_cost(led) -> Decimal:
    """Σ Einstand aller angeschafften Lots: verbleibend + veräußert (Kostenbasis bleibt erhalten)."""
    return sum((lot.cost for lot in led.lots), D(0)) + sum((d.cost for d in led.disposals), D(0))


def expected_cost(rows: list[dict]) -> Decimal:
    tot = D(0)
    for r in rows:
        v = D(r["value_eur"] or 0)
        if r["type"] == "buy":
            tot += v + D(r["fee_eur"] or 0)
        elif r["type"] in ("trade", "deposit"):
            tot += v
    return tot


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("scope", ["global", "account"])
def test_random_sequences_keep_invariants(seed, scope):
    rows = generate(seed)
    led = run_ledger(portfolio(rows), EngineOptions(scope=scope))
    ref = leg_balances(rows)
    got = {k: v for k, v in led.balances.items() if k[1] != "EUR"}
    assert got == ref  # Bestand = Summe der Buchungsbeine (Verkauf mindert um genau die Menge)
    lots: dict[tuple[str, str], Decimal] = defaultdict(lambda: D(0))
    for lot in led.lots:
        lots[(lot.account, lot.asset)] += lot.qty
    for k, q in ref.items():  # Summe der Lots = Bestand je Konto
        assert abs(lots.get(k, D(0)) - q) <= D("1e-9"), (k, lots.get(k), q)
    assert abs(acquired_cost(led) - expected_cost(rows)) <= D("1e-8")  # Kostenbasis bleibt erhalten
    assert not [i for i in led.issues if i.code in ("missing_lots", "negative_balance", "transfer_excess")]


@pytest.mark.parametrize("seed", range(6))
def test_result_independent_of_source_order(seed):
    """Metamorph: dieselben Buchungen in anderer Reihenfolge der Quelle → identische Ergebnisse."""
    rows = generate(seed)
    shuffled = rows[:]
    random.Random(seed + 100).shuffle(shuffled)
    a = run_ledger(portfolio(rows), EngineOptions(scope="account"))
    b = run_ledger(portfolio(shuffled), EngineOptions(scope="account"))
    assert a.balances == b.balances
    key = lambda lot: (lot.account, lot.asset, lot.acq_ts, lot.qty, lot.cost)  # noqa: E731
    assert sorted(map(key, a.lots)) == sorted(map(key, b.lots))
    da = sorted((d.tx_id, d.qty, d.proceeds, d.cost) for d in a.disposals)
    db = sorted((d.tx_id, d.qty, d.proceeds, d.cost) for d in b.disposals)
    assert da == db


@pytest.mark.parametrize("scope", ["global", "account"])
def test_identical_timestamps_do_not_depend_on_list_order(scope):
    """Regression: Verkauf vor Kauf mit identischem Zeitstempel ergab Veräußerung ohne Einstand und verwaisten Lot."""
    t = "2025-03-01T10:00:00Z"
    rows = [buy("b1", "2024-01-01T10:00:00Z", "ETH", 1, 1000), sell("s", t, "BTC", "0.6", 600),
            buy("b", t, "BTC", 1, 500), tx("tr", t, "trade", frm=("Börse", "ETH", "1"), to=("Börse", "KAS", 100),
                                            value=1500)]
    results = []
    for perm in (rows, list(reversed(rows)), [rows[0], rows[2], rows[1], rows[3]]):
        led = run_ledger(portfolio(perm), EngineOptions(scope=scope))
        results.append((sorted((d.tx_id, d.cost) for d in led.disposals),
                        sorted((lot.asset, lot.qty, lot.cost) for lot in led.lots), led.issues))
    assert results[0][0] == results[1][0] == results[2][0]
    assert results[0][1] == results[1][1] == results[2][1]
    assert dict(results[0][0])["s"] == D(300)  # Einstand 0,6 × 500 €
    assert not results[1][2]


def test_transfer_receiving_more_than_sent_is_flagged_and_lots_match():
    rows = [buy("b", "2025-01-01T10:00:00Z", "ETH", 1, 100),
            tx("t", "2025-01-02T10:00:00Z", "transfer", frm=("Börse", "ETH", "1"), to=("Wallet", "ETH", "1.5"))]
    led = run_ledger(portfolio(rows), EngineOptions(scope="account"))
    assert led.balances[("Wallet", "ETH")] == D("1.5")
    assert sum(lot.qty for lot in led.lots_for("ETH", "Wallet")) == D("1.5")
    phantom = [lot for lot in led.lots if lot.origin == "phantom"]
    assert len(phantom) == 1 and phantom[0].qty == D("0.5") and phantom[0].cost == 0
    assert any(i.code == "transfer_excess" for i in led.issues)


def test_internal_transfer_keeps_cost_and_acquisition_date():
    rows = [buy("b", "2024-01-01T10:00:00Z", "ETH", 2, 2000),
            tx("t", "2024-06-01T10:00:00Z", "transfer", frm=("Börse", "ETH", "1"), to=("Wallet", "ETH", "0.99"))]
    led = run_ledger(portfolio(rows), EngineOptions(scope="account"))
    w = led.lots_for("ETH", "Wallet")
    assert len(w) == 1 and w[0].acq_date == date(2024, 1, 1) and w[0].cost == D("990.00")
    fee = [d for d in led.disposals if d.kind == "transfer_fee"]
    assert fee and fee[0].qty == D("0.01") and fee[0].cost == D("10.00")
    # Gesamtvermögen in Einstand: 2000 = 1000 (Börse) + 990 (Wallet) + 10 (Gebühr)
    assert sum(lot.cost for lot in led.lots) + fee[0].cost == D(2000)


@pytest.mark.parametrize(("tag", "frm", "to"), [("split", 10, 40), ("reverse_split", 100, 10)])
def test_split_and_reverse_split_keep_cost_and_dates(tag, frm, to):
    rows = [buy("b1", "2023-01-01T10:00:00Z", "KAS", frm // 2, 100), buy("b2", "2024-01-01T10:00:00Z", "KAS", frm // 2,
                                                                        300),
            tx("c", "2025-01-01T10:00:00Z", "corporate_action", tag=tag, frm=("Börse", "KAS", frm),
               to=("Börse", "KAS", to))]
    led = run_ledger(portfolio(rows), EngineOptions(scope="account"))
    lots = led.lots_for("KAS")
    assert sum(lot.qty for lot in lots) == to == led.balances[("Börse", "KAS")]
    assert sorted((lot.acq_date, lot.cost) for lot in lots) == [(date(2023, 1, 1), D(100)), (date(2024, 1, 1), D(300))]
    assert not any(lot.via for lot in lots)  # gleiches Asset: keine Umstellung


def test_migration_with_fee_leg():
    """Kapitalmaßnahme mit Netzwerkgebühr in einem dritten Asset: Lots fortgeführt, Gebühr als Abgang."""
    rows = [buy("b1", "2023-01-01T10:00:00Z", "KAS", 100, 50), buy("b2", "2023-01-01T11:00:00Z", "ETH", 1, 1000),
            tx("m", "2024-09-04T12:00:00Z", "corporate_action", tag="migration", frm=("Börse", "KAS", 100),
               to=("Börse", "BNB", 100), fee=("ETH", "0.002", "4"))]
    led = run_ledger(portfolio(rows), EngineOptions(scope="account"))
    bnb = led.lots_for("BNB")
    assert sum(lot.qty for lot in bnb) == 100 and bnb[0].cost == 50 and bnb[0].acq_date == date(2023, 1, 1)
    assert bnb[0].via == ("m",)
    fee = next(d for d in led.disposals if d.kind == "fee")
    assert fee.asset == "ETH" and fee.qty == D("0.002") and fee.cost == D("2.000") and fee.proceeds == 4


def test_oversell_is_reported_not_hidden():
    rows = [buy("b", "2025-01-01T10:00:00Z", "BTC", "0.5", 100), sell("s", "2025-02-01T10:00:00Z", "BTC", 1, 300)]
    led = run_ledger(portfolio(rows))
    codes = {i.code for i in led.issues}
    assert {"negative_balance", "missing_lots"} <= codes
    d = led.disposals[0]
    assert [p.missing_basis for p in d.parts] == [False, True] and d.qty == 1


def test_local_date_decides_year_boundary():
    """23:30 UTC am 31.12. ist in Deutschland bereits der 1.1. (Tagesgrenze lokal, Europe/Berlin)."""
    rows = [buy("b", "2025-06-01T10:00:00Z", "BTC", 1, 100), sell("s", "2025-12-31T23:30:00Z", "BTC", 1, 200)]
    led = run_ledger(portfolio(rows))
    assert led.disposals[0].date == date(2026, 1, 1)


def test_historical_edit_recomputes_dependents():
    rows = [buy("b", "2024-01-01T10:00:00Z", "BTC", 1, 100), sell("s", "2025-02-01T10:00:00Z", "BTC", 1, 300)]
    before = run_ledger(portfolio(rows)).disposals[0].cost
    rows[0] = buy("b", "2024-01-01T10:00:00Z", "BTC", 1, 150)  # Kaufpreis nachträglich korrigiert
    after = run_ledger(portfolio(rows)).disposals[0].cost
    assert (before, after) == (D(100), D(150))
