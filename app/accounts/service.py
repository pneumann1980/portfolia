"""Kontenverwaltung (Überlagerung ``account_meta``): Konten sind bisher nur Namen in Buchungen und Datenquellen.

* **Anlegen:** ein Konto ohne Buchungen (z. B. vor dem ersten Eintrag) – steht danach überall zur Auswahl.
* **Stammdaten:** Broker, Depotgruppe und Notiz je Konto; sie überlagern die Angaben des kuratierten Imports
  (``accounts.csv``), die Import-Datei bleibt unverändert.
* **Umbenennen:** neuer Name als Konto anlegen und das alte darauf zusammenführen (Vorschau, Bestätigung, Rückgängig
  über :mod:`app.assetchange.account_merge`) – keine Buchung wird verändert.
* **Löschen:** nur ein *leeres*, selbst angelegtes Konto ohne Datenquelle. Konten mit Buchungen werden über „Entfernen“
  ausgeblendet (:mod:`app.assetchange.account_remove`, reversibel), Datenquellen unter Einstellungen → Datenquellen.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.ledger.models import AccountInfo, Portfolio
from app.util.timeutil import iso, utcnow

log = logging.getLogger(__name__)
NAME_MAX = 80
FIELD_MAX = 120
NOTE_MAX = 300
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def meta_rows(db: Any) -> dict[str, Any]:
    try:
        return {r["account"]: r for r in db.q("SELECT * FROM account_meta ORDER BY account COLLATE NOCASE")}
    except Exception:  # Tabelle fehlt (DB vor Migration 28)
        return {}


def meta_names(db: Any) -> set[str]:
    return set(meta_rows(db))


def apply_meta(db: Any, pf: Portfolio | None) -> Portfolio | None:
    """Selbst angelegte Konten und Stammdaten über dem Import: Angaben des Nutzers gelten, wo sie gesetzt sind."""
    rows = meta_rows(db)
    if pf is None or not rows:
        return pf
    accounts = dict(pf.accounts)
    for name, r in rows.items():
        cur = accounts.get(name)
        broker = r["broker"] or (cur.broker if cur else None)
        group = r["depot_group"] or (cur.depot_group if cur else None)
        accounts[name] = AccountInfo(account=name, broker=broker, depot_group=group,
                                     extra={**(cur.extra if cur else {}), **({"note": r["note"]} if r["note"] else {})})
    return dataclasses.replace(pf, accounts=accounts)


def clean_name(raw: str | None) -> tuple[str, str | None]:
    name = re.sub(r"\s+", " ", (raw or "").strip())
    if not name:
        return "", "Bitte einen Kontonamen eingeben."
    if len(name) > NAME_MAX:
        return name, f"Der Kontoname darf höchstens {NAME_MAX} Zeichen lang sein."
    if _CTRL.search(name):
        return name, "Der Kontoname enthält unzulässige Zeichen."
    return name, None


def _field(raw: str | None, limit: int) -> str | None:
    v = re.sub(r"\s+", " ", _CTRL.sub(" ", raw or "")).strip()
    return v[:limit] or None


def _exists(known: list[str], name: str) -> str | None:
    low = name.casefold()
    return next((k for k in known if k.casefold() == low), None)


@dataclass
class Row:
    name: str
    broker: str = ""
    depot_group: str = ""
    note: str = ""
    n_txs: int = 0
    first: date | None = None
    last: date | None = None
    positions: int = 0
    sources: list[str] = field(default_factory=list)
    merged_from: list[str] = field(default_factory=list)
    own: bool = False  # selbst angelegt bzw. mit eigenen Stammdaten (account_meta)

    @property
    def empty(self) -> bool:
        return self.n_txs == 0

    @property
    def deletable(self) -> bool:
        return self.own and self.empty and not self.sources


def rows(ctx: Any) -> list[Row]:
    from app.assetchange import account_merge as AM

    pf = ctx.portfolio()
    led = ctx.ledger() if pf is not None else None
    meta = meta_rows(ctx.db)
    srcs = AM.source_accounts(ctx)
    out: dict[str, Row] = {}
    for name in AM.known_accounts(ctx):
        out[name] = Row(name=name)
    for name in meta:
        out.setdefault(name, Row(name=name))
    for t in (pf.txs if pf is not None else []):
        for a in {t.from_account, t.to_account}:
            if a and a in out:
                r = out[a]
                r.n_txs += 1
                d = t.ts.date() if hasattr(t.ts, "date") else None
                if d is not None:
                    r.first = d if r.first is None or d < r.first else r.first
                    r.last = d if r.last is None or d > r.last else r.last
    if led is not None:
        for (acc, _asset), q in led.balances.items():
            if acc in out and abs(q) > 0:
                out[acc].positions += 1
    for name, r in out.items():
        info = pf.accounts.get(name) if pf is not None else None
        r.broker = (info.broker or "") if info else ""
        r.depot_group = (info.depot_group or "") if info else ""
        m = meta.get(name)
        r.note = (m["note"] or "") if m else ""
        r.own = m is not None
        r.sources = [f"{d.name} ({d.provider_label})" for d in srcs.get(name, [])]
    try:
        for h in AM.history(ctx.db):
            if h["status"] == "active" and h["target_account"] in out:
                out[h["target_account"]].merged_from.append(h["alias_account"])
    except Exception as e:  # Verlauf ist nur Anzeige
        log.debug("Verlauf der Zusammenführungen nicht lesbar: %s", e)
    return sorted(out.values(), key=lambda r: r.name.casefold())


def create(ctx: Any, name: str, broker: str = "", depot_group: str = "", note: str = "") -> tuple[str, str | None]:
    """Neues, zunächst leeres Konto. Rückgabe: (bereinigter Name, Fehlertext)."""
    from app.assetchange import account_merge as AM

    name, err = clean_name(name)
    if err:
        return name, err
    hit = _exists(AM.known_accounts(ctx), name)
    if hit is not None:
        return name, (f"Ein Konto „{hit}“ gibt es schon" + (" (Schreibweise unterscheidet sich nur in Groß-/"
                                                            "Kleinschreibung)." if hit != name else "."))
    now = iso(utcnow())
    ctx.db.x("INSERT INTO account_meta(account, broker, depot_group, note, created_at, updated_at) "
             "VALUES (?,?,?,?,?,?)", (name, _field(broker, FIELD_MAX), _field(depot_group, FIELD_MAX),
                                      _field(note, NOTE_MAX), now, now))
    ctx.invalidate_overlay()
    return name, None


def update(ctx: Any, name: str, broker: str, depot_group: str, note: str) -> str | None:
    from app.assetchange import account_merge as AM

    if name not in set(AM.known_accounts(ctx)):
        return f"Konto „{name}“ ist nicht (mehr) vorhanden."
    now = iso(utcnow())
    vals = (_field(broker, FIELD_MAX), _field(depot_group, FIELD_MAX), _field(note, NOTE_MAX))
    ctx.db.x("INSERT INTO account_meta(account, broker, depot_group, note, created_at, updated_at) "
             "VALUES (?,?,?,?,?,?) ON CONFLICT(account) DO UPDATE SET broker=excluded.broker, "
             "depot_group=excluded.depot_group, note=excluded.note, updated_at=excluded.updated_at",
             (name, *vals, now, now))
    ctx.invalidate_overlay()
    return None


def start_rename(ctx: Any, old: str, new: str) -> tuple[str, str | None]:
    """Neuen Namen anlegen (mit den Stammdaten des alten Kontos); die Zusammenführung bestätigt der Nutzer in der
    Vorschau. Rückgabe: (neuer Name, Fehlertext)."""
    from app.assetchange import account_merge as AM

    new, err = clean_name(new)
    if err:
        return new, err
    known = AM.known_accounts(ctx)
    if old not in set(known):
        return new, f"Konto „{old}“ ist nicht (mehr) vorhanden."
    if new == old:
        return new, "Der neue Name ist gleich dem alten."
    hit = _exists(known, new)
    if hit is not None:
        return new, (f"Den Namen „{hit}“ gibt es schon – dann stattdessen „{old}“ mit „{hit}“ zusammenführen.")
    pf = ctx.portfolio()
    info = pf.accounts.get(old) if pf is not None else None
    meta = meta_rows(ctx.db).get(old)
    now = iso(utcnow())
    ctx.db.x("INSERT INTO account_meta(account, broker, depot_group, note, created_at, updated_at) "
             "VALUES (?,?,?,?,?,?)", (new, (info.broker if info else None), (info.depot_group if info else None),
                                      (meta["note"] if meta else None), now, now))
    ctx.invalidate_overlay()
    return new, None


def delete_empty(ctx: Any, name: str) -> str | None:
    from app.assetchange import account_merge as AM

    r = next((x for x in rows(ctx) if x.name == name), None)
    if r is None:
        return f"Konto „{name}“ ist nicht (mehr) vorhanden."
    if not r.empty:
        return (f"„{name}“ hat {r.n_txs} Buchung(en) – zum Ausblenden „Entfernen“ wählen (rückgängig zu machen).")
    if r.sources:
        return f"„{name}“ hat eine Datenquelle ({r.sources[0]}) – diese zuerst unter Datenquellen entfernen."
    if not r.own:
        return f"„{name}“ stammt aus dem Import und lässt sich hier nicht löschen."
    if any(h["status"] == "active" and name in (h["alias_account"], h["target_account"])
           for h in AM.history(ctx.db)):
        return f"„{name}“ ist Teil einer Zusammenführung – diese zuerst zurücknehmen."
    ctx.db.x("DELETE FROM account_meta WHERE account=?", (name,))
    ctx.invalidate_overlay()
    return None
