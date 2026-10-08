"""Ersatzkurse für Assets ohne Marktkurs – eine Regel für die aktuelle Bewertung und die Historie.

Kurspunkte
* manuelle Kurse aus dem Import (``manual_prices.csv``),
* Transaktionskurse: EUR-Wert / Menge aus Käufen, Verkäufen, Tauschen, Erträgen und bewerteten Zu- und
  Abgängen, je Tag mengengewichtet. Buchungen unter 1 € liefern keinen Kurs (Rundung, Staubbeträge).
  Am selben Tag hat ein manueller Kurs Vorrang.

Gültigkeit
* Zwischen zwei Kurspunkten gilt der letzte; Splits dazwischen werden auf die Stückbasis des Tages umgerechnet.
* Nach dem letzten Kurspunkt gilt er höchstens ``max_age`` Tage (Einstellung, Krypto 30, Wertpapiere 365,
  0 = unbegrenzt). Danach gilt die Position als unbewertet (0 €): Ein Transaktionskurs von vor Monaten oder
  Jahren ist bei illiquiden Token keine Bewertung – typischer Fall sind Token, die nicht mehr gehandelt werden.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np

from app.ledger.models import AssetInfo, Portfolio

MIN_TX_VALUE = Decimal(1)
DEFAULT_MAX_AGE = {"crypto": 30, "security": 365}
SETTING_KEYS = {"crypto": "prices.fallback_max_age_crypto_days",
                "security": "prices.fallback_max_age_security_days"}
KIND_LABEL = {"manual": "manueller Kurs", "tx": "Transaktionskurs"}


@dataclass(frozen=True, slots=True)
class Point:
    date: date
    price: float  # EUR je Einheit (Stückbasis des Tages)
    kind: str  # manual | tx


@dataclass(frozen=True, slots=True)
class Latest:
    point: Point | None = None  # gültiger Kurspunkt, auf die heutige Stückbasis umgerechnet
    expired: Point | None = None  # letzter Kurspunkt, der wegen seines Alters nicht mehr gilt
    max_age: int | None = None


def max_age(settings: Any, asset: AssetInfo) -> int | None:
    """Höchstalter eines Ersatzkurses in Tagen (None = unbegrenzt)."""
    cls = "crypto" if asset.is_crypto else "security"
    try:
        v = int(settings.get(SETTING_KEYS[cls], DEFAULT_MAX_AGE[cls])) if settings is not None \
            else DEFAULT_MAX_AGE[cls]
    except (TypeError, ValueError):
        v = DEFAULT_MAX_AGE[cls]
    return v if v > 0 else None


def tx_points(pf: Portfolio, only: Collection[str] | None = None) -> dict[str, list[tuple[date, float]]]:
    """Transaktionskurse je Asset und Tag (mengengewichtet), aufsteigend."""
    agg: dict[str, dict[date, list[Decimal]]] = defaultdict(dict)
    for t in pf.txs:
        v = t.value_eur
        if v is None or v < MIN_TX_VALUE or t.type in ("transfer", "corporate_action"):
            continue
        for asset, qty in ((t.to_asset, t.to_qty), (t.from_asset, t.from_qty)):
            if not asset or not qty or qty <= 0 or (only is not None and asset not in only):
                continue
            a = pf.assets.get(asset)
            if a is None or a.is_fiat:
                continue
            day = agg[asset].setdefault(t.date, [Decimal(0), Decimal(0)])
            day[0] += v
            day[1] += qty
    return {aid: [(d, float(v / q)) for d, (v, q) in sorted(days.items())] for aid, days in agg.items()}


def points(pf: Portfolio, asset_id: str, tx: Mapping[str, list[tuple[date, float]]]) -> list[Point]:
    by_day = {d: Point(d, p, "tx") for d, p in tx.get(asset_id, [])}
    for d, p in pf.manual_prices.get(asset_id, []):
        by_day[d] = Point(d, float(p), "manual")
    return [by_day[d] for d in sorted(by_day)]


def _split_level(splits: list[tuple[date, float]], d: date) -> float:
    """Kumuliertes Split-Verhältnis aller Splits bis einschließlich Tag d."""
    f = 1.0
    for sd, ratio in splits:
        if sd <= d:
            f *= ratio
    return f


def daily(pts: list[Point], n: int, start: date, splits: list[tuple[date, float]],
          age: int | None, backfill: bool = False) -> tuple[np.ndarray, int]:
    """Ersatzkurs je Tag des Rasters (NaN = kein gültiger Kurs) und Index des ersten Kurspunkts (n = keiner).

    ``backfill``: vor dem ersten Kurspunkt gilt dieser als Schätzung (sonst NaN)."""
    out, first, _manual = daily_detail(pts, n, start, splits, age, backfill)
    return out, first


def daily_detail(pts: list[Point], n: int, start: date, splits: list[tuple[date, float]],
                 age: int | None, backfill: bool = False) -> tuple[np.ndarray, int, np.ndarray]:
    """Wie :func:`daily`, zusätzlich je Tag, ob der geltende Kurspunkt ein manueller Kurs ist (sonst
    Transaktionskurs) – für die Herkunft in der Kursqualität."""
    arr = np.full(n, np.nan)
    manual = np.zeros(n, dtype=bool)
    if not pts or n <= 0:
        return arr, n, manual
    is_manual = np.zeros(n, dtype=bool)
    level = np.ones(n)
    base = 1.0
    for sd, ratio in splits:
        i = (sd - start).days
        if i <= 0:
            base *= ratio
        elif i < n:
            level[i:] *= ratio
    level *= base
    first = n
    before: tuple[float, bool] | None = None
    for p in pts:
        norm = p.price * _split_level(splits, p.date)  # auf Stückbasis „vor allen Splits“
        i = (p.date - start).days
        if i < 0:
            before = (norm, p.kind == "manual")
            continue
        if i >= n:
            continue
        arr[i] = norm
        is_manual[i] = p.kind == "manual"
        first = min(first, i)
    if before is not None:
        if np.isnan(arr[0]):
            arr[0], is_manual[0] = before
        first = 0
    if first >= n:
        return np.full(n, np.nan), n, manual
    valid = ~np.isnan(arr)
    idx = np.where(valid, np.arange(n), 0)
    np.maximum.accumulate(idx, out=idx)
    out = arr[idx] / level
    manual = is_manual[idx]
    out[:first] = arr[first] / level[:first] if backfill else np.nan
    manual[:first] = is_manual[first] if backfill else False
    if age is not None:
        cut = (pts[-1].date - start).days + age + 1
        if cut < n:
            out[max(cut, 0):] = np.nan
            manual[max(cut, 0):] = False
    return out, first, manual


class FallbackPrices:
    """Ersatzkurse eines Portfolios; Transaktionskurse werden bei Bedarf einmal berechnet."""

    def __init__(self, pf: Portfolio, settings: Any, only: Collection[str] | None = None) -> None:
        self.pf = pf
        self.settings = settings
        self.only = only
        self._tx: dict[str, list[tuple[date, float]]] | None = None
        self._splits: dict[str, list[tuple[date, float]]] | None = None

    @property
    def tx(self) -> dict[str, list[tuple[date, float]]]:
        if self._tx is None:
            self._tx = tx_points(self.pf, self.only)
        return self._tx

    @property
    def splits(self) -> dict[str, list[tuple[date, float]]]:
        if self._splits is None:
            self._splits = self.pf.split_events()
        return self._splits

    def max_age(self, asset: AssetInfo) -> int | None:
        return max_age(self.settings, asset)

    def points(self, asset_id: str) -> list[Point]:
        return points(self.pf, asset_id, self.tx)

    def latest(self, asset: AssetInfo, today: date) -> Latest:
        """Ersatzkurs für die aktuelle Bewertung (gleiche Regel wie ``daily``/``on``)."""
        pts = self.points(asset.asset_id)
        age = self.max_age(asset)
        if not pts:
            return Latest(max_age=age)
        past = [p for p in pts if p.date <= today]
        p = past[-1] if past else pts[0]  # nur künftige Kurse (Stichtag des Imports): frühester gilt
        splits = self.splits.get(asset.asset_id, [])
        cur = Point(p.date, p.price * _split_level(splits, p.date) / _split_level(splits, today), p.kind)
        if age is not None and len(past) == len(pts) and (today - p.date).days > age:
            return Latest(expired=cur, max_age=age)
        return Latest(point=cur, max_age=age)

    def on(self, asset: AssetInfo, d: date) -> float | None:
        """Ersatzkurs am Tag d wie in der Historie (vor dem ersten Kurspunkt: dieser als Schätzung)."""
        res = self.latest(asset, d)
        return res.point.price if res.point is not None else None

    def daily(self, asset: AssetInfo, n: int, start: date, expire: bool = True,
              backfill: bool = False) -> tuple[np.ndarray, int]:
        return daily(self.points(asset.asset_id), n, start, self.splits.get(asset.asset_id, []),
                     self.max_age(asset) if expire else None, backfill)

    def daily_detail(self, asset: AssetInfo, n: int, start: date, expire: bool = True,
                     backfill: bool = False) -> tuple[np.ndarray, int, np.ndarray]:
        """Wie :meth:`daily`, zusätzlich je Tag: manueller Kurs (True) oder Transaktionskurs (False)."""
        return daily_detail(self.points(asset.asset_id), n, start, self.splits.get(asset.asset_id, []),
                            self.max_age(asset) if expire else None, backfill)
