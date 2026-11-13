"""Vorschläge beim Hinzufügen eines Titels (0.115): Eingabe von Name **oder** Symbol → Trefferliste.

Quellen, in dieser Reihenfolge:

1. eigene Assets aus dem Portfolio (lokal),
2. Krypto aus dem lokalen CoinGecko-Katalog (lokal, kein Abruf),
3. Aktien, ETFs, Fonds und Indizes aller Börsenplätze über die Yahoo-Suche – dabei geht der **Suchtext** an Yahoo
   (sonst nichts; im Demo-Modus kein Abruf),
4. US-Emittenten aus dem SEC-Verzeichnis, soweit es bereits lokal vorliegt (ergänzt Treffer, löst keinen Abruf aus).

Jeder Treffer trägt ``kind`` und ``value`` genau so, wie sie ``POST /watchlist/add`` erwartet.
"""

from __future__ import annotations

import logging
from typing import Any

from app.prices.sources import cached_catalog, catalog_path

log = logging.getLogger(__name__)
YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
YAHOO_TYPES = {"EQUITY": "Aktie", "ETF": "ETF", "MUTUALFUND": "Fonds", "INDEX": "Index", "CURRENCY": "Devise",
               "FUTURE": "Future"}
MIN_LEN = 2
PER_SOURCE = 6


def _rank(text: str, q: str) -> int | None:
    """0 = exakt, 1 = beginnt mit, 2 = enthält; ``None`` = kein Treffer."""
    t = text.lower()
    if t == q:
        return 0
    if t.startswith(q):
        return 1
    return 2 if q in t else None


def _portfolio(ctx: Any, q: str) -> list[dict[str, Any]]:
    pf = ctx.portfolio()
    out = []
    for a in (pf.assets.values() if pf else []):
        if a.is_fiat or not a.quote_id or a.quote_source not in ("coingecko", "yahoo"):
            continue
        r = min((x for x in (_rank(a.name or "", q), _rank(a.symbol or "", q), _rank(a.asset_id, q)) if x is not None),
                default=None)
        if r is not None:
            out.append((r, {"kind": "asset", "value": a.asset_id, "label": a.name, "symbol": a.symbol or a.asset_id,
                            "sub": "aus dem Portfolio"}))
    return [x for _r, x in sorted(out, key=lambda t: (t[0], t[1]["label"].lower()))[:PER_SOURCE]]


def _crypto(ctx: Any, q: str) -> list[dict[str, Any]]:
    cat = cached_catalog(catalog_path(ctx))
    if cat is None:
        return []
    out = []
    for cid, c in cat.by_id.items():
        name, sym = str(c.get("name") or ""), str(c.get("symbol") or "")
        r = min((x for x in (_rank(sym, q), _rank(name, q), _rank(cid, q)) if x is not None), default=None)
        if r is not None and (r < 2 or len(q) >= 4):  # „enthält“ erst ab 4 Zeichen, sonst zu viele Treffer
            out.append((r, len(name), {"kind": "crypto", "value": cid, "label": name or cid, "symbol": sym.upper(),
                                       "sub": f"Krypto · CoinGecko ({cid})"}))
    return [x for _r, _n, x in sorted(out, key=lambda t: (t[0], t[1], t[2]["value"]))[:PER_SOURCE]]


def _yahoo(ctx: Any, q: str) -> tuple[list[dict[str, Any]], str]:
    if ctx.config.demo_mode:
        return [], ""
    try:
        r = ctx.http.get(YAHOO_SEARCH, params={"q": q, "quotesCount": 10, "newsCount": 0, "listsCount": 0,
                                               "lang": "de-DE", "region": "DE"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=6.0)
        r.raise_for_status()
        quotes = r.json().get("quotes") or []
    except Exception as e:  # Vorschläge sind Komfort – die Eingabe von Hand funktioniert weiter
        log.info("Yahoo-Suche nicht verfügbar: %s", e)
        return [], "Yahoo-Suche derzeit nicht erreichbar – Symbol bitte von Hand eingeben."
    out = []
    for x in quotes:
        sym, kind = str(x.get("symbol") or "").upper(), str(x.get("quoteType") or "").upper()
        if not sym or kind not in YAHOO_TYPES or kind == "CRYPTOCURRENCY":
            continue
        name = " ".join(str(x.get("longname") or x.get("shortname") or sym).split())
        out.append({"kind": "security", "value": sym, "label": name, "symbol": sym,
                    "sub": " · ".join(p for p in (YAHOO_TYPES[kind], str(x.get("exchDisp") or "")) if p)})
    return out[:PER_SOURCE + 2], ""


def _sec(ctx: Any, q: str, have: set[str]) -> list[dict[str, Any]]:
    try:
        from app.discovery import service as SV
        from app.discovery.identity import directory

        sec = SV.sec(ctx)
        if sec.retrieved_at("tickers", "company_tickers_exchange") is None:
            return []  # Verzeichnis noch nicht geladen – hier kein Abruf
        d = directory(sec)
    except Exception as e:
        log.debug("SEC-Verzeichnis für Vorschläge nicht verfügbar: %s", e)
        return []
    out = []
    for iss in d.by_cik.values():
        sym = iss.ticker.replace(".", "-").upper()
        if sym in have:
            continue
        r = min((x for x in (_rank(iss.ticker, q), _rank(iss.name, q)) if x is not None), default=None)
        if r is not None and (r < 2 or len(q) >= 4):
            out.append((r, len(iss.name), {"kind": "security", "value": sym, "label": iss.name, "symbol": sym,
                                           "sub": "Aktie · SEC-Verzeichnis (USA)"}))
    return [x for _r, _n, x in sorted(out, key=lambda t: (t[0], t[1]))[:PER_SOURCE]]


def suggest(ctx: Any, query: str) -> dict[str, Any]:
    q = " ".join((query or "").split()).lower()[:60]
    if len(q) < MIN_LEN:
        return {"items": [], "note": ""}
    items = _portfolio(ctx, q)
    crypto = _crypto(ctx, q)
    yahoo, note = _yahoo(ctx, q)
    have = {i["value"] for i in yahoo}
    sec = _sec(ctx, q, have)
    seen: set[tuple[str, str]] = set()
    out = []
    for it in items + yahoo + sec + crypto:
        key = (it["kind"], it["value"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return {"items": out[:18], "note": note}
