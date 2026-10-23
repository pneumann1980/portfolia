"""Beleg ergänzt eine bestehende Buchung (M25/AP5.2): feldweiser Vergleich, Vorschlag, Vorschau, Bestätigung.

Ein Dokument muss keine neue Buchung erzeugen. Beschreibt es einen bereits gebuchten Vorgang (technische Identität
bzw. Zuordnung des Prüf-Stapels), werden die Felder verglichen und **nur belegte bzw. rekonstruierte** Belegwerte als
Ergänzung vorgeschlagen:

* EUR-Gegenwert (fehlt, ist geschätzt oder weicht ab), Handelsgebühr (fehlt bzw. weicht ab), Uhrzeit (Buchung nur mit
  Datum, gleicher Tag), Transaktions-Hash (fehlt), Belegverweis in der Notiz.
* **Nie:** Menge, Asset, Konto oder Vorgangsart – Abweichungen dort sind Widersprüche zur manuellen Klärung.
* **Von Hand erfasste bzw. bearbeitete Buchungen** (manuelle App-Buchung, bearbeitete Import-Buchung) sind **nicht
  gesperrt**: Die Vorschau warnt („von Hand geändert, Feld X steht jetzt auf Y“), übernommen wird erst nach
  ausdrücklicher Bestätigung je Buchung – nie in einer Sammelaktion. Gesperrt bleibt nur die Sparplan-Schätzung.

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
    warning: str = ""  # von Hand erfasst/bearbeitet: Änderung nur nach ausdrücklicher Bestätigung
    needs_confirm: bool = False
    fee_key: str = ""  # Kosteninformation: Feld der geschätzten Gebühr (fee_buy/fee_sell) – sonst Beleggebühr „fee“
    estimate: bool = False  # Gebühr nur geschätzt (Kosteninformation), nie belegt


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
        p.needs_confirm = True
        what = "von Hand erfasst" if meta is not None and meta["source"] == "manual" else "von Hand bearbeitet"
        now = [f"Wert {t.value_eur} €" if t.value_eur is not None else "Wert –",
               f"Gebühr {t.fee_qty} {t.fee_asset}" if t.fee_qty is not None else "Gebühr –"]
        p.warning = (f"{t.tx_id} wurde {what} (jetzt: {', '.join(now)}). Die Änderung überschreibt diese Angaben – "
                     "nur nach ausdrücklicher Bestätigung; Menge, Asset, Konto und Vorgangsart bleiben unverändert, "
                     "„Rückgängig“ stellt den Zustand wieder her.")
    if t.origin == "plan":
        p.blocked = "Sparplan-Schätzung – bitte den Sparplan bzw. die tatsächliche Ausführung erfassen."
    if (json.loads(doc["result_json"] or "{}").get("doc_type")) == "kosteninformation":
        return _propose_costinfo(ctx, doc, f, t, p)

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


def _propose_costinfo(ctx: Any, doc: Any, f: dict[str, dict[str, Any]], t: Tx, p: Proposal) -> Proposal:
    """Gebührenschätzung aus der Kosteninformation (Entscheidung F10): nur die Gebühr, **nie** Kurswert oder Uhrzeit
    (Kurs und Zeit der Ordereingabe sind nicht die der Ausführung). Vorrang hat die Abrechnung (R1/Z6): Ist die Buchung
    durch eine Abrechnung belegt, bleibt es bei der Verknüpfung. Immer nur nach Bestätigung, als „geschätzt“
    gekennzeichnet."""
    p.estimate = True
    if p.blocked:
        return p
    if t.type not in ("buy", "sell"):
        p.blocked = "Eine Gebührenschätzung gibt es nur für Käufe und Verkäufe."
        return p
    if ctx.db.q1("SELECT 1 FROM tx_document WHERE tx_id=? AND role='abrechnung'", (t.tx_id,)) is not None:
        p.blocked = ("Die Gebühr dieser Buchung ist durch eine Abrechnung belegt – die Kosteninformation bleibt "
                     "Order-Beleg (Vorrang der Abrechnung).")
        return p
    p.fee_key = "fee_buy" if t.type == "buy" else "fee_sell"
    x = f.get(p.fee_key)
    if not x or x.get("value") in (None, ""):
        p.blocked = "Die Kosteninformation nennt keine Dienstleistungskosten für diese Richtung."
        return p
    if t.fee_qty is not None and t.fee_asset not in (None, "EUR"):
        p.blocked = f"Die Buchung hat eine Gebühr in {t.fee_asset} – bitte von Hand prüfen."
        return p
    new = Decimal(x["value"]).quantize(CENT)
    if t.fee_qty is None or abs(t.fee_qty - new) >= CENT:
        old = f"{t.fee_qty} EUR" if t.fee_qty is not None else "–"
        day = (f.get("date") or {}).get("value") or ""
        shown = f"{day[8:10]}.{day[5:7]}.{day[:4]}" if day else "?"
        p.changes.append(Change("fee", "Gebühr (geschätzt)", old, f"{new} EUR",
                                f"aus Kosteninformation vom {shown}, geschätzt: Dienstleistungs- + "
                                "Fremdwährungskosten, ohne Produktkosten"))
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
    fee_ccy = "EUR" if p.estimate else (f.get("fee_ccy") or {}).get("value") or "EUR"
    fee_src = f.get(p.fee_key) if p.estimate else f.get("fee")
    vals: dict[str, Any] = {}
    for c in chosen:
        if c.field == "value_eur":
            vals["value_eur"] = Decimal(f["value_eur"]["value"]).quantize(CENT)
        elif c.field == "fee" and fee_src:
            vals["fee_asset"], vals["fee_qty"] = fee_ccy, Decimal(fee_src["value"]).quantize(CENT) if p.estimate \
                else Decimal(fee_src["value"])
            fe = None if p.estimate else (f.get("fee_eur") or {}).get("value")
            vals["fee_eur"] = Decimal(fe).quantize(CENT) if fe else (vals["fee_qty"] if fee_ccy == "EUR" else None)
        elif c.field == "txhash":
            vals["tx_hash"] = f["txhash"]["value"]
    if p.estimate:
        day = (f.get("date") or {}).get("value") or ""
        shown = f"{day[8:10]}.{day[5:7]}.{day[:4]}" if day else "?"
        note = ((t.note or "") + (" · " if t.note else "") +
                f"Gebühr aus Kosteninformation vom {shown}, geschätzt ({ref})")[:500]
    else:
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


def plan(ctx: Any, doc_id: int, n: int, selected: set[str], *, confirm: bool = True
         ) -> tuple[A.Plan, Finding, Proposal]:
    from app.documentimport.service import document_service

    doc = document_service(ctx).get(doc_id)
    p = propose(ctx, doc_id, n)
    f = finding(doc, p) if doc is not None else Finding(kind="document", status="belegt", title="–")
    opt = Option("amend", "Bestehende Buchung ergänzen", "Belegte Werte in die vorhandene Buchung übernehmen")
    pl = A.Plan(f, opt, {"fields": sorted(selected)}, version=A.data_version(ctx.db))
    if p.blocked:
        pl.errors.append(p.blocked)
        return pl, f, p
    if p.needs_confirm and not confirm:
        pl.errors.append("Diese Buchung wurde von Hand erfasst bzw. bearbeitet – bitte die Änderung ausdrücklich "
                         "bestätigen.")
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


def apply(ctx: Any, doc_id: int, n: int, selected: set[str], token: str, *, confirm: bool = False) -> A.Result:
    pl, f, p = plan(ctx, doc_id, n, selected, confirm=confirm)
    if pl.errors:
        return A.Result(errors=pl.errors)
    res = A.apply_plan(ctx, f, pl, token, lambda: plan(ctx, doc_id, n, selected, confirm=confirm)[0])
    if res.ok and p.target is not None:
        from app.documentimport import txdocs

        txdocs.link_document(ctx.db, p.target.tx_id, doc_id,
                             txdocs.role_of_document(ctx.db, doc_id, "abrechnung"), "amend")
    return res


# -- Sammelaktion ------------------------------------------------------------------------------------------------

@dataclass
class BulkItem:
    doc_id: int
    n: int
    filename: str
    target: Tx
    changes: list[Change]
    warning: str = ""
    needs_confirm: bool = False
    skipped: str = ""


def bulk_candidates(ctx: Any, items: list[tuple[int, int]]) -> list[BulkItem]:
    """Belege mit zugeordneter Buchung und Ergänzungsvorschlag (nicht gesperrt, keine Widersprüche in der Menge)."""
    out: list[BulkItem] = []
    seen: set[str] = set()
    for doc_id, n in items:
        p = propose(ctx, doc_id, n)
        doc = ctx.db.q1("SELECT filename FROM document WHERE id=?", (doc_id,))
        if p.target is None or p.blocked or not p.changes:
            continue
        it = BulkItem(doc_id, n, doc["filename"] if doc else str(doc_id), p.target, p.changes, p.warning,
                      p.needs_confirm)
        if p.contradictions:
            it.skipped = "; ".join(p.contradictions)
        elif p.target.tx_id in seen:
            it.skipped = "Dieselbe Buchung wird bereits von einem anderen Beleg dieser Sammelaktion geändert."
        seen.add(p.target.tx_id)
        out.append(it)
    return out


def bulk_plan(ctx: Any, picks: list[tuple[int, int]]) -> tuple[A.Plan, Finding, list[tuple[str, int]]]:
    """Ein Plan für mehrere Belege (eine Entscheidung, als Ganzes rückgängig zu machen). Es werden nur Ergänzungen
    ohne Warnung aufgenommen – von Hand erfasste bzw. bearbeitete Buchungen gehen nur einzeln mit Bestätigung.
    Dritter Rückgabewert: (Buchung, Beleg) für die Belegverknüpfung."""
    f = Finding(kind="document", status="belegt", priority=2, title=f"{len(picks)} Belege ergänzen Buchungen",
                known=[], key="docs|" + ",".join(f"{d}.{n}" for d, n in sorted(picks)),
                data={"type": "documents", "docs": [d for d, _n in picks]})
    opt = Option("amend", "Buchungen aus Belegen ergänzen", "Belegte Gebühren, Werte und Uhrzeiten übernehmen")
    pl = A.Plan(f, opt, {"docs": [f"{d}.{n}" for d, n in sorted(picks)]}, version=A.data_version(ctx.db))
    targets: set[str] = set()
    links: list[tuple[str, int]] = []
    for doc_id, n in picks:
        doc = document_row(ctx, doc_id)
        p = propose(ctx, doc_id, n)
        if doc is None or p.target is None or p.blocked or not p.changes:
            pl.errors.append(f"Beleg {doc_id}: keine ergänzbare Buchung.")
            continue
        if p.needs_confirm:
            pl.errors.append(f"Beleg {doc_id}: {p.target.tx_id} wurde von Hand erfasst bzw. bearbeitet – nur einzeln "
                             "mit Bestätigung änderbar.")
            continue
        if p.contradictions:
            pl.errors.append(f"Beleg {doc_id}: " + "; ".join(p.contradictions))
            continue
        if p.target.tx_id in targets:
            pl.errors.append(f"Beleg {doc_id}: {p.target.tx_id} wird bereits von einem anderen Beleg geändert.")
            continue
        targets.add(p.target.tx_id)
        try:
            op = _apply_changes(ctx, p, doc, {c.field for c in p.changes})
        except ValueError as e:
            pl.errors.append(f"Beleg {doc_id}: {e}")
            continue
        if op is not None:
            pl.ops.append(op)
            links.append((p.target.tx_id, doc_id))
    return pl, f, links


def document_row(ctx: Any, doc_id: int) -> Any:
    from app.documentimport.service import document_service

    return document_service(ctx).get(doc_id)


def bulk_preview(ctx: Any, picks: list[tuple[int, int]]) -> tuple[A.Plan, Any]:
    from app.diagnosis.engine import report_for

    pl, _f, _links = bulk_plan(ctx, picks)
    eff = A.preview(ctx, report_for(ctx), pl) if pl.ops and not pl.errors else None
    return pl, eff


def bulk_apply(ctx: Any, picks: list[tuple[int, int]], token: str) -> A.Result:
    pl, f, links = bulk_plan(ctx, picks)
    if pl.errors:
        return A.Result(errors=pl.errors)
    res = A.apply_plan(ctx, f, pl, token, lambda: bulk_plan(ctx, picks)[0])
    if res.ok:
        from app.documentimport import txdocs

        for tx_id, doc_id in links:
            txdocs.link_document(ctx.db, tx_id, doc_id, txdocs.role_of_document(ctx.db, doc_id, "abrechnung"),
                                 "amend")
    return res
