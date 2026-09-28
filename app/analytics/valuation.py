"""Aktuelle Bewertung: Positionen je Asset (optional je Konto) mit Kursen, Einstand, G/V und Gewichten."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.analytics.colors import asset_colors
from app.ledger.engine import DUST, REALIZED_KINDS, Flow, LedgerResult
from app.ledger.models import AssetInfo, Portfolio
from app.prices.fallback import FallbackPrices
from app.prices.models import PriceInfo
from app.prices.store import PriceStore


@dataclass
class Position:
    asset: AssetInfo
    qty: float
    price: PriceInfo
    value: float
    cost: float
    unrealized: float
    unrealized_pct: float | None
    day_change: float | None
    day_change_pct: float | None
    realized: float = 0.0
    income: float = 0.0
    weight: float = 0.0
    accounts: list[str] = field(default_factory=list)

    @property
    def asset_id(self) -> str:
        return self.asset.asset_id

    @property
    def segment(self) -> str:
        return self.asset.segment

    @property
    def category(self) -> str:
        return self.asset.category_label

    @property
    def colors(self) -> dict[str, str]:
        return asset_colors(self.asset_id, self.segment)

    @property
    def avg_cost(self) -> float | None:
        return self.cost / self.qty if self.qty else None


@dataclass
class Valuation:
    positions: list[Position]
    total_value: float
    total_cost: float
    unrealized: float
    day_change: float
    day_change_pct: float | None
    realized: float
    income: float
    fees: float
    invested: float
    last_update: datetime | None
    account: str | None = None

    @property
    def total_gain(self) -> float:
        return self.total_value - self.invested

    @property
    def unvalued(self) -> list[Position]:
        return [p for p in self.positions if not p.price.valued]

    @property
    def stale(self) -> list[Position]:
        return [p for p in self.positions if p.price.valued and p.price.stale]

    def by_id(self) -> dict[str, Position]:
        return {p.asset_id: p for p in self.positions}


class FlowValuer:
    """Bewertet Zahlungsströme ohne EUR-Betrag (z. B. USD-Einzahlung) mit dem Kurs am Flussdatum.

    Assets ohne Kursquelle: Ersatzkurs nach derselben Regel wie Historie und aktuelle Bewertung – sonst
    entstünde bei Token-Zugängen ohne Wert ein Scheingewinn (Wert > 0, Zufluss 0 €)."""

    def __init__(self, store: PriceStore, series_for, settings: Any = None) -> None:
        self.store = store
        self.series_for = series_for
        self.settings = settings
        self._fb: tuple[Portfolio, FallbackPrices] | None = None

    def _fallback(self, pf: Portfolio) -> FallbackPrices:
        cur = self._fb
        if cur is None or cur[0] is not pf:
            cur = (pf, FallbackPrices(pf, self.settings))
            self._fb = cur
        return cur[1]

    def amount(self, f: Flow, pf: Portfolio) -> float:
        if f.amount is not None:
            return float(f.amount)
        if not f.asset or not f.qty:
            return 0.0
        a = pf.asset(f.asset)
        q = float(f.qty)
        if a.is_fiat:
            fx = self.store.fx_on_or_before(a.asset_id, f.date)
            return q / fx[0] if fx and fx[0] else 0.0
        s = self.series_for(a)
        if s:
            row = self.store.close_on_or_before(s, f.date)
            if row is not None:
                rate = 1.0
                if row["ccy"] and row["ccy"].upper() != "EUR":
                    fx = self.store.fx_on_or_before(row["ccy"], f.date)
                    rate = 1.0 / fx[0] if fx and fx[0] else 0.0
                return q * row["close"] * rate
            return 0.0
        p = self._fallback(pf).on(a, f.date)
        return q * p if p is not None else 0.0


def value_positions(pf: Portfolio, ledger: LedgerResult, prices: dict[str, PriceInfo], invested: float,
                    accounts: frozenset[str] | None = None, last_update: datetime | None = None,
                    label: str | None = None) -> Valuation:
    """Bewertung aller Positionen; ``accounts`` schränkt auf Konten (z. B. ein Depot) ein."""
    holdings: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    accounts_by_asset: dict[str, list[str]] = defaultdict(list)
    for (acc, asset), q in ledger.balances.items():
        if accounts is not None and acc not in accounts:
            continue
        if abs(q) > DUST:
            holdings[asset] += q
            accounts_by_asset[asset].append(acc)
    cost_by_asset: dict[str, float] = defaultdict(float)
    for lot in ledger.lots:
        if accounts is None or lot.account in accounts:
            cost_by_asset[lot.asset] += float(lot.cost)
    realized: dict[str, float] = defaultdict(float)
    for d in ledger.disposals:
        if d.kind in REALIZED_KINDS and (accounts is None or d.account in accounts):
            realized[d.asset] += float(d.gain)
    income: dict[str, float] = defaultdict(float)
    for e in ledger.income:
        if accounts is None or e.account in accounts:
            key = e.related_asset if (e.fiat and e.related_asset) else e.asset
            income[key] += float(e.value_eur)

    positions: list[Position] = []
    for aid, qd in holdings.items():
        if abs(qd) <= DUST:
            continue
        a = pf.asset(aid)
        qty = float(qd)
        pi = prices.get(aid) or PriceInfo(0.0, None, None, None, "none", "unvalued", False, note="kein Kurs")
        value = qty * pi.price_eur
        cost = value if a.is_fiat else cost_by_asset.get(aid, 0.0)
        unreal = 0.0 if a.is_fiat else value - cost
        unreal_pct = (unreal / cost) if (cost and not a.is_fiat and pi.valued) else None
        day = None
        day_pct = None
        if pi.valued and pi.prev_close_eur and not a.is_fiat:
            day = qty * (pi.price_eur - pi.prev_close_eur)
            day_pct = pi.price_eur / pi.prev_close_eur - 1 if pi.prev_close_eur else None
        positions.append(Position(a, qty, pi, value, cost, unreal if pi.valued else 0.0, unreal_pct, day, day_pct,
                                  realized.get(aid, 0.0), income.get(aid, 0.0),
                                  accounts=sorted(accounts_by_asset.get(aid, []))))
    # geschlossene Positionen mit realisiertem G/V/Erträgen nicht als Position, aber in Summen
    total_value = sum(p.value for p in positions)
    for p in positions:
        p.weight = p.value / total_value if total_value else 0.0
    positions.sort(key=lambda p: p.value, reverse=True)
    day_change = sum(p.day_change or 0.0 for p in positions)
    prev_total = total_value - day_change
    fees = sum(float(f.eur) for f in ledger.fees if accounts is None or f.account in accounts)
    return Valuation(
        positions=positions,
        total_value=total_value,
        total_cost=sum(p.cost for p in positions),
        unrealized=sum(p.unrealized for p in positions if not p.asset.is_fiat),
        day_change=day_change,
        day_change_pct=(day_change / prev_total) if prev_total else None,
        realized=sum(realized.values()),
        income=sum(income.values()),
        fees=fees,
        invested=invested,
        last_update=last_update,
        account=label,
    )


def net_invested(ledger: LedgerResult, pf: Portfolio, valuer: FlowValuer, until: date | None = None,
                 accounts: frozenset[str] | None = None) -> float:
    total = 0.0
    for f in ledger.flows:
        if until and f.date > until:
            continue
        if accounts is not None and f.account not in accounts:
            continue
        total += valuer.amount(f, pf)
    return total


def latest_update_time(prices: dict[str, PriceInfo]) -> datetime | None:
    ts = [p.ts for p in prices.values() if p.ts and p.kind == "quote"]
    return max(ts) if ts else None


def utc_now() -> datetime:
    return datetime.now(UTC)
