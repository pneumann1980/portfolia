"""Watchlist als eine Tabelle (0.115): Titel aus den Listen, in Discovery beobachtete Unternehmen ohne Liste und
offene Discovery-Vorschläge – jede Zeile mit Markierung, woher sie stammt. Dazu der Kontext für das Detailfenster
(gleiches Seitenfenster wie bei den Positionen, für Discovery-Unternehmen um Steckbrief und SEC-Kennzahlen erweitert).

Nur gespeicherte Daten: kein KI-Aufruf, kein Abruf. Kurse der Listentitel kommen aus der Marktdaten-Ablage; Vorschläge
ohne Listeneintrag zeigen den Kurs der letzten Datenanalyse in Handelswährung.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ledger.models import AssetInfo
from app.prices.market import MarketSnapshot, market_data
from app.watchlist.service import Item, WatchlistService

MARKERS = {"liste": "Watchlist", "ohne_liste": "beobachtet · ohne Liste", "vorschlag": "Vorschlag"}
SORTS = {"standard": "Standard", "name": "Name", "change_24h": "24h", "change_7d": "7 Tage",
         "market_cap": "Marktkapitalisierung", "score": "Portfolia-Score"}


@dataclass
class Row:
    key: str                      # „i<Eintrag>“ bzw. „c<Unternehmen>“ – eindeutig je Zeile
    marker: str                   # liste | ohne_liste | vorschlag
    name: str
    sym: str
    sub: str = ""
    item: Item | None = None
    company_id: int | None = None
    card: dict[str, Any] | None = None  # Discovery-Karte (Stand, Score, Herkunft) – falls verknüpft
    lists: list[str] = field(default_factory=list)
    snap: MarketSnapshot | None = None
    held: bool = False
    ambiguous: bool = False
    panel: str = ""
    href: str = ""

    @property
    def score(self) -> float | None:
        n = (self.card or {}).get("nash")
        if not n:
            return None
        return float(n["data"]["pts"] if n["quali_open"] else n["total"])

    @property
    def native_price(self) -> tuple[float, str] | None:
        """Kurs der letzten Datenanalyse (Handelswährung) – nur für Zeilen ohne Kurs aus der Marktdaten-Ablage."""
        m = (self.card or {}).get("market") or {}
        return (float(m["price"]), str(m.get("ccy") or "")) if m.get("price") else None


def _asset_panel(ctx: Any, asset_id: str) -> tuple[str, str]:
    from urllib.parse import quote

    q = quote(asset_id, safe=":")
    return f"/panel/asset/{q}", f"/asset/{q}"


def rows(ctx: Any, svc: WatchlistService, lists: list[Any], list_id: int | None, only: str = "",
         sort: str = "standard") -> tuple[list[Row], dict[str, int]]:
    """Zeilen der Tabelle und Zähler je Ansicht. ``list_id``: nur diese Liste (eigene Reihenfolge);
    ``only='vorschlaege'``: nur offene Discovery-Vorschläge; sonst alles."""
    from app.discovery import service as SV
    from app.discovery import watch as W

    db = ctx.db
    cards = {int(c["id"]): c for c in SV.all_cards(ctx)}
    unique, amb = W.symbol_index(db)
    pf = ctx.portfolio()
    out: list[Row] = []
    seen: dict[tuple[str, str], Row] = {}
    for li in lists:
        if list_id is not None and int(li["id"]) != list_id:
            continue
        for it in svc.items(int(li["id"]), "manual"):
            k = (it.quote_source, it.quote_id)
            if k in seen:
                seen[k].lists.append(str(li["name"]))
                continue
            cid = unique.get(it.quote_id.upper()) if it.quote_source == "yahoo" else None
            in_pf = bool(it.asset_id and pf is not None and it.asset_id in pf.assets)
            if in_pf:  # eigenes Asset: dasselbe Detailfenster wie unter „Positionen“
                panel, href = _asset_panel(ctx, str(it.asset_id))
            else:
                panel, href = f"/panel/watchlist/item/{it.id}", f"/watchlist/item/{it.id}"
            row = Row(f"i{it.id}", "liste", it.label, it.sym, it.snap.provider_label if it.snap else it.quote_source,
                      item=it, company_id=cid, card=cards.get(cid) if cid else None, lists=[str(li["name"])],
                      snap=it.snap, held=it.held, panel=panel, href=href,
                      ambiguous=it.quote_source == "yahoo" and it.quote_id.upper() in amb)
            seen[k] = row
            out.append(row)
    listed = {r.company_id for r in out if r.company_id}
    counts = {"alle": 0, "vorschlaege": 0}
    extra: list[Row] = []
    if list_id is None:
        for cid, c in sorted(cards.items(), key=lambda kv: str(kv[1]["name"]).lower()):
            if cid in listed or c["lists"]:  # steht (in einer anderen Ansicht) bereits als Listentitel in der Tabelle
                continue
            if c["status"] == "beobachten":
                marker = "ohne_liste"
            elif c["status"] in ("verworfen", "zurueckgestellt") or c["held"]:
                continue
            else:
                marker = "vorschlag"
            extra.append(Row(f"c{cid}", marker, str(c["name"]), str(c["ticker"]), str(c.get("sector") or ""),
                             company_id=cid, card=c, panel=f"/panel/watchlist/c/{cid}", href=f"/discovery/c/{cid}"))
        if extra:  # Kurse aus der Marktdaten-Ablage, soweit vorhanden (kein Abruf)
            series = {r.key: ctx.prices.series_for(AssetInfo(r.sym, r.name, "security", "yahoo",
                                                             str((r.card or {}).get("yahoo_symbol") or r.sym)))
                      for r in extra}
            snaps = market_data(ctx).snapshots([s for s in series.values() if s])
            for r in extra:
                s = snaps.get(series.get(r.key) or "")
                r.snap = s if s is not None and s.price_eur else None
    counts["vorschlaege"] = sum(1 for r in extra if r.marker == "vorschlag")
    counts["alle"] = len(out) + len(extra)
    if only == "vorschlaege":
        out = [r for r in extra if r.marker == "vorschlag"]
    else:
        out = out + extra

    def num(attr: str) -> Any:
        return lambda r: (getattr(r.snap, attr, None) is None, -(getattr(r.snap, attr, None) or 0))

    if sort == "name":
        out.sort(key=lambda r: r.name.lower())
    elif sort in ("change_24h", "change_7d"):
        out.sort(key=num(sort))
    elif sort == "market_cap":
        out.sort(key=num("market_cap_eur"))
    elif sort == "score":
        out.sort(key=lambda r: (r.score is None, -(r.score or 0)))
    return out, counts


def detail(ctx: Any, svc: WatchlistService, item: Item | None = None, company_id: int | None = None
           ) -> dict[str, Any] | None:
    """Kontext für das Detailfenster eines Listentitels bzw. eines Discovery-Unternehmens ohne Listeneintrag."""
    from app.discovery import holdings as H
    from app.discovery import service as SV
    from app.discovery import watch as W

    db = ctx.db
    if item is not None and company_id is None:
        company_id = W.company_for(db, item.quote_source, item.quote_id)[0]
    co = SV.company(db, company_id) if company_id else None
    if item is None and co is None:
        return None
    disc = None
    members: list[dict[str, Any]] = []
    if co is not None:
        an = SV.analysis(db, int(co["id"]))
        res = an["result"] if an else {}
        pv = SV.preview_of(db, int(co["id"]))
        members = W.memberships(db, co)
        disc = {"co": co, "res": res, "preview": pv, "summary": SV.summary(res) if res else None,
                "stage": SV._stage(res, pv is not None, SV.current_filed(db, co, res)),
                "why": SV.why_found(db, int(co["id"]), co.get("origin") or ""),
                "watched": bool(members) or co["status"] == "beobachten"}
    snap = item.snap if item is not None else None
    series = item.series if item is not None else None
    if item is None and co is not None:
        sym = W.symbol_of(co)
        series = ctx.prices.series_for(AssetInfo(sym, co["name"], "security", "yahoo", sym)) if sym else None
        s = market_data(ctx).snapshots([series]).get(series) if series else None
        snap = s if s is not None and s.price_eur else None
    return {"it": item, "co": co, "disc": disc, "members": members, "snap": snap, "series": series,
            "stages": SV.STAGES, "watch_lists": [dict(r) for r in svc.lists()],
            "sec": H.brief_for_company(ctx, int(co["id"])) if co is not None else None,
            "chart_src": f"/api/watchlist/{item.id}/chart" if item is not None
            else (f"/api/watchlist/c/{co['id']}/chart" if co is not None and series else ""),
            "name": item.label if item is not None else co["name"],  # type: ignore[index]
            "sym": item.sym if item is not None else co["ticker"]}  # type: ignore[index]
