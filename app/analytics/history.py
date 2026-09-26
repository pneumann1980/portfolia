"""Tagesreihen: Bestände aus dem Ledger × historische Schlusskurse (EUR) → Depotwert, Flüsse, Kapital.

Bewertungsregeln für Lücken:
* Zwischen zwei Kursen: letzter bekannter Schlusskurs (Wochenende/Feiertag).
* Vor dem ersten verfügbaren Kurs: Transaktionskurs (value_eur/Menge) als Schätzung, sonst erster Kurs;
  solche Tage werden als „geschätzt“ gezählt und im UI ausgewiesen.
* Assets ohne jede Kursquelle: 0 € (konsistent mit der aktuellen Bewertung „unbewertet“).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from app.analytics.valuation import FlowValuer
from app.db import Database
from app.ledger.engine import LedgerResult
from app.ledger.models import AssetInfo, Portfolio
from app.prices.store import PriceStore
from app.util.timeutil import iso, today_local

log = logging.getLogger(__name__)


@dataclass
class History:
    start: date
    dates: list[date]
    value: np.ndarray
    inflow: np.ndarray  # ≥ 0
    outflow: np.ndarray  # ≥ 0 (Betrag)
    invested: np.ndarray
    income: np.ndarray
    fees: np.ndarray
    asset_ids: list[str]
    asset_qty: np.ndarray  # (M, N)
    asset_price: np.ndarray  # (M, N) EUR
    asset_value: np.ndarray  # (M, N)
    asset_in: np.ndarray  # (M, N) Zuflüsse in die Position ≥ 0
    asset_out: np.ndarray  # (M, N) Abflüsse aus der Position ≥ 0
    estimated_days: dict[str, int] = field(default_factory=dict)
    unvalued_assets: list[str] = field(default_factory=list)
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def n(self) -> int:
        return len(self.dates)

    @property
    def net_flow(self) -> np.ndarray:
        return self.inflow - self.outflow

    def index_of(self, d: date) -> int:
        """Index des Tages d (geklemmt auf den Bereich)."""
        i = (d - self.start).days
        return max(0, min(self.n - 1, i))

    def asset_index(self) -> dict[str, int]:
        return {a: i for i, a in enumerate(self.asset_ids)}

    def group(self, asset_ids: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        idx = self.asset_index()
        rows = [idx[a] for a in asset_ids if a in idx]
        if not rows:
            z = np.zeros(self.n)
            return z, z, z
        return (self.asset_value[rows].sum(axis=0), self.asset_in[rows].sum(axis=0),
                self.asset_out[rows].sum(axis=0))


def _implied_prices(pf: Portfolio, ledger: LedgerResult) -> dict[str, list[tuple[date, float]]]:
    """Transaktionskurse (EUR je Einheit) aus Käufen, Verkäufen, Tauschen, Erträgen und Zu-/Abgängen."""
    out: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for t in pf.txs:
        v = t.value_eur
        if v is None or v <= 0 or t.type in ("transfer", "corporate_action"):
            continue
        for asset, qty in ((t.to_asset, t.to_qty), (t.from_asset, t.from_qty)):
            if not asset or not qty or qty <= 0:
                continue
            a = pf.assets.get(asset)
            if a is None or a.is_fiat:
                continue
            out[asset].append((t.date, float(v / qty)))
    for lst in out.values():
        lst.sort()
    return out


def _ffill(n: int, start: date, points: list[tuple[date, float]]) -> tuple[np.ndarray, int]:
    """Punkte (aufsteigend) auf das Tagesraster legen und vorwärts füllen.

    Rückgabe: (Array mit NaN vor dem ersten Wert, Index des ersten Wertes bzw. n)."""
    arr = np.full(n, np.nan)
    first = n
    before = None
    for d, v in points:
        i = (d - start).days
        if i < 0:
            before = v
            continue
        if i >= n:
            continue
        arr[i] = v
        first = min(first, i)
    if before is not None:
        if np.isnan(arr[0]):
            arr[0] = before
        first = 0
    valid = ~np.isnan(arr)
    if not valid.any():
        return arr, n
    idx = np.where(valid, np.arange(n), 0)
    np.maximum.accumulate(idx, out=idx)
    filled = arr[idx]
    filled[:first] = np.nan
    return filled, first


def compute_history(pf: Portfolio, ledger: LedgerResult, store: PriceStore, series_for: Any,
                    valuer: FlowValuer, end: date | None = None) -> History | None:
    if ledger.first_date is None:
        return None
    start = ledger.first_date
    end = end or today_local()
    if end < start:
        end = start
    n = (end - start).days + 1
    dates = [start + timedelta(days=i) for i in range(n)]

    # -- Bestände je Asset (global) --------------------------------------------------------------
    deltas: dict[str, np.ndarray] = {}
    for ev in ledger.qty_events:
        i = (ev.date - start).days
        if i < 0 or i >= n:
            continue
        arr = deltas.get(ev.asset)
        if arr is None:
            arr = np.zeros(n)
            deltas[ev.asset] = arr
        arr[i] += float(ev.delta)
    asset_ids = sorted(deltas)
    m = len(asset_ids)
    qty = np.zeros((m, n))
    for k, a in enumerate(asset_ids):
        q = np.cumsum(deltas[a])
        q[np.abs(q) < 1e-12] = 0.0
        qty[k] = q

    # -- Kurse je Asset (EUR) --------------------------------------------------------------------
    implied = _implied_prices(pf, ledger)
    fx_cache: dict[str, np.ndarray] = {}

    def fx_arr(ccy: str) -> np.ndarray:
        c = ccy.upper()
        if c == "EUR":
            return np.ones(n)
        if c not in fx_cache:
            pts = sorted((date.fromisoformat(d), 1.0 / r) for d, r in store.fx_series_map(c).items() if r)
            arr, first = _ffill(n, start, pts)
            if first < n and first > 0:
                arr[:first] = arr[first]
            fx_cache[c] = arr
        return fx_cache[c]

    price = np.zeros((m, n))
    estimated: dict[str, int] = {}
    unvalued: list[str] = []
    for k, aid in enumerate(asset_ids):
        a: AssetInfo = pf.asset(aid)
        if a.is_fiat:
            fx = fx_arr(aid)
            price[k] = np.where(np.isnan(fx), 0.0, fx)
            continue
        s = series_for(a)
        arr = np.full(n, np.nan)
        first = n
        if s:
            by_ccy: dict[str, list[tuple[date, float]]] = defaultdict(list)
            for d, close, ccy in store.daily_closes(s):
                by_ccy[(ccy or "EUR").upper()].append((date.fromisoformat(d), close))
            for ccy, pts in by_ccy.items():
                a_arr, f = _ffill(n, start, pts)
                arr = np.where(np.isnan(arr), a_arr * fx_arr(ccy), arr)
                first = min(first, f)
        manual = pf.manual_prices.get(aid)
        if manual:
            man, mf = _ffill(n, start, manual)
            arr = np.where(np.isnan(arr), man, arr)
            first = min(first, mf)
        if first >= n and not s and not manual:
            unvalued.append(aid)  # keine Kursquelle: 0 € (wie aktuelle Bewertung)
            continue
        held = qty[k] != 0
        if first > 0:
            imp = implied.get(aid, [])
            imp_arr = _ffill(n, start, imp)[0] if imp else np.full(n, np.nan)
            gap = np.arange(n) < first
            fallback = arr[first] if first < n else (imp[0][1] if imp else np.nan)
            fill = np.where(np.isnan(imp_arr), fallback, imp_arr)
            arr = np.where(gap, fill, arr)
            estimated[aid] = int(np.sum(gap & held))
        arr = np.where(np.isnan(arr), 0.0, arr)
        if not np.any(arr):
            unvalued.append(aid)
        price[k] = arr

    value_a = qty * price

    # -- Flüsse -------------------------------------------------------------------------------
    inflow = np.zeros(n)
    outflow = np.zeros(n)
    for f in ledger.flows:
        i = (f.date - start).days
        if i < 0 or i >= n:
            continue
        amt = valuer.amount(f, pf)
        if amt >= 0:
            inflow[i] += amt
        else:
            outflow[i] += -amt
    invested = np.cumsum(inflow - outflow)

    a_in = np.zeros((m, n))
    a_out = np.zeros((m, n))
    aidx = {a: i for i, a in enumerate(asset_ids)}
    for af in ledger.asset_flows:
        k = aidx.get(af.asset)
        i = (af.date - start).days
        if k is None or i < 0 or i >= n:
            continue
        amt = float(af.amount) if af.amount is not None else float(af.qty) * price[k][i]
        if amt >= 0:
            a_in[k][i] += amt
        else:
            a_out[k][i] += -amt

    income = np.zeros(n)
    for e in ledger.income:
        i = (e.date - start).days
        if 0 <= i < n:
            income[i] += float(e.value_eur)
    fees = np.zeros(n)
    for fe in ledger.fees:
        i = (fe.date - start).days
        if 0 <= i < n:
            fees[i] += float(fe.eur)

    return History(start=start, dates=dates, value=value_a.sum(axis=0), inflow=inflow, outflow=outflow,
                   invested=invested, income=income, fees=fees, asset_ids=asset_ids, asset_qty=qty,
                   asset_price=price, asset_value=value_a, asset_in=a_in, asset_out=a_out,
                   estimated_days=estimated, unvalued_assets=unvalued)


def persist_snapshots(db: Database, hist: History, import_id: int | None, kind: str = "backfill",
                      only_last: bool = False) -> int:
    now = iso(datetime.now(UTC))
    rng = range(hist.n - 1, hist.n) if only_last else range(hist.n)
    rows = [(hist.dates[i].isoformat(), import_id, float(hist.value[i]), float(hist.invested[i]),
             float(hist.inflow[i]), float(hist.outflow[i]), float(hist.income[i]), float(hist.fees[i]),
             len(hist.unvalued_assets), int(sum(1 for v in hist.estimated_days.values() if v)), now,
             kind) for i in rng]
    arows = []
    for k, aid in enumerate(hist.asset_ids):
        q = hist.asset_qty[k]
        for i in rng:
            flow = hist.asset_in[k][i] - hist.asset_out[k][i]
            if q[i] != 0 or flow != 0:
                arows.append((hist.dates[i].isoformat(), aid, float(q[i]), float(hist.asset_price[k][i]),
                              float(hist.asset_value[k][i]), float(flow), None))
    with db.transaction() as c:
        if not only_last:
            c.execute("DELETE FROM snapshot_daily")
            c.execute("DELETE FROM snapshot_asset_daily")
        else:
            d = hist.dates[-1].isoformat()
            c.execute("DELETE FROM snapshot_asset_daily WHERE date=?", (d,))
        c.executemany(
            """INSERT INTO snapshot_daily(date, import_id, value_eur, invested_eur, inflow_eur, outflow_eur,
                   income_eur, fees_eur, unvalued_count, estimated_count, computed_at, kind)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET import_id=excluded.import_id, value_eur=excluded.value_eur,
                   invested_eur=excluded.invested_eur, inflow_eur=excluded.inflow_eur,
                   outflow_eur=excluded.outflow_eur, income_eur=excluded.income_eur, fees_eur=excluded.fees_eur,
                   unvalued_count=excluded.unvalued_count, estimated_count=excluded.estimated_count,
                   computed_at=excluded.computed_at, kind=excluded.kind""",
            rows,
        )
        c.executemany(
            """INSERT OR REPLACE INTO snapshot_asset_daily(date, asset_id, qty, price_eur, value_eur, flow_eur,
                   price_kind) VALUES (?,?,?,?,?,?,?)""",
            arows,
        )
    return len(rows)
