"""Zeiträume und Kennzahlen (TTWROR, IRR, absoluter G/V, Drawdown) auf Basis der Tagesreihen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

from app.analytics.history import History
from app.analytics.performance import annualize, cumulative_index, drawdown, irr_for_period, ttwror
from app.util.timeutil import add_years

PERIODS = ("1M", "3M", "6M", "YTD", "1J", "3J", "5J", "MAX")
PERIOD_LABELS = {"1M": "1 Monat", "3M": "3 Monate", "6M": "6 Monate", "YTD": "Seit Jahresbeginn", "1J": "1 Jahr",
                 "3J": "3 Jahre", "5J": "5 Jahre", "MAX": "Gesamt"}


def _minus_months(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30,
                      31, 30, 31][m - 1])
    return date(y, m, day)


def base_date(key: str, end: date) -> date | None:
    """Basisdatum (Bewertung zum Tagesende) für einen Zeitraum; None = seit Beginn."""
    key = key.upper()
    if key == "1M":
        return _minus_months(end, 1)
    if key == "3M":
        return _minus_months(end, 3)
    if key == "6M":
        return _minus_months(end, 6)
    if key == "YTD":
        return date(end.year - 1, 12, 31)
    if key in ("1J", "1Y"):
        return add_years(end, -1)
    if key in ("3J", "3Y"):
        return add_years(end, -3)
    if key in ("5J", "5Y"):
        return add_years(end, -5)
    return None


@dataclass
class Series:
    values: np.ndarray
    inflow: np.ndarray
    outflow: np.ndarray


def total_series(h: History) -> Series:
    return Series(h.value, h.inflow, h.outflow)


def group_series(h: History, asset_ids: list[str]) -> Series:
    v, i, o = h.group(asset_ids)
    return Series(v, i, o)


def bounds(h: History, key: str | None = None, start: date | None = None,
           end: date | None = None) -> tuple[int, int, bool]:
    """(Basisindex, Endindex, ab_Beginn). ab_Beginn=True: vor dem ersten Tag liegt ein virtueller Nulltag."""
    end_d = end or h.dates[-1]
    end_i = h.index_of(end_d)
    if start is None and key:
        start = base_date(key, h.dates[end_i])
    if start is None or start < h.start:
        return 0, end_i, True
    return h.index_of(start), end_i, False


def metrics(h: History, s: Series, start_i: int, end_i: int, from_inception: bool) -> dict[str, Any]:
    v, i, o = s.values, s.inflow, s.outflow
    if from_inception:
        v = np.concatenate([[0.0], v])
        i = np.concatenate([[0.0], i])
        o = np.concatenate([[0.0], o])
        dates = [h.start - timedelta(days=1), *h.dates]
        a, b = 0, end_i + 1
    else:
        dates = h.dates
        a, b = start_i, end_i
    if b <= a:
        return {"ttwror": None, "ttwror_pa": None, "irr": None, "gain": None, "days": 0}
    tw = ttwror(v, i, o, a, b)
    days = (dates[b] - dates[a]).days
    net = float(np.sum(i[a + 1:b + 1]) - np.sum(o[a + 1:b + 1]))
    gain = float(v[b] - v[a] - net)
    irr = irr_for_period(dates, v, i - o, a, b)
    idx = cumulative_index(v[a:b + 1], i[a:b + 1], o[a:b + 1])
    dd = drawdown(idx)
    return {
        "ttwror": tw,
        "ttwror_pa": annualize(tw, days),
        "irr": irr,
        "gain": gain,
        "start_value": float(v[a]),
        "end_value": float(v[b]),
        "net_flows": net,
        "inflows": float(np.sum(i[a + 1:b + 1])),
        "outflows": float(np.sum(o[a + 1:b + 1])),
        "days": days,
        "max_drawdown": float(dd.min()) if len(dd) else None,
        "start_date": dates[a],
        "end_date": dates[b],
    }


def index_series(h: History, s: Series, start_i: int, end_i: int,
                 from_inception: bool) -> tuple[list[date], np.ndarray]:
    """Kumulierter TTWROR-Index (Basis 0 %) für Charts."""
    if from_inception:
        v = np.concatenate([[0.0], s.values[: end_i + 1]])
        i = np.concatenate([[0.0], s.inflow[: end_i + 1]])
        o = np.concatenate([[0.0], s.outflow[: end_i + 1]])
        idx = cumulative_index(v, i, o, base=1.0)[1:]
        return h.dates[: end_i + 1], idx - 1.0
    idx = cumulative_index(s.values[start_i:end_i + 1], s.inflow[start_i:end_i + 1], s.outflow[start_i:end_i + 1],
                           base=1.0)
    return h.dates[start_i:end_i + 1], idx - 1.0


def annual_returns(h: History, s: Series) -> list[dict[str, Any]]:
    out = []
    first_year = h.start.year
    last_year = h.dates[-1].year
    for y in range(first_year, last_year + 1):
        start = date(y - 1, 12, 31)
        end = min(date(y, 12, 31), h.dates[-1])
        a, b, inc = bounds(h, start=start, end=end)
        m = metrics(h, s, a, b, inc)
        out.append({"year": y, "ttwror": m["ttwror"], "gain": m["gain"], "partial": end < date(y, 12, 31)
                    or (inc and h.start > date(y, 1, 1))})
    return out
