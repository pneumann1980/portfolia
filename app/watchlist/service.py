"""Watchlists: beobachtete Werte (Krypto über CoinGecko, Wertpapiere über Yahoo) ohne Bestand.

Datenmodell (Migration 15): ``watchlist`` (mehrere Listen möglich, eine Standardliste) und ``watchlist_item`` (Kennung
des Kursanbieters, optionaler Bezug zu einem Portfolio-Asset, Reihenfolge). Kurse, 24h/7d, Marktkapitalisierung und
Sparkline kommen ausschließlich über :mod:`app.prices.market` – dieselbe Ablage wie das Portfolio, keine eigenen
Abrufe. Eine Watchlist ändert nie Buchungen; „Position erstellen“ führt in die normale Buchungserfassung.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.ledger.models import AssetInfo
from app.prices.market import MarketSnapshot, market_data
from app.prices.sources import cached_catalog, catalog_path, parse_coin_id
from app.util.timeutil import iso

_YAHOO = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,24}$")
MAX_ITEMS = 200
SORTS = {"manual": "eigene Reihenfolge", "name": "Name", "change_24h": "24h", "change_7d": "7 Tage",
         "market_cap": "Marktkapitalisierung"}


@dataclass
class Item:
    id: int
    list_id: int
    quote_source: str
    quote_id: str
    asset_class: str
    asset_id: str | None
    symbol: str | None
    name: str | None
    position: int
    series: str | None = None
    snap: MarketSnapshot | None = None
    held: bool = False

    @property
    def label(self) -> str:
        return self.name or (self.snap.name if self.snap else None) or self.symbol or self.quote_id

    @property
    def sym(self) -> str:
        return (self.symbol or (self.snap.symbol if self.snap else None) or self.quote_id).upper()


class WatchlistService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.db = ctx.db

    # -- Listen ---------------------------------------------------------------------------------------------
    def lists(self) -> list[Any]:
        self.default_id()
        return self.db.q("SELECT * FROM watchlist ORDER BY is_default DESC, position, id")

    def default_id(self) -> int:
        row = self.db.q1("SELECT id FROM watchlist WHERE is_default=1 ORDER BY id LIMIT 1")
        if row is not None:
            return int(row["id"])
        cur = self.db.x("INSERT INTO watchlist(name, position, is_default, created_at) VALUES ('Watchlist', 0, 1, ?)",
                        (iso(datetime.now(UTC)),))
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def create_list(self, name: str) -> tuple[int | None, list[str]]:
        name = re.sub(r"\s+", " ", name or "").strip()[:40]
        if not name:
            return None, ["Name der Liste fehlt."]
        cur = self.db.x("INSERT INTO watchlist(name, position, is_default, created_at) VALUES (?, "
                        "(SELECT COALESCE(MAX(position), 0) + 1 FROM watchlist), 0, ?)", (name, iso(datetime.now(UTC))))
        return int(cur.lastrowid), []  # type: ignore[arg-type]

    # -- Einträge -------------------------------------------------------------------------------------------
    def items(self, list_id: int, sort: str = "manual", with_market: bool = True) -> list[Item]:
        rows = self.db.q("SELECT * FROM watchlist_item WHERE list_id=? ORDER BY position, id", (list_id,))
        val = self.ctx.valuation() if with_market else None
        held = {p.asset_id for p in val.positions if p.qty} if val else set()
        out = []
        for r in rows:
            it = Item(int(r["id"]), int(r["list_id"]), r["quote_source"], r["quote_id"], r["asset_class"],
                      r["asset_id"], r["symbol"], r["name"], int(r["position"]))
            it.series = self.series(it)
            it.held = bool(it.asset_id and it.asset_id in held)
            out.append(it)
        if with_market:
            snaps = market_data(self.ctx).snapshots([i.series for i in out if i.series])
            for it in out:
                it.snap = snaps.get(it.series or "")
        if sort in ("change_24h", "change_7d", "market_cap"):
            attr = {"market_cap": "market_cap_eur"}.get(sort, sort)
            out.sort(key=lambda i: (getattr(i.snap, attr, None) is None,
                                    -(getattr(i.snap, attr, None) or 0)))
        elif sort == "name":
            out.sort(key=lambda i: i.label.lower())
        return out

    def get(self, item_id: int) -> Item | None:
        r = self.db.q1("SELECT * FROM watchlist_item WHERE id=?", (item_id,))
        if r is None:
            return None
        it = Item(int(r["id"]), int(r["list_id"]), r["quote_source"], r["quote_id"], r["asset_class"],
                  r["asset_id"], r["symbol"], r["name"], int(r["position"]))
        it.series = self.series(it)
        it.snap = market_data(self.ctx).snapshots([it.series]).get(it.series or "") if it.series else None
        return it

    def series(self, it: Item) -> str | None:
        a = AssetInfo(it.asset_id or it.quote_id, it.name or it.quote_id, it.asset_class, it.quote_source, it.quote_id)
        return self.ctx.prices.series_for(a)

    def resolve(self, kind: str, raw: str) -> tuple[dict[str, Any] | None, list[str], list[dict[str, Any]]]:
        """Eingabe → (Eintrag, Fehler, Auswahl bei Mehrdeutigkeit).

        ``kind``: ``asset`` (Portfolio-Asset), ``crypto`` (CoinGecko-ID, Link oder Symbol – Symbol nur, wenn im
        lokalen Katalog eindeutig), ``security`` (Yahoo-Symbol)."""
        raw = (raw or "").strip()
        if not raw:
            return None, ["Bitte ein Symbol bzw. eine Kennung eingeben."], []
        if kind == "asset":
            pf = self.ctx.portfolio()
            a = pf.assets.get(raw) if pf else None
            if a is None or a.is_fiat:
                return None, ["Asset nicht gefunden."], []
            if a.quote_source not in ("coingecko", "yahoo") or not a.quote_id:
                return None, [f"{a.name} hat keine Kursquelle (CoinGecko/Yahoo) – unter Datenqualität → Kursquellen "
                              "zuordnen."], []
            return {"quote_source": a.quote_source, "quote_id": a.quote_id, "asset_class": a.asset_class,
                    "asset_id": a.asset_id, "symbol": a.symbol, "name": a.name}, [], []
        if kind == "security":
            sym = raw.upper()
            if not _YAHOO.match(sym):
                return None, ["Ungültiges Yahoo-Symbol (z. B. SAP.DE, AAPL, EUNL.DE)."], []
            return {"quote_source": "yahoo", "quote_id": sym, "asset_class": "security", "symbol": sym.split(".")[0],
                    "asset_id": self._asset_for("yahoo", sym)}, [], []
        cat = cached_catalog(catalog_path(self.ctx))
        cid = parse_coin_id(raw)
        if cid and (cat is None or cid in cat.by_id):
            coin = cat.by_id.get(cid) if cat else None
            return {"quote_source": "coingecko", "quote_id": cid, "asset_class": "crypto",
                    "symbol": str(coin.get("symbol") or "").upper() if coin else None,
                    "name": coin.get("name") if coin else None, "asset_id": self._asset_for("coingecko", cid)}, [], []
        if cat is None:
            return None, ["CoinGecko-Katalog noch nicht geladen – bitte die CoinGecko-ID oder den Link von "
                          "coingecko.com eingeben."], []
        cands = cat.candidates(raw)
        if len(cands) == 1:
            c = cands[0]
            return {"quote_source": "coingecko", "quote_id": c["id"], "asset_class": "crypto",
                    "symbol": str(c.get("symbol") or "").upper(), "name": c.get("name"),
                    "asset_id": self._asset_for("coingecko", c["id"])}, [], []
        if not cands:
            return None, [f"„{raw}“ nicht im CoinGecko-Katalog gefunden."], []
        return None, [f"„{raw}“ ist nicht eindeutig ({len(cands)} Coins) – bitte auswählen."], [
            {"id": c["id"], "name": c.get("name"), "symbol": str(c.get("symbol") or "").upper()} for c in cands[:20]]

    def _asset_for(self, source: str, qid: str) -> str | None:
        pf = self.ctx.portfolio()
        if pf is None:
            return None
        return next((a.asset_id for a in pf.assets.values() if a.quote_source == source and a.quote_id == qid), None)

    def add(self, list_id: int, entry: dict[str, Any]) -> tuple[int | None, list[str]]:
        n = self.db.scalar("SELECT COUNT(*) FROM watchlist_item WHERE list_id=?", (list_id,), default=0)
        if n >= MAX_ITEMS:
            return None, [f"Höchstens {MAX_ITEMS} Einträge je Liste."]
        if self.db.q1("SELECT 1 FROM watchlist_item WHERE list_id=? AND quote_source=? AND quote_id=?",
                      (list_id, entry["quote_source"], entry["quote_id"])):
            return None, ["Bereits in der Watchlist."]
        cur = self.db.x(
            "INSERT INTO watchlist_item(list_id, quote_source, quote_id, asset_class, asset_id, symbol, name, "
            "position, added_at) VALUES (?,?,?,?,?,?,?, "
            "(SELECT COALESCE(MAX(position), 0) + 1 FROM watchlist_item WHERE list_id=?), ?)",
            (list_id, entry["quote_source"], entry["quote_id"], entry.get("asset_class") or "crypto",
             entry.get("asset_id"), (entry.get("symbol") or None), (entry.get("name") or None), list_id,
             iso(datetime.now(UTC))))
        return int(cur.lastrowid), []  # type: ignore[arg-type]

    def remove(self, item_id: int) -> bool:
        return bool(self.db.x("DELETE FROM watchlist_item WHERE id=?", (item_id,)).rowcount)

    def move(self, item_id: int, direction: int) -> None:
        """Eintrag um eine Stelle nach oben (−1) bzw. unten (+1) verschieben."""
        it = self.db.q1("SELECT id, list_id FROM watchlist_item WHERE id=?", (item_id,))
        if it is None:
            return
        ids = [int(r["id"]) for r in self.db.q("SELECT id FROM watchlist_item WHERE list_id=? ORDER BY position, id",
                                               (it["list_id"],))]
        i = ids.index(item_id)
        j = i + (1 if direction > 0 else -1)
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        with self.db.transaction() as c:
            c.executemany("UPDATE watchlist_item SET position=? WHERE id=?", [(n, x) for n, x in enumerate(ids, 1)])

    def all_series(self) -> list[str]:
        out = []
        for r in self.db.q("SELECT DISTINCT quote_source, quote_id, asset_class FROM watchlist_item"):
            s = self.ctx.prices.series_for(AssetInfo(r["quote_id"], r["quote_id"], r["asset_class"],
                                                     r["quote_source"], r["quote_id"]))
            if s:
                out.append(s)
        return out


def watchlist_service(ctx: Any) -> WatchlistService:
    return WatchlistService(ctx)
