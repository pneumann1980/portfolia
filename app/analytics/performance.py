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

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
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


R_MIN = -0.9999  # −99,99 % p. a.
R_MAX = 1e6  # 100.000.000 % p. a. – darüber ist eine Jahresrendite nicht mehr aussagekräftig
_MIN_WIDTH = 1e-10  # kleinste Intervallbreite in x = ln(1 + r)
_MAX_INTERVALS = 200_000


@dataclass
class RootSearch:
    """Ergebnis der Nullstellensuche für NPV(r) = 0 im Bereich [R_MIN, R_MAX].

    ``certain``: Jede Teilstrecke des Bereichs ist entweder als nullstellenfrei oder als „genau eine Nullstelle“
    nachgewiesen (Intervallschranken, siehe :func:`xirr_search`). Sonst bleiben Stellen, an denen sich eine doppelte
    bzw. sehr eng benachbarte Lösung nicht numerisch sicher ausschließen lässt → Ergebnis „nicht eindeutig
    bestimmbar“."""

    roots: list[float]
    certain: bool
    descartes: int  # Vorzeichenwechsel der Zahlungsreihe = obere Schranke der Lösungsanzahl (r > −100 %)


def _prep(flows: Sequence[tuple[date, float]]) -> tuple[np.ndarray, np.ndarray] | None:
    agg: dict[date, float] = {}
    for d, cf in flows:
        agg[d] = agg.get(d, 0.0) + float(cf)
    items = sorted((d, cf) for d, cf in agg.items() if abs(cf) > 1e-9 and math.isfinite(cf))
    if len(items) < 2:
        return None
    t0 = items[0][0]
    ts = np.array([(d - t0).days / 365.0 for d, _ in items])
    cfs = np.array([cf for _, cf in items])
    return ts, cfs / np.max(np.abs(cfs))  # Skalierung ändert die Nullstellen nicht


def xirr_search(flows: Sequence[tuple[date, float]]) -> RootSearch:
    """Alle Lösungen von NPV(r) = Σ c_i (1 + r)^(−t_i) = 0 für r ∈ [R_MIN, R_MAX] – mit Nachweis statt Raster.

    Substitution x = ln(1 + r): NPV wird zur Exponentialsumme h(x) = Σ c_i e^(λ_i x) (für x < 0 mit e^(t_max x)
    multipliziert, damit nichts überläuft – die Nullstellen bleiben gleich). Für ein Intervall [a, b] mit Mitte m
    und Halbbreite ρ gilt, weil Σ |c_i λ_i^k| e^(λ_i x) konvex ist, |h^(k)(ξ)| ≤ max(S_k(a), S_k(b)) =: B_k:

    * |h(m)| > B_1 ρ  → keine Nullstelle in [a, b];
    * |h'(m)| > B_2 ρ → h streng monoton → höchstens eine Nullstelle, genau eine bei Vorzeichenwechsel.

    Unentschiedene Intervalle werden halbiert (bis 1e-10 in x). Was dann noch offen ist (doppelte bzw. extrem eng
    benachbarte Nullstellen, Rundungsrauschen), macht das Ergebnis „unsicher“. Nach der Descartes-Regel für
    Exponentialsummen gibt es höchstens so viele Lösungen wie Vorzeichenwechsel der Zahlungsreihe."""
    prep = _prep(flows)
    if prep is None:
        return RootSearch([], True, 0)
    ts, cfs = prep
    descartes = int(np.sum(np.sign(cfs[1:]) != np.sign(cfs[:-1])))
    if descartes == 0:
        return RootSearch([], True, 0)
    tmax = float(ts[-1])
    roots: list[float] = []
    certain = True
    x_lo, x_hi = math.log1p(R_MIN), math.log1p(R_MAX)
    for lam, lo, hi in ((tmax - ts, x_lo, 0.0), (-ts, 0.0, x_hi)):
        found, ok = _isolate(cfs, lam, lo, hi)
        roots += found
        certain = certain and ok
    roots = sorted({round(r, 12) for r in roots})
    merged: list[float] = []
    for r in roots:  # gemeinsamer Rand der Teilbereiche bzw. Intervalle
        if not merged or abs(r - merged[-1]) > 1e-9 * max(1.0, abs(r)):
            merged.append(r)
    return RootSearch(merged, certain and len(merged) <= descartes, descartes)


def _isolate(c: np.ndarray, lam: np.ndarray, lo: float, hi: float) -> tuple[list[float], bool]:
    """Nullstellen von h(x) = Σ c e^(λ x) auf [lo, hi] (r-Werte) und ob alles nachgewiesen ist."""
    def ev(x: np.ndarray) -> tuple[np.ndarray, ...]:
        with np.errstate(over="ignore", invalid="ignore", under="ignore"):
            e = np.exp(np.outer(x, lam))
            h = e @ c
            d1 = e @ (c * lam)
            s0 = e @ np.abs(c)
            s1 = e @ np.abs(c * lam)
            s2 = e @ np.abs(c * lam * lam)
        return h, d1, s0, s1, s2

    edges = np.linspace(lo, hi, 65)
    todo = [(float(x), float(y)) for x, y in itertools.pairwise(edges)]
    roots: list[float] = []
    certain = True
    seen = 0
    while todo:
        batch, todo = todo[:4096], todo[4096:]
        seen += len(batch)
        a = np.array([p[0] for p in batch])
        b = np.array([p[1] for p in batch])
        m = (a + b) / 2
        rho = (b - a) / 2
        ha, _da, _s0a, s1a, s2a = ev(a)
        hb, _db, _s0b, s1b, s2b = ev(b)
        hm, dm, s0m, _s1m, _s2m = ev(m)
        tol = 1e-12 * s0m * len(c)  # Rundungsfehler der Summe
        b1 = np.maximum(s1a, s1b)
        b2 = np.maximum(s2a, s2b)
        finite = np.isfinite(ha) & np.isfinite(hb) & np.isfinite(hm) & np.isfinite(dm) & np.isfinite(b1) & \
            np.isfinite(b2)
        zero_free = finite & (np.abs(hm) - tol > b1 * rho)
        monotone = finite & ~zero_free & (np.abs(dm) - tol * np.maximum(1.0, np.abs(dm)) > b2 * rho)
        for i in range(len(batch)):
            if zero_free[i]:
                continue
            if monotone[i]:
                if ha[i] == 0.0:
                    roots.append(math.expm1(float(a[i])))
                elif hb[i] == 0.0:
                    roots.append(math.expm1(float(b[i])))
                elif ha[i] * hb[i] < 0:
                    roots.append(math.expm1(_bisect(c, lam, float(a[i]), float(b[i]), float(ha[i]))))
                continue
            if b[i] - a[i] < _MIN_WIDTH or seen > _MAX_INTERVALS:
                certain = False
                if finite[i] and ha[i] * hb[i] < 0:
                    roots.append(math.expm1(float(m[i])))
                continue
            todo.append((float(a[i]), float(m[i])))
            todo.append((float(m[i]), float(b[i])))
    return roots, certain


def _bisect(c: np.ndarray, lam: np.ndarray, a: float, b: float, fa: float) -> float:
    for _ in range(80):
        m = (a + b) / 2
        with np.errstate(over="ignore", invalid="ignore", under="ignore"):
            fm = float(np.exp(lam * m) @ c)
        if fm == 0.0 or b - a < 1e-15:
            return m
        if (fa < 0) == (fm < 0):
            a, fa = m, fm
        else:
            b = m
    return (a + b) / 2


def xirr_roots(flows: Sequence[tuple[date, float]]) -> list[float]:
    """Alle Lösungen im Bereich −99,99 % … 10⁸ % p. a. (siehe :func:`xirr_search`)."""
    return xirr_search(flows).roots


def sign_changes(flows: Sequence[tuple[date, float]]) -> int:
    signs = [cf > 0 for _, cf in sorted(flows, key=lambda x: x[0]) if abs(cf) > 1e-9]
    return sum(1 for a, b in itertools.pairwise(signs) if a != b)


def xirr_detail(flows: Sequence[tuple[date, float]], guess: float = 0.1) -> tuple[float | None, bool]:
    """(Zinsfuß, nicht eindeutig). Bis zu einem Vorzeichenwechsel ist die Lösung eindeutig (Descartes) – dann
    Newton/Bisektion wie bisher. Sonst entscheidet :func:`xirr_search`: genau eine nachgewiesene Lösung → diese;
    mehrere oder nicht sicher trennbare → keine Zahl, sondern „nicht eindeutig bestimmbar“ (statt still eine vom
    Startwert abhängige Lösung zu zeigen)."""
    if sign_changes(flows) <= 1:
        r = xirr(flows, guess)
        return (r if r is not None and math.isfinite(r) else None), False
    res = xirr_search(flows)
    if not res.certain or len(res.roots) > 1:
        return None, True
    if res.roots:
        return res.roots[0], False
    return None, False


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
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            return float(np.sum(cfs / np.power(1.0 + r, ts)))

    def df(r: float) -> float:
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
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
    """IRR über (start, end]: −V_start, −Flüsse (Zufluss ins Depot = Auszahlung des Anlegers), +V_end.
    None auch bei mehrdeutiger Lösung (siehe :func:`irr_period_detail`)."""
    return irr_period_detail(dates, values, flows_by_day, start, end)[0]


def irr_period_detail(dates: Sequence[date], values: np.ndarray, flows_by_day: np.ndarray, start: int,
                      end: int) -> tuple[float | None, bool]:
    cfs: list[tuple[date, float]] = []
    if values[start] > 0:
        cfs.append((dates[start], -float(values[start])))
    for t in range(start + 1, end + 1):
        f = float(flows_by_day[t])
        if abs(f) > 1e-9:
            cfs.append((dates[t], -f))
    cfs.append((dates[end], float(values[end])))
    return xirr_detail(cfs)
