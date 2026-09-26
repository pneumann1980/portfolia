"""Synthetische Kurse für den Demo-Modus (DEMO_MODE=true) – niemals im Normalbetrieb aktiv.

Deterministischer Random Walk je Serie, verankert an Transaktionskursen (value_eur/Menge), damit die
Oberfläche mit den Beispieldaten plausibel aussieht. Das UI zeigt im Demo-Modus einen deutlichen Hinweis.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import numpy as np

from app.prices.models import Bar, IntradayBar, Quote

CACHE_SERIES = 48  # kompakte Pfade (≈ 150 KB je Serie über 10 Jahre); ältere werden bei Bedarf neu erzeugt


@dataclass(frozen=True)
class _Path:
    day: np.ndarray  # Tage seit Start (int32)
    close: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    factor: np.ndarray

    def __len__(self) -> int:
        return len(self.day)

    def bar(self, start: date, i: int) -> Bar:
        return Bar(date=start + timedelta(days=int(self.day[i])), close=float(self.close[i]), open=float(self.open[i]),
                   high=float(self.high[i]), low=float(self.low[i]), volume=None, split_factor=float(self.factor[i]))


class DemoProvider:
    name = "demo"

    def __init__(self, anchors: dict[str, tuple[date, float]] | None = None, start: date = date(2015, 1, 1)) -> None:
        self.anchors = anchors or {}
        self.splits: dict[str, list[tuple[date, float]]] = {}
        self.start = start
        self._cache: OrderedDict[str, _Path] = OrderedDict()

    def _rng(self, series: str) -> random.Random:
        seed = int(hashlib.sha256(series.encode()).hexdigest()[:12], 16)
        return random.Random(seed)

    def _path(self, series: str) -> _Path:
        hit = self._cache.get(series)
        if hit is not None:
            self._cache.move_to_end(series)
            return hit
        rng = self._rng(series)
        crypto = series.startswith("demo:cg:") or series.startswith("cg:")
        vol = 0.024 if crypto else 0.011
        drift = 0.00025 if crypto else 0.00022
        end = date.today()
        n = (end - self.start).days + 1
        logp = [0.0]
        for _ in range(n - 1):
            logp.append(logp[-1] + drift + vol * rng.gauss(0, 1))
        anchor = self.anchors.get(series)
        if anchor:
            ad, ap = anchor
            idx = max(0, min(n - 1, (ad - self.start).days))
            shift = math.log(max(ap, 1e-9)) - logp[idx]
        else:
            shift = math.log(10 + rng.random() * 200)
        splits = self.splits.get(series, [])
        days, closes, opens, highs, lows, factors = [], [], [], [], [], []
        for i, lp in enumerate(logp):
            d = self.start + timedelta(days=i)
            # Pfad ist auf heutiger Stückbasis; vor Splits wie gehandelt (× Verhältnis) ausgeben
            factor = 1.0
            for sd, ratio in splits:
                if sd > d:
                    factor *= ratio
            c = math.exp(lp + shift) * factor
            o = c * math.exp(vol * 0.3 * rng.gauss(0, 1))
            h = max(o, c) * (1 + abs(rng.gauss(0, vol * 0.5)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, vol * 0.5)))
            if not crypto and d.weekday() >= 5:
                continue
            days.append(i)
            closes.append(round(c, 6))
            opens.append(round(o, 6))
            highs.append(round(h, 6))
            lows.append(round(lo, 6))
            factors.append(factor)
        path = _Path(np.array(days, dtype=np.int32), np.array(closes), np.array(opens), np.array(highs),
                     np.array(lows), np.array(factors))
        self._cache[series] = path
        while len(self._cache) > CACHE_SERIES:
            self._cache.popitem(last=False)
        return path

    def history(self, series: str, start: date, end: date | None = None) -> list[Bar]:
        end = end or date.today()
        p = self._path(series)
        i0 = int(np.searchsorted(p.day, (start - self.start).days, side="left"))
        i1 = int(np.searchsorted(p.day, (end - self.start).days, side="right"))
        return [p.bar(self.start, i) for i in range(i0, i1)]

    def quote(self, series: str) -> Quote | None:
        path = self._path(series)
        if not len(path):
            return None
        last = path.bar(self.start, len(path) - 1)
        prev = path.bar(self.start, len(path) - 2) if len(path) > 1 else last
        # leichte Intraday-Bewegung
        rng = self._rng(series + datetime.now(UTC).strftime("%Y%m%d%H%M")[:-1])
        price = last.close * (1 + rng.gauss(0, 0.004))
        return Quote(series=series, price=price, ccy="EUR", market_time=datetime.now(UTC), source="demo",
                     prev_close=prev.close, change_pct=(price / prev.close - 1) * 100 if prev.close else None)

    def intraday(self, series: str, rng_key: str) -> list[IntradayBar]:
        q = self.quote(series)
        if q is None:
            return []
        rng = self._rng(series + rng_key)
        steps = 96 if rng_key == "1T" else 7 * 24
        step = timedelta(minutes=15) if rng_key == "1T" else timedelta(hours=1)
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        p = q.price
        pts = []
        for i in range(steps):
            pts.append(IntradayBar(ts=now - step * i, close=p))
            p = p / (1 + rng.gauss(0, 0.003))
        return list(reversed(pts))
