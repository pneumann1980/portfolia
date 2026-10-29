"""Von vorn beginnen: alle Korrekturen und Antworten der Datenqualität zurücknehmen.

Es gibt hier **keine eigene Rücknahmelogik** – jede Änderung wird über die Rückgängig-Funktion zurückgenommen, die
sie auch einzeln hat, über alle Protokolle hinweg **neueste zuerst** (spätere Änderungen hängen nie an früheren):

* Korrekturen der Diagnose einzeln (:func:`actions.undo`), gesammelt (:func:`bulk.undo`), als Klärung
  (:func:`clarify.undo_run`) bzw. aus einem Autopilot-Lauf (:func:`bulk.undo`), Entscheidungen ohne Datenänderung
  (:func:`actions.reopen`, Regel-Markierungen über :func:`rules.undo`);
* Entscheidungen zu neuen Vorgängen (:func:`csvimport.batch.undo`), Quellenübernahmen (:func:`takeover.undo`),
  zurückgelegte Buchungen (:func:`recheck.undo`);
* danach die Zustände ohne eigenes Protokoll: Vormerkungen, zurückgestellte Klärungsfälle, Antworten im Assistenten
  („zur Kenntnis genommen“, „Quelle nicht maßgeblich“).

Umfang (``scopes``): ``antworten`` (immer) · ``import`` · ``takeover`` · ``auto`` (Autopilot und automatische
Klärung) · ``regeln`` (Regeln ausschalten, ihre Markierungen zurücknehmen). Nie berührt: Import, Abrufe, eigene
Buchungen im Journal, Kontostände/Nachweise, manuelle Kurse, Einstellungen.

Ablauf: :func:`plan` (Vorschau mit Prüfsumme) → :func:`run` prüft die Prüfsumme, legt eine Sicherung an, schaltet
den Autopiloten aus (sonst korrigiert er sofort wieder) und nimmt zurück. Schlägt eine Rücknahme fehl, hält der Lauf
an: Jede Rücknahme ist für sich atomar und die Reihenfolge stimmt, der Zwischenstand ist also stimmig; die Sicherung
von vorher lässt sich in der App wiederherstellen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)
JOB = "dq_reset"
LABEL = "Von vorn beginnen"
STATE_KEY = "dq_reset_last"
CONFIRM_WORD = "ZURÜCKSETZEN"
_LOCK = threading.Lock()

# Schlüssel → (Bezeichnung, Erklärung, immer dabei)
SCOPES: dict[str, tuple[str, str, bool]] = {
    "antworten": ("Korrekturen und Antworten", "Alles, was du in „Buchungen prüfen“ und auf den Expertenseiten "
                  "entschieden oder korrigiert hast – einzeln, gesammelt oder als Klärung.", True),
    "import": ("Entscheidungen zu neuen Vorgängen", "Übernommene, verknüpfte und abgelehnte Vorgänge aus Abrufen und "
               "CSV-Importen werden wieder offen (die Abrufe selbst bleiben).", False),
    "takeover": ("Quellenübernahmen", "Konten, bei denen eine Börse oder Wallet maßgeblich gemacht wurde, nutzen "
                 "wieder die Buchungen des Imports.", False),
    "auto": ("Korrekturen des Autopiloten", "Was der Autopilot und die automatische Klärung von selbst korrigiert "
             "haben.", False),
    "regeln": ("Dauerhafte Regeln", "Regeln werden ausgeschaltet, ihre Markierungen zurückgenommen.", False),
}
KIND_LABEL = {"fix": "Korrektur", "bulk": "Sammelkorrektur", "klaerung": "Klärung", "autopilot": "Autopilot-Korrektur",
              "mark": "Entscheidung", "regelmarke": "Regel-Markierungen", "import": "Entscheidung zu neuen Vorgängen",
              "takeover": "Quellenübernahme", "recheck": "Zurückgelegte Buchung"}
_RANK = {"import": 1, "takeover": 2, "recheck": 3}  # gleiche Sekunde: Diagnose (4) zuerst zurücknehmen
_RULE_TOKEN = re.compile(r"^Regel #\d+ · ([0-9a-f]+)")


@dataclass
class Unit:
    kind: str
    scope: str
    at: str
    ref: str  # Entscheidung, Sammelkennung, Klärungs-Token, Aktion, Quelle, Protokolleintrag
    label: str
    n: int = 1  # Anzahl Entscheidungen bzw. Einträge

    @property
    def sort_key(self) -> tuple[Any, ...]:
        dt = parse_iso(self.at) or datetime.min.replace(tzinfo=UTC)
        return dt, _RANK.get(self.kind, 4), self.ref


@dataclass
class Plan:
    scopes: list[str]
    units: list[Unit] = field(default_factory=list)  # neueste zuerst
    states: dict[str, int] = field(default_factory=dict)  # Zustände ohne Protokoll (Anzahl)
    rules: list[int] = field(default_factory=list)  # aktive Regeln (Umfang „regeln“)
    available: dict[str, int] = field(default_factory=dict)  # je Umfang vorhandene Änderungen (auch nicht gewählt)
    token: str = ""

    @property
    def total(self) -> int:
        return len(self.units) + sum(self.states.values()) + len(self.rules)

    def by_kind(self) -> list[tuple[str, int]]:
        out: dict[str, int] = {}
        for u in self.units:
            out[u.kind] = out.get(u.kind, 0) + u.n
        return sorted(out.items(), key=lambda kv: -kv[1])


def _now() -> str:
    return iso(datetime.now(UTC)) or ""


def normalize(scopes: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    return ["antworten"] + [s for s in SCOPES if s != "antworten" and s in set(scopes)]


# ----------------------------------------------------------------------------------------------------
# Bestandsaufnahme
# ----------------------------------------------------------------------------------------------------

def _all_units(ctx: Any) -> list[Unit]:
    """Alle zurücknehmbaren Änderungen (jede genau einmal, dem Umfang zugeordnet)."""
    from app.diagnosis import recheck as RC

    db = ctx.db
    out: list[Unit] = []
    skip_dec: set[int] = set()
    skip_act: set[int] = set()
    # Quellenübernahmen (Korrektur und Stapelaktion gehören dazu)
    for r in db.q("SELECT t.id, t.source_id, t.applied_at, t.created_at, t.decision_id, t.action_id, d.name "
                  "FROM ds_takeover t LEFT JOIN data_source d ON d.id=t.source_id WHERE t.status='applied'"):
        out.append(Unit("takeover", "takeover", r["applied_at"] or r["created_at"], str(r["source_id"]),
                        f"Quellenübernahme {r['name'] or r['source_id']}"))
        if r["decision_id"]:
            skip_dec.add(int(r["decision_id"]))
        if r["action_id"]:
            skip_act.add(int(r["action_id"]))
    # Autopilot-Läufe: welche Sammelkorrekturen bzw. Regel-Markierungen stammen von ihm?
    auto_bulks: set[str] = set()
    auto_rule_tokens: set[str] = set()
    for r in db.q("SELECT summary_json FROM autopilot_run WHERE status='ok'"):
        try:
            s = json.loads(r["summary_json"] or "{}")
        except ValueError:
            continue
        fx = s.get("fixes") or {}
        auto_bulks |= set(fx.get("bulk_ids") or ([fx["bulk_id"]] if fx.get("bulk_id") else []))
        if (s.get("rules") or {}).get("token"):
            auto_rule_tokens.add(str(s["rules"]["token"]))
    clar = {r["bulk_id"]: r for r in db.q("SELECT token, bulk_id, case_key, created_at FROM clarify_run "
                                          "WHERE status='ok' AND bulk_id IS NOT NULL")}
    # Korrekturen der Diagnose
    bulks: dict[str, list[Any]] = {}
    for r in db.q("SELECT id, kind, title, params_json, note, created_at FROM diag_decision WHERE action='fix' AND "
                  "status='active' ORDER BY id"):
        if int(r["id"]) in skip_dec or r["kind"] == "takeover":
            continue
        try:
            bulk = (json.loads(r["params_json"] or "{}") or {}).get("bulk")
        except ValueError:
            bulk = None
        if bulk:
            bulks.setdefault(str(bulk), []).append(r)
        else:
            out.append(Unit("fix", "antworten", r["created_at"], str(r["id"]), r["title"]))
    for bulk, rows in bulks.items():
        last = rows[-1]["created_at"]
        if bulk in clar:
            auto = any((x["note"] or "").endswith("(auto)") for x in rows)
            out.append(Unit("klaerung", "auto" if auto else "antworten", last, clar[bulk]["token"],
                            f"Klärung {clar[bulk]['case_key']}", len(rows)))
        elif bulk in auto_bulks:
            out.append(Unit("autopilot", "auto", last, bulk, "Autopilot-Lauf", len(rows)))
        else:
            out.append(Unit("bulk", "antworten", last, bulk, f"Sammelkorrektur {bulk}", len(rows)))
    # Entscheidungen ohne Datenänderung
    rule_marks: dict[str, list[Any]] = {}
    for r in db.q("SELECT id, title, note, created_at FROM diag_decision WHERE action IN ('dismiss', 'reject', "
                  "'defer') AND status='active' ORDER BY id"):
        m = _RULE_TOKEN.match(r["note"] or "")
        if m:
            rule_marks.setdefault(m.group(1), []).append(r)
        else:
            out.append(Unit("mark", "antworten", r["created_at"], str(r["id"]), r["title"]))
    for token, rows in rule_marks.items():
        out.append(Unit("regelmarke", "auto" if token in auto_rule_tokens else "regeln", rows[-1]["created_at"], token,
                        "Markierungen nach Regeln", len(rows)))
    # Entscheidungen zu neuen Vorgängen
    for r in db.q("SELECT id, label, created_at FROM import_action WHERE status='active' ORDER BY id"):
        if int(r["id"]) not in skip_act:
            out.append(Unit("import", "import", r["created_at"], str(r["id"]), r["label"]))
    # Zurückgelegte Buchungen
    for e in RC.entries(db):
        if not e.get("undone"):
            out.append(Unit("recheck", "antworten", e.get("at") or "", str(e["id"]), e.get("title") or e["tx_id"]))
    return out


def _states(ctx: Any) -> dict[str, int]:
    from app.diagnosis import guide as G
    from app.diagnosis import recheck as RC

    db = ctx.db
    return {k: v for k, v in {
        "vormerkungen": len(RC.marks(db)),
        "faelle": int(db.scalar("SELECT COUNT(*) FROM clarify_case WHERE status='zurueck'", default=0) or 0),
        "kenntnis": len(db.get_state(G.INTEGRITY_ACK_KEY) or {}),
        "vorrang": len(db.get_state(G.DECLINED_KEY) or []),
    }.items() if v}


STATE_LABEL = {"vormerkungen": "Vormerkungen „erneut prüfen“", "faelle": "Zurückgestellte Klärungsfälle",
               "kenntnis": "„Zur Kenntnis genommen“ (Nachrechnen)", "vorrang": "„Quelle nicht maßgeblich“"}


def plan(ctx: Any, scopes: list[str] | tuple[str, ...] | set[str]) -> Plan:
    sc = normalize(scopes)
    every = _all_units(ctx)
    p = Plan(sc)
    for u in every:
        p.available[u.scope] = p.available.get(u.scope, 0) + u.n
    p.units = sorted((u for u in every if u.scope in sc), key=lambda u: u.sort_key, reverse=True)
    p.states = _states(ctx)
    if "regeln" in sc:
        p.rules = [int(r["id"]) for r in ctx.db.q("SELECT id FROM decision_rule WHERE status='active' ORDER BY id")]
    p.available["regeln"] = p.available.get("regeln", 0) + int(
        ctx.db.scalar("SELECT COUNT(*) FROM decision_rule WHERE status='active'", default=0) or 0)
    raw = json.dumps({"s": sc, "u": [(u.kind, u.ref, u.n) for u in p.units], "st": p.states, "r": p.rules},
                     sort_keys=True)
    p.token = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return p


# ----------------------------------------------------------------------------------------------------
# Ausführen
# ----------------------------------------------------------------------------------------------------

def _undo_unit(ctx: Any, u: Unit) -> list[str]:
    """Eine Änderung über ihre eigene Rückgängig-Funktion zurücknehmen; Rückgabe: Fehler (leer = erledigt)."""
    from app.diagnosis import actions as A

    if u.kind == "fix":
        return A.undo(ctx, int(u.ref)).errors
    if u.kind == "mark":
        return A.reopen(ctx, int(u.ref)).errors
    if u.kind in ("bulk", "autopilot"):
        from app.diagnosis import bulk as B

        return B.undo(ctx, u.ref).errors
    if u.kind == "klaerung":
        from app.diagnosis import clarify as K

        return K.undo_run(ctx, u.ref).errors
    if u.kind == "regelmarke":
        from app.diagnosis import rules as R

        R.undo(ctx, u.ref)
        return []
    if u.kind == "import":
        from app.csvimport import batch as BA
        from app.csvimport.service import csv_service

        return list(BA.undo(csv_service(ctx), int(u.ref)).get("errors") or [])
    if u.kind == "takeover":
        from app.datasources import takeover as TK

        return list(TK.undo(ctx, int(u.ref)).get("errors") or [])
    if u.kind == "recheck":
        from app.diagnosis import recheck as RC

        return list(RC.undo(ctx, int(u.ref)).get("errors") or [])
    return [f"Unbekannte Art {u.kind}."]


def _clear_states(ctx: Any) -> None:
    from app.diagnosis import clarify as K
    from app.diagnosis import guide as G
    from app.diagnosis import recheck as RC

    db = ctx.db
    for r in db.q("SELECT case_key FROM clarify_case WHERE status='zurueck'"):
        K.reopen(ctx, r["case_key"])
    db.set_state(RC.MARKS_KEY, {})
    for key in (G.INTEGRITY_ACK_KEY, G.DECLINED_KEY, G.RECHECK_KEPT_KEY):
        db.set_state(key, None)


def _close_autopilot_runs(ctx: Any) -> None:
    """Autopilot-Läufe, von denen nichts mehr aktiv ist, als zurückgenommen kennzeichnen (Anzeige im Bericht)."""
    db = ctx.db
    for r in db.q("SELECT token, summary_json FROM autopilot_run WHERE status='ok'"):
        try:
            s = json.loads(r["summary_json"] or "{}")
        except ValueError:
            continue
        fx = s.get("fixes") or {}
        bulks = fx.get("bulk_ids") or ([fx["bulk_id"]] if fx.get("bulk_id") else [])
        rtoken = (s.get("rules") or {}).get("token")
        if not bulks and not rtoken:
            continue
        alive = any(db.q1("SELECT 1 FROM diag_decision WHERE status='active' AND params_json LIKE ?",
                          (f'%"bulk": "{b}"%',)) for b in bulks)
        alive = alive or (rtoken and db.q1("SELECT 1 FROM diag_decision WHERE status='active' AND note LIKE ?",
                                           (f"Regel #% · {rtoken}%",)) is not None)
        if not alive:
            db.x("UPDATE autopilot_run SET status='reverted', reverted_at=? WHERE token=?", (_now(), r["token"]))


def backup(ctx: Any, reason: str) -> str:
    """Sicherung der Datenbank mit Anlass (für die Liste „Sicherung wiederherstellen“)."""
    from app.dbrestore import set_label
    from app.jobs.maintenance import backup_now

    name = str(backup_now(ctx, "reset")["file"])
    set_label(ctx.config.backup_dir, name, reason)
    return name


def run(ctx: Any, scopes: list[str], token: str, prog: Any | None = None) -> dict[str, Any]:
    """Zurücksetzen ausführen – nur, wenn der Stand noch der Vorschau entspricht (``token``)."""
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "errors": ["Das Zurücksetzen läuft bereits."]}
    try:
        return _run(ctx, scopes, token, prog)
    finally:
        _LOCK.release()


def _run(ctx: Any, scopes: list[str], token: str, prog: Any | None) -> dict[str, Any]:
    from app.progress import job_progress

    p = plan(ctx, scopes)
    out: dict[str, Any] = {"ok": False, "errors": [], "started_at": _now(), "scopes": p.scopes, "done": 0,
                           "total": len(p.units), "failed": None, "states": p.states, "rules": len(p.rules)}
    if p.token != token:
        out["errors"] = ["Seit der Vorschau hat sich etwas geändert – bitte die Vorschau neu öffnen. Es wurde nichts "
                         "geändert."]
        return out
    prog = prog or job_progress(ctx, JOB, LABEL, unit="Änderungen", phases=["backup", "undo", "states"])
    try:
        prog.phase("backup", text="Sicherung anlegen")
        out["backup"] = backup(ctx, "vor „Von vorn beginnen“")
        out["autopilot_was_on"] = bool(ctx.settings.get("autopilot.enabled", False))
        ctx.settings.set("autopilot.enabled", False)  # sonst korrigiert er sofort wieder
        prog.phase("undo", total=len(p.units), text="Änderungen zurücknehmen", unit="Änderungen")
        for i, u in enumerate(p.units):
            errs = _undo_unit(ctx, u)
            if errs:
                out["failed"] = {"kind": u.kind, "label": u.label[:160], "at": u.at, "errors": errs[:3]}
                out["errors"] = [f"{KIND_LABEL.get(u.kind, u.kind)} „{u.label[:80]}“: {' '.join(errs[:2])}"]
                break
            out["done"] = i + 1
            prog.update(i + 1, text=f"{KIND_LABEL.get(u.kind, u.kind)}: {u.label[:60]}")
        if out["failed"] is None:
            prog.phase("states", text="Vormerkungen und Antworten zurücksetzen")
            _clear_states(ctx)
            if "regeln" in p.scopes:
                from app.diagnosis import rules as R

                for rid in p.rules:
                    R.switch_off(ctx, rid, revert=True)
            _close_autopilot_runs(ctx)
            out["ok"] = True
    except Exception as e:  # unerwartet: anhalten, Stand bleibt stimmig (jede Rücknahme ist atomar)
        log.exception("Zurücksetzen fehlgeschlagen")
        out["errors"] = [f"{type(e).__name__}: {e}"[:300]]
    finally:
        out["finished_at"] = _now()
        ctx.db.set_state(STATE_KEY, out)
        ctx.invalidate_data()
        prog.finish(out["ok"], result=f"{out['done']} von {out['total']} zurückgenommen")
        log.info("Von vorn beginnen: %s von %s Änderungen zurückgenommen (%s)", out["done"], out["total"],
                 "ok" if out["ok"] else "; ".join(out["errors"])[:200])
    if out["ok"] and ctx.scheduler is not None:
        from app.diagnosis import checkrun as CR

        ctx.scheduler.trigger(CR.JOB, 1.0)  # Aufgaben neu berechnen (lokal, ohne Abrufe)
    return out


def job_run(ctx: Any, scopes: list[str] | None = None, token: str = "") -> dict[str, Any]:
    return run(ctx, list(scopes or []), token)


def last(db: Any) -> dict[str, Any]:
    raw = db.get_state(STATE_KEY)
    return raw if isinstance(raw, dict) else {}


def running(ctx: Any) -> dict[str, Any] | None:
    from app.progress import view

    st = ctx.job_status().get(JOB) or {}
    if not st.get("running"):
        return None
    p = view({**(st.get("progress") or {}), "running": True, "label": LABEL})
    return None if p.get("stale") else p
