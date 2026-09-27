"""Kurs- und Devisenabfragen zu einem Stichtag (für Schätzungen und manuell erfasste Buchungen).

Nutzt ausschließlich bereits gespeicherte Kurse (Tagesschlusskurse, letzter Kurs, manuelle Kurse) – es wird
kein externer Dienst abgefragt.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.ledger.models import Portfolio
from app.util.timeutil import fmt_de_date

# älteste Schlusskurse, die ersatzweise für einen Tag ohne eigenen Kurs gelten (Wochenende, Feiertag)
MAX_STALE_DAYS = 5
# ohne Kursreihe (z. B. gerade angelegtes Asset): letzter Transaktionskurs höchstens so viele Tage zuvor
MAX_TX_PRICE_DAYS = 31


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def fx_to_eur(ctx: Any, amount: Decimal, ccy: str | None, d: date) -> tuple[Decimal, str] | None:
    """Betrag in Fremdwährung → EUR mit dem Devisenkurs ≤ d (Kurs = Einheiten Fremdwährung je 1 EUR)."""
    if not ccy or ccy.upper() == "EUR":
        return amount, "EUR"
    fx = ctx.store.fx_on_or_before(ccy, d)
    if not fx or not fx[0]:
        return None
    return amount / _dec(fx[0]), f"Devisenkurs {ccy.upper()} {fmt_de_date(fx[1])}"


def price_eur_on(ctx: Any, pf: Portfolio, asset_id: str, d: date, today: date) -> tuple[Decimal, str, bool] | None:
    """(Kurs in EUR, Quelle, endgültig) eines Assets am Tag d.

    Reihenfolge: Schlusskurs des Tages (endgültig) → am laufenden Tag bzw. Vortag der aktuelle Kurs →
    letzter Schlusskurs höchstens ``MAX_STALE_DAYS`` Tage zuvor → manueller Kurs ≤ d → letzter Transaktionskurs
    (Kauf, Verkauf, Tausch) höchstens ``MAX_TX_PRICE_DAYS`` Tage zuvor.
    """
    a = pf.asset(asset_id)
    if a.is_fiat:
        conv = fx_to_eur(ctx, Decimal(1), asset_id, d)
        return (conv[0], conv[1], True) if conv else None
    series = ctx.prices.series_for(a)
    if series:
        row = ctx.store.close_on_or_before(series, d)
        if row is not None and row["date"] == d.isoformat():
            conv = fx_to_eur(ctx, _dec(row["close"]), row["ccy"], d)
            if conv and conv[0]:
                return conv[0], f"Schlusskurs {fmt_de_date(d)}", True
        if d >= today - timedelta(days=1):
            q = ctx.store.latest(series)
            if q is not None and q["price"]:
                conv = fx_to_eur(ctx, _dec(q["price"]), q["ccy"], d)
                if conv and conv[0]:
                    return conv[0], "aktueller Kurs (vorläufig)", False
        if row is not None and (d - date.fromisoformat(row["date"])).days <= MAX_STALE_DAYS:
            conv = fx_to_eur(ctx, _dec(row["close"]), row["ccy"], d)
            if conv and conv[0]:
                return conv[0], f"Schlusskurs {fmt_de_date(row['date'])} (letzter verfügbarer)", False
    manual = [x for x in pf.manual_prices.get(asset_id, []) if x[0] <= d]
    if manual:
        md, mp = max(manual)
        return _dec(mp), f"manueller Kurs {fmt_de_date(md)}", False
    best: tuple[date, int, Decimal] | None = None
    for t in pf.txs:
        if t.type not in ("buy", "sell", "trade") or not t.value_eur or t.date > d \
                or (d - t.date).days > MAX_TX_PRICE_DAYS or t.flag == "estimated":
            continue
        qty = t.to_qty if t.to_asset == asset_id else (t.from_qty if t.from_asset == asset_id else None)
        if qty and (best is None or (t.date, t.seq) > best[:2]):
            best = (t.date, t.seq, t.value_eur / qty)
    if best is not None:
        return best[2], f"Transaktionskurs {fmt_de_date(best[0])} (ersatzweise)", False
    return None
