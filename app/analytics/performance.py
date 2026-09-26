"""Renditekennzahlen nach der Methodik von Portfolio Performance.

TTWROR (True Time-Weighted Rate of Return), täglich:

    r_t = (MV_t + Abflüsse_t) / (MV_{t-1} + Zuflüsse_t) − 1
    TTWROR = Π (1 + r_t) − 1,   p. a.: (1 + TTWROR)^(365/Tage) − 1

Zuflüsse gelten als zu Tagesbeginn, Abflüsse als zu Tagesende erfolgt. Damit sind externe
Zahlungsströme neutralisiert (zeitgewichtet).

IRR/XIRR (Interner Zinsfuß, geldgewichtet): Anfangswert als Einzahlung zum Periodenstart, externe
Zahlungsströme mit Datum, Endwert als Auszahlung zum Periodenende; Lösung von
Σ c_i / (1 + r)^((t_i − t_0)/365) = 0.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

import numpy as np


def daily_returns(values: np.ndarray, inflow: np.ndarray, outflow: np.ndarray) -> np.ndarray:
    """values[t] = Marktwert Tagesende; inflow ≥ 0; outflow ≥ 0 (Beträge). r[0] = 0."""
    n = len(values)
    r = np.zeros(n)
    if n < 2:
        return r
    prev = values[:-1] + inflow[1:]
    cur = values[1:] + outflow[1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        rr = np.where(prev > 1e-9, cur / prev - 1.0, 0.0)
    rr[~np.isfinite(rr)] = 0.0
    r[1:] = rr
    return r


def ttwror(values: Sequence[float] | np.ndarray, inflow: Sequence[float] | np.ndarray,
           outflow: Sequence[float] | np.ndarray, start: int = 0, end: int | None = None) -> float:
    """TTWROR über die Tage (start, end] (Indexe in die Tagesreihen)."""
    v = np.asarray(values, dtype=float)
    i = np.asarray(inflow, dtype=float)
    o = np.asarray(outflow, dtype=float)
    end = len(v) - 1 if end is None else end
    if end <= start:
        return 0.0
    r = daily_returns(v[start:end + 1], i[start:end + 1], o[start:end + 1])
    return float(np.prod(1.0 + r[1:]) - 1.0)


def cumulative_index(values: np.ndarray, inflow: np.ndarray, outflow: np.ndarray, base: float = 100.0) -> np.ndarray:
    r = daily_returns(values, inflow, outflow)
    return base * np.cumprod(1.0 + r)


def annualize(total: float, days: int) -> float | None:
    if days <= 0 or total <= -1:
        return None
    if days < 365:
        # Wie Portfolio Performance: unter einem Jahr keine Hochrechnung der TTWROR anzeigen
        return None
    return (1.0 + total) ** (365.0 / days) - 1.0


def drawdown(index: np.ndarray) -> np.ndarray:
    if len(index) == 0:
        return index
    peak = np.maximum.accumulate(index)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, index / peak - 1.0, 0.0)
    return dd


def xnpv(rate: float, flows: Sequence[tuple[date, float]]) -> float:
    t0 = flows[0][0]
    return sum(cf / (1.0 + rate) ** ((d - t0).days / 365.0) for d, cf in flows)


def xirr(flows: Sequence[tuple[date, float]], guess: float = 0.1) -> float | None:
    """Interner Zinsfuß p. a.; None, wenn keine Lösung existiert (kein Vorzeichenwechsel)."""
    flows = sorted([(d, float(cf)) for d, cf in flows if abs(cf) > 1e-9], key=lambda x: x[0])
    if len(flows) < 2:
        return None
    if not (any(cf > 0 for _, cf in flows) and any(cf < 0 for _, cf in flows)):
        return None
    if (flows[-1][0] - flows[0][0]).days <= 0:
        return None
    t0 = flows[0][0]
    ts = np.array([(d - t0).days / 365.0 for d, _ in flows])
    cfs = np.array([cf for _, cf in flows])

    def f(r: float) -> float:
        return float(np.sum(cfs / np.power(1.0 + r, ts)))

    def df(r: float) -> float:
        return float(np.sum(-ts * cfs / np.power(1.0 + r, ts + 1.0)))

    r = guess
    for _ in range(50):
        try:
            fv = f(r)
            d = df(r)
        except (OverflowError, ZeroDivisionError, FloatingPointError):
            break
        if not math.isfinite(fv) or not math.isfinite(d) or d == 0:
            break
        nr = r - fv / d
        if nr <= -0.999999:
            nr = (r - 0.999999) / 2
        if abs(nr - r) < 1e-10:
            return nr
        r = nr
    # Bisektion als robuste Rückfallebene
    lo, hi = -0.9999, 10.0
    flo, fhi = f(lo), f(hi)
    tries = 0
    while flo * fhi > 0 and tries < 8:
        hi *= 10
        fhi = f(hi)
        tries += 1
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < 1e-9 or (hi - lo) < 1e-12:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def irr_for_period(dates: Sequence[date], values: np.ndarray, flows_by_day: np.ndarray, start: int,
                   end: int) -> float | None:
    """IRR über (start, end]: −V_start, −Flüsse (Zufluss ins Depot = Auszahlung des Anlegers), +V_end."""
    cfs: list[tuple[date, float]] = []
    if values[start] > 0:
        cfs.append((dates[start], -float(values[start])))
    for t in range(start + 1, end + 1):
        f = float(flows_by_day[t])
        if abs(f) > 1e-9:
            cfs.append((dates[t], -f))
    cfs.append((dates[end], float(values[end])))
    return xirr(cfs)
