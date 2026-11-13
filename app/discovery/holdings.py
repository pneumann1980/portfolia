"""SEC-Kennzahlen für Aktien im Bestand (0.114).

* **Welche Aktien:** nur Wertpapiere mit Yahoo-Kursquelle, deren Symbol ein US-Symbol ohne Börsenkürzel ist
  (``AAPL``, ``BRK-B`` – nicht ``SAP.DE``, kein Index, keine Devisen) **und** das im SEC-Verzeichnis eindeutig einem
  Emittenten gehört. Alles andere bleibt ohne SEC-Daten; es wird nichts geraten.
* **Laden:** ein eigener täglicher Hintergrundlauf (Job ``bestand``), unabhängig vom Schalter „Automatische Suche“.
  Er legt fehlende Unternehmen an (Herkunft ``portfolio``) und aktualisiert SEC- und Marktdaten nach denselben Regeln
  wie die Datenaktualisierung der Kandidaten (neuer SEC-Bericht → sofort, sonst wöchentlich/monatlich). Keine KI.
* **Nach dem vollständigen Verkauf:** Ein nur wegen des Bestands angelegtes Unternehmen (Herkunft ``portfolio``,
  Status ``neu``) wird zurückgestellt – Kennzahlen und Analysen bleiben gespeichert.
* **Anzeige:** :func:`brief_for_asset` liefert für den Einzeltitel Kennzahlen, Portfolia-Score (Datenteil) und den
  Verweis auf den Unternehmenssteckbrief – ausschließlich aus gespeicherten Daten.
"""

from __future__ import annotations

import logging
from typing import Any

from app.discovery.identity import norm_ticker
from app.watchlist.service import YAHOO_SYMBOL

log = logging.getLogger(__name__)
ORIGIN = "portfolio"
# Kennzahlen im Einzeltitel: (Schlüssel der SEC-Fundamentaldaten, Beschriftung, Format)
NUMBERS = (("revenue", "Umsatz", "money"), ("revenue_growth", "Umsatzwachstum", "pct_sign"),
           ("gross_margin", "Bruttomarge", "pct"), ("op_margin", "Operative Marge", "pct"),
           ("net_margin", "Nettomarge", "pct"), ("fcf", "Free Cashflow", "money"),
           ("fcf_margin", "Free-Cashflow-Marge", "pct"), ("net_cash", "Nettofinanzposition", "money"),
           ("debt", "Finanzverbindlichkeiten", "money"), ("roic", "ROIC", "pct"))


def us_symbol(quote_source: str | None, quote_id: str | None) -> str | None:
    """Yahoo-Symbol ohne Börsenkürzel (US-Listing) oder ``None``."""
    s = str(quote_id or "").strip().upper()
    if quote_source != "yahoo" or not s or not YAHOO_SYMBOL.match(s):
        return None
    return None if ("." in s or "=" in s or s.startswith("^")) else s


def held_symbols(ctx: Any) -> dict[str, str]:
    """US-Symbole der gehaltenen Wertpapiere → Asset-ID."""
    val = ctx.valuation()
    out: dict[str, str] = {}
    for p in (val.positions if val is not None else []):
        a = p.asset
        if not p.qty or p.qty <= 0 or not a.is_security:
            continue
        sym = us_symbol(a.quote_source, a.quote_id)
        if sym:
            out.setdefault(sym, a.asset_id)
    return out


def company_id_for(db: Any, quote_source: str | None, quote_id: str | None) -> int | None:
    """Discovery-Unternehmen zu einem Asset: exaktes US-Symbol, über die eindeutigen Ticker-Aliasse der SEC."""
    sym = us_symbol(quote_source, quote_id)
    if sym is None:
        return None
    r = db.q1("SELECT company_id FROM disc_alias WHERE kind='ticker' AND value_norm=?", (norm_ticker(sym),))
    if r is None:
        r = db.q1("SELECT id AS company_id FROM disc_company WHERE ticker=?", (norm_ticker(sym),))
    return int(r["company_id"]) if r else None


def sync(ctx: Any) -> dict[str, Any]:
    """Bestand abgleichen: fehlende Unternehmen anlegen, vollständig verkaufte (nur bestandsbedingte) zurückstellen."""
    from app.discovery import service as SV

    db = ctx.db
    held = held_symbols(ctx)
    added: list[str] = []
    skipped: dict[str, str] = {}
    ids: dict[str, int] = {}
    for sym in sorted(held):
        cid = company_id_for(db, "yahoo", sym)
        if cid is None:
            res, _d, err = SV.resolve(ctx, ticker=sym)
            if err:
                skipped[sym] = err
                continue
            if res.status != "ok" or norm_ticker(sym) not in {norm_ticker(t) for t in res.issuer.tickers}:
                skipped[sym] = res.reason or "kein eindeutiger SEC-Emittent"
                continue
            cid, why = SV.upsert_issuer(ctx, res.issuer, ORIGIN)
            if cid is None:
                skipped[sym] = why
                continue
            added.append(sym)
        ids[sym] = cid
    sold = []
    keep = set(ids.values())
    for r in db.q("SELECT id, ticker FROM disc_company WHERE origin=? AND status='neu'", (ORIGIN,)):
        if int(r["id"]) not in keep:
            SV.set_status(db, int(r["id"]), "zurueckgestellt", note="Aktie nicht mehr im Bestand")
            sold.append(r["ticker"])
    return {"gehalten": len(held), "ids": ids, "neu": added, "ohne_sec": skipped, "zurueckgestellt": sold}


def run(ctx: Any, job: Any) -> tuple[str, dict[str, Any]]:
    """Job ``bestand``: Abgleich und Datenaktualisierung (SEC, Kurse – ohne KI) der Aktien im Bestand."""
    from app.discovery import engine as EN

    job.step("Bestand mit dem SEC-Verzeichnis abgleichen")
    st = sync(ctx)
    ids = set(st.pop("ids").values())
    if not ids:
        return "abgeschlossen", {**st, "analysiert": []}
    status, res = EN.auto_analyses(ctx, job, ids=ids)
    return status, {**st, **res}


def run_scheduled(ctx: Any) -> dict[str, Any]:
    """Täglicher Lauf – unabhängig von „Automatische Suche“ (das ist keine Kandidatensuche)."""
    from app.discovery import jobs as J

    if not held_symbols(ctx):
        return {"skipped": "keine US-Aktien im Bestand"}
    return J.start(ctx, "bestand", trigger="automatisch", inline=True)


def brief_for_asset(ctx: Any, asset: Any) -> dict[str, Any] | None:
    """SEC-Block für den Einzeltitel. ``None``: kein US-Symbol (kein Block). Sonst ``state``:
    ``geladen`` (mit Kennzahlen), ``ausstehend`` (Unternehmen bekannt, noch keine Daten) oder ``unbekannt``
    (noch nicht abgeglichen bzw. kein SEC-Emittent)."""
    if not getattr(asset, "is_security", False):
        return None
    sym = us_symbol(asset.quote_source, asset.quote_id)
    if sym is None:
        return None
    cid = company_id_for(ctx.db, "yahoo", sym)
    if cid is None:
        return {"state": "unbekannt", "symbol": sym}
    return brief_for_company(ctx, cid, sym)


def brief_for_company(ctx: Any, cid: int, sym: str = "") -> dict[str, Any] | None:
    """SEC-Block für ein Discovery-Unternehmen (auch für die Watchlist, 0.115) – nur gespeicherte Daten."""
    from app.discovery import nash as NASH
    from app.discovery import service as SV

    db = ctx.db
    co = SV.company(db, cid)
    if co is None:
        return None
    sym = sym or co["ticker"]
    an = SV.analysis(db, cid)
    if an is None:
        return {"state": "ausstehend", "symbol": sym, "company_id": cid, "name": co["name"]}
    res = an["result"]
    fu = res.get("fundamentals") or {}
    numbers = []
    for key, label, fmt in NUMBERS:
        v = SV._latest(fu, key)
        if v is not None:
            numbers.append({"key": key, "label": label, "value": v, "fmt": fmt, "ccy": fu.get("currency") or ""})
    sc = res.get("scores") or {}
    years = fu.get("years") or []
    return {"state": "geladen", "symbol": sym, "company_id": cid, "name": co["name"], "numbers": numbers,
            "year": years[-1] if years else None, "filed": (res.get("latest_filing") or {}).get("filed"),
            "analysed_at": an["created_at"], "nash": NASH.split(sc.get("nash125")),
            "scores": {k: sc.get(k) for k in ("garp", "quality", "valuation")},
            "stage": SV._stage(res, SV.preview_of(db, cid) is not None, SV.current_filed(db, co, res))}
