"""Kursquellen zuordnen: Kryptowerte ohne Kursquelle (Import „none“/„manual“) automatisch auf CoinGecko suchen.

Ablauf (nach einem Import, täglich und auf Knopfdruck):

1. **Katalog** ``/coins/list`` inkl. Plattformen – ein Aufruf, 7 Tage zwischengespeichert. Gesucht wird lokal:
   An CoinGecko gehen weder Symbole noch Mengen oder Konten, nur danach die IDs der Kandidaten.
2. **Kandidaten** je Asset: Coins mit gleichem Symbol. Konten, auf denen das Asset gebucht ist, liefern
   Chain-Hinweise („MetaMask (BNB)“ → BNB Smart Chain, „Kaspa (KAS)“ → Kaspa). Coins, die nachweislich nur auf
   anderen Chains existieren, entfallen; Coins auf der Chain werden bevorzugt.
3. **Marktdaten** der Kandidaten (``/coins/markets``: Kurs, Marktkapitalisierung, Allzeithoch/-tief in EUR).
   Kandidaten ohne aktuellen Kurs entfallen, ebenso solche, deren Spanne (Allzeittief ÷ 3 … Allzeithoch × 3)
   die eigenen Transaktionskurse nicht enthält – dann ist es ein anderer Token mit gleichem Symbol
   (Beispiel: LUNA zu Kursen von LUNA Classic).
4. **Sicherheit:** „hoch“ = genau ein plausibler Kandidat (Chain passt bzw. unbekannt) und eigene Kurse liegen in
   seiner Spanne; „mittel“ = eindeutiger Marktführer (≥ 10× Marktkapitalisierung) oder ohne eigene Kurse bzw.
   ohne prüfbare Chain; sonst „niedrig“. Automatisch übernommen wird bis zur eingestellten Stufe (Standard: nur
   „hoch“), alles andere erscheint als Vorschlag unter *Datenqualität → Kursquellen* zur Bestätigung.

Spam-Token (Status „spam“ bzw. Kategorie mit „Spam“) werden nicht gesucht. Zuordnungen gelten über dem Import
(Tabelle ``asset_source``), fließen in den Gesamtexport ein und lassen sich jederzeit zurücknehmen.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import logging
import re
import statistics
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.ledger.models import AssetInfo, Portfolio
from app.prices.fallback import FallbackPrices
from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

CATALOG_TTL = timedelta(days=7)
RECHECK_AFTER = timedelta(days=7)
DOMINANCE = 10.0
RANGE_SLACK = 3.0
LEVELS = {"hoch": 3, "mittel": 2, "niedrig": 1}
AUTO_LEVELS = {"hoch": "nur eindeutige Treffer (empfohlen)", "mittel": "auch wahrscheinliche Treffer",
               "aus": "nie automatisch – nur Vorschläge"}
STATUS_LABEL = {"active": "zugeordnet", "suggested": "Vorschlag", "none": "kein Treffer", "rejected": "abgelehnt"}
OWN_SOURCES = ("coingecko", "yahoo")
_RUN_LOCK = threading.Lock()  # Job und Knopf „Jetzt suchen“ nicht gleichzeitig

# Kontoname → CoinGecko-Plattform(en). Börsenkonten liefern bewusst keinen Hinweis.
CHAIN_PATTERNS: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"\b(eth|ethereum|erc-?20)\b"), ("ethereum",)),
    (re.compile(r"\b(bnb|bsc|bep-?20|binance smart chain)\b"), ("binance-smart-chain",)),
    (re.compile(r"\b(avax|avalanche)\b"), ("avalanche",)),
    (re.compile(r"\b(matic|polygon)\b"), ("polygon-pos",)),
    (re.compile(r"\b(sol|solana|spl)\b"), ("solana",)),
    (re.compile(r"\b(arbitrum|arb)\b"), ("arbitrum-one",)),
    (re.compile(r"\bbase\b"), ("base",)),
    (re.compile(r"\boptimism\b"), ("optimistic-ethereum",)),
    (re.compile(r"\b(pulsechain|pls)\b"), ("pulsechain",)),
    (re.compile(r"\b(kaspa|kas|krc-?20)\b"), ("kaspa",)),
    (re.compile(r"\b(tron|trx|trc-?20)\b"), ("tron",)),
    (re.compile(r"\b(cardano|ada)\b"), ("cardano",)),
    (re.compile(r"\bpeaq\b"), ("peaq",)),
    (re.compile(r"\b(xverse|ordinals|runes|brc-?20)\b"), ("ordinals", "bitcoin")),
]
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_URL_RE = re.compile(r"coingecko\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?[^/?#]+/([a-z0-9-]+)", re.I)


def chain_hints(accounts: list[str]) -> set[str]:
    out: set[str] = set()
    for acc in accounts:
        low = acc.lower()
        for rx, platforms in CHAIN_PATTERNS:
            if rx.search(low):
                out.update(platforms)
    return out


def parse_coin_id(raw: str) -> str | None:
    """CoinGecko-ID aus Eingabe (ID oder Link auf coingecko.com)."""
    v = (raw or "").strip()
    m = _URL_RE.search(v)
    if m:
        v = m.group(1)
    v = v.strip().strip("/").lower()
    return v if _ID_RE.match(v) else None


def is_spam(a: AssetInfo) -> bool:
    return (a.status or "").lower() == "spam" or "spam" in (a.category or "").lower()


def needs_source(a: AssetInfo) -> bool:
    return a.is_crypto and not (a.quote_source in OWN_SOURCES and a.quote_id)


# -- Katalog --------------------------------------------------------------------------------------------

class Catalog:
    """CoinGecko-Coinliste, nach Symbol indiziert (Datei-Cache im Cache-Verzeichnis)."""

    def __init__(self, coins: list[dict[str, Any]], fetched_at: datetime) -> None:
        self.fetched_at = fetched_at
        self.by_id: dict[str, dict[str, Any]] = {}
        self.by_symbol: dict[str, list[dict[str, Any]]] = {}
        for c in coins:
            cid = str(c.get("id") or "").strip()
            sym = str(c.get("symbol") or "").strip().lower()
            if not cid or not sym:
                continue
            self.by_id[cid] = c
            self.by_symbol.setdefault(sym, []).append(c)

    def __len__(self) -> int:
        return len(self.by_id)

    def candidates(self, symbol: str) -> list[dict[str, Any]]:
        sym = symbol.strip().lower()
        seen: dict[str, dict[str, Any]] = {}
        for key in dict.fromkeys((sym, sym.replace("-", ""), sym.replace(".", ""))):
            for c in self.by_symbol.get(key, []):
                seen.setdefault(c["id"], c)
        return list(seen.values())

    @staticmethod
    def platforms(coin: dict[str, Any]) -> list[str]:
        return sorted(k for k, v in (coin.get("platforms") or {}).items() if k)

    @classmethod
    def load(cls, path: Path, fetch: Any, force: bool = False) -> Catalog:
        """Aus dem Cache (≤ 7 Tage) oder per ``fetch()`` neu; bei Abruffehlern notfalls veralteter Cache."""
        cached: tuple[list[dict[str, Any]], datetime] | None = None
        if path.exists():
            try:
                raw = json.loads(gzip.decompress(path.read_bytes()))
                cached = (raw["coins"], parse_iso(raw["fetched_at"]) or datetime.fromtimestamp(0, UTC))
            except (OSError, ValueError, KeyError) as e:
                log.warning("CoinGecko-Katalog im Cache unlesbar: %s", e)
        if cached and not force and datetime.now(UTC) - cached[1] < CATALOG_TTL:
            return cls(*cached)
        try:
            coins = fetch()
            if not coins:
                raise ValueError("CoinGecko lieferte einen leeren Katalog")
        except Exception:
            if cached:
                log.warning("CoinGecko-Katalog nicht abrufbar – verwende Stand vom %s", cached[1].date())
                return cls(*cached)
            raise
        now = datetime.now(UTC)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(json.dumps({"fetched_at": iso(now), "coins": coins}).encode()))
        tmp.replace(path)
        return cls(coins, now)


# -- Entscheidung --------------------------------------------------------------------------------------

@dataclass
class Candidate:
    id: str
    name: str
    platforms: list[str]
    price: float | None = None
    market_cap: float | None = None
    ath: float | None = None
    atl: float | None = None
    chain_match: bool = False
    other_chain: bool = False  # nachweislich nur auf anderen Chains
    plausible: bool | None = None  # None = keine eigenen Kurse zum Vergleich

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Decision:
    coin_id: str | None
    confidence: str | None
    reason: str
    candidates: list[Candidate] = field(default_factory=list)


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def decide(coins: list[dict[str, Any]], markets: dict[str, dict[str, Any]], hints: set[str],
           ref_price: float | None) -> Decision:
    cands = []
    for c in coins:
        m = markets.get(c["id"], {})
        plats = Catalog.platforms(c)
        cand = Candidate(c["id"], str(c.get("name") or c["id"]), plats, _f(m.get("current_price")),
                         _f(m.get("market_cap")), _f(m.get("ath")), _f(m.get("atl")))
        if hints:
            native = not plats and any(cand.id == h or cand.id.startswith(h + "-") for h in hints)
            cand.chain_match = bool(set(plats) & hints) or native
            cand.other_chain = bool(plats) and not cand.chain_match
        if ref_price and cand.atl and cand.ath:
            cand.plausible = cand.atl / RANGE_SLACK <= ref_price <= cand.ath * RANGE_SLACK
        cands.append(cand)
    cands.sort(key=lambda c: -(c.market_cap or 0))
    if not cands:
        return Decision(None, None, "kein CoinGecko-Coin mit diesem Symbol")
    priced = [c for c in cands if c.price]
    if not priced:
        return Decision(None, None, "Coins mit diesem Symbol haben keinen aktuellen Kurs", cands)
    pool = [c for c in priced if c.plausible is not False and not c.other_chain]
    if not pool:
        why = []
        if any(c.plausible is False for c in priced):
            why.append("eigene Transaktionskurse liegen außerhalb der Kursspanne")
        if any(c.other_chain for c in priced):
            why.append("nur auf anderen Chains als in den Konten")
        return Decision(None, None, "; ".join(why) or "kein passender Kandidat", cands)
    on_chain = [c for c in pool if c.chain_match]
    if on_chain:
        pool = on_chain
    top = pool[0]
    parts = []
    if len(pool) == 1:
        level = "hoch" if top.plausible else "mittel"
        parts.append("einziger passender Coin" if len(priced) == 1 else f"einziger passender von {len(priced)} Coins")
    else:
        second = pool[1].market_cap or 0.0
        dominant = bool(top.market_cap) and (second == 0 or top.market_cap >= DOMINANCE * second)  # type: ignore[operator]
        level = ("mittel" if top.plausible else "niedrig") if dominant else "niedrig"
        parts.append(f"größte Marktkapitalisierung von {len(pool)} Kandidaten" if dominant
                     else f"{len(pool)} ähnlich große Kandidaten")
    if hints:
        if top.chain_match:
            parts.append("Chain passt")
        else:
            parts.append("Chain nicht prüfbar")
            level = min(level, "mittel", key=lambda x: LEVELS[x])
    if top.plausible:
        parts.append("eigene Kurse liegen in der Kursspanne")
    elif top.plausible is None:
        parts.append("keine eigenen Kurse zum Vergleich")
    return Decision(top.id, level, ", ".join(parts), cands)


# -- Dienst --------------------------------------------------------------------------------------------

class SourceService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.db = ctx.db

    @property
    def cache_path(self) -> Path:
        return self.ctx.config.cache_dir / "coingecko-coins.json.gz"

    def rows(self) -> dict[str, Any]:
        return {r["asset_id"]: r for r in self.db.q("SELECT * FROM asset_source")}

    def targets(self, pf: Portfolio, force: bool = False) -> list[AssetInfo]:
        """Kryptowerte ohne eigene Kursquelle, ohne Spam; bereits entschiedene/frische Einträge nicht erneut."""
        rows = self.rows()
        now = datetime.now(UTC)
        used = {a for t in pf.txs for a in (t.from_asset, t.to_asset) if a}
        out = []
        for aid in sorted(used):
            a = pf.assets.get(aid)
            if a is None or not needs_source(a) or is_spam(a):
                continue
            r = rows.get(aid)
            if r is not None:
                if r["status"] in ("active", "rejected"):
                    continue
                checked = parse_iso(r["checked_at"])
                if not force and checked and now - checked < RECHECK_AFTER:
                    continue
            out.append(a)
        return out

    def run(self, force: bool = False) -> dict[str, Any]:
        if not _RUN_LOCK.acquire(blocking=False):
            return {"skipped": "Suche läuft bereits"}
        try:
            return self._run(force)
        finally:
            _RUN_LOCK.release()

    def _run(self, force: bool) -> dict[str, Any]:
        cg = getattr(self.ctx.prices, "cg", None)
        if cg is None:
            return {"skipped": "CoinGecko nicht verfügbar (Demo-Modus)"}
        pf = self.ctx.recorded_portfolio()
        if pf is None:
            return {"skipped": "keine Buchungen"}
        targets = self.targets(pf, force)
        if not targets:
            return {"checked": 0}
        from app.prices.coingecko import BudgetExceeded

        try:
            catalog = Catalog.load(self.cache_path, cg.coins_list, force=False)
        except BudgetExceeded as e:
            return {"skipped": str(e)}
        accounts: dict[str, set[str]] = {}
        for t in pf.txs:
            for acc, aid in ((t.from_account, t.from_asset), (t.to_account, t.to_asset)):
                if acc and aid:
                    accounts.setdefault(aid, set()).add(acc)
        fb = FallbackPrices(pf, None, only={a.asset_id for a in targets})
        per_asset = {a.asset_id: catalog.candidates(a.symbol) for a in targets}
        ids = sorted({c["id"] for cs in per_asset.values() for c in cs})
        try:
            markets = cg.markets(ids) if ids else {}
        except BudgetExceeded as e:
            return {"skipped": str(e)}
        level = str(self.ctx.settings.get("prices.auto_map", "hoch"))
        now = iso(datetime.now(UTC))
        applied, suggested, none = [], [], []
        with self.db.transaction() as c:
            for a in targets:
                pts = [p.price for p in fb.points(a.asset_id)]
                ref = statistics.median(pts) if pts else None
                d = decide(per_asset[a.asset_id], markets, chain_hints(sorted(accounts.get(a.asset_id, ()))), ref)
                if d.coin_id is None:
                    status = "none"
                    none.append(a.asset_id)
                elif level in LEVELS and LEVELS[d.confidence or "niedrig"] >= LEVELS[level]:
                    status = "active"
                    applied.append(f"{a.asset_id}→{d.coin_id}")
                else:
                    status = "suggested"
                    suggested.append(a.asset_id)
                c.execute(
                    """INSERT INTO asset_source(asset_id, quote_source, quote_id, status, origin, confidence, reason,
                           candidates_json, checked_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(asset_id) DO UPDATE SET quote_id=excluded.quote_id, status=excluded.status,
                           origin=excluded.origin, confidence=excluded.confidence, reason=excluded.reason,
                           candidates_json=excluded.candidates_json, checked_at=excluded.checked_at,
                           updated_at=excluded.updated_at""",
                    (a.asset_id, "coingecko", d.coin_id, status, "auto", d.confidence, d.reason,
                     json.dumps([x.as_dict() for x in d.candidates[:12]], ensure_ascii=False), now, now))
        if applied:
            log.info("Kursquellen automatisch zugeordnet: %s", ", ".join(applied))
            self._changed()
        return {"checked": len(targets), "applied": applied, "suggested": len(suggested), "none": len(none),
                "catalog": len(catalog)}

    # -- Entscheidungen des Nutzers ---------------------------------------------------------------------
    def _known(self, coin_id: str) -> bool:
        try:
            raw = json.loads(gzip.decompress(self.cache_path.read_bytes()))
        except (OSError, ValueError):
            return True  # ohne Katalog nicht prüfbar – Format ist geprüft
        return any(c.get("id") == coin_id for c in raw.get("coins", []))

    def accept(self, asset_id: str, raw_id: str) -> str | None:
        """Zuordnung übernehmen (Vorschlag oder eigene Eingabe). Rückgabe: Fehlertext oder None."""
        pf = self.ctx.recorded_portfolio()
        a = pf.assets.get(asset_id) if pf else None
        if a is None or not a.is_crypto:
            return "Asset nicht gefunden."
        coin_id = parse_coin_id(raw_id)
        if coin_id is None:
            return "Ungültige CoinGecko-ID – z. B. „xen-crypto“ oder den Link von coingecko.com einfügen."
        if not self._known(coin_id):
            return f"„{coin_id}“ ist im CoinGecko-Katalog nicht vorhanden."
        now = iso(datetime.now(UTC))
        self.db.x(
            """INSERT INTO asset_source(asset_id, quote_source, quote_id, status, origin, confidence, reason,
                   checked_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(asset_id) DO UPDATE SET quote_id=excluded.quote_id, status='active', origin='user',
                   reason=excluded.reason, updated_at=excluded.updated_at""",
            (asset_id, "coingecko", coin_id, "active", "user", None, "vom Nutzer bestätigt", now, now))
        log.info("Kursquelle zugeordnet: %s → CoinGecko %s", asset_id, coin_id)
        self._changed()
        return None

    def reject(self, asset_id: str) -> None:
        self.db.x("UPDATE asset_source SET status='rejected', origin='user', updated_at=? WHERE asset_id=?",
                  (iso(datetime.now(UTC)), asset_id))
        self._changed()

    def reset(self, asset_id: str) -> None:
        """Zuordnung bzw. Ablehnung entfernen – das Asset wird beim nächsten Lauf neu gesucht."""
        self.db.x("DELETE FROM asset_source WHERE asset_id=?", (asset_id,))
        self._changed()

    def _changed(self) -> None:
        self.ctx.invalidate_overlay()
        sched = getattr(self.ctx, "scheduler", None)
        if sched is not None:
            sched.trigger("prices_crypto", 2, force=True)
            sched.trigger("history_backfill", 20)

    # -- Anzeige ----------------------------------------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        pf = self.ctx.recorded_portfolio()
        led = self.ctx.ledger()
        held = led.holdings_by_asset() if led else {}
        rows = self.rows()
        items = []
        if pf is not None:
            used = {a for t in pf.txs for a in (t.from_asset, t.to_asset) if a}
            for aid in sorted(used):
                a = pf.assets.get(aid)
                if a is None or not a.is_crypto:
                    continue
                r = rows.get(aid)
                own = a.quote_source in OWN_SOURCES and a.quote_id and (r is None or r["status"] != "active")
                if own or (r is None and not needs_source(a)):
                    continue
                if r is None and is_spam(a):
                    continue
                items.append({
                    "asset": a, "row": r, "held": float(held.get(aid, 0) or 0) > 0, "spam": is_spam(a),
                    "status": r["status"] if r else "open",
                    "candidates": json.loads(r["candidates_json"]) if r and r["candidates_json"] else [],
                })
        order = {"suggested": 0, "active": 1, "open": 2, "none": 3, "rejected": 4}
        items.sort(key=lambda x: (order.get(x["status"], 9), not x["held"], x["asset"].asset_id.lower()))
        catalog_at = None
        if self.cache_path.exists():
            catalog_at = datetime.fromtimestamp(self.cache_path.stat().st_mtime, UTC)
        return {"items": items, "catalog_at": catalog_at,
                "counts": {k: sum(1 for x in items if x["status"] == k) for k in order}}


def apply_sources(db: Any, assets: dict[str, AssetInfo]) -> dict[str, AssetInfo]:
    """Aktive Zuordnungen auf Assets ohne eigene Kursquelle anwenden (gleiches Objekt, wenn nichts zu tun ist)."""
    try:
        rows = db.q("SELECT asset_id, quote_source, quote_id, origin FROM asset_source "
                    "WHERE status='active' AND quote_id IS NOT NULL")
    except Exception:  # Tabelle fehlt (sehr alte DB vor der Migration)
        return assets
    out = None
    for r in rows:
        a = assets.get(r["asset_id"])
        if a is None or not needs_source(a):
            continue
        if out is None:
            out = dict(assets)
        out[a.asset_id] = dataclasses.replace(a, quote_source=r["quote_source"], quote_id=r["quote_id"],
                                              extra={**a.extra, "source_map": r["origin"]})
    return out if out is not None else assets


def source_service(ctx: Any) -> SourceService:
    return SourceService(ctx)
