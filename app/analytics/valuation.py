"""Aktuelle Bewertung: Positionen je Asset (optional je Konto) mit Kursen, Einstand, G/V und Gewichten.

„Heute“ ist der Kalendertag (Europe/Berlin): Bezugskurs je Position ist ``PriceInfo.day_ref_eur`` (Wertpapiere:
Vortagesschluss, wenn heute gehandelt wurde, sonst ±0; Krypto: Kurs vor Mitternacht bzw. 24 h). Heutige Käufe und
Verkäufe zählen ab ihrem Kurs (nicht mit der ganzen Tagesbewegung), der Prozentwert der Summe bezieht sich auf den
Wert um 0:00 Uhr. Zusätzlich: Wertpapiere ohne Handel heute mit ihrer Bewegung am letzten Handelstag
(:attr:`Valuation.session`).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
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
    session_change: float | None = None  # Wertpapier ohne Handel heute: Bewegung am letzten Handelstag
    session_change_pct: float | None = None

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
    session: dict[str, Any] | None = None  # Wertpapiere ohne Handel heute: Bewegung am letzten Handelstag
    day_basis_24h: bool = False  # mindestens ein Krypto-Kurs ohne Wert von Mitternacht (Veränderung über 24 h)

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


def _today_flows(pf: Portfolio, accounts: frozenset[str] | None, today: date) -> dict[str, list[tuple[float,
                                                                                                    float | None]]]:
    """Heutige Zu- und Abgänge je Asset: (Menge mit Vorzeichen, EUR-Wert oder ``None``) – Kauf, Verkauf, Gebühr,
    Ein-/Auszahlung, Transfer (zwischen zwei eigenen Konten im Umfang heben sich beide Seiten auf)."""
    out: dict[str, list[tuple[float, float | None]]] = defaultdict(list)

    def inside(acc: str | None) -> bool:
        return acc is not None and (accounts is None or acc in accounts)

    for t in pf.txs:
        if t.date != today:
            continue
        v = float(t.value_eur) if t.value_eur is not None else None
        if t.to_asset and t.to_qty and inside(t.to_account):
            out[t.to_asset].append((float(t.to_qty), v))
        if t.from_asset and t.from_qty and inside(t.from_account):
            out[t.from_asset].append((-float(t.from_qty), v))
        if t.fee_asset and t.fee_qty and inside(t.from_account or t.to_account):
            out[t.fee_asset].append((-float(t.fee_qty), float(t.fee_eur) if t.fee_eur is not None else None))
    return out


def _day(qty: float, now: float, ref: float, flows: list[tuple[float, float | None]]) -> tuple[float, float]:
    """(Veränderung heute, Wert um 0:00 Uhr) einer Position: Wert jetzt − Wert um 0:00 − Saldo der heutigen Zu- und
    Abgänge (zu ihrem EUR-Wert; ohne Wert zum Bezugskurs, d. h. ohne eigenen Beitrag)."""
    net_qty = sum(q for q, _v in flows)
    flow_value = sum((v if v is not None else abs(q) * ref) * (1 if q > 0 else -1) for q, v in flows)
    qty0 = qty - net_qty
    return qty * now - qty0 * ref - flow_value, qty0 * ref


def _session(positions: list[Position]) -> dict[str, Any] | None:
    """Wertpapiere ohne Handel heute (Wochenende, Feiertag, vor Börsenbeginn): Bewegung am letzten Handelstag."""
    rows = [p for p in positions if p.session_change is not None]
    if not rows:
        return None
    base = sum(p.value - (p.session_change or 0.0) for p in rows)
    change = sum(p.session_change or 0.0 for p in rows)
    dates = {p.price.session_date for p in rows if p.price.session_date is not None}
    return {"change": change, "pct": change / base if base else None, "n": len(rows),
            "date": max(dates) if dates else None, "same_date": len(dates) == 1}


def value_positions(pf: Portfolio, ledger: LedgerResult, prices: dict[str, PriceInfo], invested: float,
                    accounts: frozenset[str] | None = None, last_update: datetime | None = None,
                    label: str | None = None, today: date | None = None) -> Valuation:
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

    from app.util.timeutil import today_local

    flows = _today_flows(pf, accounts, today or today_local())
    ref_total = 0.0  # Wert um 0:00 Uhr (Bezug des Prozentwerts)
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
        if a.is_fiat:  # Bargeld: keine Kursbewegung; heutige Ein-/Auszahlungen gehören nicht zum Wert um 0:00
            ref_total += (qty - sum(q for q, _v in flows.get(aid, []))) * pi.price_eur
        elif pi.valued and pi.day_ref_eur:
            day, base = _day(qty, pi.price_eur, pi.day_ref_eur, flows.get(aid, []))
            ref_total += base
            day_pct = pi.price_eur / pi.day_ref_eur - 1
        else:
            ref_total += value
        sess = sess_pct = None
        if pi.kind == "quote" and not a.is_crypto and not a.is_fiat and not pi.traded_today and pi.prev_close_eur:
            sess = qty * (pi.price_eur - pi.prev_close_eur)
            sess_pct = pi.price_eur / pi.prev_close_eur - 1
        positions.append(Position(a, qty, pi, value, cost, unreal if pi.valued else 0.0, unreal_pct, day, day_pct,
                                  realized.get(aid, 0.0), income.get(aid, 0.0),
                                  accounts=sorted(accounts_by_asset.get(aid, [])), session_change=sess,
                                  session_change_pct=sess_pct))
    # geschlossene Positionen mit realisiertem G/V/Erträgen nicht als Position, aber in Summen
    total_value = sum(p.value for p in positions)
    for p in positions:
        p.weight = p.value / total_value if total_value else 0.0
    positions.sort(key=lambda p: p.value, reverse=True)
    day_change = sum(p.day_change or 0.0 for p in positions)
    fees = sum(float(f.eur) for f in ledger.fees if accounts is None or f.account in accounts)
    return Valuation(
        positions=positions,
        total_value=total_value,
        total_cost=sum(p.cost for p in positions),
        unrealized=sum(p.unrealized for p in positions if not p.asset.is_fiat),
        day_change=day_change,
        day_change_pct=(day_change / ref_total) if ref_total > 0 else None,
        realized=sum(realized.values()),
        income=sum(income.values()),
        fees=fees,
        invested=invested,
        last_update=last_update,
        account=label,
        session=_session(positions),
        day_basis_24h=any(p.price.day_basis == "24h" and p.day_change is not None for p in positions),
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


