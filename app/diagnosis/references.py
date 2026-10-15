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
from datetime import time as dtime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.util.numbers import parse_number
from app.util.timeutil import iso, local_tz, today_local

SOURCES = {"statement": "Kontoauszug", "api": "Anzeige beim Anbieter", "other": "sonstiger Beleg"}
# Umfang des Nachweises: Spot-/Funding-/Derivatekonto bzw. zusammengefasste Anzeige mehrerer Konten
SCOPES = {"spot": "Spot", "funding": "Funding", "derivatives": "Derivate (Wallet-Bestand)",
          "combined": "zusammengefasste Konten"}


@dataclass
class Result:
    ok: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)


def add(ctx: Any, account: str, asset: str, qty: str, as_of: str, source: str = "statement",
        note: str = "", time_of_day: str = "", tz: str = "", basis: str = "", *, subaccount: str = "",
        scope: str = "", qty_locked: str = "", qty_margin: str = "", qty_pnl: str = "",
        members: list[str] | None = None, case_key: str = "") -> Result:
    """Referenzbestand anlegen. ``time_of_day`` (HH:MM[:SS]) + ``tz`` (IANA, Vorgabe: fachliche Zeitzone) ergeben einen
    exakten Zeitpunkt; ohne Uhrzeit gilt das Ende des Stichtags (mit ``tz`` in dieser Zeitzone).

    Bestandsnachweis (Abschlussdialog der Klärung): ``scope`` – Spot, Funding, Derivate (Wallet-Bestand) oder
    zusammengefasste Konten (``members``, Summe der Konten); ``qty`` ist die Gesamtmenge im Umfang, ``qty_locked`` der
    gebundene Anteil davon. Bei Derivaten sind ``qty_margin`` (gebundene Margin) und ``qty_pnl`` (offenes Ergebnis) nur
    Angaben – sie fließen nie in den Vergleich mit dem Ledger ein, ein EUR-Gesamtwert ersetzt keine Assetmenge."""
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
    scope = (scope or "").strip()
    if scope and scope not in SCOPES:
        errors.append("Unbekannter Umfang des Nachweises.")
    extra: dict[str, str | None] = {}
    for name, val in (("qty_locked", qty_locked), ("qty_margin", qty_margin), ("qty_pnl", qty_pnl)):
        if (val or "").strip():
            n = parse_number(val)
            if n is None:
                errors.append(f"{name.replace('qty_', 'Menge ')}: keine Zahl.")
            else:
                extra[name] = str(n)
    if q is not None and extra.get("qty_locked") is not None and not (0 <= Decimal(extra["qty_locked"]) <= q):
        errors.append("Die gebundene Menge muss zwischen 0 und dem Gesamtbestand liegen.")
    member_list = [m.strip() for m in (members or []) if m.strip()]
    if scope == "combined":
        if len(member_list) < 2:
            errors.append("Für zusammengefasste Konten bitte mindestens zwei Konten angeben.")
        elif accounts and not set(member_list) <= accounts:
            errors.append("Mindestens eines der zusammengefassten Konten ist nicht vorhanden.")
    zone_name = (tz or "").strip() or None
    at_utc: str | None = None
    if zone_name:
        try:
            zone = ZoneInfo(zone_name)
        except Exception:
            errors.append("Unbekannte Zeitzone (z. B. Europe/Berlin, UTC).")
            zone = None
    else:
        zone = local_tz()
    tod = (time_of_day or "").strip()
    if tod and d is not None and zone is not None:
        try:
            parts = [int(x) for x in tod.split(":")]
            t = dtime(parts[0], parts[1] if len(parts) > 1 else 0, parts[2] if len(parts) > 2 else 0)
            at = datetime.combine(d, t, tzinfo=zone).astimezone(UTC)
            if at > datetime.now(UTC):
                errors.append("Der Zeitpunkt liegt in der Zukunft.")
            at_utc = iso(at)
            zone_name = zone_name or str(zone)
        except (ValueError, IndexError):
            errors.append("Uhrzeit ungültig (HH:MM).")
    if errors:
        return Result(errors=errors)
    stamp = iso(datetime.now(UTC)) or ""
    row = {"account": account, "asset_id": asset, "qty": str(q), "as_of": d.isoformat() if d else "", "source": src,
           "note": (note or "").strip()[:300] or None, "as_of_ts": at_utc, "tz": zone_name,
           "basis": basis if basis in ("booking", "value") else None,
           "subaccount": (subaccount or "").strip()[:60] or None,
           "scope": scope or None, "members": member_list if scope == "combined" else None,
           "case_key": (case_key or "").strip()[:64] or None, **extra}
    with ctx.db.transaction() as c:
        c.execute("INSERT INTO reference_balance(account, asset_id, qty, as_of, source, note, status, created_at, "
                  "as_of_ts, tz, basis, subaccount, scope, qty_locked, qty_margin, qty_pnl, members_json, case_key) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (row["account"], row["asset_id"], row["qty"], row["as_of"], src, row["note"], "active", stamp,
                   at_utc, zone_name, row["basis"], row["subaccount"], row["scope"], extra.get("qty_locked"),
                   extra.get("qty_margin"), extra.get("qty_pnl"),
                   json.dumps(member_list) if scope == "combined" else None, row["case_key"]))
        c.execute("INSERT INTO journal_log(at, action, ref, before_json, after_json) VALUES (?,?,?,?,?)",
                  (stamp, "reference_add", f"{account}|{asset}", None, json.dumps(row, ensure_ascii=False)))
    from app.diagnosis import clarify

    clarify.notify_change()  # neuer Nachweis = neue Evidenz → Folgelauf
    when = f"{d:%d.%m.%Y} {tod} ({zone_name})" if at_utc else f"{d:%d.%m.%Y}"
    return Result(ok=True, message=f"Referenzbestand {account} · {asset} zum {when} hinterlegt (Prüfwert, keine "
                                   "Buchung).")


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
