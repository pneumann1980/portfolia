"""Erkennung laufender Sparpläne aus den Import-Transaktionen (rein rechnerisch, ohne DB).

Ein Sparplan ist eine Folge von Käufen desselben Assets auf demselben Konto in festem Rhythmus
(wöchentlich, 14-täglich, 2× monatlich, monatlich, zweimonatlich, vierteljährlich) mit stabiler Sparrate.
Die Suche arbeitet rückwärts vom jüngsten Kauf und überspringt Einzelkäufe, die nicht in den Rhythmus
passen. Wochenend-Verschiebungen (z. B. Ausführung am Montag statt am Samstag) werden toleriert.

Status relativ zum Importstand (``valuation_date``): *active*, wenn keine planmäßige Ausführung vor dem
Stichtag fehlt; *paused* bei einer fehlenden, *ended* bei mehreren fehlenden Ausführungen.
"""

from __future__ import annotations

import calendar
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.ledger.models import Portfolio
from app.util.timeutil import local_tz

ZERO = Decimal(0)
FREQS: dict[str, str] = {
    "weekly": "wöchentlich",
    "biweekly": "alle 2 Wochen",
    "semimonthly": "2× monatlich",
    "monthly": "monatlich",
    "bimonthly": "alle 2 Monate",
    "quarterly": "vierteljährlich",
}
MONTH_STEP = {"monthly": 1, "bimonthly": 2, "quarterly": 3}
DAY_STEP = {"weekly": 7, "biweekly": 14}
GRACE_DAYS = {"weekly": 2, "biweekly": 3, "semimonthly": 3, "monthly": 5, "bimonthly": 5, "quarterly": 5}
FREQ_PRIORITY = ("monthly", "weekly", "semimonthly", "biweekly", "quarterly", "bimonthly")
WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MIN_RUN = 3
LOOKBACK_DAYS = 430


@dataclass
class Execution:
    tx_id: str
    local: datetime
    date: date
    amount: Decimal  # value_eur (Brutto-Gegenwert ohne Gebühr)
    fee: Decimal
    qty: Decimal
    funding_asset: str | None
    date_only: bool

    @property
    def total(self) -> Decimal:
        return self.amount + self.fee


@dataclass
class Plan:
    key: str
    account: str
    asset_id: str
    freq: str
    days: tuple[int, ...] = ()
    weekday: int | None = None
    time_local: str = "12:00"
    date_only: bool = False
    amount: Decimal = ZERO
    fee: Decimal = ZERO
    qty_decimals: int = 6
    funding_asset: str | None = "EUR"
    funding: str = "cash"
    weekend_shift: bool = True
    executions: list[Execution] = field(default_factory=list)
    confidence: str = "niedrig"
    status: str = "active"
    next_due: date | None = None
    missed: int = 0

    @property
    def first_date(self) -> date:
        return self.executions[0].date

    @property
    def last_date(self) -> date:
        return self.executions[-1].date

    @property
    def label(self) -> str:
        if self.freq in ("weekly", "biweekly") and self.weekday is not None:
            return f"{FREQS[self.freq]} ({WEEKDAYS[self.weekday]})"
        if self.days:
            return f"{FREQS[self.freq]} am " + " und ".join(f"{d}." for d in self.days)
        return FREQS.get(self.freq, self.freq)

    def assess(self, cutoff: date, last_seen: date | None = None) -> None:
        """Status zum Datenstand: nächster Termin, verpasste Termine (ab letzter Ausführung bzw. ``last_seen``)."""
        last = max(self.last_date, last_seen) if last_seen else self.last_date
        due = self.schedule(last, cutoff + timedelta(days=3660), limit=1)
        self.next_due = due[0] if due else None
        missed = self.schedule(last, cutoff - timedelta(days=GRACE_DAYS[self.freq]))
        self.missed = len(missed)
        self.status = "active" if not missed else ("paused" if len(missed) == 1 else "ended")

    def schedule(self, after: date, until: date, limit: int = 120) -> list[date]:
        """Planmäßige Termine > ``after`` und ≤ ``until`` (mit Wochenend-Verschiebung)."""
        return schedule(self.freq, self.days, self.weekday, self.last_date, self.weekend_shift, after, until, limit)


# ----------------------------------------------------------------------------------------------------
# Kalenderhilfen
# ----------------------------------------------------------------------------------------------------

def add_months(d: date, months: int, day: int | None = None) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day or d.day, last))


def shift_weekend(d: date, enabled: bool) -> date:
    if enabled and d.weekday() >= 5:
        return d + timedelta(days=7 - d.weekday())
    return d


def schedule(freq: str, days: tuple[int, ...], weekday: int | None, last: date, weekend_shift: bool, after: date,
             until: date, limit: int = 120) -> list[date]:
    """Planmäßige Termine nach der letzten Ausführung ``last``: > ``after`` und ≤ ``until``."""
    out: list[date] = []
    if freq in DAY_STEP:
        step = DAY_STEP[freq]
        base = last
        if weekday is not None:  # verschobene Ausführung (z. B. Feiertag) auf den üblichen Wochentag zurückführen
            back = (last.weekday() - weekday) % 7
            if back <= 3:
                base = last - timedelta(days=back)
        n = 0
        while len(out) < limit and n < 2000:
            n += 1
            due = shift_weekend(base + timedelta(days=n * step), weekend_shift)
            if due > until:
                break
            if due > after and due > last:
                out.append(due)
        return out
    months = MONTH_STEP.get(freq, 1)
    first_of_month = date(last.year, last.month, 1)
    n = 0
    while len(out) < limit and n < 600:
        offset = n if freq == "semimonthly" else (n + 1) * months
        n += 1
        month = add_months(first_of_month, offset, 1)
        for day in sorted(days or (last.day,)):
            due = shift_weekend(add_months(month, 0, day), weekend_shift)
            if due > until:
                return out
            if due > last and due > after:
                out.append(due)
    return out


# ----------------------------------------------------------------------------------------------------
# Erkennung
# ----------------------------------------------------------------------------------------------------

def buy_series(pf: Portfolio, since: date | None = None) -> dict[tuple[str, str], list[Execution]]:
    """Käufe gegen Fiat (oder ohne Abgangsbein) je Konto und Asset, nur Importdaten."""
    tz = local_tz()
    out: dict[tuple[str, str], list[Execution]] = defaultdict(list)
    for t in pf.txs:
        if t.flag == "estimated" or t.type != "buy":  # freigegebene Ausführungen zählen als erfasst
            continue
        if not t.to_asset or not t.to_account or not t.to_qty or t.to_qty <= 0:
            continue
        if pf.asset(t.to_asset).is_fiat:
            continue
        if t.from_asset and not pf.asset(t.from_asset).is_fiat:
            continue
        if not t.value_eur or t.value_eur <= 0:
            continue
        if since is not None and t.date < since:
            continue
        fee = ZERO
        if t.fee_eur and (not t.fee_asset or pf.asset(t.fee_asset).is_fiat):
            fee = t.fee_eur
        out[(t.to_account, t.to_asset)].append(Execution(
            t.tx_id, t.ts.astimezone(tz), t.date, t.value_eur, fee, t.to_qty, t.from_asset, t.date_only))
    for v in out.values():
        v.sort(key=lambda e: (e.local, e.tx_id))
    return out


def _window(d: date, freq: str) -> tuple[date, date, date]:
    """(frühestes, spätestes, erwartetes) Datum der vorherigen Ausführung."""
    if freq in DAY_STEP:
        step = DAY_STEP[freq]
        tol = 2 if freq == "weekly" else 3
        exp = d - timedelta(days=step)
        return exp - timedelta(days=tol), exp + timedelta(days=tol), exp
    if freq == "semimonthly":
        return d - timedelta(days=19), d - timedelta(days=12), d - timedelta(days=15)
    exp = add_months(d, -MONTH_STEP[freq])
    return exp - timedelta(days=5), exp + timedelta(days=5), exp


def _run(execs: list[Execution], anchor: int, freq: str) -> list[Execution]:
    run = [execs[anchor]]
    cur = execs[anchor]
    idx = anchor
    while True:
        lo, hi, exp = _window(cur.date, freq)
        best: tuple[tuple[int, Decimal], int] | None = None
        for j in range(idx - 1, -1, -1):
            e = execs[j]
            if e.date >= cur.date:
                continue
            if e.date < lo:
                break
            if e.date <= hi:
                score = (abs((e.date - exp).days), abs(e.total - cur.total))
                if best is None or score < best[0]:
                    best = (score, j)
        if best is None:
            return run
        idx = best[1]
        cur = execs[idx]
        run.insert(0, cur)


def _mode(values: list[int]) -> int:
    c = Counter(values)
    top = max(c.values())
    return min(v for v, n in c.items() if n == top)


def _qty_decimals(run: list[Execution]) -> int:
    dec = 0
    for e in run:
        exp = e.qty.normalize().as_tuple().exponent
        if isinstance(exp, int) and exp < 0:
            dec = max(dec, -exp)
    return min(dec, 8)


def _deviation(values: list[Decimal]) -> Decimal:
    med = Decimal(str(statistics.median(values)))
    if med <= 0:
        return Decimal(1)
    return max(abs(v - med) / med for v in values)


def _external_funding(pf: Portfolio, account: str, run: list[Execution]) -> bool:
    """Ausführungen, denen eine passende Fiat-Einzahlung vorausgeht (Lastschrift) → externe Finanzierung."""
    deposits = [t for t in pf.txs if t.type == "deposit" and t.to_account == account and t.to_asset
                and pf.asset(t.to_asset).is_fiat and not t.tag and t.to_qty]
    if not deposits:
        return False
    hits = 0
    for e in run:
        for t in deposits:
            if abs((t.date - e.date).days) <= 3 and abs(t.to_qty - e.total) <= e.total * Decimal("0.05"):
                hits += 1
                break
    return hits * 2 >= len(run)


def _best_run(execs: list[Execution]) -> tuple[str, list[Execution]] | None:
    best: tuple[tuple[int, int, int], str, list[Execution]] | None = None
    for rank, anchor in enumerate(range(len(execs) - 1, max(-1, len(execs) - 5), -1)):
        for prio, freq in enumerate(FREQ_PRIORITY):
            run = _run(execs, anchor, freq)
            if len(run) < MIN_RUN:
                continue
            key = (len(run), -rank, -prio)
            if best is None or key > best[0]:
                best = (key, freq, run)
    if best is None:
        return None
    return best[1], best[2]


def detect_plans(pf: Portfolio, cutoff: date, lookback_days: int = LOOKBACK_DAYS) -> list[Plan]:
    plans: list[Plan] = []
    series = buy_series(pf, since=cutoff - timedelta(days=lookback_days))
    for (account, asset_id), execs in sorted(series.items()):
        if len(execs) < MIN_RUN:
            continue
        found = _best_run(execs)
        if found is None:
            continue
        freq, run = found
        freq, days, weekday = _classify(freq, run)
        tail = run[-6:]
        dev = _deviation([e.total for e in tail[-3:]])
        n = len(run)
        step = False
        if dev > Decimal("0.25"):
            # Sparrate geändert? Zwei gleiche jüngste Ausführungen nach einer vorher stabilen Folge
            before = run[:-2]
            if (len(before) >= 2 and _deviation([e.total for e in run[-2:]]) <= Decimal("0.02")
                    and _deviation([e.total for e in before[-4:]]) <= Decimal("0.10")):
                step = True
            else:
                continue  # keine stabile Sparrate (eher manuelle Käufe)
        if step:
            confidence = "mittel" if n >= 4 else "niedrig"
        elif n >= 6 and _deviation([e.total for e in tail]) <= Decimal("0.02"):
            confidence = "hoch"
        elif n >= 4 and dev <= Decimal("0.10"):
            confidence = "mittel"
        else:
            confidence = "niedrig"
        last2 = run[-2:]
        if len(last2) == 2 and _deviation([e.total for e in last2]) <= Decimal("0.02"):
            amount, fee = run[-1].amount, run[-1].fee
        else:
            last3 = run[-3:]
            amount = Decimal(str(statistics.median([e.amount for e in last3])))
            fee = Decimal(str(statistics.median([e.fee for e in last3])))
        timed = [e.local.hour * 60 + e.local.minute for e in run if not e.date_only]
        minutes = int(statistics.median(timed)) if timed else 12 * 60
        funding_asset = Counter(e.funding_asset for e in run).most_common(1)[0][0]
        plan = Plan(
            key=f"{account}|{asset_id}", account=account, asset_id=asset_id, freq=freq, days=days, weekday=weekday,
            time_local=f"{minutes // 60:02d}:{minutes % 60:02d}", date_only=not timed, amount=amount, fee=fee,
            qty_decimals=_qty_decimals(run), funding_asset=funding_asset,
            funding="external" if (funding_asset is None or _external_funding(pf, account, run)) else "cash",
            weekend_shift=not any(e.date.weekday() >= 5 for e in run), executions=run, confidence=confidence,
        )
        plan.assess(cutoff)
        plans.append(plan)
    return plans


def _classify(freq: str, run: list[Execution]) -> tuple[str, tuple[int, ...], int | None]:
    """Ausführungstage/Wochentag bestimmen; 14-tägliche Folgen mit festen Monatstagen gelten als 2× monatlich."""
    doms = [e.date.day for e in run]
    if freq in ("weekly", "biweekly"):
        return freq, (), _mode([e.date.weekday() for e in run])
    if freq == "semimonthly":
        ordered = sorted(doms)
        gaps = [(ordered[i + 1] - ordered[i], i) for i in range(len(ordered) - 1)]
        if gaps:
            _, split = max(gaps)
            a, b = ordered[:split + 1], ordered[split + 1:]
            months = {(e.date.year, e.date.month) for e in run}
            if a and b and max(a) - min(a) <= 5 and max(b) - min(b) <= 5 and len(months) * 2 >= len(run):
                return "semimonthly", (_mode(a), _mode(b)), None
        return "biweekly", (), _mode([e.date.weekday() for e in run])
    return freq, (_mode(doms),), None
