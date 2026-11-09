"""Discovery-Nutzerdaten im Gesamtexport (``state.json`` → ``discovery``) und ihre Wiederherstellung.

Enthalten: Unternehmen (Status, Notiz, Herkunft, Aliasse), Signale, alle Analyseversionen (vollständiger Bericht),
Meilensteine, KI-Teilanalysen, Kandidaten (auch verworfene), Job-Historie, KI-Aufrufprotokoll, Discovery-
Verbrauch je Tag und der Vorwärtstest (0.110: Vorschauen und Analysen mit Stichtag, Kurs und Score). Nicht
enthalten: das Research-Universum und der SEC-Zwischenspeicher – beides wird aus öffentlichen
SEC-Daten neu aufgebaut (Fortschritt des Screenings beginnt nach einer Wiederherstellung von vorn).

Wiederherstellung (eine Transaktion, wiederholbar ohne Dubletten): Unternehmen über CIK bzw. Ticker zuordnen, IDs neu
vergeben und alle Verweise umschreiben; vorhandene Einträge bleiben unverändert. Aktive Jobs des alten Systems werden
als „unterbrochen“ übernommen.
"""

from __future__ import annotations

import json
from typing import Any

FORMAT_VERSION = 1


def _rows(db: Any, sql: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.q(sql)]


def collect(db: Any, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "version": FORMAT_VERSION,
        "companies": _rows(db, "SELECT * FROM disc_company ORDER BY id"),
        "aliases": _rows(db, "SELECT * FROM disc_alias ORDER BY company_id, kind, value_norm"),
        "signals": _rows(db, "SELECT * FROM disc_signal ORDER BY id"),
        "analyses": _rows(db, "SELECT * FROM disc_analysis ORDER BY id"),
        "milestones": _rows(db, "SELECT * FROM disc_milestone ORDER BY id"),
        "ai_parts": _rows(db, "SELECT * FROM disc_ai_part ORDER BY id"),
        "candidates": _rows(db, "SELECT * FROM disc_candidate ORDER BY id"),
        "jobs": _rows(db, "SELECT * FROM disc_job ORDER BY id"),
        "ai_calls": _rows(db, "SELECT * FROM disc_ai_call ORDER BY id"),
        "ai_usage": _rows(db, "SELECT * FROM disc_ai_usage ORDER BY day"),
        "forward": _rows(db, "SELECT * FROM disc_forward ORDER BY id"),
    }
    if full:  # Ansicht im Browser: Berichte entpackt
        for a in out["analyses"]:
            a["result"] = json.loads(a.pop("result_json") or "{}")
    return out


def _cols(c: Any, table: str) -> set[str]:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def _ins(c: Any, table: str, row: dict[str, Any], cols_ok: set[str]) -> int:
    cols = [k for k in row if k in cols_ok and k != "id"]
    cur = c.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [row[k] for k in cols])
    return int(cur.lastrowid or 0)


def restore(c: Any, data: Any) -> int:
    """Discovery-Daten in die offene Transaktion ``c`` übernehmen; Rückgabe: Anzahl neu angelegter Einträge."""
    if not isinstance(data, dict):
        return 0
    n = 0
    cmap: dict[int, int] = {}
    cols = {t: _cols(c, t) for t in ("disc_company", "disc_signal", "disc_analysis", "disc_milestone", "disc_ai_part",
                                     "disc_candidate", "disc_job", "disc_ai_call", "disc_forward")}
    for co in data.get("companies") or []:
        if not isinstance(co, dict) or not co.get("ticker"):
            continue
        row = None
        if co.get("cik"):
            row = c.execute("SELECT id FROM disc_company WHERE cik=?", (co["cik"],)).fetchone()
        if row is None:
            row = c.execute("SELECT id FROM disc_company WHERE ticker=?", (co["ticker"],)).fetchone()
        if row is not None:
            cmap[int(co["id"])] = int(row[0])
            continue
        cmap[int(co["id"])] = _ins(c, "disc_company", co, cols["disc_company"])
        n += 1
    for a in data.get("aliases") or []:
        if isinstance(a, dict) and int(a.get("company_id") or 0) in cmap:
            n += c.execute("INSERT OR IGNORE INTO disc_alias(kind, value_norm, company_id, value) VALUES (?,?,?,?)",
                           (a["kind"], a["value_norm"], cmap[int(a["company_id"])], a["value"])).rowcount or 0
    for s in data.get("signals") or []:
        old = int(s.get("company_id") or 0) if isinstance(s, dict) else 0
        if old not in cmap:
            continue
        new = cmap[old]
        fp = s["fp"] if new == old else f"r{new}:{s['fp']}"[:64]
        if c.execute("SELECT 1 FROM disc_signal WHERE fp IN (?, ?)", (s["fp"], fp)).fetchone() is None:
            _ins(c, "disc_signal", {**s, "company_id": new, "fp": fp}, cols["disc_signal"])
            n += 1
    jmap: dict[int, int] = {}
    for j in data.get("jobs") or []:
        if not isinstance(j, dict):
            continue
        row = c.execute("SELECT id FROM disc_job WHERE kind=? AND created_at=? AND slot=?",
                        (j.get("kind"), j.get("created_at"), j.get("slot"))).fetchone()
        if row is not None:
            jmap[int(j["id"])] = int(row[0])
            continue
        status = "unterbrochen" if j.get("status") in ("geplant", "laeuft") else j.get("status")
        jmap[int(j["id"])] = _ins(c, "disc_job", {**j, "status": status,
                                                  "company_id": cmap.get(int(j.get("company_id") or 0),
                                                                         j.get("company_id"))}, cols["disc_job"])
        n += 1
    amap: dict[int, int] = {}
    for a in data.get("analyses") or []:
        old = int(a.get("company_id") or 0) if isinstance(a, dict) else 0
        if old not in cmap:
            continue
        row = c.execute("SELECT id FROM disc_analysis WHERE company_id=? AND created_at=? AND input_hash=?",
                        (cmap[old], a.get("created_at"), a.get("input_hash"))).fetchone()
        if row is not None:
            amap[int(a["id"])] = int(row[0])
            continue
        res = a.get("result_json") or json.dumps(a.get("result") or {}, ensure_ascii=False)
        amap[int(a["id"])] = _ins(c, "disc_analysis", {**a, "company_id": cmap[old], "result_json": res,
                                                       "job_id": jmap.get(int(a.get("job_id") or 0))},
                                  cols["disc_analysis"])
        n += 1
    for m in data.get("milestones") or []:
        if not isinstance(m, dict) or int(m.get("analysis_id") or 0) not in amap:
            continue
        new_a = amap[int(m["analysis_id"])]
        if c.execute("SELECT 1 FROM disc_milestone WHERE analysis_id=? AND text=?", (new_a, m.get("text"))).fetchone():
            continue
        _ins(c, "disc_milestone", {**m, "analysis_id": new_a, "company_id": cmap.get(int(m["company_id"]))},
             cols["disc_milestone"])
        n += 1
    for p in data.get("ai_parts") or []:
        old = int(p.get("company_id") or 0) if isinstance(p, dict) else 0
        if old not in cmap or c.execute("SELECT 1 FROM disc_ai_part WHERE company_id=? AND part=? AND created_at=?",
                                        (cmap[old], p.get("part"), p.get("created_at"))).fetchone():
            continue
        _ins(c, "disc_ai_part", {**p, "company_id": cmap[old]}, cols["disc_ai_part"])
        n += 1
    for k in data.get("candidates") or []:
        if not isinstance(k, dict) or c.execute("SELECT 1 FROM disc_candidate WHERE created_at=? AND path=? AND "
                                                "COALESCE(name, '')=?", (k.get("created_at"), k.get("path"),
                                                                         k.get("name") or "")).fetchone():
            continue
        _ins(c, "disc_candidate", {**k, "company_id": cmap.get(int(k.get("company_id") or 0)),
                                   "job_id": jmap.get(int(k.get("job_id") or 0))}, cols["disc_candidate"])
        n += 1
    for a in data.get("ai_calls") or []:
        if not isinstance(a, dict) or c.execute("SELECT 1 FROM disc_ai_call WHERE started_at=? AND step=? AND "
                                                "COALESCE(input_fp, '')=?", (a.get("started_at"), a.get("step"),
                                                                             a.get("input_fp") or "")).fetchone():
            continue
        _ins(c, "disc_ai_call", {**a, "company_id": cmap.get(int(a.get("company_id") or 0)),
                                 "job_id": jmap.get(int(a.get("job_id") or 0))}, cols["disc_ai_call"])
        n += 1
    for u in data.get("ai_usage") or []:
        if isinstance(u, dict) and u.get("day"):
            n += c.execute("INSERT OR IGNORE INTO disc_ai_usage(day, calls, input_tokens, output_tokens) VALUES "
                           "(?,?,?,?)", (u["day"], int(u.get("calls") or 0), int(u.get("input_tokens") or 0),
                                         int(u.get("output_tokens") or 0))).rowcount or 0
    for f in data.get("forward") or []:  # je Unternehmen, Art und Tag eindeutig (Index) – wiederholbar
        old = int(f.get("company_id") or 0) if isinstance(f, dict) else 0
        if old not in cmap or not f.get("kind") or not f.get("day"):
            continue
        if c.execute("SELECT 1 FROM disc_forward WHERE company_id=? AND kind=? AND day=?",
                     (cmap[old], f["kind"], f["day"])).fetchone():
            continue
        _ins(c, "disc_forward", {**f, "company_id": cmap[old]}, cols["disc_forward"])
        n += 1
    return n
