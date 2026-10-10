"""Referenzbestände (M27): vom Nutzer bestätigte Kontostände zum Stichtag – Prüfwert für den Soll-Ist-Abgleich.

Ein Referenzbestand ist **keine Buchung**: Er wirkt nie auf Bestand, Lots, Performance oder Steuer und wird von der
Diagnose nur verglichen (Soll aus allen wirksamen Buchungen bis zum selben Stichtag). Fehlt er, gilt der Ist-Bestand
als unbekannt – nicht als 0. Anlegen und Entfernen werden im Änderungsprotokoll (``journal_log``) festgehalten;
„Entfernen“ setzt nur den Status, der Eintrag bleibt nachvollziehbar.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app.util.numbers import parse_number
from app.util.timeutil import iso, today_local

SOURCES = {"statement": "Kontoauszug", "api": "Anzeige beim Anbieter", "other": "sonstiger Beleg"}


@dataclass
class Result:
    ok: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)


def add(ctx: Any, account: str, asset: str, qty: str, as_of: str, source: str = "statement",
        note: str = "") -> Result:
    account, asset = (account or "").strip(), (asset or "").strip()
    errors: list[str] = []
    pf = ctx.recorded_portfolio()
    accounts = set(pf.all_accounts()) | set(pf.accounts) if pf is not None else set()
    if not account or (accounts and account not in accounts):
        errors.append("Bitte ein vorhandenes Konto wählen.")
    if not asset or (pf is not None and asset not in pf.assets):
        errors.append("Bitte ein vorhandenes Asset (Asset-ID) wählen – gleichnamige Tokens anderer Netzwerke sind "
                      "eigene Assets.")
    q = parse_number(qty)
    if q is None:
        errors.append("Bestand fehlt oder ist keine Zahl (0 ist ein gültiger Bestand).")
    try:
        d = date.fromisoformat((as_of or "").strip()[:10])
    except ValueError:
        d = None
        errors.append("Stichtag fehlt oder ist ungültig.")
    if d is not None and d > today_local():
        errors.append("Der Stichtag liegt in der Zukunft.")
    src = source if source in SOURCES else "other"
    if errors:
        return Result(errors=errors)
    stamp = iso(datetime.now(UTC)) or ""
    row = {"account": account, "asset_id": asset, "qty": str(q), "as_of": d.isoformat() if d else "", "source": src,
           "note": (note or "").strip()[:300] or None}
    with ctx.db.transaction() as c:
        c.execute("INSERT INTO reference_balance(account, asset_id, qty, as_of, source, note, status, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?)", (row["account"], row["asset_id"], row["qty"], row["as_of"], src,
                                               row["note"], "active", stamp))
        c.execute("INSERT INTO journal_log(at, action, ref, before_json, after_json) VALUES (?,?,?,?,?)",
                  (stamp, "reference_add", f"{account}|{asset}", None, json.dumps(row, ensure_ascii=False)))
    return Result(ok=True, message=f"Referenzbestand {account} · {asset} zum {d:%d.%m.%Y} hinterlegt (Prüfwert, "
                                   "keine Buchung).")


def remove(ctx: Any, rid: int) -> Result:
    r = ctx.db.q1("SELECT * FROM reference_balance WHERE id=?", (rid,))
    if r is None or r["status"] != "active":
        return Result(errors=["Referenzbestand nicht (mehr) vorhanden."])
    stamp = iso(datetime.now(UTC)) or ""
    with ctx.db.transaction() as c:
        c.execute("UPDATE reference_balance SET status='deleted', deleted_at=? WHERE id=? AND status='active'",
                  (stamp, rid))
        c.execute("INSERT INTO journal_log(at, action, ref, before_json, after_json) VALUES (?,?,?,?,?)",
                  (stamp, "reference_delete", f"{r['account']}|{r['asset_id']}", json.dumps(dict(r)), None))
    return Result(ok=True, message=f"Referenzbestand {r['account']} · {r['asset_id']} entfernt.")
