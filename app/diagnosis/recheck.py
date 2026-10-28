"""Übernommene Buchungen erneut prüfen: Herkunft zeigen, vormerken oder zurück in den Prüfstapel legen.

Häufig verursacht gerade die Buchung eine Abweichung, die man früher mit „übernehmen“ bestätigt hat (Dublette, falscher
Betrag). Statt den ganzen Stapel zurückzunehmen, gibt es für **eine** Buchung zwei Wege:

* **Vormerken** (Standard): Die Buchung bleibt unverändert; sie erscheint als Aufgabe „erneut prüfen“
  (``/quality/erneut``). Die Markierung ist jederzeit entfernbar.
* **Zurück in den Prüfstapel**: Die Buchung wird zurückgenommen (Status „rückgängig gemacht“, nicht gelöscht), ihre
  Zeile steht im Stapel wieder offen und ohne Entscheidung. Der Vorzustand ist protokolliert und lässt sich
  zurückholen, solange die Zeile nicht inzwischen anders entschieden wurde.

Schutz: nie automatisch; nur für Buchungen, die ein aktueller Befund (belegt, wahrscheinlich oder Verdacht) nennt; nur
für aus dem Prüfstapel übernommene App-Buchungen, deren Ursprungszeile noch existiert; Protokoll begrenzt.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)
MARKS_KEY = "recheck_marks"
LOG_KEY = "recheck_log"
LOG_LIMIT = 200
NAMING_STATUS = ("belegt", "wahrscheinlich", "verdacht")
NOT_NAMED = "Kein aktueller Befund nennt diese Buchung – erneut prüfen ist nur bei einem Befund möglich."


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def marks(db: Any) -> dict[str, dict[str, Any]]:
    raw = db.get_state(MARKS_KEY)
    return raw if isinstance(raw, dict) else {}


def entries(db: Any) -> list[dict[str, Any]]:
    raw = db.get_state(LOG_KEY)
    return raw if isinstance(raw, list) else []


def info(ctx: Any, tx_id: str, _marks: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Herkunft einer aktiven App-Buchung aus dem Prüfstapel (oder ``None``: keine solche Buchung)."""
    r = ctx.db.q1(
        "SELECT j.tx_id, j.batch_id, j.source, r.idx, r.line, b.filename, b.kind, b.committed_at "
        "FROM journal_tx j LEFT JOIN csv_batch b ON b.id=j.batch_id "
        "LEFT JOIN csv_row r ON r.batch_id=j.batch_id AND r.tx_id=j.tx_id "
        "WHERE j.tx_id=? AND j.status='active' AND j.source <> 'transfer'", (tx_id,))
    if r is None or not r["batch_id"]:
        return None
    mk = (_marks if _marks is not None else marks(ctx.db)).get(tx_id)
    has_row = r["idx"] is not None and r["filename"] is not None
    where = (f"Prüfstapel {r['batch_id']}, Zeile {r['line'] or r['idx']}" if has_row
             else f"Prüfstapel {r['batch_id']} (Ursprungszeile nicht mehr vorhanden)")
    return {"tx_id": tx_id, "batch_id": int(r["batch_id"]), "idx": r["idx"], "line": r["line"],
            "file": r["filename"] or "", "kind": r["kind"] or "", "at": r["committed_at"] or "", "text": where,
            "can_reopen": has_row, "marked": mk is not None, "mark": mk,
            "url": f"/journal/csv/{r['batch_id']}"}


def named_by_finding(ctx: Any, tx_id: str) -> str:
    """Titel des ersten aktuellen Befunds, der die Buchung nennt (sonst leer)."""
    from app.diagnosis.engine import report_for

    for f in report_for(ctx).findings:
        if f.status in NAMING_STATUS and (any(t.tx_id == tx_id for t in f.txs)
                                          or any(tx_id in (a.tx_id, b.tx_id) for a, b, _w in f.pairs)):
            return f.title
    return ""


def mark(ctx: Any, tx_id: str, reason: str = "") -> dict[str, Any]:
    i = info(ctx, tx_id)
    if i is None:
        return {"errors": ["Die Buchung stammt nicht aus dem Prüfstapel oder zählt nicht mehr."]}
    title = named_by_finding(ctx, tx_id)
    if not title:
        return {"errors": [NOT_NAMED]}
    m = marks(ctx.db)
    m[tx_id] = {"at": _now(), "reason": (reason or title)[:200]}
    ctx.db.set_state(MARKS_KEY, m)
    return {"errors": [], "title": title}


def unmark(ctx: Any, tx_id: str) -> dict[str, Any]:
    m = marks(ctx.db)
    if m.pop(tx_id, None) is None:
        return {"errors": ["Keine Markierung vorhanden."]}
    ctx.db.set_state(MARKS_KEY, m)
    return {"errors": []}


def reopen(ctx: Any, tx_id: str) -> dict[str, Any]:
    """Buchung zurücknehmen, ihre Zeile im Prüfstapel wieder öffnen (ohne Entscheidung)."""
    from app.csvimport import batch as B
    from app.csvimport.service import csv_service

    i = info(ctx, tx_id)
    if i is None or not i["can_reopen"]:
        return {"errors": ["Die Ursprungszeile im Prüfstapel existiert nicht mehr – die Buchung lässt sich nicht "
                           "zurücklegen (stattdessen vormerken oder in den Buchungen bearbeiten)."]}
    title = named_by_finding(ctx, tx_id)
    if not title:
        return {"errors": [NOT_NAMED]}
    svc = csv_service(ctx)
    db = ctx.db
    bid, idx = i["batch_id"], int(i["idx"])
    row = db.q1("SELECT status, decision, tx_id FROM csv_row WHERE batch_id=? AND idx=?", (bid, idx))
    prev = {"status": row["status"], "decision": row["decision"], "tx_id": row["tx_id"]}
    from app.journal.service import _now as jnow

    stamp = jnow()
    with db.transaction() as c:
        n = B._revert_txs(c, svc, bid, [tx_id], stamp)
        if not n:
            return {"errors": ["Die Buchung ließ sich nicht zurücknehmen."]}
        c.execute("UPDATE csv_row SET decision=NULL WHERE batch_id=? AND idx=?", (bid, idx))
    svc.evaluate(bid)
    svc.refresh_status(bid)
    svc.journal.after_change()
    m = marks(db)
    m.pop(tx_id, None)
    db.set_state(MARKS_KEY, m)
    log_ = entries(db)
    eid = (max((e["id"] for e in log_), default=0)) + 1
    log_.append({"id": eid, "tx_id": tx_id, "batch_id": bid, "idx": idx, "prev": prev, "at": _now(), "title": title,
                 "undone": False})
    db.set_state(LOG_KEY, log_[-LOG_LIMIT:])
    return {"errors": [], "batch_id": bid, "id": eid}


def undo(ctx: Any, entry_id: int) -> dict[str, Any]:
    """Zurückgelegte Buchung wieder in Kraft setzen – nur, wenn Zeile und Buchung unverändert sind."""
    from app.csvimport.service import csv_service

    db = ctx.db
    log_ = entries(db)
    e = next((x for x in log_ if x.get("id") == entry_id and not x.get("undone")), None)
    if e is None:
        return {"errors": ["Eintrag nicht gefunden oder bereits zurückgeholt."]}
    bid, idx, tx_id = int(e["batch_id"]), int(e["idx"]), e["tx_id"]
    row = db.q1("SELECT status, decision, tx_id FROM csv_row WHERE batch_id=? AND idx=?", (bid, idx))
    jt = db.q1("SELECT status FROM journal_tx WHERE tx_id=?", (tx_id,))
    if row is None or jt is None or jt["status"] != "reverted" or row["status"] != "new" or row["tx_id"] is not None:
        return {"errors": ["Die Zeile wurde inzwischen anders entschieden oder gebucht – Zurückholen würde doppelt "
                           "buchen. Es wurde nichts geändert."]}
    svc = csv_service(ctx)
    from app.journal.service import _now as jnow

    stamp = jnow()
    with db.transaction() as c:
        c.execute("UPDATE journal_tx SET status='active', updated_at=? WHERE tx_id=? AND status='reverted'",
                  (stamp, tx_id))
        p = e["prev"]
        c.execute("UPDATE csv_row SET status=?, decision=?, tx_id=? WHERE batch_id=? AND idx=?",
                  (p["status"], p["decision"], p["tx_id"], bid, idx))
    svc.evaluate(bid)
    svc.refresh_status(bid)
    svc.journal.after_change()
    e["undone"] = True
    db.set_state(LOG_KEY, log_)
    return {"errors": []}
