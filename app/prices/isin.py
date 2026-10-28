"""ISIN-Ermittlung für Wertpapiere, die nur mit WKN geführt werden (automatisch, ohne Eingabe).

Quellen in dieser Reihenfolge (jede Zuordnung wird mit Quelle und Zeitpunkt gespeichert, nie still ersetzt):

1. **Eigene Belege:** ISIN/WKN-Paare aus den ausgewerteten Bankdokumenten (``bank_doc.meta``) – offline und exakt.
2. **Onvista-Suche** (öffentlich, ohne Schlüssel): Treffer zählen nur, wenn die WKN des Treffers **genau** der gesuchten
   WKN entspricht und die ISIN die Prüfziffer besteht; mehrdeutige Treffer (verschiedene ISIN) werden verworfen.
3. **Derivate deutscher Emittenten:** ISIN = ``DE000`` + WKN + Prüfziffer (Regel der WKN-Vergabe), nur für Assets vom
   Typ Derivat, die keine der Quellen kennt – als „berechnet“ gekennzeichnet.

Ergebnisse stehen in der Einstellung ``assets.isin`` (Asset-ID → ISIN, Quelle, Name, Zeitpunkt). Ohne Treffer wird
nach 14 Tagen erneut gesucht; Netzwerkfehler werden nicht als „kein Treffer“ gespeichert. Die importierten Stammdaten
bleiben unverändert – die Steuerauswertung ergänzt fehlende ISIN aus diesem Verzeichnis.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db import fallback
from app.documentimport.profiles import isin_ok
from app.tax.classify import classify_asset
from app.util.http import HttpError, RateLimiter, request_with_retry
from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)
KEY = "assets.isin"
KEY_LAST = "assets.isin_last"
ONVISTA_SEARCH = "https://api.onvista.de/api/v1/instruments/search"
RETRY_DAYS = 14
MAX_PER_RUN = 120
_WKN = re.compile(r"^[A-Z0-9]{6}$")


def isin_check_digit(body: str) -> str:
    """Prüfziffer für die ersten 11 Zeichen einer ISIN."""
    digits = "".join(str(int(c, 36)) for c in body)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def de_isin_from_wkn(wkn: str) -> str | None:
    """ISIN deutscher Emittenten: ``DE000`` + WKN + Prüfziffer (nur gültig, wenn die Prüfziffer besteht)."""
    w = wkn.strip().upper()
    if not _WKN.match(w):
        return None
    body = f"DE000{w}"
    isin = body + isin_check_digit(body)
    return isin if isin_ok(isin) else None


def onvista_lookup(client: Any, wkn: str, limiter: RateLimiter | None = None) -> tuple[str, str] | None:
    """(ISIN, Name) zu einer WKN über die Onvista-Suche; ``None`` ohne eindeutigen Treffer."""
    resp = request_with_retry(client, "GET", ONVISTA_SEARCH, params={"searchValue": wkn}, limiter=limiter, retries=2)
    try:
        data = resp.json()
    except ValueError:
        return None
    hits = {(str(x.get("isin") or "").upper(), str(x.get("name") or ""))
            for x in (data.get("list") or []) if isinstance(x, dict)
            and str(x.get("wkn") or "").upper() == wkn.upper() and isin_ok(str(x.get("isin") or "").upper())}
    isins = {h[0] for h in hits}
    if len(isins) != 1:
        return None
    isin = next(iter(isins))
    return isin, next(name for i, name in sorted(hits) if i == isin)


class IsinService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    # -- Lesen ---------------------------------------------------------------------------------------------
    def entries(self) -> dict[str, dict[str, Any]]:
        v = self.ctx.settings.get(KEY)
        return dict(v) if isinstance(v, dict) else {}

    def last(self) -> dict[str, Any]:
        v = self.ctx.settings.get(KEY_LAST)
        return dict(v) if isinstance(v, dict) else {}

    def mapping(self) -> dict[str, str]:
        """Asset-ID → ISIN (nur gefundene)."""
        return {aid: e["isin"] for aid, e in self.entries().items() if isinstance(e, dict) and e.get("isin")}

    def _candidates(self) -> list[Any]:
        pf = self.ctx.portfolio()
        if pf is None:
            return []
        return [a for a in pf.assets.values() if a.is_security and a.wkn and not a.isin]

    def pending(self) -> list[Any]:
        """Wertpapiere ohne ISIN, die (erneut) gesucht werden sollen."""
        entries = self.entries()
        cutoff = datetime.now(UTC) - timedelta(days=RETRY_DAYS)
        out = []
        for a in self._candidates():
            e = entries.get(a.asset_id)
            if e and (e.get("isin") or (parse_iso(e.get("checked") or "") or cutoff) > cutoff):
                continue
            out.append(a)
        return out

    # -- Ermitteln -------------------------------------------------------------------------------------------
    def _local_pairs(self) -> dict[str, str]:
        """WKN → ISIN aus den Kennungen der ausgewerteten Bankdokumente."""
        out: dict[str, set[str]] = {}
        try:
            rows = self.ctx.db.q("SELECT meta FROM bank_doc WHERE meta IS NOT NULL AND meta LIKE '%isin%'")
        except sqlite3.Error as e:  # Tabelle fehlt (sehr alte DB)
            fallback("_local_pairs", e)
            return {}
        for r in rows:
            try:
                m = json.loads(r["meta"] or "{}")
            except ValueError:
                continue
            isin, wkn = str(m.get("isin") or "").upper(), str(m.get("wkn") or "").upper()
            if isin and wkn and isin_ok(isin):
                out.setdefault(wkn, set()).add(isin)
        return {w: next(iter(s)) for w, s in out.items() if len(s) == 1}

    def resolve(self, client: Any = None, limit: int = MAX_PER_RUN, sleep_s: float = 0.6) -> dict[str, int]:
        """Sucht fehlende ISIN; Rückgabe: Zähler (found, local, online, computed, none, errors, skipped)."""
        stats = {"found": 0, "local": 0, "online": 0, "computed": 0, "none": 0, "errors": 0, "skipped": 0}
        todo = self.pending()
        if not todo:
            return stats
        entries = self.entries()
        now = iso(datetime.now(UTC))
        local = self._local_pairs()
        limiter = RateLimiter(sleep_s)
        online = client if client is not None else (None if self.ctx.config.demo_mode else self.ctx.http)
        down = False
        for a in todo[:limit]:
            wkn = a.wkn.upper()
            if wkn in local:
                entries[a.asset_id] = {"isin": local[wkn], "source": "Bankbeleg", "name": a.name, "checked": now}
                stats["local"] += 1
                stats["found"] += 1
                continue
            hit = None
            if online is not None and not down:
                try:
                    hit = onvista_lookup(online, wkn, limiter)
                except HttpError as e:
                    stats["errors"] += 1
                    log.warning("ISIN-Suche für %s fehlgeschlagen: %s", wkn, e)
                    down = e.status is None  # Netz weg: übrige nicht versuchen
                    continue
            if hit:
                entries[a.asset_id] = {"isin": hit[0], "source": "Onvista", "name": hit[1], "checked": now}
                stats["online"] += 1
                stats["found"] += 1
                continue
            if classify_asset(a)[0] == "derivative" and (isin := de_isin_from_wkn(wkn)):
                entries[a.asset_id] = {"isin": isin, "source": "berechnet (DE-WKN-Regel)", "name": a.name,
                                       "checked": now}
                stats["computed"] += 1
                stats["found"] += 1
                continue
            if online is None or down:
                stats["skipped"] += 1
                continue
            entries[a.asset_id] = {"isin": None, "source": None, "name": a.name, "checked": now}
            stats["none"] += 1
        stats["skipped"] += max(0, len(todo) - limit)
        if stats["found"] or stats["none"]:
            self.ctx.settings.set(KEY, entries)
        self.ctx.settings.set(KEY_LAST, {"at": now, **stats, "open": len(self.pending())})
        log.info("ISIN-Ermittlung: %s", stats)
        return stats


def isin_service(ctx: Any) -> IsinService:
    return IsinService(ctx)
