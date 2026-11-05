"""„Fiat überall aus“ (0.97.0): Wirkung vorab zeigen, Schalter setzen/zurücknehmen.

Nur Assets zählen; EUR/USD-Guthaben werden nicht geführt. Käufe sind dann Zuflüsse, Verkäufe Abflüsse. EUR-Werte von
Käufen, Verkäufen und Gebühren (Anschaffungskosten, Erlöse) bleiben unverändert – die Steuer ändert sich dadurch nicht.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import date
from decimal import Decimal
from typing import Any

from app.util.timeutil import today_local

log = logging.getLogger(__name__)

SETTING = "ledger.fiat_off"


def is_off(ctx: Any) -> bool:
    return bool(ctx.settings.get(SETTING, False))


def preview(ctx: Any) -> dict[str, Any]:
    """Beide Rechenweisen nebeneinander (rein lesend): eingesetztes Kapital je Jahresende, entfallende Fiat-Bestände,
    Zahl der Fiat-Buchungen ohne Wirkung und der Prüfsummenvergleich aller Asset-Bestände."""
    from app.analytics.valuation import net_invested

    pf = ctx.portfolio()
    if pf is None:
        return {"has_data": False}
    cur = ctx.engine_options()
    on = ctx.ledger(opts=dataclasses.replace(cur, fiat_off=False))
    off = ctx.ledger(opts=dataclasses.replace(cur, fiat_off=True))
    if on is None or off is None:
        return {"has_data": False}
    today = today_local()
    years = list(range(on.first_date.year if on.first_date else today.year, today.year + 1))
    rows = []
    for y in years:
        d = min(date(y, 12, 31), today)
        a = net_invested(on, pf, ctx.valuer, until=d)
        b = net_invested(off, pf, ctx.valuer, until=d)
        label = f"31.12.{y}" if d == date(y, 12, 31) else f"heute ({d:%d.%m.%Y})"
        rows.append({"label": label, "with_fiat": a, "without_fiat": b, "diff": b - a})
    fiat_pos = sorted(((acc, a, q) for (acc, a), q in on.balances.items() if pf.asset(a).is_fiat and q),
                      key=lambda x: (x[0], x[1]))
    # Asset-Bestände (nicht Fiat) müssen in beiden Weisen identisch sein; ebenso Lots und realisierte Ergebnisse
    def assets_only(led: Any) -> dict[tuple[str, str], Decimal]:
        return {k: v for k, v in led.balances.items() if not pf.asset(k[1]).is_fiat}

    def lots(led: Any) -> list[tuple[Any, ...]]:
        return sorted((x.asset, x.account, str(x.qty), str(x.cost), x.acq_date.isoformat(), x.acq_tx) for x in led.lots)

    def disp(led: Any) -> list[tuple[Any, ...]]:
        return sorted((d.tx_id, d.kind, str(d.proceeds), str(d.cost)) for d in led.disposals)

    checks = [
        ("Bestände aller Assets (ohne Fiat)", assets_only(on) == assets_only(off)),
        ("offene Lots (Menge, Anschaffungskosten, Anschaffungsdatum)", lots(on) == lots(off)),
        ("Veräußerungen (Erlös, Anschaffungskosten)", disp(on) == disp(off)),
    ]
    fiat_txs = [t for t in pf.txs if t.type in ("deposit", "withdrawal", "transfer")
                and any(a and pf.asset(a).is_fiat for a in (t.from_asset, t.to_asset))
                and not any(a and not pf.asset(a).is_fiat for a in (t.from_asset, t.to_asset))]
    return {
        "has_data": True, "off_now": is_off(ctx), "rows": rows, "fiat_positions": fiat_pos,
        "fiat_txs": len(fiat_txs), "checks": checks, "ok": all(c[1] for c in checks),
        "cash_accounts": sorted(a for a, v in on.cash_tracked.items() if v),
    }


def switch(ctx: Any, off: bool) -> dict[str, Any]:
    """Schalter setzen. Nur wenn die Prüfsummen von Beständen, Lots und Veräußerungen in beiden Weisen gleich sind."""
    if off:
        pv = preview(ctx)
        if not pv.get("has_data"):
            return {"errors": ["Keine Daten vorhanden."]}
        if not pv["ok"]:
            return {"errors": ["Asset-Bestände, Lots oder Veräußerungen wären nicht identisch – nicht umgeschaltet."]}
    if is_off(ctx) == off:
        return {"errors": []}
    ctx.settings.set(SETTING, bool(off))
    ctx.invalidate_data()
    log.info("Einstellung „Fiat-Guthaben nicht führen“: %s", "an" if off else "aus")
    return {"errors": []}
