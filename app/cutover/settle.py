"""Ausgleich zum Stichtag (0.102.0): alles „glattziehen“, ohne an Einzelfällen hängen zu bleiben.

Grundlage ist der Bestand laut Beleg nach dem Stichtag (Referenzbestand oder Bestand der Quelle). Je Konto und
Asset wird die Differenz zum berechneten Bestand **am Vortag des Stichtags** gebucht, sodass ab dem Stichtag
Berechnung und Beleg übereinstimmen:

* Beleg niedriger als berechnet → **Abgang** (Tag ``cutover_correction``): verbraucht die ältesten Lots, ist weder
  Veräußerung noch Verlust noch Zahlungsstrom (steuerlich neutral). Mehr als der Eröffnungsbestand kann nicht
  ausgeglichen werden; der Rest wird ausgewiesen (Ursache im aktiven Zeitraum).
* Beleg höher als berechnet → **Zugang** am Vortag zu 0 € (Tag ``opening_balance``). Die Haltefrist beginnt neu; ein
  Verkauf binnen eines Jahres wäre voll steuerpflichtig. Wo ab dem Stichtag aus diesem Konto/Asset verkauft wurde, weist
  die Vorschau darauf hin.

Es werden keine Ursachen unterdrückt: liegt eine Ursache nahe (offene Abgänge anderer Konten, Abgänge ohne Gebühr),
steht sie in der Vorschau. Jede Buchung ist gekennzeichnet („Stichtag-Ausgleich“) und einzeln rücknehmbar.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.cutover import core as CO
from app.cutover import corrections as CR
from app.cutover import derive as DV
from app.util.timeutil import local_tz

log = logging.getLogger(__name__)
ZERO = Decimal(0)


def key_id(account: str, asset: str) -> str:
    return hashlib.sha256(f"{account}\x00{asset}".encode()).hexdigest()[:12]


def _evidence_text(ev: dict[str, Any]) -> str:
    when = ev["at"].astimezone(local_tz())
    return f"{ev['kind']} {ev['qty'].normalize():f} am {when:%d.%m.%Y}"[:90]


def plan(ctx: Any, m: CO.Model) -> dict[str, Any]:
    """Vorschau des Ausgleichs (rein lesend): je Konto/Asset mit Abweichung eine Zeile."""
    txs = [t for t in m.pf_full.txs if t.date >= m.cutoff and t.flag != "estimated"]
    sold = {(d.account, d.asset) for d in m.led_full.disposals if d.date >= m.cutoff}
    rows: list[dict[str, Any]] = []
    for ev in DV.evidence(ctx, m, observed=True, stale=True):
        key = (ev["account"], ev["asset"])
        if m.pf_full.asset(ev["asset"]).is_fiat:
            continue
        window = [t for t in txs if t.ts <= ev["at"] and DV._touches(t, key)]
        have_open = m.state.balances.get(key, ZERO)
        computed = have_open + sum((DV._effect(t, key) for t in window), ZERO)
        diff = ev["qty"] - computed
        if diff == 0 or abs(diff) <= CO.STAUB_QTY:
            continue
        kind = "remove" if diff < 0 else "add"
        qty = abs(diff)
        rest = ZERO
        if kind == "remove" and qty > have_open:
            qty, rest = have_open, qty - have_open
        cause = DV._causes(m, key, diff, ev["at"], window) if window else {"cause": "", "text": "", "tx_ids": []}
        rows.append({
            "id": key_id(*key), "account": key[0], "asset": key[1], "kind": kind, "qty": qty, "rest": rest,
            "diff": diff, "reference": ev["qty"], "computed": computed, "opening": have_open, "at": ev["at"],
            "kind_label": ev["kind"], "source": ev["source"], "stale": bool(ev.get("stale")),
            "window": len(window), "cause": cause.get("cause", ""), "cause_text": cause.get("text", ""),
            "tx_ids": cause.get("tx_ids", []), "evidence": _evidence_text(ev),
            "sold": kind == "add" and key in sold,
            "restart": kind == "add"})
    return {"rows": [r for r in rows if r["qty"] > 0 or r["rest"] > 0],
            "n_add": sum(1 for r in rows if r["kind"] == "add"),
            "n_remove": sum(1 for r in rows if r["kind"] == "remove" and r["qty"] > 0),
            "n_rest": sum(1 for r in rows if r["rest"] > 0), "n_sold": sum(1 for r in rows if r["sold"]),
            "day": m.cutoff - timedelta(days=1)}


def apply(ctx: Any, cutoff: Any, ids: set[str], *, reapprove: bool = True) -> dict[str, Any]:
    """Ausgewählte Zeilen buchen (je Zeile eine Buchung; Fehler einzelner Zeilen stoppen die übrigen nicht) und – wenn
    ein Stichtag festgeschrieben ist – die betroffenen Konten neu freigeben."""
    from app.cutover import fix as FX

    ms = CO.models(ctx, cutoff)
    if not ms:
        return {"errors": ["Keine Daten vorhanden."], "done": [], "failed": []}
    rows = [r for r in plan(ctx, ms[0])["rows"] if r["id"] in ids and r["qty"] > 0]
    if not rows:
        return {"errors": ["Nichts ausgewählt bzw. kein Ausgleich nötig."], "done": [], "failed": []}
    done: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for r in rows:
        res = CR.apply(ctx, cutoff, account=r["account"], asset=r["asset"], kind=r["kind"], qty=r["qty"],
                       mode="settle", evidence=r["evidence"])
        (failed if res["errors"] else done).append({**r, "errors": res["errors"], "tx_id": res.get("tx_id")})
    accounts = {d["account"] for d in done}
    re_ok = False
    if reapprove and accounts and FX.state(ctx) is not None:
        re_ok = not FX.reapprove(ctx, accounts)["errors"]
    log.info("Stichtag-Ausgleich %s: %d gebucht, %d fehlgeschlagen", cutoff, len(done), len(failed))
    return {"errors": [], "done": done, "failed": failed, "reapproved": re_ok}
