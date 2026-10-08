"""Erkennung möglicher Ticker-/Token-Änderungen – nur Hinweise, nie eine Änderung.

Signale (je gehaltenem Asset, ohne Netzabruf):

1. **Bekannte Umstellungen** (:mod:`app.assetchange.known`, kuratiert mit Verhältnis und Stichtag) – Sicherheit hoch.
2. **CoinGecko-Katalog** (lokal zwischengespeichert, ``/coins/list``): CoinGecko benennt ersetzte Coins um, z. B.
   „MATIC (migrated to POL)“, „Aergo [OLD]“, „… (Legacy)“. Nachfolger = eindeutiger Coin mit dem genannten Symbol
   bzw. gleichem Namen ohne Zusatz. Das Verhältnis steht nicht im Katalog → muss geprüft werden. Sicherheit mittel.
3. **Beobachtete Bestände** (Datenquellen): Der Anbieter meldet für ein Konto 0 des bisherigen Assets, aber vom
   neuen Asset so viel *mehr* als gebucht, wie dem Restbestand × Verhältnis entspricht (± 1 %). Bestätigt 1./2. (dann
   hoch) oder ist für sich ein Hinweis (mittel; Verhältnis 1 bzw. laut Register).
4. **Kursstillstand**: Seit mehr als 30 Tagen kein Marktkurs, während andere Reihen aktuell sind – möglicherweise
   umbenannt, umgestellt oder eingestellt (Nachfolger unbekannt, Sicherheit niedrig).
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from app.assetchange.known import BY_OLD_CG
from app.assetchange.service import DUST, _s, asset_change_service, version
from app.util.timeutil import today_local

log = logging.getLogger(__name__)
STALE_DAYS = 30
_MIGRATED = re.compile(r"\(migrated to ([^)]+)\)", re.I)
_OLD = re.compile(r"\s*[\[(](old|legacy)[\])]\s*$", re.I)
CONF_ORDER = {"hoch": 0, "mittel": 1, "niedrig": 2}


@dataclass
class Hint:
    old_asset: str
    old_name: str
    kind: str                       # migration | rename | unknown
    qty: Decimal
    new_asset: str | None = None    # vorhandenes Asset
    new_symbol: str | None = None   # Vorschlag für ein neues Asset
    new_name: str | None = None
    quote_source: str | None = None
    quote_id: str | None = None
    ratio: Decimal | None = None    # None = unbekannt, prüfen
    effective: date | None = None
    confidence: str = "mittel"
    reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.old_asset}>{self.new_asset or self.quote_id or self.new_symbol or '?'}"

    @property
    def target(self) -> str:
        return self.new_asset or self.new_symbol or (self.quote_id or "?")

    def query(self) -> str:
        q = {"asset": self.old_asset, "hint": self.key, "kind": "migration" if self.kind != "rename" else "rename"}
        if self.effective:
            q["date"] = self.effective.isoformat()
        if self.ratio is not None:
            q["ratio"] = _s(self.ratio)
        if self.new_asset:
            q["new_asset"] = self.new_asset
        elif self.new_symbol:
            q |= {"new_asset": self.new_symbol, "new_name": self.new_name or self.new_symbol}
        if self.quote_id and not self.new_asset:
            q |= {"quote_source": self.quote_source or "coingecko", "quote_id": self.quote_id}
        return urlencode(q)


def _bump_conf(h: Hint, conf: str) -> None:
    if CONF_ORDER[conf] < CONF_ORDER[h.confidence]:
        h.confidence = conf


def detect(ctx: Any) -> list[Hint]:
    pf = ctx.portfolio()
    led = ctx.ledger()
    if pf is None or led is None:
        return []
    held = {aid: q for aid, q in led.holdings_by_asset().items() if q > DUST}
    by_quote = {(a.quote_source, a.quote_id): a.asset_id for a in pf.assets.values() if a.quote_id}
    hints: dict[str, Hint] = {}
    try:
        from app.prices.sources import cached_catalog, catalog_path

        cat = cached_catalog(catalog_path(ctx))
    except Exception:
        cat = None
    for aid, qty in held.items():
        a = pf.assets.get(aid)
        if a is None or a.is_fiat:
            continue
        h = _known(a, qty, by_quote) or (_catalog(a, qty, cat, by_quote) if cat is not None else None)
        if h is not None:
            hints[aid] = h
    _observed(ctx, pf, held, hints)
    _stale(ctx, pf, held, hints)
    svc = asset_change_service(ctx)
    gone = svc.dismissed_keys()
    out = [h for h in hints.values() if h.key not in gone]
    out.sort(key=lambda h: (CONF_ORDER[h.confidence], h.old_asset.lower()))
    return out


def _known(a: Any, qty: Decimal, by_quote: dict[tuple[str, str], str]) -> Hint | None:
    k = BY_OLD_CG.get(a.quote_id or "") if a.quote_source == "coingecko" else None
    conf = "hoch"
    if k is None and a.is_crypto and not a.quote_id:
        k = next((x for x in BY_OLD_CG.values() if x.old_symbol == a.symbol.upper()), None)
        conf = "mittel"
    if k is None:
        return None
    new = by_quote.get(("coingecko", k.new_cg))
    return Hint(a.asset_id, a.name, "migration", qty, new_asset=new, new_symbol=None if new else k.new_symbol,
                new_name=k.new_name, quote_source="coingecko", quote_id=k.new_cg, ratio=k.ratio,
                effective=k.effective, confidence=conf, reasons=[k.note])


def _catalog(a: Any, qty: Decimal, cat: Any, by_quote: dict[tuple[str, str], str]) -> Hint | None:
    if a.quote_source != "coingecko" or not a.quote_id:
        return None
    coin = cat.by_id.get(a.quote_id)
    if coin is None:
        return None
    name = str(coin.get("name") or "")
    succ: list[dict[str, Any]] = []
    m = _MIGRATED.search(name)
    if m:
        sym = m.group(1).split(" - ")[-1].strip().lower()
        succ = [c for c in cat.by_symbol.get(sym, []) if c["id"] != coin["id"]
                and not _OLD.search(str(c.get("name") or ""))]
        old_sym = str(coin.get("symbol") or "").upper()
        if len(succ) > 1:  # „POL (ex-MATIC)“ eindeutig vor gleichnamigen Coins
            ex = [c for c in succ if f"ex-{old_sym}".lower() in str(c.get("name") or "").lower()]
            succ = ex or succ
        why = f"CoinGecko führt den Coin als „{name}“."
    elif _OLD.search(name):
        base = _OLD.sub("", name).strip().lower()
        succ = [c for c in cat.by_id.values() if str(c.get("name") or "").strip().lower() == base
                and c["id"] != coin["id"]]
        if not succ:
            succ = [c for c in cat.by_symbol.get(str(coin.get("symbol") or "").lower(), [])
                    if c["id"] != coin["id"] and not _OLD.search(str(c.get("name") or ""))]
        why = f"CoinGecko führt den Coin als „{name}“ (alte Version)."
    else:
        return None
    if len(succ) != 1:
        return Hint(a.asset_id, a.name, "unknown", qty, confidence="niedrig",
                    reasons=[why + (" Nachfolger nicht eindeutig." if succ else " Nachfolger nicht gefunden.")])
    c = succ[0]
    new = by_quote.get(("coingecko", c["id"]))
    return Hint(a.asset_id, a.name, "migration", qty, new_asset=new,
                new_symbol=None if new else str(c.get("symbol") or "").upper(), new_name=c.get("name"),
                quote_source="coingecko", quote_id=c["id"], ratio=None, confidence="mittel",
                reasons=[why, f"Nachfolger laut Katalog: {c.get('name')} ({c['id']}). Verhältnis und Stichtag bitte "
                              "prüfen (stehen nicht im Katalog)."])


def _observed(ctx: Any, pf: Any, held: dict[str, Decimal], hints: dict[str, Hint]) -> None:
    try:
        from app.datasources.service import datasource_service

        ds_svc = datasource_service(ctx)
        sources = [ds for ds in ds_svc.list() if ds_svc.balances(int(ds.id))]
    except Exception as e:
        log.debug("Beobachtete Bestände nicht verfügbar: %s", e)
        return
    for ds in sources:
        try:
            items = ds_svc.holdings(ds)["items"]
        except Exception as e:
            log.debug("Bestände %s: %s", ds.id, e)
            continue
        gone = [i for i in items if i["asset_id"] and (i["explained"] or 0) > DUST and not (i["observed"] or 0)
                and i["state"] in ("diff", "only_portfolia")]
        more = [i for i in items if i["asset_id"] and i["state"] == "diff" and (i["diff"] or 0) > DUST]
        for o in gone:
            h = hints.get(o["asset_id"])
            ratios = [h.ratio] if h is not None and h.ratio else [Decimal(1)]
            for n in more:
                if n["asset_id"] == o["asset_id"] or (h is not None and h.new_asset and n["asset_id"] != h.new_asset):
                    continue
                r = ratios[0]
                want = o["explained"] * r
                if want <= 0 or abs(n["diff"] - want) > want / 100:
                    continue
                a = pf.assets.get(o["asset_id"])
                why = (f"Konto „{ds.account}“: Anbieter meldet 0 {o['asset_id']} und {_s(n['diff'])} {n['asset_id']} "
                       f"mehr als gebucht (= Restbestand × {_s(r)}).")
                if h is None:
                    if a is None or o["asset_id"] not in held:
                        continue
                    h = Hint(a.asset_id, a.name, "migration", held[a.asset_id], new_asset=n["asset_id"], ratio=r,
                             confidence="mittel", reasons=[why])
                    hints[a.asset_id] = h
                else:
                    if h.new_asset is None and h.new_symbol and h.new_symbol.upper() != n["asset_id"].upper():
                        continue
                    h.new_asset = h.new_asset or n["asset_id"]
                    h.reasons.append(why)
                    _bump_conf(h, "hoch")
                break


def _stale(ctx: Any, pf: Any, held: dict[str, Decimal], hints: dict[str, Hint]) -> None:
    today = today_local()
    fresh = ctx.db.q1("SELECT 1 FROM price_daily WHERE date>=? LIMIT 1", ((today - timedelta(days=3)).isoformat(),))
    if fresh is None:  # Kursabruf insgesamt gestört bzw. offline – kein Hinweis je Asset
        return
    for aid, qty in held.items():
        if aid in hints:
            continue
        a = pf.assets.get(aid)
        if a is None or a.is_fiat or a.quote_source not in ("coingecko", "yahoo") or not a.quote_id:
            continue
        series = ctx.prices.series_for(a)
        if not series:
            continue
        row = ctx.db.q1("SELECT MAX(date) AS d FROM price_daily WHERE series=? AND source NOT LIKE 'prev:%'",
                        (series,))
        last = row["d"] if row is not None else None
        if not last or (today - date.fromisoformat(last)).days <= STALE_DAYS:
            continue
        src = "CoinGecko" if a.quote_source == "coingecko" else "Yahoo"
        hints[aid] = Hint(aid, a.name, "unknown", qty, confidence="niedrig", reasons=[
            f"Seit {date.fromisoformat(last):%d.%m.%Y} kein Marktkurs von {src} ({a.quote_id}) – möglicherweise "
            "umbenannt, umgestellt oder eingestellt."])


# -- Zwischenspeicher (Hinweise erscheinen auf mehreren Seiten) ---------------------------------------------------
_CACHE: dict[int, tuple[tuple[Any, ...], list[Hint]]] = {}
_LOCK = threading.Lock()


def hints(ctx: Any) -> list[Hint]:
    key = (getattr(ctx, "data_version", 0), getattr(ctx, "overlay_version", 0), version(), today_local())
    with _LOCK:
        hit = _CACHE.get(id(ctx))
        if hit is not None and hit[0] == key:
            return hit[1]
    try:
        res = detect(ctx)
    except Exception as e:  # Hinweise dürfen keine Seite blockieren
        log.warning("Erkennung von Ticker-Änderungen fehlgeschlagen: %s", e)
        res = []
    with _LOCK:
        _CACHE[id(ctx)] = (key, res)
    return res
