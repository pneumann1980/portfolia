"""Synthetische Kurse für den Demo-Modus (DEMO_MODE=true) – niemals im Normalbetrieb aktiv.

Deterministischer Random Walk je Serie, verankert an Transaktionskursen (value_eur/Menge), damit die
Oberfläche mit den Beispieldaten plausibel aussieht. Das UI zeigt im Demo-Modus einen deutlichen Hinweis.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import UTC, date, datetime, timedelta

from app.prices.models import Bar, IntradayBar, Quote


class DemoProvider:
    name = "demo"

    def __init__(self, anchors: dict[str, tuple[date, float]] | None = None, start: date = date(2015, 1, 1)) -> None:
        self.anchors = anchors or {}
        self.splits: dict[str, list[tuple[date, float]]] = {}
        self.start = start
        self._cache: dict[str, dict[date, Bar]] = {}

    def _rng(self, series: str) -> random.Random:
        seed = int(hashlib.sha256(series.encode()).hexdigest()[:12], 16)
        return random.Random(seed)

    def _path(self, series: str) -> dict[date, Bar]:
        if series in self._cache:
            return self._cache[series]
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
        out: dict[date, Bar] = {}
        splits = self.splits.get(series, [])
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
            out[d] = Bar(date=d, close=round(c, 6), open=round(o, 6), high=round(h, 6), low=round(lo, 6), volume=None,
                         split_factor=factor)
        self._cache[series] = out
        return out

    def history(self, series: str, start: date, end: date | None = None) -> list[Bar]:
        end = end or date.today()
        return [b for d, b in sorted(self._path(series).items()) if start <= d <= end]

    def quote(self, series: str) -> Quote | None:
        path = self._path(series)
        if not path:
            return None
        days = sorted(path)
        last = path[days[-1]]
        prev = path[days[-2]] if len(days) > 1 else last
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
