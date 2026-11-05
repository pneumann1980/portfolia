"""0.98.0 Stufe 2: Eröffnungsstand + Buchungen ab Stichtag == vollständige Berechnung (Gleichwertigkeit)."""

from __future__ import annotations

import dataclasses
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.cutover import core as CO
from app.ledger.engine import EngineOptions, run_ledger
from tests.helpers import portfolio, tx

D = Decimal
ACCS = ("Börse", "Wallet", "Depot")


def _history(seed: int, n: int = 80) -> list[dict]:
    """Zufällige, in sich stimmige Historie: Einzahlungen, Käufe, Verkäufe, Tausch, Transfers, Erträge, Gebühren,
    Abgänge, Kapitalmaßnahme – über 2023–2026."""
    rnd = random.Random(seed)
    rows: list[dict] = []
    held: dict[tuple[str, str], D] = {}
    d0 = date(2023, 1, 1)
    for i in range(n):
        day = d0 + timedelta(days=int(i * 1100 / n) + rnd.randint(0, 3))
        ts = f"{day.isoformat()}T{rnd.randint(8, 20):02d}:{rnd.randint(0, 59):02d}:00Z"
        acc = rnd.choice(ACCS)
        asset = rnd.choice(("BTC", "ETH", "KAS"))
        kind = rnd.choice(("buy", "buy", "buy", "sell", "trade", "transfer", "income", "lost", "fee_sell", "dep", "ca"))
        qty = D(rnd.randint(1, 900)) / D(100)
        eur = (qty * D(rnd.randint(20, 900))).quantize(D("0.01"))
        key = (acc, asset)
        tid = f"T{seed}-{i:03d}"
        if kind == "buy":
            fee = ("EUR", "1.5", "1.5") if rnd.random() < 0.3 else None
            rows.append(tx(tid, ts, "buy", frm=(acc, "EUR", str(eur)), to=(acc, asset, str(qty)), value=str(eur),
                           fee=fee))
            held[key] = held.get(key, D(0)) + qty
        elif kind in ("sell", "fee_sell") and held.get(key, D(0)) > 0:
            q = min(held[key], qty)
            fee = (asset, "0.001", "0.4") if kind == "fee_sell" and q > D("0.01") else None
            rows.append(tx(tid, ts, "sell", frm=(acc, asset, str(q)), to=(acc, "EUR", str(eur)), value=str(eur),
                           fee=fee))
            held[key] -= q + (D("0.001") if fee else D(0))
            held[key] = max(held[key], D(0))
        elif kind == "trade" and held.get(key, D(0)) > 0:
            q = min(held[key], qty)
            other = "ETH" if asset != "ETH" else "BTC"
            rows.append(tx(tid, ts, "trade", frm=(acc, asset, str(q)), to=(acc, other, str(qty)), value=str(eur)))
            held[key] -= q
            held[(acc, other)] = held.get((acc, other), D(0)) + qty
        elif kind == "transfer" and held.get(key, D(0)) > 0:
            q = min(held[key], qty)
            dst = rnd.choice([a for a in ACCS if a != acc])
            rows.append(tx(tid, ts, "transfer", frm=(acc, asset, str(q)), to=(dst, asset, str(q))))
            held[key] -= q
            held[(dst, asset)] = held.get((dst, asset), D(0)) + q
        elif kind == "income":
            rows.append(tx(tid, ts, "deposit", tag="staking", to=(acc, asset, str(qty / 10)), value=str(eur / 10)))
            held[key] = held.get(key, D(0)) + qty / 10
        elif kind == "lost" and held.get(key, D(0)) > 0:
            q = min(held[key], qty / 5)
            rows.append(tx(tid, ts, "withdrawal", tag="lost", frm=(acc, asset, str(q)), value="0"))
            held[key] -= q
        elif kind == "dep":
            rows.append(tx(tid, ts, "deposit", to=(acc, "EUR", str(eur)), value=str(eur)))
        elif kind == "ca" and held.get(key, D(0)) > 0 and asset == "KAS":
            q = held[key]
            rows.append(tx(tid, ts, "corporate_action", frm=(acc, "KAS", str(q)), to=(acc, "BNB", str(q * 2)),
                           value="0"))
            held[key] = D(0)
            held[(acc, "BNB")] = held.get((acc, "BNB"), D(0)) + q * 2
    return rows


def _check(rows, cutoff, **opts):
    pf = portfolio(rows)
    base = EngineOptions(**opts)
    full = run_ledger(pf, dataclasses.replace(base, snapshot_dates=(cutoff - timedelta(days=1),)))
    st = full.state_snapshots[cutoff - timedelta(days=1)]
    act_pf = CO.active_portfolio(pf, cutoff, st)
    act = run_ledger(act_pf, base)
    return pf, full, act, CO.compare(full, act, cutoff, {t.tx_id: t.date for t in pf.txs})


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("scope,fiat_off", [("global", False), ("account", False), ("account", True),
                                            ("global", True)])
def test_opening_plus_active_equals_full_ledger(seed, scope, fiat_off):
    rows = _history(seed)
    for cutoff in (date(2024, 1, 1), date(2024, 9, 15), date(2025, 1, 1), date(2026, 1, 1)):
        _pf, _full, _act, checks = _check(rows, cutoff, scope=scope, fiat_off=fiat_off)
        bad = [c for c in checks if not c.ok]
        assert not bad, (seed, scope, fiat_off, cutoff, [(c.key, c.detail) for c in bad])


@pytest.mark.parametrize("method", ["fifo", "lifo", "hifo"])
def test_equivalence_holds_for_other_lot_orders(method):
    rows = _history(3)
    _pf, _f, _a, checks = _check(rows, date(2025, 1, 1), scope="account", method=method)
    assert all(c.ok for c in checks), [(c.key, c.detail) for c in checks if not c.ok]


def test_lots_keep_original_acquisition_date_and_cost():
    rows = [
        tx("B1", "2023-03-01T10:00:00Z", "buy", frm=("Börse", "EUR", "1000"), to=("Börse", "BTC", "1"), value="1000"),
        tx("S1", "2025-02-01T10:00:00Z", "sell", frm=("Börse", "BTC", "0.4"), to=("Börse", "EUR", "800"), value="800"),
    ]
    _pf, _full, act, checks = _check(rows, date(2025, 1, 1))
    assert all(c.ok for c in checks)
    (lot,) = act.lots
    assert lot.acq_date == date(2023, 3, 1) and lot.qty == D("0.6") and lot.cost == D(600)  # Stichtag ≠ Kaufdatum
    (d,) = act.disposals
    assert d.parts[0].acq_date == date(2023, 3, 1) and d.parts[0].cost == D(400)  # > 1 Jahr: steuerfrei erkennbar


def test_transfer_across_cutoff_keeps_the_lot_with_its_origin():
    rows = [
        tx("B1", "2024-06-01T10:00:00Z", "buy", frm=("Börse", "EUR", "500"), to=("Börse", "BTC", "0.5"), value="500"),
        tx("T1", "2025-01-05T10:00:00Z", "transfer", frm=("Börse", "BTC", "0.5"), to=("Wallet", "BTC", "0.5")),
        tx("S1", "2025-03-01T10:00:00Z", "sell", frm=("Wallet", "BTC", "0.5"), to=("Wallet", "EUR", "900"),
           value="900"),
    ]
    _pf, _f, act, checks = _check(rows, date(2025, 1, 1), scope="account")
    assert all(c.ok for c in checks)
    assert act.disposals[0].parts[0].acq_date == date(2024, 6, 1) and not act.disposals[0].missing_basis


def test_empty_archive_and_empty_active_are_handled():
    rows = [tx("B1", "2024-06-01T10:00:00Z", "buy", frm=("Börse", "EUR", "10"), to=("Börse", "BTC", "1"), value="10")]
    for cutoff in (date(2020, 1, 1), date(2030, 1, 1)):
        _pf, _f, act, checks = _check(rows, cutoff)
        assert all(c.ok for c in checks), cutoff
        assert sum(x.qty for x in act.lots) == D(1)


def test_negative_balance_reported_once_across_the_cutoff():
    rows = [
        tx("S0", "2024-06-01T10:00:00Z", "sell", frm=("Börse", "BTC", "1"), to=("Börse", "EUR", "10"), value="10"),
        tx("S1", "2025-02-01T10:00:00Z", "sell", frm=("Börse", "BTC", "1"), to=("Börse", "EUR", "10"), value="10"),
    ]
    _pf, full, act, checks = _check(rows, date(2025, 1, 1))
    assert all(c.ok for c in checks)
    assert sum(i.code == "negative_balance" for i in full.issues) == sum(i.code == "negative_balance"
                                                                          for i in act.issues) + 1


def test_the_random_histories_are_not_trivial():
    n_open = n_disp = 0
    for seed in range(12):
        _pf, _full, act, _c = _check(_history(seed), date(2025, 1, 1), scope="account")
        n_open += len(act.lots)
        n_disp += len(act.disposals)
    assert n_open > 50 and n_disp > 30


def test_active_ledger_ignores_the_archive_but_keeps_state():
    rows = _history(5)
    pf, full, act, _c = _check(rows, date(2025, 1, 1))
    assert act.tx_count == sum(1 for t in pf.txs if t.date >= date(2025, 1, 1))
    assert act.tx_count < full.tx_count
    assert len(act.qty_events) < len(full.qty_events)
