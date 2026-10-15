"""Dauerhafte Entscheidungsregeln: wiederkehrende Hinweise einmal entscheiden, nicht jedes Mal.

Eine Regel sagt: „Befunde der Art X (optional: Konto Y, Asset Z) brauchen keine Handlung.“ Sie wirkt wie eine
Einzelentscheidung „als geprüft“ (:func:`app.diagnosis.triage.write_dismissals`) – ohne jede Datenänderung – und ist
bewusst eng begrenzt:

* nur Befunde mit Status **hinweis** (Standard) oder zusätzlich **verdacht**; ``wahrscheinlich`` und ``belegt``
  erfasst keine Regel, solche Aufgaben verlangen eine Entscheidung;
* Befunde, die schon einmal als geprüft markiert waren und sich seither **geändert** haben („überholt“), werden von
  Regeln nicht erneut ausgeblendet – neue Evidenz soll auffallen;
* jede Anwendung ist protokolliert (Notiz „Regel #id · Kennung“) und gemeinsam rückgängig zu machen;
* eine Regel lässt sich ausschalten, auf Wunsch samt Rücknahme ihrer Markierungen.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.diagnosis import actions as A
from app.diagnosis import triage as TR
from app.diagnosis.model import KINDS, Finding

log = logging.getLogger(__name__)
RULE_PREFIX = "Regel #"
MAX_STATUS = {"hinweis": ("hinweis",), "verdacht": ("hinweis", "verdacht")}
MAX_RULES = 200


@dataclass
class Rule:
    id: int
    kind: str
    account: str
    asset: str
    max_status: str
    note: str
    status: str
    created_at: str
    applied_count: int = 0
    last_applied_at: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active"

    def scope(self) -> str:
        parts = [KINDS.get(self.kind, self.kind)]
        if self.account:
            parts.append(f"Konto {self.account}")
        if self.asset:
            parts.append(f"Asset {self.asset}")
        parts.append("nur Hinweise" if self.max_status == "hinweis" else "Hinweise und Verdacht")
        return " · ".join(parts)

    def matches(self, f: Finding) -> bool:
        return (f.parent is None and f.kind == self.kind and f.status in MAX_STATUS.get(self.max_status, ())
                and (not self.account or self.account in f.accounts) and (not self.asset or self.asset in f.assets))


@dataclass
class Applied:
    token: str = ""
    count: int = 0
    by_rule: dict[int, int] = field(default_factory=dict)
    skipped_changed: int = 0


def _rule(r: Any) -> Rule:
    return Rule(int(r["id"]), r["kind"], r["account"] or "", r["asset"] or "", r["max_status"], r["note"] or "",
                r["status"], r["created_at"], int(r["applied_count"] or 0), r["last_applied_at"] or "")


def rules(db: Any, *, active_only: bool = False) -> list[Rule]:
    sql = "SELECT * FROM decision_rule" + (" WHERE status='active'" if active_only else "") + " ORDER BY id"
    return [_rule(r) for r in db.q(sql)]


def create(ctx: Any, kind: str, account: str = "", asset: str = "", max_status: str = "hinweis",
           note: str = "") -> A.Result:
    kind, account, asset, note = kind.strip(), account.strip(), asset.strip(), note.strip()[:300]
    if kind not in KINDS:
        return A.Result(errors=["Unbekannte Befundart."])
    if max_status not in MAX_STATUS:
        return A.Result(errors=["Unbekannte Stufe."])
    with A._LOCK:
        if ctx.db.scalar("SELECT COUNT(*) FROM decision_rule") >= MAX_RULES:
            return A.Result(errors=[f"Höchstens {MAX_RULES} Regeln."])
        if ctx.db.scalar("SELECT 1 FROM decision_rule WHERE status='active' AND kind=? AND COALESCE(account,'')=? AND "
                         "COALESCE(asset,'')=? AND max_status=?", (kind, account, asset, max_status)):
            return A.Result(errors=["Eine gleiche Regel ist bereits aktiv."])
        cur = ctx.db.x("INSERT INTO decision_rule(kind, account, asset, max_status, note, status, created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (kind, account or None, asset or None, max_status, note or None,
                                                  "active", A._now()))
    return A.Result(ok=True, message=f"Regel #{cur.lastrowid} angelegt.")


def preview(ctx: Any, rule: Rule, report: Any = None, open_: list[Finding] | None = None) -> list[Finding]:
    """Welche offenen Befunde die Regel jetzt träfe (lesend)."""
    from app.diagnosis.engine import report_for

    report = report or report_for(ctx)
    changed = _changed_ids(ctx, report)
    return [f for f in (open_ if open_ is not None else TR.open_findings(ctx, report))
            if rule.matches(f) and f.id not in changed]


def _changed_ids(ctx: Any, report: Any = None) -> set[str]:
    from app.diagnosis.engine import report_for

    report = report or report_for(ctx)
    states = A.mark_states(report.findings, A.active_marks(ctx.db))
    return {fid for fid, (_d, ok) in states.items() if not ok}


def apply_all(ctx: Any, only: int | None = None) -> tuple[A.Result, str]:
    """Aktive Regeln anwenden; Rückgabe: Ergebnis und Sammel-Kennung (leer, wenn nichts markiert wurde)."""
    out = apply_rules(ctx, only)
    msg = (f"{out.count} Befund(e) nach Regeln als geprüft markiert – keine Daten geändert."
           if out.count else "Keine offenen Befunde, auf die eine Regel zutrifft.")
    if out.skipped_changed:
        msg += f" {out.skipped_changed} geänderte Befund(e) bleiben offen (neue Evidenz)."
    return A.Result(ok=True, message=msg), out.token


def apply_rules(ctx: Any, only: int | None = None) -> Applied:
    out = Applied()
    with A._LOCK:
        active = [r for r in rules(ctx.db, active_only=True) if only is None or r.id == only]
        if not active:
            return out
        from app.diagnosis.engine import report_for

        report = report_for(ctx)
        changed = _changed_ids(ctx, report)
        open_ = TR.open_findings(ctx, report)
        token = uuid.uuid4().hex[:10]
        taken: set[str] = set()
        stamp = A._now()
        for r in active:
            hits = [f for f in open_ if f.id not in taken and r.matches(f)]
            out.skipped_changed += sum(1 for f in hits if f.id in changed)
            hits = [f for f in hits if f.id not in changed]
            if not hits:
                continue
            label = f"{RULE_PREFIX}{r.id} · {token}" + (f": {r.note}" if r.note else "")
            TR.write_dismissals(ctx, hits, label)
            taken |= {f.id for f in hits}
            out.by_rule[r.id] = len(hits)
            out.count += len(hits)
            ctx.db.x("UPDATE decision_rule SET applied_count=applied_count+?, last_applied_at=? WHERE id=?",
                     (len(hits), stamp, r.id))
        if out.count:
            out.token = token
            log.info("Entscheidungsregeln angewandt: %s Befunde (%s)", out.count, token)
    return out


def undo(ctx: Any, token: str) -> int:
    """Anwendung der Regeln mit dieser Kennung zurücknehmen."""
    if not token or not token.isalnum():
        return 0
    with A._LOCK:
        cur = ctx.db.x("UPDATE diag_decision SET status='undone', undone_at=? WHERE status='active' AND action="
                       "'dismiss' AND note LIKE ?", (A._now(), f"{RULE_PREFIX}% · {token}%"))
    return int(cur.rowcount or 0)


def switch_off(ctx: Any, rid: int, *, revert: bool = False) -> A.Result:
    """Regel ausschalten; ``revert``: auch ihre noch aktiven Markierungen zurücknehmen."""
    with A._LOCK:
        cur = ctx.db.x("UPDATE decision_rule SET status='off', off_at=? WHERE id=? AND status='active'",
                       (A._now(), rid))
    if not cur.rowcount:
        return A.Result(errors=["Regel nicht gefunden oder bereits aus."])
    n = TR.undo_label(ctx, f"{RULE_PREFIX}{rid} · ") if revert else 0
    return A.Result(ok=True, message=f"Regel #{rid} ausgeschaltet" + (f", {n} Markierung(en) zurückgenommen."
                                                                  if revert else "."))


def switch_on(ctx: Any, rid: int) -> A.Result:
    with A._LOCK:
        cur = ctx.db.x("UPDATE decision_rule SET status='active', off_at=NULL WHERE id=? AND status='off'", (rid,))
    return A.Result(ok=True, message=f"Regel #{rid} aktiv.") if cur.rowcount else A.Result(errors=["Regel nicht aus."])


def recent(db: Any, limit: int = 8) -> list[dict[str, Any]]:
    """Letzte Anwendungen der Regeln (für das gemeinsame Rückgängig)."""
    out: dict[str, dict[str, Any]] = {}
    for r in db.q("SELECT note, status, created_at FROM diag_decision WHERE action='dismiss' AND note LIKE ? "
                  "ORDER BY id DESC LIMIT 3000", (f"{RULE_PREFIX}%",)):
        head = str(r["note"]).split(":", 1)[0]
        token = head.rsplit("·", 1)[-1].strip()
        e = out.setdefault(token, {"token": token, "at": r["created_at"], "active": 0, "total": 0})
        e["total"] += 1
        e["active"] += 1 if r["status"] == "active" else 0
    return [e for e in out.values() if e["active"]][:limit]
