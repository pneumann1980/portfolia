"""Tagesreihen: Bestände aus dem Ledger × historische Schlusskurse (EUR) → Depotwert, Flüsse, Kapital.

Bewertungsregeln für Lücken:
* Zwischen zwei Kursen: letzter bekannter Schlusskurs (Wochenende/Feiertag).
* Vor dem ersten verfügbaren Kurs: Ersatzkurs (manuell bzw. Transaktionskurs value_eur/Menge) als Schätzung,
  sonst erster Kurs; solche Tage werden als „geschätzt“ gezählt und im UI ausgewiesen.
* Assets ohne Marktkurse: Ersatzkurse nach derselben Regel wie die aktuelle Bewertung (``app.prices.fallback``:
  zwischen zwei Kurspunkten fortgeschrieben, nach dem letzten höchstens N Tage), sonst 0 € („unbewertet“).
  So werden auch längst verkaufte Positionen ohne Kursquelle mit ihren Kauf-/Verkaufskursen bewertet.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from app.analytics import quality as Q
from app.analytics.quality import AssetQuality
from app.analytics.valuation import FlowValuer
from app.db import Database
from app.ledger.engine import LedgerResult
from app.ledger.models import AssetInfo, Portfolio
from app.prices.fallback import FallbackPrices
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
    estimated_days: dict[str, int] = field(default_factory=dict)  # Tage vor dem ersten Marktkurs (geschätzt)
    fallback_days: dict[str, int] = field(default_factory=dict)  # Tage mit Ersatzkurs (ohne Marktkurse)
    unvalued_assets: list[str] = field(default_factory=list)  # heute gehalten, ohne gültigen Kurs (0 €)
    unvalued_past: list[str] = field(default_factory=list)  # nur früher zeitweise ohne gültigen Kurs
    asset_kind: np.ndarray | None = None  # (M, N) Herkunft des Kurses je Tag (app.analytics.quality, Codes)
    quality: dict[str, AssetQuality] = field(default_factory=dict)  # Kursqualität je Asset (ohne Fiat)
    # Bewertungslücken (Position ohne Kurs, 0 €): neutrale Ein-/Ausgänge nur für Renditekennzahlen – eine fehlende
    # Kursinformation ist kein Wertverlust (echter Verlust: Ausbuchung). (M, N) je Asset, ≥ 0
    asset_gap_in: np.ndarray | None = None
    asset_gap_out: np.ndarray | None = None
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

    def gaps(self, asset_ids: list[str] | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Neutrale Ein-/Ausgänge wegen Bewertungslücken (Summe aller bzw. ausgewählter Assets)."""
        if self.asset_gap_in is None or self.asset_gap_out is None or not len(self.asset_ids):
            z = np.zeros(self.n)
            return z, z
        if asset_ids is None:
            return self.asset_gap_in.sum(axis=0), self.asset_gap_out.sum(axis=0)
        idx = self.asset_index()
        rows = [idx[a] for a in asset_ids if a in idx]
        if not rows:
            z = np.zeros(self.n)
            return z, z
        return self.asset_gap_in[rows].sum(axis=0), self.asset_gap_out[rows].sum(axis=0)


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


def _ffill_valid(values: np.ndarray, ok: np.ndarray) -> np.ndarray:
    """Letzter gültiger Wert je Tag (0 vor dem ersten)."""
    idx = np.where(ok, np.arange(len(values)), -1)
    np.maximum.accumulate(idx, out=idx)
    return np.where(idx >= 0, values[np.maximum(idx, 0)], 0.0)


def _row_get(row: Any, key: str) -> Any:
    try:
        return row[key] if row is not None else None
    except (IndexError, KeyError):  # ältere Datenbank ohne Spalte
        return None


def _market_prices(n: int, start: date, points: list[tuple[str, float, str | None, str]], fx_arr: Any,
                   max_carry: int) -> tuple[np.ndarray, np.ndarray, list[str | None], int]:
    """Marktkurse einer Reihe auf dem Tagesraster: Wert in EUR (NaN vor dem ersten Kurs), Herkunft je Tag, Quelle je
    Tag und Index des ersten Kurses.

    Je Währung wird der zuletzt bekannte Schlusskurs fortgeschrieben und mit dem Devisenkurs *des Tages* umgerechnet
    (wie bisher); gibt es Kurse in mehreren Währungen (z. B. Ersatzanbieter in USD), gilt je Tag der jüngste Kurs.
    Herkunft: Hauptanbieter → Marktkurs, sonst alternativer Anbieter; älter als ``max_carry`` Tage → fortgeschrieben."""
    from app.prices.service import PRIMARY_SOURCES

    by_ccy: dict[str, list[tuple[int, float, str]]] = defaultdict(list)
    for d, close, ccy, src in points:
        by_ccy[(ccy or "EUR").upper()].append(((date.fromisoformat(d) - start).days, close, src))
    best_val = np.full(n, np.nan)
    best_day = np.full(n, np.iinfo(np.int64).min, dtype=np.int64)
    best_src = np.full(n, -1, dtype=np.int64)
    sources: list[str] = []
    for ccy, pts in by_ccy.items():
        pid = np.full(n, -1, dtype=np.int64)
        before = -1
        for j, (i, _c, _s) in enumerate(pts):
            if i < 0:
                before = j
            elif i < n:
                pid[i] = j
        if before >= 0 and pid[0] < 0:
            pid[0] = before
        has = pid >= 0
        if not has.any():
            continue
        idx = np.where(has, np.arange(n), 0)
        np.maximum.accumulate(idx, out=idx)
        fill = pid[idx]
        fill[: int(np.argmax(has))] = -1
        ok = fill >= 0
        closes = np.array([c for _i, c, _s in pts], dtype=float)
        pdays = np.array([i for i, _c, _s in pts], dtype=np.int64)
        base = len(sources)
        sources += [s for _i, _c, s in pts]
        vals = np.where(ok, closes[np.maximum(fill, 0)], np.nan) * fx_arr(ccy)
        pday = np.where(ok, pdays[np.maximum(fill, 0)], np.iinfo(np.int64).min)
        take = ok & ~np.isnan(vals) & (pday > best_day)
        best_val = np.where(take, vals, best_val)
        best_day = np.where(take, pday, best_day)
        best_src = np.where(take, fill + base, best_src)
    valid = ~np.isnan(best_val)
    code = np.full(n, Q.NONE, dtype=np.int8)
    srcs: list[str | None] = [None] * n
    if not valid.any():
        return best_val, code, srcs, n
    first = int(np.argmax(valid))
    age = np.where(valid, np.arange(n) - np.where(valid, best_day, 0), 0)
    primary = np.array([x in PRIMARY_SOURCES for x in sources], dtype=bool)
    code[valid] = Q.ALT
    code[valid & primary[np.maximum(best_src, 0)]] = Q.MARKET
    code[valid & (age > max_carry)] = Q.INTERP
    for i in np.nonzero(valid & (code != Q.MARKET))[0]:  # nur Tage ohne Marktkurs (selten)
        src = sources[int(best_src[i])]
        srcs[i] = (f"Kurs vom {(start + timedelta(days=int(best_day[i]))):%d.%m.%Y} ({src or '–'})"
                   if code[i] == Q.INTERP else src)
    return best_val, code, srcs, first


def compute_history(pf: Portfolio, ledger: LedgerResult, store: PriceStore, series_for: Any,
                    valuer: FlowValuer, end: date | None = None, settings: Any = None) -> History | None:
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
    fb = FallbackPrices(pf, settings)
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
    kinds = np.zeros((m, n), dtype=np.int8)  # Herkunft des Kurses je Tag (app.analytics.quality)
    estimated: dict[str, int] = {}
    fallback_days: dict[str, int] = {}
    unvalued: list[str] = []
    unvalued_past: list[str] = []
    quality: dict[str, AssetQuality] = {}
    days = np.arange(n)
    for k, aid in enumerate(asset_ids):
        a: AssetInfo = pf.asset(aid)
        if a.is_fiat:
            fx = fx_arr(aid)
            price[k] = np.where(np.isnan(fx), 0.0, fx)
            kinds[k] = Q.MARKET
            continue
        s = series_for(a)
        arr = np.full(n, np.nan)
        code = np.full(n, Q.NONE, dtype=np.int8)
        srcs: list[str | None] = [None] * n
        first = n
        first_src = "erster Marktkurs"
        held = qty[k] != 0
        meta = store.meta(s) if s else None
        if s:
            arr, code, srcs, first = _market_prices(n, start, store.daily_points(s), fx_arr,
                                                    Q.MAX_CARRY["crypto" if a.is_crypto else "security"])
        if first < n:
            if first > 0:
                # vor dem ersten Marktkurs: Ersatzkurs (ohne Ablauf – der Marktkurs folgt), sonst erster Marktkurs
                est, _, manual = fb.daily_detail(a, n, start, expire=False)
                gap = days < first
                has_est = gap & ~np.isnan(est)
                arr = np.where(gap, np.where(np.isnan(est), arr[first], est), arr)
                code[gap] = Q.FIRST
                code[has_est] = np.where(manual[has_est], Q.MANUAL, Q.TX)
                first_src = f"erster Marktkurs {(start + timedelta(days=first)):%d.%m.%Y}"
                estimated[aid] = int(np.sum(gap & held))
        else:
            # keine Marktkurse: Ersatzkurse mit begrenzter Gültigkeit (wie die aktuelle Bewertung); vor dem
            # ersten Kurspunkt gilt dieser als Schätzung
            arr, _, manual = fb.daily_detail(a, n, start, backfill=True)
            ok = ~np.isnan(arr)
            code[:] = Q.NONE
            code[ok] = np.where(manual[ok], Q.MANUAL, Q.TX)
            n_fb = int(np.sum(held & ok))
            if n_fb:
                fallback_days[aid] = n_fb
        valid = ~np.isnan(arr)
        code[~valid] = Q.NONE
        if held[-1] and not valid[-1]:
            unvalued.append(aid)
        elif np.any(held & ~valid):
            unvalued_past.append(aid)
        price[k] = np.where(valid, arr, 0.0)
        kinds[k] = code
        labels = {Q.TX: "Transaktionen", Q.MANUAL: "manuelle Kurse", Q.FIRST: first_src}
        srcs = [x if x is not None else labels.get(c) for x, c in zip(srcs, code.tolist(), strict=True)]
        # „konnte nicht geladen werden“ nur, wenn auch keine früher geladenen Marktkurse vorliegen
        failed = (meta["history_error"] or "Abruf fehlgeschlagen") if s and first >= n and meta is not None \
            and meta["history_status"] == "error" else None
        alt_note = _row_get(meta, "alt_note")
        quality[aid] = Q.summarize(aid, Q.segments(aid, code, held, start, srcs), failed, alt_note)

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

    # -- Bewertungslücken: Übergang in/aus „kein Kurs“ und Zu-/Abflüsse unbewerteter Positionen ----------------
    g_in = np.zeros((m, n))
    g_out = np.zeros((m, n))
    lost: dict[tuple[str, int], float] = defaultdict(float)  # ausdrücklich ausgebucht (Verlust, Diebstahl, Burn)
    for d in ledger.disposals:
        if d.kind == "lost" and 0 <= (d.date - start).days < n:
            lost[(d.asset, (d.date - start).days)] += float(d.qty)
    for k in range(m):
        if not np.any(kinds[k] == Q.NONE):
            continue
        q = qty[k]
        ok = kinds[k] != Q.NONE
        held_prev = np.concatenate([[False], q[:-1] != 0])
        ok_prev = np.concatenate([[True], ok[:-1]])
        q_prev = np.concatenate([[0.0], q[:-1]])
        p_prev = np.concatenate([[0.0], price[k][:-1]])
        enter = ok_prev & ~ok & held_prev  # Kurs fällt weg: bisheriger Wert verlässt die bewertete Menge
        leave = ~ok_prev & ok & held_prev  # Kurs kommt zurück: Wert tritt wieder ein (keine Scheinrendite)
        g_out[k] += np.where(enter, q_prev * p_prev, 0.0)
        g_in[k] += np.where(leave, q_prev * price[k], 0.0)
        g_out[k] += np.where(~ok, a_in[k], 0.0)  # Wert fließt in eine unbewertete Position
        g_in[k] += np.where(~ok, a_out[k], 0.0)  # … bzw. aus ihr heraus (Verkaufserlös)
        # Ausbuchung einer unbewerteten Position: echter Verlust zum letzten bekannten Kurs (Wert tritt kurz ein
        # und geht verloren) – sonst bliebe die ausdrücklich gebuchte Wertberichtigung ohne Wirkung auf die Rendite
        aid = asset_ids[k]
        last_ok = _ffill_valid(price[k], ok)
        for (a_id, t), lq in lost.items():
            if a_id == aid and not ok[t]:
                g_in[k][t] += lq * last_ok[t]

    return History(start=start, dates=dates, value=value_a.sum(axis=0), inflow=inflow, outflow=outflow,
                   invested=invested, income=income, fees=fees, asset_ids=asset_ids, asset_qty=qty,
                   asset_price=price, asset_value=value_a, asset_in=a_in, asset_out=a_out,
                   estimated_days=estimated, fallback_days=fallback_days, unvalued_assets=unvalued,
                   unvalued_past=unvalued_past, asset_kind=kinds, quality=quality, asset_gap_in=g_in,
                   asset_gap_out=g_out)


def persist_snapshots(db: Database, hist: History, import_id: int | None, kind: str = "backfill",
                      only_last: bool = False) -> int:
    now = iso(datetime.now(UTC))
    rng = range(hist.n - 1, hist.n) if only_last else range(hist.n)
    rows = [(hist.dates[i].isoformat(), import_id, float(hist.value[i]), float(hist.invested[i]),
             float(hist.inflow[i]), float(hist.outflow[i]), float(hist.income[i]), float(hist.fees[i]),
             len(hist.unvalued_assets), int(sum(1 for v in hist.estimated_days.values() if v)), now,
             kind) for i in rng]
    arows = []
    kinds = hist.asset_kind
    for k, aid in enumerate(hist.asset_ids):
        q = hist.asset_qty[k]
        for i in rng:
            flow = hist.asset_in[k][i] - hist.asset_out[k][i]
            if q[i] != 0 or flow != 0:
                kind = Q.CODES.get(int(kinds[k][i])) if kinds is not None else None
                arows.append((hist.dates[i].isoformat(), aid, float(q[i]), float(hist.asset_price[k][i]),
                              float(hist.asset_value[k][i]), float(flow), kind))
    gaps = [(sg.asset_id, sg.kind, sg.method, sg.source, sg.start.isoformat(), sg.end.isoformat(), sg.days, now)
            for aq in hist.quality.values() for sg in aq.segments]
    with db.transaction() as c:
        if not only_last:
            c.execute("DELETE FROM snapshot_daily")
            c.execute("DELETE FROM snapshot_asset_daily")
            c.execute("DELETE FROM price_gap")
            c.executemany("INSERT OR REPLACE INTO price_gap(asset_id, kind, method, source, date_from, date_to, days, "
                          "computed_at) VALUES (?,?,?,?,?,?,?,?)", gaps)
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
