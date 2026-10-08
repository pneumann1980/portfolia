"""Sammelbearbeitung des Abgleichs: mehrere Dubletten-/Transfer-Befunde mit ihrer bevorzugten Lösung gemeinsam
prüfen und übernehmen – ohne eigene Korrekturlogik.

Jeder Befund wird wie in der Einzelbearbeitung geplant (:func:`app.diagnosis.actions.build_plan`, bevorzugte
Lösung aus :func:`app.diagnosis.recommend.recommend`). Die Sammelvorschau rechnet **alle** Änderungen zusammen auf
einer Kopie durch (:func:`app.diagnosis.actions.preview`) und schließt aus, was nicht sicher gemeinsam ausführbar ist:

* widersprüchliche Fälle (keine bevorzugte Lösung) – immer nur einzeln;
* prüfbedürftige Fälle (Empfehlung unter Vorbehalt) – nur, wenn ausdrücklich einbezogen;
* Befunde, die dieselbe Buchung betreffen (verändern, behalten oder als Transfer deuten) – Reihenfolge entschiede
  über das Ergebnis;
* Befunde, deren Änderungen zusammen einen negativen Bestand erzeugten.

Übernommen wird in **einer** Transaktion (ganz oder gar nicht) und nur, wenn Datenstand und Auswahl exakt der
Vorschau entsprechen (Prüfsumme). Jede Korrektur wird einzeln protokolliert (eine Entscheidung je Befund, einzeln
rückgängig zu machen); die Sammlung lässt sich als Ganzes zurücknehmen.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.diagnosis import actions as A
from app.diagnosis.integrity import RECON_KINDS, confidence_of
from app.diagnosis.model import Finding, Report
from app.diagnosis.recommend import recommend

log = logging.getLogger(__name__)
MAX_ITEMS = 200


@dataclass
class BulkItem:
    finding: Finding
    confidence: str
    option_label: str = ""
    plan: A.Plan | None = None
    reason: str = ""  # Ausschlussgrund (leer = ausführbar)
    conflict_with: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.reason and self.plan is not None


@dataclass
class BulkPlan:
    items: list[BulkItem]
    include_review: bool
    version: str
    effects: A.Effects | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ready(self) -> list[BulkItem]:
        return [i for i in self.items if i.ready]

    @property
    def excluded(self) -> list[BulkItem]:
        return [i for i in self.items if not i.ready]

    @property
    def conflicts(self) -> list[BulkItem]:
        return [i for i in self.items if i.conflict_with]

    @property
    def token(self) -> str:
        payload = json.dumps({"v": self.version, "r": self.include_review,
                              "p": sorted((i.finding.id, i.plan.token) for i in self.ready if i.plan)},
                             sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]


def candidates(report: Report) -> list[tuple[Finding, str, str]]:
    """Befunde des Abgleichs mit bevorzugter Lösung: (Befund, Konfidenz, bevorzugte Lösung)."""
    out = []
    for f in report.findings:
        if f.kind not in RECON_KINDS:
            continue
        rec = recommend(report, f)
        p = rec.primary
        out.append((f, confidence_of(f, p is not None, rec.conditional), p.label if p else ""))
    return out


def _involved(f: Finding, plan: A.Plan | None) -> set[str]:
    ids = {r.tx_id for r in f.txs} | {x.tx_id for a, b, _w in f.pairs for x in (a, b)}
    if plan is not None:
        for op in plan.ops:
            if op.kind in ("hide", "cover"):
                ids.add(op.target)
                ids.update(op.members)
                if op.kind == "cover" and op.link:
                    ids.add(op.link)
    return ids


def _positions(plan: A.Plan) -> set[tuple[str, str]]:
    """(Konto, Asset), deren Bestand die Änderungen des Plans berühren."""
    out: set[tuple[str, str]] = set()
    for op in plan.ops:
        if op.kind == "create" and op.tx is not None:
            out |= {(op.tx.from_account or "", op.tx.from_asset or ""), (op.tx.to_account or "", op.tx.to_asset or "")}
        elif op.ref is not None:
            out |= {(leg[0] or "", leg[1]) for leg in (op.ref.out, op.ref.inn) if leg is not None}
    return {p for p in out if p[0] and p[1]}


def plan(ctx: Any, finding_ids: list[str], *, include_review: bool = False, report: Report | None = None,
         with_effects: bool = True) -> BulkPlan:
    """Sammelvorschau (rechnet nur im Speicher)."""
    report = report or A.report_for(ctx)
    conf = {f.id: (c, label) for f, c, label in candidates(report)}
    items: list[BulkItem] = []
    for fid in list(dict.fromkeys(finding_ids))[:MAX_ITEMS]:
        f = report.by_id(fid)
        if f is None:
            continue
        c, label = conf.get(fid, ("", ""))
        it = BulkItem(f, c, label)
        items.append(it)
        if fid not in conf:
            it.reason = "kein Befund des Abgleichs – bitte einzeln in der Diagnose bearbeiten"
            continue
        if c == "widersprüchlich":
            it.reason = "widersprüchlich – keine bevorzugte Lösung, nur einzeln entscheidbar"
            continue
        if c == "prüfbedürftig" and not include_review:
            it.reason = "prüfbedürftig – erst prüfen, dann ausdrücklich einbeziehen"
            continue
        rec = recommend(report, f)
        p = A.build_plan(ctx, report, f, rec.primary.key)  # type: ignore[union-attr]
        if p.errors:
            it.reason = "; ".join(p.errors)[:300]
            continue
        it.plan = p
    # Überschneidungen: dieselbe Buchung in mehreren Befunden → keine Sammelausführung
    seen: dict[str, str] = {}
    involved = {it.finding.id: _involved(it.finding, it.plan) for it in items if it.ready}
    for it in items:
        if not it.ready:
            continue
        for tid in sorted(involved[it.finding.id]):
            other = seen.get(tid)
            if other is not None and other != it.finding.id:
                it.conflict_with.append(other)
                o = next(x for x in items if x.finding.id == other)
                if it.finding.id not in o.conflict_with:
                    o.conflict_with.append(it.finding.id)
            seen.setdefault(tid, it.finding.id)
    for it in items:
        if it.conflict_with and not it.reason:
            it.reason = "betrifft dieselbe Buchung wie ein anderer ausgewählter Befund – nur einzeln"
    bp = BulkPlan(items, include_review, A.data_version(ctx.db))
    if with_effects and bp.ready:
        _effects(ctx, report, bp)
    return bp


def _merged(bp: BulkPlan) -> A.Plan:
    ops: list[A.Op] = []
    for i, it in enumerate(bp.ready):
        assert it.plan is not None
        for op in it.plan.ops:
            if op.kind == "create" and op.tx is not None:  # Platzhalter-Kennungen je Plan eindeutig machen
                op = dataclasses.replace(op, tx=dataclasses.replace(op.tx, tx_id=f"{op.tx.tx_id}-{i}"))
            ops.append(op)
    first = bp.ready[0]
    assert first.plan is not None
    return A.Plan(first.finding, first.plan.option, {}, ops=ops, version=bp.version)


def _effects(ctx: Any, report: Report, bp: BulkPlan) -> None:
    """Gesamtwirkung; entstehen zusammen neue negative Bestände (auch zwischenzeitlich), werden die beteiligten
    Befunde ausgeschlossen und neu gerechnet (höchstens dreimal)."""
    for _round in range(3):
        if not bp.ready:
            bp.effects = None
            return
        eff = A.preview(ctx, report, _merged(bp))
        if not eff.negative_new:
            bp.effects = eff
            return
        keys = set(eff.negative_new)
        hit = [it for it in bp.ready if it.plan is not None and _positions(it.plan) & keys] or bp.ready
        for it in hit:
            it.reason = "zusammen mit den übrigen Korrekturen entstünde ein negativer Bestand – nur einzeln"
        bp.notes.append(f"{len(hit)} Befund(e) ausgeschlossen: gemeinsam negativer Bestand bei "
                        + ", ".join(f"{a} auf {acc}" for acc, a in sorted(keys)[:5]) + ".")
    bp.effects = A.preview(ctx, report, _merged(bp)) if bp.ready else None


@dataclass
class BulkResult:
    ok: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)
    bulk_id: str = ""
    decision_ids: list[int] = field(default_factory=list)


def execute(ctx: Any, finding_ids: list[str], token: str, *, include_review: bool = False) -> BulkResult:
    """Alle ausführbaren Korrekturen der Vorschau in einer Transaktion – nur bei unveränderter Vorschau (``token``);
    erneutes Absenden derselben Vorschau führt nichts doppelt aus."""
    with A._LOCK:
        done = ctx.db.q("SELECT id FROM diag_decision WHERE action='fix' AND params_json LIKE ?",
                        (f'%"bulk": "{token[:16]}"%',))
        if done:
            return BulkResult(ok=True, bulk_id=token[:16], decision_ids=[int(r["id"]) for r in done],
                              message="Diese Sammelkorrektur wurde bereits übernommen.")
        report = A.report_for(ctx)
        bp = plan(ctx, finding_ids, include_review=include_review, report=report, with_effects=True)
        if bp.token != token:
            return BulkResult(errors=["Die Sammelvorschau ist nicht mehr aktuell – Daten oder Auswahl haben sich "
                                      "geändert. Es wurde nichts geändert; bitte die Vorschau neu öffnen."])
        if not bp.ready:
            return BulkResult(errors=["Nichts auszuführen – alle ausgewählten Befunde sind ausgeschlossen."])
        from app.journal.service import journal_service

        js = journal_service(ctx)
        stamp = A._now()
        bulk_id = token[:16]
        ids: list[int] = []
        try:
            with ctx.db.transaction() as c:
                for it in bp.ready:
                    assert it.plan is not None
                    created: dict[str, str] = {}
                    recs = [A._exec(c, ctx, js, op, stamp, created) for op in sorted(
                        it.plan.ops, key=lambda o: 0 if o.kind == "create" else 1)]
                    f = it.finding
                    cur = c.execute(
                        "INSERT INTO diag_decision(finding_id, kind, title, action, option, option_label, "
                        "params_json, ops_json, fingerprint, note, status, created_at) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f.id, f.kind, f.title[:300], "fix", it.plan.option.key, it.plan.option.label[:200],
                         json.dumps({**it.plan.params, "bulk": bulk_id}, ensure_ascii=False),
                         json.dumps(recs, ensure_ascii=False, default=str), A.fingerprint(f),
                         f"Sammelkorrektur {bulk_id}", "active", stamp))
                    ids.append(int(cur.lastrowid))
                js._log(c, "diagnose_bulk", f"bulk:{bulk_id}", None,
                        {"findings": [it.finding.id for it in bp.ready], "decisions": ids}, stamp)
        except A.Conflict as e:
            return BulkResult(errors=[f"{e} Es wurde nichts geändert – bitte die Vorschau neu öffnen."])
        A._after(ctx, any(it.plan is not None and it.plan.touches_quotes for it in bp.ready))
        log.info("Sammelkorrektur %s übernommen: %d Befunde", bulk_id, len(ids))
        return BulkResult(ok=True, bulk_id=bulk_id, decision_ids=ids,
                          message=f"Sammelkorrektur übernommen: {len(ids)} Befund(e), je Befund einzeln "
                                  "protokolliert und rückgängig zu machen.")


def undo(ctx: Any, bulk_id: str) -> BulkResult:
    """Sammelkorrektur als Ganzes zurücknehmen (umgekehrte Reihenfolge, eine Transaktion; Konflikt → nichts)."""
    with A._LOCK:
        rows = ctx.db.q("SELECT * FROM diag_decision WHERE action='fix' AND status='active' AND params_json LIKE ? "
                        "ORDER BY id DESC", (f'%"bulk": "{bulk_id}"%',))
        if not rows:
            return BulkResult(errors=["Keine aktive Sammelkorrektur mit dieser Kennung."])
        from app.journal.service import journal_service

        js = journal_service(ctx)
        stamp = A._now()
        try:
            with ctx.db.transaction() as c:
                for r in rows:
                    for rec in reversed(json.loads(r["ops_json"] or "[]")):
                        A._revert(c, js, rec, stamp)
                    c.execute("UPDATE diag_decision SET status='undone', undone_at=? WHERE id=?", (stamp, r["id"]))
                js._log(c, "diagnose_bulk_undo", f"bulk:{bulk_id}", None, {"decisions": [r["id"] for r in rows]},
                        stamp)
        except A.Conflict as e:
            return BulkResult(errors=[f"Rückgängig nicht möglich: {e} Es wurde nichts geändert."])
        A._after(ctx, False)
        return BulkResult(ok=True, bulk_id=bulk_id, decision_ids=[int(r["id"]) for r in rows],
                          message=f"Sammelkorrektur zurückgenommen ({len(rows)} Befund(e)).")


def recent(db: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Letzte Sammelkorrekturen (für „rückgängig“)."""
    out: dict[str, dict[str, Any]] = {}
    for r in db.q("SELECT id, params_json, status, created_at, title FROM diag_decision WHERE action='fix' AND "
                  "params_json LIKE '%\"bulk\"%' ORDER BY id DESC LIMIT 400"):
        try:
            b = json.loads(r["params_json"] or "{}").get("bulk")
        except ValueError:
            continue
        if not b:
            continue
        e = out.setdefault(b, {"bulk_id": b, "created_at": r["created_at"], "n": 0, "active": 0})
        e["n"] += 1
        e["active"] += 1 if r["status"] == "active" else 0
        if len(out) > limit:
            break
    return list(out.values())[:limit]
