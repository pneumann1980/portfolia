"""Beleg ergänzt eine bestehende Buchung (M25/AP5.2): feldweiser Vergleich, Vorschlag, Vorschau, Bestätigung.

Ein Dokument muss keine neue Buchung erzeugen. Beschreibt es einen bereits gebuchten Vorgang (technische Identität
bzw. Zuordnung des Prüf-Stapels), werden die Felder verglichen und **nur belegte bzw. rekonstruierte** Belegwerte als
Ergänzung vorgeschlagen:

* EUR-Gegenwert (fehlt, ist geschätzt oder weicht ab), Handelsgebühr (fehlt bzw. weicht ab), Uhrzeit (Buchung nur mit
  Datum, gleicher Tag), Transaktions-Hash (fehlt), Belegverweis in der Notiz.
* **Nie:** Menge, Asset, Konto oder Vorgangsart – Abweichungen dort sind Widersprüche zur manuellen Klärung.
* **Manuell korrigierte Buchungen** (manuelle App-Buchung bzw. bearbeitete Import-Buchung) werden nicht überschrieben;
  dann bleibt nur „Beleg verknüpfen“.

Ausgeführt wird über die Korrektur-Engine der Diagnose (Operation ``amend``): Vorschau mit Wirkung auf Bestände,
Einstand, realisierte Ergebnisse und Steuer; Übernehmen nur mit unverändertem Datenstand; „Rückgängig“ stellt den
vorherigen Zustand exakt wieder her.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import UTC
from decimal import Decimal
from typing import Any

from app.diagnosis import actions as A
from app.diagnosis.model import Finding
from app.diagnosis.recommend import Option
from app.ledger.models import Tx

GOOD = ("belegt", "rekonstruiert")
CENT = Decimal("0.01")


@dataclass
class Change:
    field: str
    label: str
    old: str
    new: str
    reason: str


@dataclass
class Proposal:
    doc_id: int
    n: int
    target: Tx | None
    changes: list[Change] = field(default_factory=list)
    blocked: str = ""
    contradictions: list[str] = field(default_factory=list)


def _dec(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except ArithmeticError:
        return None


def _fields(doc: Any, n: int) -> dict[str, dict[str, Any]]:
    res = json.loads(doc["result_json"] or "{}")
    for t in res.get("txs") or []:
        if int(t.get("n") or 0) == n:
            return t.get("fields") or {}
    return {}


def _target_for(ctx: Any, doc: Any, n: int) -> Tx | None:
    """Vorhandene Buchung: technische Identität (Recherche) bzw. Ziel der Zuordnung im Prüf-Stapel."""
    res = json.loads(doc["result_json"] or "{}")
    tid = next((t.get("existing") for t in res.get("txs") or [] if int(t.get("n") or 0) == n), None)
    if not tid and doc["batch_id"]:
        from app.csvimport.service import csv_service

        for rc in csv_service(ctx).rows(int(doc["batch_id"])):
            raw = rc.rec.raw or {}
            d = raw.get("document") or {}
            if d.get("id") == doc["id"] and rc.match and rc.match.get("target") and rc.rec.event_line in (0, None) \
                    and rc.match.get("cat") in ("dublette", "ergaenzung", "widerspruch"):
                tid = rc.match["target"]
                break
    pf = ctx.portfolio()
    if not tid or pf is None:
        return None
    return next((t for t in pf.txs if t.tx_id == tid), None)


def propose(ctx: Any, doc_id: int, n: int) -> Proposal:
    from app.documentimport.service import document_service

    doc = document_service(ctx).get(doc_id)
    if doc is None:
        return Proposal(doc_id, n, None, blocked="Beleg nicht gefunden.")
    f = _fields(doc, n)
    t = _target_for(ctx, doc, n)
    p = Proposal(doc_id, n, t)
    if t is None:
        p.blocked = "Keine zugeordnete Buchung – der Beleg beschreibt einen neuen Vorgang (Prüf-Stapel)."
        return p
    meta = ctx.db.q1("SELECT * FROM journal_tx WHERE tx_id=?", (t.tx_id,)) if t.origin == "journal" else None
    ov = ctx.db.q1("SELECT action FROM tx_override WHERE tx_id=?", (t.tx_id,)) if t.origin == "import" else None
    if (meta is not None and meta["source"] == "manual") or (ov is not None and ov["action"] == "edit"):
        p.blocked = (f"{t.tx_id} wurde manuell erfasst bzw. bearbeitet – Portfolia überschreibt manuelle Korrekturen "
                     "nicht. Beleg nur verknüpfen oder die Buchung selbst bearbeiten.")
    if t.origin == "plan":
        p.blocked = "Sparplan-Schätzung – bitte den Sparplan bzw. die tatsächliche Ausführung erfassen."

    def good(name: str) -> dict[str, Any] | None:
        x = f.get(name)
        return x if x and x.get("status") in GOOD and x.get("value") not in (None, "") else None

    # Widersprüche (nicht ergänzbar)
    q = good("quantity")
    t_q = t.to_qty if t.type in ("buy", "deposit") else t.from_qty
    if q and t_q is not None and abs(Decimal(q["value"]) - t_q) > Decimal("1e-8"):
        p.contradictions.append(f"Menge laut Beleg {q['value']} ≠ Buchung {t_q} – nicht ergänzbar, bitte klären")
    v = good("value_eur")
    if v and t.type in ("buy", "sell", "trade", "deposit", "withdrawal"):
        new = Decimal(v["value"]).quantize(CENT)
        if t.value_eur is None or t.flag == "estimated" or abs(t.value_eur - new) >= CENT:
            old = f"{t.value_eur}" if t.value_eur is not None else "–"
            p.changes.append(Change("value_eur", "EUR-Gegenwert", old, str(new), v.get("reason") or ""))
    fee, fee_eur = good("fee"), good("fee_eur")
    if fee and t.type in ("buy", "sell", "trade"):
        ccy = (f.get("fee_ccy") or {}).get("value") or "EUR"
        if t.fee_qty is None or (t.fee_asset == ccy and abs(t.fee_qty - Decimal(fee["value"])) >= CENT):
            old = f"{t.fee_qty} {t.fee_asset}" if t.fee_qty is not None else "–"
            p.changes.append(Change("fee", "Gebühr", old, f"{fee['value']} {ccy}" + (
                f" ({fee_eur['value']} €)" if fee_eur else ""), fee.get("reason") or ""))
    tm, d = good("time"), good("date")
    if tm and d and t.date_only and t.date.isoformat() == d["value"]:
        p.changes.append(Change("time", "Uhrzeit", "nur Datum", tm["value"], tm.get("reason") or ""))
    h = good("txhash")
    have_hash = (meta is not None and meta["tx_hash"]) or (t.note and h and h["value"] in (t.note or ""))
    if h and not have_hash:
        p.changes.append(Change("txhash", "Transaktions-Hash", "–", h["value"], h.get("reason") or ""))
    return p


def _apply_changes(ctx: Any, p: Proposal, doc: Any, selected: set[str]) -> A.Op | None:
    """Operation „amend“ mit neuem Zustand der Buchung und Ausgangszustand (für Prüfsumme und Rückgängig)."""
    from app.documentimport.profiles import parse_time
    from app.journal.overrides import asset_classes, to_tx
    from app.journal.service import tx_row
    from app.util.timeutil import iso

    t = p.target
    assert t is not None
    f = _fields(doc, p.n)
    chosen = [c for c in p.changes if c.field in selected]
    if not chosen:
        return None
    ref = f"Beleg {doc['filename']} (SHA {doc['sha256'][:12]})"
    new_ts = t.ts
    if any(c.field == "time" for c in chosen):
        from app.documentimport import parse as P

        tm = parse_time(f["time"]["value"])
        tz = (f.get("tz") or {}).get("value")
        new_ts, _do, _why = P.combine(t.date, tm, tz)
    fee_ccy = (f.get("fee_ccy") or {}).get("value") or "EUR"
    vals: dict[str, Any] = {}
    for c in chosen:
        if c.field == "value_eur":
            vals["value_eur"] = Decimal(f["value_eur"]["value"]).quantize(CENT)
        elif c.field == "fee":
            vals["fee_asset"], vals["fee_qty"] = fee_ccy, Decimal(f["fee"]["value"])
            fe = (f.get("fee_eur") or {}).get("value")
            vals["fee_eur"] = Decimal(fe).quantize(CENT) if fe else (Decimal(f["fee"]["value"]) if fee_ccy == "EUR"
                                                                     else None)
        elif c.field == "txhash":
            vals["tx_hash"] = f["txhash"]["value"]
    note = ((t.note or "") + (" · " if t.note else "") + f"ergänzt aus {ref}")[:500]
    labels = ", ".join(c.label for c in chosen)
    if t.origin == "import":
        row = tx_row(t)
        if "value_eur" in vals:
            row["value_eur"] = str(vals["value_eur"])
        if "fee_qty" in vals:
            row |= {"fee_asset": vals["fee_asset"], "fee_qty": str(vals["fee_qty"]),
                    "fee_eur": str(vals["fee_eur"]) if vals.get("fee_eur") is not None else ""}
        if "tx_hash" in vals:
            note = f"{note} · txhash={vals['tx_hash']}"[:500]
        if new_ts != t.ts:
            row["datetime"] = new_ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        row["note"] = note
        row["tx_id"] = t.tx_id
        classes = asset_classes(ctx.portfolio().assets)
        new_tx, errs = to_tx(row, t.seq, classes)
        if new_tx is None:
            raise ValueError("; ".join(errs))
        new_tx = dataclasses.replace(new_tx, origin=t.origin, source=t.source)
        ov = A._row(ctx.db.q1("SELECT * FROM tx_override WHERE tx_id=?", (t.tx_id,)))
        return A.Op("amend", t.tx_id, f"Ergänzen ({labels}): Import-Buchung {t.tx_id}", origin="import", row=row,
                    before={"override": ov, "tx": tx_row(t)}, tx=new_tx)
    meta = ctx.db.q1("SELECT * FROM journal_tx WHERE tx_id=?", (t.tx_id,))
    if meta is None or meta["status"] != "active":
        raise ValueError(f"{t.tx_id} ist keine aktive App-Buchung mehr.")
    cols = {k: meta[k] for k in A.AMEND_COLS}
    after = dict(cols)
    if "value_eur" in vals:
        after["value_eur"], after["value_source"] = str(vals["value_eur"]), f"Beleg {doc['sha256'][:12]}"
    if "fee_qty" in vals:
        after["fee_asset"], after["fee_qty"] = vals["fee_asset"], str(vals["fee_qty"])
        after["fee_eur"] = str(vals["fee_eur"]) if vals.get("fee_eur") is not None else None
    if "tx_hash" in vals:
        after["tx_hash"] = vals["tx_hash"]
    if new_ts != t.ts:
        after["ts_utc"], after["date_only"] = iso(new_ts), 0
    after["note"] = note
    new_tx = dataclasses.replace(
        t, ts=new_ts if new_ts != t.ts else t.ts, date_only=t.date_only and new_ts == t.ts,
        value_eur=vals.get("value_eur", t.value_eur), fee_asset=vals.get("fee_asset", t.fee_asset),
        fee_qty=vals.get("fee_qty", t.fee_qty), fee_eur=vals.get("fee_eur", t.fee_eur), note=note)
    return A.Op("amend", t.tx_id, f"Ergänzen ({labels}): App-Buchung {t.tx_id}", origin="journal",
                before={"updated_at": meta["updated_at"], "cols": cols, "after": after}, tx=new_tx)


def finding(doc: Any, p: Proposal) -> Finding:
    t = p.target
    return Finding(kind="document", status="belegt", priority=2,
                   title=f"Beleg {doc['filename']} ergänzt Buchung {t.tx_id if t else '–'}",
                   known=[f"{c.label}: {c.old} → {c.new} ({c.reason})" for c in p.changes],
                   key=f"doc|{doc['sha256']}|{p.n}|{t.tx_id if t else ''}",
                   data={"type": "document", "doc": doc["id"], "n": p.n, "tx": t.tx_id if t else None})


def plan(ctx: Any, doc_id: int, n: int, selected: set[str]) -> tuple[A.Plan, Finding, Proposal]:
    from app.documentimport.service import document_service

    doc = document_service(ctx).get(doc_id)
    p = propose(ctx, doc_id, n)
    f = finding(doc, p) if doc is not None else Finding(kind="document", status="belegt", title="–")
    opt = Option("amend", "Bestehende Buchung ergänzen", "Belegte Werte in die vorhandene Buchung übernehmen")
    pl = A.Plan(f, opt, {"fields": sorted(selected)}, version=A.data_version(ctx.db))
    if p.blocked:
        pl.errors.append(p.blocked)
        return pl, f, p
    try:
        op = _apply_changes(ctx, p, doc, selected)
    except ValueError as e:
        pl.errors.append(str(e))
        return pl, f, p
    if op is None:
        pl.errors.append("Keine Ergänzung ausgewählt.")
    else:
        pl.ops.append(op)
    pl.warnings += p.contradictions
    return pl, f, p


def preview(ctx: Any, doc_id: int, n: int, selected: set[str]) -> tuple[A.Plan, Any, Proposal]:
    from app.diagnosis.engine import report_for

    pl, _f, p = plan(ctx, doc_id, n, selected)
    eff = A.preview(ctx, report_for(ctx), pl) if not pl.errors else None
    return pl, eff, p


def apply(ctx: Any, doc_id: int, n: int, selected: set[str], token: str) -> A.Result:
    pl, f, _p = plan(ctx, doc_id, n, selected)
    if pl.errors:
        return A.Result(errors=pl.errors)
    return A.apply_plan(ctx, f, pl, token, lambda: plan(ctx, doc_id, n, selected)[0])
