"""Stichtag: Vorschau-Daten für die Oberfläche (0.98.0)."""

from __future__ import annotations

import dataclasses
import re
from datetime import date
from typing import Any

from app.cutover import core as CO

DEFAULT_CUTOFF = date(2025, 1, 1)


def parse_cutoff(raw: str | None, default: date | None = None) -> date:
    m = re.fullmatch(r"\s*(\d{4})-(\d{2})-(\d{2})\s*", raw or "")
    if not m:
        return default or DEFAULT_CUTOFF
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return default or DEFAULT_CUTOFF


def preview(ctx: Any, cutoff: date, *, impact: bool = False, derive: bool = False) -> dict[str, Any]:
    """Alles für die Vorschau (nur lesend): Pflichtprüfungen, Eröffnungsstand je Konto, Belege, Auffälligkeiten."""
    ms = CO.models(ctx, cutoff)
    if not ms:
        return {"has_data": False, "cutoff": cutoff}
    m = ms[0]  # angezeigt wird der steuerrelevante Eröffnungsstand
    checks = [dataclasses.replace(c, label=f"{c.label} [{mm.label}: {CO._scope_label(mm.opts)}]" if len(ms) > 1
                                  else c.label) for mm in ms for c in mm.checks]
    ov = CO.overview(ctx, m)
    refs = CO.reference_check(ctx, m)
    accounts = CO.account_status(ctx, m, ov, refs)
    out: dict[str, Any] = {
        "has_data": True, "cutoff": cutoff, "checks": checks, "ok": all(mm.ok for mm in ms), "overview": ov,
        "refs": refs, "accounts": accounts, "tax": CO.tax_view(m),
        "fiat_off": bool(ctx.settings.get("ledger.fiat_off", False)), "scope": m.opts.scope,
        "scope_label": CO._scope_label(m.opts), "models": [(mm.label, CO._scope_label(mm.opts)) for mm in ms],
        "impact": None,
    }
    from app.cutover import corrections as CR
    from app.cutover import fix as FX

    out["token"] = FX.token_for(ctx, cutoff, accounts, out["ok"])
    out["proposals"] = CR.proposals(ctx, m, refs)
    out["corrections"] = CR.listing(ctx, cutoff)
    out["assets"] = sorted({r["asset"] for r in ov["rows"]} | {r["asset"] for r in refs})
    out["all_accounts"] = sorted(m.pf_full.all_accounts())
    out["pf_assets"] = sorted(a for a, i in m.pf_full.assets.items() if not i.is_fiat)
    if impact:
        out["impact"] = CO.impact_on_findings(ctx, m)
    out["derived"] = None
    if derive:  # Belege nach dem Stichtag (liest die Diagnose: auf Wunsch)
        from app.cutover import derive as DV

        out["derived"] = DV.derive(ctx, m)
    return out
