"""Stichtag festschreiben, Freigabe je Konto, Rücknahme (0.99.0).

Ablauf: Vorschau (``service.preview``) → je Konto freigeben → festschreiben. Festschreiben ist *eine*
Schreibtransaktion, gebunden an den angezeigten Datenstand (Token). Es verändert keine Buchung: Das Archiv wird
lediglich geschützt (``guard``), der Eröffnungsstand mit Prüfsumme je Konto protokolliert. Die Rücknahme stellt den
Zustand davor her.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, date, datetime, time
from typing import Any

from app.cutover import after, guard
from app.cutover import core as CO
from app.util.timeutil import iso, local_tz

log = logging.getLogger(__name__)
_LOCK = threading.Lock()


def cutoff_utc(cutoff: date) -> str:
    """Beginn des Stichtags (Ortszeit) als UTC-Zeitpunkt – Vergleichswert für ``ts_utc``."""
    return iso(datetime.combine(cutoff, time(0), tzinfo=local_tz()).astimezone(UTC))


def _now() -> str:
    return iso(datetime.now(UTC))


def token_for(ctx: Any, cutoff: date, accounts: list[dict[str, Any]], ok: bool) -> str:
    """Bindung der Freigabe an den angezeigten Stand: Stichtag, Datenstand und Prüfsummen aller Konten."""
    from app.diagnosis import actions as DA

    payload = [cutoff.isoformat(), DA.data_version(ctx.db), ok, sorted((a["account"], a["fingerprint"])
                                                                        for a in accounts)]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:32]


def overall_fingerprint(accounts: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(sorted((a["account"], a["fingerprint"]) for a in accounts)).encode()
                          ).hexdigest()[:24]


def fix(ctx: Any, cutoff: date, token: str, approved: set[str]) -> dict[str, Any]:
    """Stichtag festschreiben. Voraussetzungen: kein aktiver Stichtag, alle Pflichtprüfungen bestanden, **jedes** Konto
    mit Eröffnungsstand freigegeben, Token passt zum aktuellen Stand. Rückgabe: ``errors`` (leer bei Erfolg)."""
    from app.cutover import service as CS
    from app.diagnosis import actions as DA

    db = ctx.db
    with _LOCK:
        if guard.active(db) is not None:
            return {"errors": ["Es ist bereits ein Stichtag festgeschrieben – erst zurücknehmen."]}
        version = DA.data_version(db)
        pv = CS.preview(ctx, cutoff)
        if not pv.get("has_data"):
            return {"errors": ["Keine Daten vorhanden."]}
        if not pv["ok"]:
            return {"errors": ["Die Pflichtprüfungen sind nicht bestanden – nichts festgeschrieben."]}
        if token != token_for(ctx, cutoff, pv["accounts"], pv["ok"]):
            return {"errors": ["Die Vorschau ist nicht mehr aktuell (Daten oder Belege haben sich geändert) – bitte "
                               "neu laden und erneut freigeben."]}
        missing = [a["account"] for a in pv["accounts"] if a["account"] not in approved]
        if missing:
            return {"errors": [f"Nicht alle Konten sind freigegeben ({len(missing)} offen, z. B. {missing[0]})."]}
        ov = pv["overview"]
        summary = {"lots": ov["n_lots"], "cost": str(ov["total_cost"]), "archive_txs": ov["archive"]["txs"],
                   "active_txs": ov["archive"]["active_txs"], "corrections": ov["archive"]["corrections"],
                   "accounts": len(pv["accounts"])}
        m = CO.models(ctx, cutoff)[0]
        opts = {"scope": m.opts.scope, "method": m.opts.method, "fiat_off": bool(m.opts.fiat_off)}
        checks = [{"label": c.label, "ok": c.ok} for c in pv["checks"]]
        stamp = _now()
        with db.transaction() as c:
            DA.assert_version(c, version)  # erste Anweisung: Daten seit der Vorschau unverändert
            cur = c.execute(
                "INSERT INTO cutover(cutoff, cutoff_utc, status, options_json, fingerprint, summary_json, checks_json, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (cutoff.isoformat(), cutoff_utc(cutoff), "active", json.dumps(opts), overall_fingerprint(
                    pv["accounts"]), json.dumps(summary), json.dumps(checks, ensure_ascii=False), stamp))
            cid = int(cur.lastrowid)  # type: ignore[arg-type]
            for a in pv["accounts"]:
                c.execute("INSERT INTO cutover_account(cutover_id, account, status, fingerprint, approved_at) "
                          "VALUES (?,?,?,?,?)", (cid, a["account"], a["status"], a["fingerprint"], stamp))
            c.execute("INSERT INTO journal_log(at, action, ref, before_json, after_json) VALUES (?,?,?,?,?)",
                      (stamp, "cutover_fix", f"cutover:{cid}", None, json.dumps({"cutoff": cutoff.isoformat(),
                                                                                  **summary})))
    ctx.invalidate_data()
    after.refresh_cases(ctx)
    log.info("Stichtag %s festgeschrieben: %d Konten, %d Lots, Archiv %d Buchungen", cutoff, summary["accounts"],
             summary["lots"], summary["archive_txs"])
    return {"errors": [], "id": cid}


def undo(ctx: Any) -> dict[str, Any]:
    """Stichtag zurücknehmen: Das Archiv ist wieder bearbeitbar, alle Auswertungen laufen über den vollen Stand."""
    db = ctx.db
    with _LOCK:
        row = guard.active(db)
        if row is None:
            return {"errors": ["Es ist kein Stichtag festgeschrieben."]}
        stamp = _now()
        with db.transaction() as c:
            c.execute("UPDATE cutover SET status='undone', undone_at=? WHERE id=? AND status='active'",
                      (stamp, row["id"]))
            c.execute("INSERT INTO journal_log(at, action, ref, before_json, after_json) VALUES (?,?,?,?,?)",
                      (stamp, "cutover_undo", f"cutover:{row['id']}", None, json.dumps({"cutoff": row["cutoff"]})))
    ctx.invalidate_data()
    after.refresh_cases(ctx)
    log.info("Stichtag %s zurückgenommen", row["cutoff"])
    return {"errors": []}


def state(ctx: Any) -> dict[str, Any] | None:
    """Zustand des aktiven Stichtags: Eröffnungsstand gegen die Freigaben prüfen (ohne Seiteneffekt)."""
    from app.cutover import service as CS

    row = guard.active(ctx.db)
    if row is None:
        return None
    cutoff = date.fromisoformat(row["cutoff"])
    approved = {r["account"]: r for r in ctx.db.q("SELECT * FROM cutover_account WHERE cutover_id=?", (row["id"],))}
    pv = CS.preview(ctx, cutoff)
    now = {a["account"]: a for a in pv.get("accounts", [])} if pv.get("has_data") else {}
    changed = []
    for acc in sorted(set(approved) | set(now)):
        a, n = approved.get(acc), now.get(acc)
        if a is None:
            changed.append({"account": acc, "why": "neu im Eröffnungsstand (nicht freigegeben)", "status": n["status"]})
        elif n is None:
            changed.append({"account": acc, "why": "kein Bestand mehr zum Stichtag", "status": a["status"]})
        elif a["fingerprint"] != n["fingerprint"]:
            changed.append({"account": acc, "why": "Eröffnungsstand seit der Freigabe verändert",
                            "status": n["status"]})
    return {"id": row["id"], "cutoff": cutoff, "created_at": row["created_at"], "changed": changed,
            "ok": bool(pv.get("ok")), "checks": pv.get("checks", []), "accounts": pv.get("accounts", []),
            "approved": len(approved), "preview": pv}


def reapprove(ctx: Any, accounts: set[str]) -> dict[str, Any]:
    """Neuen Eröffnungsstand einzelner Konten ausdrücklich freigeben (z. B. nach einer Stichtag-Korrektur)."""
    from app.cutover import service as CS

    row = guard.active(ctx.db)
    if row is None:
        return {"errors": ["Es ist kein Stichtag festgeschrieben."]}
    pv = CS.preview(ctx, date.fromisoformat(row["cutoff"]))
    if not pv.get("has_data") or not pv["ok"]:
        return {"errors": ["Die Pflichtprüfungen sind nicht bestanden."]}
    now = {a["account"]: a for a in pv["accounts"]}
    stamp = _now()
    with ctx.db.transaction() as c:
        for acc in accounts:
            n = now.get(acc)
            if n is None:
                c.execute("DELETE FROM cutover_account WHERE cutover_id=? AND account=?", (row["id"], acc))
                continue
            c.execute("INSERT INTO cutover_account(cutover_id, account, status, fingerprint, approved_at) "
                      "VALUES (?,?,?,?,?) ON CONFLICT(cutover_id, account) DO UPDATE SET status=excluded.status, "
                      "fingerprint=excluded.fingerprint, approved_at=excluded.approved_at",
                      (row["id"], acc, n["status"], n["fingerprint"], stamp))
        fp = overall_fingerprint(list(pv["accounts"]))
        c.execute("UPDATE cutover SET fingerprint=? WHERE id=?", (fp, row["id"]))
    ctx.invalidate_data()
    after.refresh_cases(ctx)
    return {"errors": []}
