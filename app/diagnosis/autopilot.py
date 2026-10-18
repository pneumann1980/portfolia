"""Autopilot und Bericht nach Importen: erledigt, was beweisbar ist – und sagt, was übrig bleibt.

Nach einem Import (kuratierte Datei, übernommener CSV-Stapel) läuft ein kurzer Durchgang:

1. **Bericht** (immer): wie viele Aufgaben und Hinweise offen sind, was neu ist, was verschwunden ist.
   Der Bericht verändert nichts.
2. **Entscheidungsregeln** (Einstellung ``autopilot.rules``, Standard an, nur wenn der Autopilot eingeschaltet ist):
   die vom Nutzer angelegten Regeln (:mod:`app.diagnosis.rules`) – nur Hinweise/Verdacht, ohne Datenänderung.
3. **Netzwerkgebühren aus Abgang + Rückfluss** (Einstellung ``autopilot.roundtrips``, Standard **aus**, nur mit
   eingeschaltetem Autopilot): z. B. KRC-20-Mint (Commit/Reveal) – Abgang und Rückfluss auf demselben Konto werden durch
   eine Gebühren-Buchung über die Differenz ersetzt (:func:`app.diagnosis.audit.mint_roundtrips`). Nur Befunde ohne
   Vorbehalt (nicht „Verdacht“), als eigene Gruppe geprüft und ausgeführt, gleiche Sicherungen wie unten.
4. **Beweisbare Korrekturen** (Einstellung ``autopilot.fixes``, Standard **aus**, nur mit eingeschaltetem Autopilot):
   Abgleichs-Befunde mit eindeutiger Lösung (:func:`app.diagnosis.integrity.confidence_of` =
   „eindeutig“; auf Wunsch auch „hoch plausibel“, Einstellung ``autopilot.fixes_level``), deren Plan bestehende
   Buchungen ausblendet bzw. verknüpft oder zwei vorhandene Transfer-Seiten zu einem Transfer zusammenfasst (keine
   sonstigen neuen Buchungen), die gemeinsam keinen negativen Bestand und keinen neuen Befund erzeugen. Ausgeführt
   wie jede Sammelkorrektur (:func:`app.diagnosis.bulk.execute`): eine Transaktion, Prüfsumme, je Befund protokolliert.

Nie: Ähnlichkeitswerte als Beleg, erfundene Buchungen, Auflösung konkurrierender Kandidaten, Eingriffe in Rohimporte.
Jeder Lauf ist mit einer Kennung protokolliert und lässt sich gemeinsam zurücknehmen (:func:`undo`).
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from app.diagnosis import actions as A
from app.diagnosis import bulk as B
from app.diagnosis import rules as R
from app.diagnosis import triage as TR
from app.diagnosis.engine import report_for
from app.diagnosis.model import KINDS

log = logging.getLogger(__name__)
SAFE_OPS = ("hide", "cover")  # bestehende Buchungen ausblenden/verknüpfen
LEVELS = {"eindeutig": ("eindeutig",), "hoch plausibel": ("eindeutig", "hoch plausibel")}
MAX_FIXES = 50
KEEP_RUNS = 60
MAX_IDS = 2000
_RUN = threading.Lock()


def _safe(plan: A.Plan) -> bool:
    """Plan ohne erfundene Buchung: nur Ausblenden/Verknüpfen – oder ein Transfer, der genau zwei vorhandene Buchungen
    zusammenfasst (beide werden im selben Plan ausgeblendet)."""
    hides = sum(1 for op in plan.ops if op.kind == "hide")
    creates = sum(1 for op in plan.ops if op.kind == "create")
    for op in plan.ops:
        if op.kind in SAFE_OPS:
            continue
        if op.kind == "create" and op.tx is not None and op.tx.type == "transfer" and hides >= 2:
            continue
        # Abgang + Rückfluss auf demselben Konto → Netzwerkgebühr (ersetzt je Paar genau zwei vorhandene Buchungen)
        if op.kind == "create" and op.tx is not None and op.tx.type == "withdrawal" and not op.tx.to_asset \
                and (op.tx.tag or "").lower() == "fee" and plan.finding.data.get("type") == "roundtrip" \
                and hides == 2 * creates:
            continue
        # gekürzte Ausbuchung: ersetzt eine ausgeblendete Ausbuchung gleicher Art durch eine kleinere Menge
        if op.kind == "create" and op.tx is not None and op.tx.type == "withdrawal" and not op.tx.to_asset \
                and (op.tx.tag or "").lower() in ("lost", "stolen", "burn") and hides >= 2:
            continue
        return False
    return True


def enabled(ctx: Any) -> bool:
    return bool(ctx.settings.get("autopilot.enabled", False))


def _tasks(ctx: Any, report: Any) -> list[Any]:
    return [f for f in TR.open_findings(ctx, report) if not TR.is_hint(f)]


def after_import(ctx: Any, trigger: str = "import") -> None:
    """Hook nach Import/Übernahme: im Hintergrund (im Betrieb) bzw. sofort (ohne Scheduler, z. B. Tests). Scheitert
    nie in den Aufrufer hinein."""
    def job() -> None:
        try:
            run(ctx, trigger)
        except Exception:  # pragma: no cover - Absicherung
            log.exception("Autopilot-Lauf fehlgeschlagen")
        try:
            from app.diagnosis import clarify

            clarify.run(ctx, trigger)  # Konfliktklärung nach dem Autopilot (Übernahme nur nach dessen Einstellung)
        except Exception:  # pragma: no cover - Absicherung
            log.exception("Konfliktklärung fehlgeschlagen")
        finally:
            ctx.db.close_thread_conn()

    if getattr(ctx, "scheduler", None) is not None:
        threading.Thread(target=job, name="autopilot", daemon=True).start()
    else:
        job()


def last_runs(db: Any, limit: int = 10) -> list[dict[str, Any]]:
    out = []
    for r in db.q("SELECT * FROM autopilot_run ORDER BY id DESC LIMIT ?", (limit,)):
        try:
            s = json.loads(r["summary_json"] or "{}")
        except ValueError:
            s = {}
        out.append({"id": r["id"], "token": r["token"], "trigger": r["trigger"], "status": r["status"],
                    "created_at": r["created_at"], "reverted_at": r["reverted_at"], "s": s})
    return out


def run(ctx: Any, trigger: str = "manual", *, force: bool = False) -> dict[str, Any] | None:
    """Ein Durchgang. ``force``: Aktionen auch ohne eingeschalteten Autopilot (nur ausdrücklich per Knopf)."""
    if not _RUN.acquire(blocking=False):
        return None
    try:
        return _run(ctx, trigger, force)
    finally:
        _RUN.release()


def _run(ctx: Any, trigger: str, force: bool) -> dict[str, Any]:
    token = uuid.uuid4().hex[:10]
    on = enabled(ctx) or force
    prev = last_runs(ctx.db, 1)
    prev_ids = set((prev[0]["s"].get("open_ids") if prev else None) or [])
    summary: dict[str, Any] = {"enabled": on, "rules": {"count": 0, "token": ""}, "fixes": {"applied": 0}}
    status = "ok"
    try:
        if on and ctx.settings.get("autopilot.rules", True):
            applied = R.apply_rules(ctx)
            summary["rules"] = {"count": applied.count, "token": applied.token, "by_rule": applied.by_rule,
                                "skipped_changed": applied.skipped_changed}
        if on and (ctx.settings.get("autopilot.fixes", False) or ctx.settings.get("autopilot.roundtrips", False)):
            summary["fixes"] = _fixes(ctx)
    except Exception as e:  # Aktionen scheitern nie in den Bericht hinein
        log.exception("Autopilot: Aktion fehlgeschlagen")
        status = "error"
        summary["error"] = f"{type(e).__name__}: {e}"[:300]
    report = report_for(ctx)
    tasks = _tasks(ctx, report)
    hints = [f for f in TR.open_findings(ctx, report) if TR.is_hint(f)]
    ids = [f.id for f in tasks]
    new = [f for f in tasks if f.id not in prev_ids] if prev else []
    by_kind: dict[str, int] = {}
    for f in tasks:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    summary.update(
        tasks=len(tasks), hints=len(hints), by_kind=by_kind,
        new=[{"id": f.id, "title": f.title[:160], "kind": f.kind, "status": f.status} for f in new[:20]],
        n_new=len(new), n_gone=len(prev_ids - set(ids)) if prev else 0, first=not prev,
        open_ids=ids[:MAX_IDS], at=datetime.now(UTC).isoformat(timespec="seconds"))
    ctx.db.x("INSERT INTO autopilot_run(token, trigger, status, summary_json, created_at) VALUES (?,?,?,?,?)",
             (token, trigger, status, json.dumps(summary, ensure_ascii=False), A._now()))
    ctx.db.x("DELETE FROM autopilot_run WHERE id NOT IN (SELECT id FROM autopilot_run ORDER BY id DESC LIMIT ?)",
             (KEEP_RUNS,))
    summary["token"] = token
    log.info("Autopilot (%s): %s Aufgaben, %s Hinweise, Regeln %s, Korrekturen %s", trigger, len(tasks), len(hints),
             summary["rules"]["count"], summary["fixes"].get("applied", 0))
    return summary


def _fixes(ctx: Any) -> dict[str, Any]:
    """Beweisbare Korrekturen (siehe Modul-Doku). Konservativ: bei jedem Zweifel nichts tun und den Grund melden.
    Die Befundgruppen (Netto-Gebühren aus Abgang und Rückfluss getrennt von Dubletten/Transfers) werden je für sich
    geprüft: ein Hindernis in einer Gruppe hält die andere nicht auf."""
    out: dict[str, Any] = {"applied": 0, "bulk_id": "", "bulk_ids": [], "skipped": []}
    report = report_for(ctx)
    open_ids = {f.id for f in TR.open_findings(ctx, report, cases=True)}
    level = LEVELS.get(str(ctx.settings.get("autopilot.fixes_level", "eindeutig")), LEVELS["eindeutig"])
    recon_on = bool(ctx.settings.get("autopilot.fixes", False))
    rt_on = bool(ctx.settings.get("autopilot.roundtrips", False))
    groups: dict[str, list[str]] = {}
    for f, conf, _label in B.candidates(report):
        if f.id not in open_ids:
            continue
        if f.kind == "roundtrip":
            # eigene Freigabe: belegt und wahrscheinlich (ohne Vorbehalt); „Verdacht“ (vereinzelt) nie automatisch
            if (rt_on and conf in LEVELS["hoch plausibel"]) or (recon_on and conf in level):
                groups.setdefault("roundtrip", []).append(f.id)
        elif recon_on and conf in level:
            groups.setdefault("recon", []).append(f.id)
    for name in ("recon", "roundtrip"):
        ids = groups.get(name)
        if not ids:
            continue
        if name != "recon" or out["bulk_ids"]:
            report = report_for(ctx)  # nach einer Gruppe neu rechnen
        _fix_group(ctx, report, ids, out)
    out["skipped"] = out["skipped"][:10]
    if out["bulk_ids"]:
        out["bulk_id"] = out["bulk_ids"][0]
    return out


def _fix_group(ctx: Any, report: Any, cand: list[str], out: dict[str, Any]) -> None:
    bp = B.plan(ctx, cand[:MAX_FIXES], include_review=False, report=report, with_effects=False)
    safe = [it.finding.id for it in bp.ready if it.plan is not None and _safe(it.plan)]
    for it in bp.items:
        if it.finding.id not in safe:
            why = it.reason or "Plan enthält neue Buchungen, die nicht aus genau zwei vorhandenen entstehen"
            out["skipped"].append({"title": it.finding.title[:120], "why": why[:160]})
    if not safe:
        return
    final = B.plan(ctx, safe, include_review=False, report=report, with_effects=True)
    ready = [it.finding.id for it in final.ready]
    eff = final.effects
    if len(ready) != len(safe) or eff is None or eff.negative_new or eff.issues_new or eff.new:
        out["note"] = ("Keine automatische Korrektur: gemeinsame Wirkung nicht unbedenklich "
                       "(Ausschluss, negativer Bestand oder neue Befunde) – bitte in der Sammelbearbeitung prüfen.")
        return
    res = B.execute(ctx, ready, final.token)
    if not res.ok:
        out["note"] = "; ".join(res.errors)[:200]
        return
    out["applied"] += len(res.decision_ids)
    out["bulk_ids"].append(res.bulk_id)


def undo(ctx: Any, token: str) -> A.Result:
    """Einen Lauf gemeinsam zurücknehmen: zuerst Korrekturen (bei Konflikt nichts), dann Regelmarkierungen."""
    row = next((r for r in last_runs(ctx.db, KEEP_RUNS) if r["token"] == token), None)
    if row is None:
        return A.Result(errors=["Unbekannter Lauf."])
    if row["status"] == "reverted":
        return A.Result(errors=["Dieser Lauf ist bereits zurückgenommen."])
    s = row["s"]
    msgs = []
    fx = s.get("fixes") or {}
    for bulk_id in fx.get("bulk_ids") or ([fx["bulk_id"]] if fx.get("bulk_id") else []):
        res = B.undo(ctx, bulk_id)
        if not res.ok:
            return A.Result(errors=res.errors)
        msgs.append(res.message)
    rtoken = (s.get("rules") or {}).get("token")
    if rtoken:
        n = R.undo(ctx, rtoken)
        msgs.append(f"{n} Regel-Markierung(en) zurückgenommen.")
    if not msgs:
        return A.Result(errors=["Dieser Lauf hat nichts verändert."])
    ctx.db.x("UPDATE autopilot_run SET status='reverted', reverted_at=? WHERE token=?", (A._now(), token))
    return A.Result(ok=True, message=" ".join(msgs))


def kind_label(kind: str) -> str:
    return KINDS.get(kind, kind)
