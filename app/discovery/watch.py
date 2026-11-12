"""Beobachten (0.114): Verknüpfung zwischen Discovery-Unternehmen und Watchlist-Einträgen – ohne eigene Tabelle.

**Eine Wahrheit je Frage**

* *In welchen Listen steht ein Titel?* → ausschließlich ``watchlist_item`` (Listenmitgliedschaft).
* *Welches Unternehmen gehört zu einem Eintrag?* → abgeleitet, nicht gespeichert: ein Watchlist-Eintrag mit
  ``quote_source='yahoo'`` gehört zu genau dem Discovery-Unternehmen, dessen Yahoo-Symbol
  (``yahoo_symbol``, ersatzweise ``ticker``) **exakt** gleich der ``quote_id`` ist. „SAP.DE“ (Xetra) ist damit
  nie das US-Listing „SAP“; Krypto-Einträge (CoinGecko) sind nie verknüpft. Tragen mehrere Unternehmen dasselbe
  Symbol, gilt die Zuordnung als **mehrdeutig** und es wird nicht verknüpft.
* *Wird ein Unternehmen beobachtet?* → Status ``beobachten`` (steuert unverändert die Discovery-Jobs) **oder**
  mindestens eine Listenmitgliedschaft.

„Beobachten“ setzt den Status und nimmt das Unternehmen in eine Liste auf (wiederholbar, nie doppelt). Entfernt
man es aus der letzten Liste, wird ein Status ``beobachten`` auf ``zurueckgestellt`` gesetzt: Es erscheint weder
unter „Beobachtet“ noch unter den offenen Kandidaten und wird nicht mehr automatisch aktualisiert. Unternehmen,
Analysen und Signale bleiben erhalten. Nichts hier ruft die KI oder das Netz auf.
"""

from __future__ import annotations

from typing import Any

from app.watchlist.service import YAHOO_SYMBOL, watchlist_service


def symbol_of(co: dict[str, Any]) -> str | None:
    """Yahoo-Symbol eines Discovery-Unternehmens (Großbuchstaben) oder ``None``, wenn keines gültig ist."""
    sym = str(co.get("yahoo_symbol") or co.get("ticker") or "").strip().upper()
    return sym if sym and YAHOO_SYMBOL.match(sym) else None


def symbol_index(db: Any) -> tuple[dict[str, int], set[str]]:
    """(eindeutig: Symbol → Unternehmens-ID, mehrdeutige Symbole) über alle Discovery-Unternehmen – eine Abfrage."""
    seen: dict[str, list[int]] = {}
    for r in db.q("SELECT id, UPPER(COALESCE(NULLIF(yahoo_symbol, ''), ticker)) AS s FROM disc_company"):
        seen.setdefault(str(r["s"]), []).append(int(r["id"]))
    return ({s: ids[0] for s, ids in seen.items() if len(ids) == 1},
            {s for s, ids in seen.items() if len(ids) > 1})


def company_for(db: Any, quote_source: str, quote_id: str) -> tuple[int | None, bool]:
    """Unternehmen zu einem Watchlist-Eintrag: (ID oder ``None``, mehrdeutig?)."""
    if quote_source != "yahoo" or not quote_id:
        return None, False
    rows = db.q("SELECT id FROM disc_company WHERE UPPER(COALESCE(NULLIF(yahoo_symbol, ''), ticker)) = ?",
                (str(quote_id).upper(),))
    if len(rows) == 1:
        return int(rows[0]["id"]), False
    return None, len(rows) > 1


def memberships(db: Any, co: dict[str, Any]) -> list[dict[str, Any]]:
    """Listen, in denen das Unternehmen steht: ``[{item_id, list_id, list_name}]`` (leer bei mehrdeutigem Symbol)."""
    sym = symbol_of(co)
    if sym is None or company_for(db, "yahoo", sym)[0] != int(co["id"]):
        return []
    return [dict(r) for r in db.q(
        "SELECT i.id AS item_id, i.list_id, w.name AS list_name FROM watchlist_item i JOIN watchlist w "
        "ON w.id = i.list_id WHERE i.quote_source = 'yahoo' AND UPPER(i.quote_id) = ? "
        "ORDER BY w.is_default DESC, w.position, w.id", (sym,))]


def watched_index(db: Any) -> dict[int, list[str]]:
    """Unternehmens-ID → Namen der Listen, in denen es steht (für Karten und Zähler; zwei Abfragen insgesamt)."""
    unique, _amb = symbol_index(db)
    out: dict[int, list[str]] = {}
    for r in db.q("SELECT UPPER(i.quote_id) AS s, w.name FROM watchlist_item i JOIN watchlist w ON w.id = i.list_id "
                  "WHERE i.quote_source = 'yahoo' ORDER BY w.is_default DESC, w.position, w.id"):
        cid = unique.get(str(r["s"]))
        if cid is not None:
            out.setdefault(cid, []).append(str(r["name"]))
    return out


def unlisted(db: Any) -> list[dict[str, Any]]:
    """Unternehmen mit Status „beobachten“ ohne Listenmitgliedschaft (Altbestand bzw. ohne eindeutiges Symbol)."""
    listed = watched_index(db)
    return [dict(r) for r in db.q("SELECT id, ticker, name, yahoo_symbol FROM disc_company WHERE status = 'beobachten' "
                                  "ORDER BY name") if int(r["id"]) not in listed]


def count_watched(db: Any) -> int:
    """Anzahl beobachteter Titel: verschiedene Kennungen über alle Listen plus Unternehmen ohne Liste."""
    n = db.scalar("SELECT COUNT(*) FROM (SELECT DISTINCT quote_source, quote_id FROM watchlist_item)", default=0)
    return int(n or 0) + len(unlisted(db))


def observe(ctx: Any, cid: int, list_id: int | None = None) -> dict[str, Any]:
    """Unternehmen beobachten: Status setzen und in eine Liste aufnehmen (Standardliste, wenn keine gültige gewählt).

    Rückgabe ``{ok, listed, already, list, note|error}``. Wiederholung erzeugt keinen zweiten Eintrag."""
    from app.discovery import service as SV

    db = ctx.db
    co = SV.company(db, cid)
    if co is None:
        return {"ok": False, "error": "Unternehmen nicht gefunden."}
    ws = watchlist_service(ctx)
    lists = {int(r["id"]): str(r["name"]) for r in ws.lists()}
    lid = list_id if list_id in lists else ws.default_id()
    was = co["status"] == "beobachten"
    sym = symbol_of(co)
    linked, ambiguous = company_for(db, "yahoo", sym) if sym else (None, False)
    if sym is None or linked != cid:  # keine eindeutige Kurskennung: nur Status, sichtbar unter „ohne Liste“
        if not was:
            SV.set_status(db, cid, "beobachten")
        why = "Symbol mehrdeutig" if ambiguous else "kein gültiges Yahoo-Symbol"
        return {"ok": True, "listed": False, "already": was, "list": None,
                "note": f"ohne Kursliste ({why}) – erscheint unter „Beobachtet“ im Abschnitt „Ohne Liste“"}
    if any(m["list_id"] == lid for m in memberships(db, co)):
        if not was:
            SV.set_status(db, cid, "beobachten")
        return {"ok": True, "listed": True, "already": True, "list": lists[lid]}
    _item, errs = ws.add(lid, {"quote_source": "yahoo", "quote_id": sym, "asset_class": "security",
                               "symbol": co["ticker"], "name": co["name"], "asset_id": ws._asset_for("yahoo", sym)})
    if errs:
        return {"ok": False, "error": "; ".join(errs)}
    if not was:
        SV.set_status(db, cid, "beobachten")
    return {"ok": True, "listed": True, "already": False, "list": lists[lid]}


def after_item_removed(ctx: Any, quote_source: str, quote_id: str) -> int | None:
    """Nach dem Entfernen eines Watchlist-Eintrags aufrufen: War es die letzte Liste des verknüpften Unternehmens,
    wird ein Status „beobachten“ auf „zurückgestellt“ gesetzt. Rückgabe: Unternehmens-ID (falls verknüpft)."""
    from app.discovery import service as SV

    db = ctx.db
    cid, _amb = company_for(db, quote_source, quote_id)
    if cid is None:
        return None
    co = SV.company(db, cid)
    if co is not None and co["status"] == "beobachten" and not memberships(db, co):
        SV.set_status(db, cid, "zurueckgestellt")
    return cid
