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

Entscheidung je Zeile (0.103.0): ``auto`` (Vorgabe nach Gewicht bzw. Kontoregel), ``settle`` (auf den Beleg
ausgleichen), ``keep`` (App behalten, nichts buchen) oder ``custom`` (eigener Zielbestand für heute).
Gewicht: ``small`` (Wert unter 1 €, pauschal),
``critical`` (Verkauf ab dem Stichtag, Rest, große oder nicht bewertbare Differenz: einzeln entscheiden, nicht
vorausgewählt) und ``normal``.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.cutover import core as CO
from app.cutover import corrections as CR
from app.cutover import derive as DV
from app.util.timeutil import local_tz

log = logging.getLogger(__name__)
ZERO = Decimal(0)


SMALL_EUR = Decimal(1)
BIG_EUR = Decimal(100)
DECISIONS = ("auto", "settle", "keep", "custom")


def key_id(account: str, asset: str) -> str:
    return hashlib.sha256(f"{account}\x00{asset}".encode()).hexdigest()[:12]


def _evidence_text(ev: dict[str, Any]) -> str:
    when = ev["at"].astimezone(local_tz())
    return f"{ev['kind']} {ev['qty'].normalize():f} am {when:%d.%m.%Y}"[:90]


def _value(prices: dict[str, Any], asset: str, qty: Decimal) -> Decimal | None:
    pi = prices.get(asset)
    if pi is None or getattr(pi, "kind", "") in ("unvalued", "") or not pi.price_eur:
        return None
    return abs(qty) * Decimal(str(pi.price_eur))


def _tier(r: dict[str, Any]) -> str:
    v = r["value"]
    if r["sold"] or r["rest"] > 0 or v is None or v >= BIG_EUR:
        return "critical"
    return "small" if v < SMALL_EUR else "normal"


def _row(m: CO.Model, prices: dict[str, Any], sold: set[tuple[str, str]], txs: list[Any], key: tuple[str, str],
         ev: dict[str, Any]) -> dict[str, Any] | None:
    """Eine Ausgleichszeile aus einem Beleg ``ev`` (Menge ``qty`` zum Zeitpunkt ``at``); ``None`` ohne Abweichung."""
    window = [t for t in txs if t.ts <= ev["at"] and DV._touches(t, key)]
    have_open = m.state.balances.get(key, ZERO)
    computed = have_open + sum((DV._effect(t, key) for t in window), ZERO)
    diff = ev["qty"] - computed
    if diff == 0 or abs(diff) <= CO.STAUB_QTY:
        return None
    kind = "remove" if diff < 0 else "add"
    qty = abs(diff)
    rest = ZERO
    if kind == "remove" and qty > have_open:
        qty, rest = have_open, qty - have_open
    cause = DV._causes(m, key, diff, ev["at"], window) if window else {"cause": "", "text": "", "tx_ids": []}
    now = m.led_full.balances.get(key, ZERO)
    signed = -qty if kind == "remove" else qty
    row = {
        "id": key_id(*key), "account": key[0], "asset": key[1], "kind": kind, "qty": qty, "rest": rest,
        "diff": diff, "reference": ev["qty"], "computed": computed, "opening": have_open, "at": ev["at"],
        "kind_label": ev["kind"], "source": ev["source"], "stale": bool(ev.get("stale")),
        "window": len(window), "cause": cause.get("cause", ""), "cause_text": cause.get("text", ""),
        "tx_ids": cause.get("tx_ids", []), "evidence": _evidence_text(ev),
        "sold": kind == "add" and key in sold, "restart": kind == "add",
        "now": now, "after": now + signed, "signed": signed,
        "since": sum(1 for t in txs if t.ts > ev["at"] and DV._touches(t, key)),
        "value": _value(prices, key[1], qty + rest), "custom": bool(ev.get("custom"))}
    row["tier"] = _tier(row)
    return row


def plan(ctx: Any, m: CO.Model) -> dict[str, Any]:
    """Vorschau des Ausgleichs (rein lesend): je Konto/Asset mit Abweichung eine Zeile, nach Konto gegliedert."""
    txs = [t for t in m.pf_full.txs if t.date >= m.cutoff and t.flag != "estimated"]
    sold = {(d.account, d.asset) for d in m.led_full.disposals if d.date >= m.cutoff}
    try:
        prices = ctx.price_infos(m.pf_full, m.led_full)
    except Exception as e:  # Bewertung ist Zusatz – der Ausgleich funktioniert auch ohne Kurse
        log.debug("Kurse für den Ausgleich nicht verfügbar: %s", e)
        prices = {}
    rows: list[dict[str, Any]] = []
    for ev in DV.evidence(ctx, m, observed=True, stale=True):
        key = (ev["account"], ev["asset"])
        if m.pf_full.asset(ev["asset"]).is_fiat:
            continue
        r = _row(m, prices, sold, txs, key, ev)
        if r is not None and (r["qty"] > 0 or r["rest"] > 0):
            rows.append(r)
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda x: (x["account"], x["tier"] != "critical", x["asset"])):
        groups.setdefault(r["account"], []).append(r)
    return {"rows": rows, "groups": [{"account": a, "id": key_id(a, ""), "rows": rs} for a, rs in groups.items()],
            "n_add": sum(1 for r in rows if r["kind"] == "add"),
            "n_remove": sum(1 for r in rows if r["kind"] == "remove" and r["qty"] > 0),
            "n_rest": sum(1 for r in rows if r["rest"] > 0), "n_sold": sum(1 for r in rows if r["sold"]),
            "n_small": sum(1 for r in rows if r["tier"] == "small"),
            "n_critical": sum(1 for r in rows if r["tier"] == "critical"),
            "n_stale": sum(1 for r in rows if r["stale"]), "day": m.cutoff - timedelta(days=1),
            "_prices": prices, "_sold": sold, "_txs": txs}


def custom_row(m: CO.Model, pl: dict[str, Any], row: dict[str, Any], target: Decimal) -> dict[str, Any] | None:
    """Zeile für einen eigenen Zielbestand (heute): Differenz zum aktuellen Bestand der App."""
    ev = {"account": row["account"], "asset": row["asset"], "qty": target, "at": datetime.now(UTC),
          "kind": "Eigener Zielbestand", "source": "manuell", "stale": False, "custom": True}
    return _row(m, pl["_prices"], pl["_sold"], pl["_txs"], (row["account"], row["asset"]), ev)


def resolve(m: CO.Model, pl: dict[str, Any], form: Mapping[str, Any]) -> dict[str, Any]:
    """Entscheidungen aus dem Formular: ``dec_<id>`` (auto|settle|keep|custom), ``tgt_<id>`` (Zielbestand),
    ``acc_<kontoid>`` (default|all|remove|none), ``small_all`` (Kleinstbeträge pauschal). Rückgabe: ``chosen`` (Zeilen
    mit Buchung), ``skipped``, ``errors`` und die Zusammenfassung."""
    chosen: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    small_all = str(form.get("small_all", "")) in ("1", "on", "true")
    for g in pl["groups"]:
        rule = str(form.get(f"acc_{g['id']}", "default"))
        for r in g["rows"]:
            dec = str(form.get(f"dec_{r['id']}", "auto"))
            if dec not in DECISIONS:
                dec = "auto"
            if dec == "auto":
                if rule == "all":
                    dec = "settle"
                elif rule == "none":
                    dec = "keep"
                elif rule == "remove":
                    dec = "settle" if r["kind"] == "remove" else "keep"
                else:
                    dec = "settle" if (r["tier"] == "normal" or (r["tier"] == "small" and small_all)) else "keep"
            if dec == "keep":
                skipped.append(r)
            elif dec == "settle":
                (chosen if r["qty"] > 0 else skipped).append(r)
            else:  # custom
                raw = str(form.get(f"tgt_{r['id']}", "")).strip().replace(",", ".")
                try:
                    target = Decimal(raw)
                    if not target.is_finite() or target < 0:
                        raise ValueError(raw)
                except (InvalidOperation, ValueError):
                    errors.append(f"{r['account']} · {r['asset']}: Zielbestand „{raw}“ ungültig.")
                    skipped.append(r)
                    continue
                cr = custom_row(m, pl, r, target)
                if cr is None:
                    skipped.append(r)  # Zielbestand entspricht dem Bestand der App: nichts zu buchen
                elif cr["qty"] > 0:
                    chosen.append(cr)
                else:
                    errors.append(f"{r['account']} · {r['asset']}: Abgang nicht am Stichtag möglich "
                                  "(Bestand zum Stichtag zu klein).")
                    skipped.append(r)
    return {"chosen": chosen, "skipped": skipped, "errors": errors, "summary": summarize(chosen, skipped)}


def summarize(chosen: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> dict[str, Any]:
    adds = [r for r in chosen if r["kind"] == "add"]
    rem = [r for r in chosen if r["kind"] == "remove"]

    def val(rs: list[dict[str, Any]]) -> Decimal:
        return sum((r["value"] for r in rs if r["value"] is not None), ZERO)

    return {"n": len(chosen), "n_add": len(adds), "n_remove": len(rem), "value_add": val(adds),
            "value_remove": val(rem), "n_unvalued": sum(1 for r in chosen if r["value"] is None),
            "n_sold": sum(1 for r in adds if r["sold"]), "n_rest": sum(1 for r in chosen if r["rest"] > 0),
            "n_skipped": len(skipped), "n_stale": sum(1 for r in chosen if r["stale"]),
            "accounts": sorted({r["account"] for r in chosen})}


def _book(ctx: Any, cutoff: Any, rows: list[dict[str, Any]], *, reapprove: bool) -> dict[str, Any]:
    from app.cutover import fix as FX

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


def apply_form(ctx: Any, cutoff: Any, form: Mapping[str, Any], *, reapprove: bool = True) -> dict[str, Any]:
    """Entscheidungen des Formulars buchen (je Zeile eine Buchung; ein Fehler stoppt die übrigen Zeilen nicht)."""
    ms = CO.models(ctx, cutoff)
    if not ms:
        return {"errors": ["Keine Daten vorhanden."], "done": [], "failed": []}
    res = resolve(ms[0], plan(ctx, ms[0]), form)
    if res["errors"]:
        return {"errors": res["errors"], "done": [], "failed": []}
    if not res["chosen"]:
        return {"errors": ["Nichts zum Ausgleichen ausgewählt."], "done": [], "failed": []}
    return _book(ctx, cutoff, res["chosen"], reapprove=reapprove)


def apply(ctx: Any, cutoff: Any, ids: set[str], *, reapprove: bool = True) -> dict[str, Any]:
    """Ausgewählte Zeilen auf den Beleg ausgleichen (``ids`` = Zeilenkennungen)."""
    return apply_form(ctx, cutoff, {f"dec_{i}": "settle" for i in ids}, reapprove=reapprove)
