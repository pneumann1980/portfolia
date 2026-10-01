"""Kursquellen zuordnen: Kryptowerte ohne Kursquelle (Import „none“/„manual“) automatisch auf CoinGecko suchen.

Ablauf (nach einem Import, täglich und auf Knopfdruck):

1. **Katalog** ``/coins/list`` inkl. Plattformen – ein Aufruf, 7 Tage zwischengespeichert. Gesucht wird lokal:
   An CoinGecko gehen weder Symbole noch Mengen oder Konten, nur danach die IDs der Kandidaten.
2. **Kandidaten** je Asset: Ist das Asset Tokens einer Wallet-Anbindung zugeordnet (``SYMBOL@CHAIN:Contract``),
   entscheidet der Contract im Katalog – eindeutig, „hoch“, ohne Marktdaten. Sonst Coins mit gleichem Symbol;
   Konten, auf denen das Asset gebucht ist, liefern Chain-Hinweise („MetaMask (BNB)“ → BNB Smart Chain,
   „Kaspa (KAS)“ → Kaspa). Coins, die nachweislich nur auf anderen Chains existieren, entfallen; Coins auf der
   Chain werden bevorzugt.
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
from collections.abc import Callable
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
CATALOG_FILE = "coingecko-coins.json.gz"
CATALOG_RETRY = timedelta(minutes=30)  # Katalog-Abruf höchstens so oft anstoßen (nie je Seitenaufruf)

# Chain-Kürzel der Wallet-Anbindungen (Token-Kennung ``SYMBOL@CHAIN:Contract``) → CoinGecko-Plattform
CHAIN_PLATFORMS: dict[str, tuple[str, ...]] = {"ETH": ("ethereum",), "BSC": ("binance-smart-chain",),
                                               "AVAX": ("avalanche",), "SOL": ("solana",)}
_KASPA_PLATFORM = re.compile(r"kaspa|krc", re.I)  # KRC-20: Plattform-Bezeichnung bei CoinGecko nicht festgelegt

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


def split_token(key: str | None) -> tuple[str, str, str] | None:
    """Token-Kennung ``SYMBOL@CHAIN:Contract`` → (Symbol, CHAIN, Contract); sonst None."""
    sym, at, rest = (key or "").strip().partition("@")
    chain, colon, contract = rest.partition(":")
    if not at or not colon or not chain or not contract.strip():
        return None
    return sym, chain.upper(), contract.strip()


# -- Katalog --------------------------------------------------------------------------------------------

class Catalog:
    """CoinGecko-Coinliste, nach Symbol indiziert (Datei-Cache im Cache-Verzeichnis)."""

    def __init__(self, coins: list[dict[str, Any]], fetched_at: datetime) -> None:
        self.fetched_at = fetched_at
        self.by_id: dict[str, dict[str, Any]] = {}
        self.by_symbol: dict[str, list[dict[str, Any]]] = {}
        self._contracts: dict[str, list[tuple[str, str, dict[str, Any]]]] | None = None
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

    def by_contract(self, platform: Callable[[str], bool], contract: str,
                    case_sensitive: bool = False) -> list[dict[str, Any]]:
        """Coins mit dieser Contract-Adresse auf einer passenden Plattform.

        Verglichen wird ohne Groß-/Kleinschreibung (EVM-Adressen, gespeicherte Zuordnungen in Großschreibung);
        ``case_sensitive`` (Solana-Mints) entscheidet nur, falls dabei mehrere Coins übrig bleiben.
        """
        if self._contracts is None:
            idx: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
            for c in self.by_id.values():
                for plat, addr in (c.get("platforms") or {}).items():
                    a = str(addr or "").strip()
                    if plat and a:
                        idx.setdefault(a.lower(), []).append((str(plat).lower(), a, c))
            self._contracts = idx
        want = contract.strip()
        hits = [(addr, c) for plat, addr, c in self._contracts.get(want.lower(), []) if platform(plat)]
        if case_sensitive and len({c["id"] for _, c in hits}) > 1:
            hits = [(addr, c) for addr, c in hits if addr == want]
        return list({c["id"]: c for _, c in hits}.values())

    def for_token(self, chain: str, contract: str) -> list[dict[str, Any]]:
        """Coins zu einem Token einer Wallet-Anbindung (Chain-Kürzel wie in ``SYMBOL@CHAIN:Contract``)."""
        chain = chain.upper()
        if chain == "KAS":
            return self.by_contract(lambda p: bool(_KASPA_PLATFORM.search(p)), contract)
        plats = CHAIN_PLATFORMS.get(chain)
        if not plats:
            return []
        return self.by_contract(lambda p: p in plats, contract, case_sensitive=chain == "SOL")

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


_CACHED: dict[str, Any] = {}
_CACHED_LOCK = threading.Lock()
_TRIGGERED: dict[str, datetime] = {}  # Katalogpfad → Zeitpunkt des letzten Anstoßes


def catalog_path(ctx: Any) -> Path:
    return Path(ctx.config.cache_dir) / CATALOG_FILE


def cached_catalog(path: Path) -> Catalog | None:
    """Katalog aus dem Datei-Cache, ohne Abruf; im Speicher gehalten, solange die Datei unverändert bleibt."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _CACHED_LOCK:
        if _CACHED.get("key") == key:
            return _CACHED["catalog"]  # type: ignore[no-any-return]
    try:
        raw = json.loads(gzip.decompress(path.read_bytes()))
        cat = Catalog(list(raw["coins"]), parse_iso(raw["fetched_at"]) or datetime.fromtimestamp(st.st_mtime, UTC))
    except (OSError, ValueError, KeyError, TypeError, EOFError) as e:
        log.warning("CoinGecko-Katalog im Cache unlesbar: %s", e)
        return None
    with _CACHED_LOCK:
        _CACHED.clear()
        _CACHED.update(key=key, catalog=cat)
    return cat


def refresh_catalog(ctx: Any, force: bool = False) -> dict[str, Any]:
    """Coin-Katalog laden bzw. nach 7 Tagen erneuern – ein Aufruf ohne Bezug zum Portfolio."""
    cg = getattr(ctx.prices, "cg", None)
    if cg is None:
        return {"skipped": "CoinGecko nicht verfügbar (Demo-Modus)"}
    cat = Catalog.load(catalog_path(ctx), cg.coins_list, force=force)
    return {"coins": len(cat), "fetched_at": iso(cat.fetched_at)}


def catalog_state(ctx: Any, start: bool = False) -> dict[str, Any]:
    """Lokaler Katalog für Vorschläge samt Zustand; ``start`` lädt einen fehlenden oder veralteten Katalog im
    Hintergrund (Job „coingecko_catalog“), nach einem Fehler frühestens nach ``CATALOG_RETRY`` erneut."""
    path = catalog_path(ctx)
    cat = cached_catalog(path)
    cg = getattr(getattr(ctx, "prices", None), "cg", None)
    job = ctx.db.q1("SELECT running, last_end, last_ok, last_error FROM job_status WHERE job='coingecko_catalog'")
    now = datetime.now(UTC)
    running = bool(job and job["running"])
    ended = parse_iso(job["last_end"]) if job and job["last_end"] else None
    failed = bool(ended and job and not job["last_ok"] and not running)
    stale = cat is None or now - cat.fetched_at > CATALOG_TTL
    # höchstens ein Versuch je ``CATALOG_RETRY`` – auch wenn ein Abruf „erfolgreich“ auf den alten Stand zurückfiel
    due = start and stale and cg is not None and not running and not (ended and now - ended < CATALOG_RETRY)
    if due and getattr(ctx, "scheduler", None) is not None and ctx.scheduler.trigger("coingecko_catalog", 0.2):
        _TRIGGERED[str(path)] = now
    # angestoßen, aber noch nicht gestartet (Warteschlange des Schedulers) – ebenfalls „wird geladen“
    trig = _TRIGGERED.get(str(path))  # job_status speichert sekundengenau
    queued = bool(trig and now - trig < timedelta(minutes=2) and not (ended and ended >= trig.replace(microsecond=0)))
    return {"catalog": cat, "fetched_at": cat.fetched_at if cat else None, "stale": stale,
            "available": cg is not None, "loading": running or queued,
            "error": (job["last_error"] or "Abruf fehlgeschlagen") if failed and job and not queued else None}


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
        return catalog_path(self.ctx)

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
        exact = {a.asset_id: d for a in targets if (d := self._by_contract(catalog, a.asset_id)) is not None}
        per_asset = {a.asset_id: catalog.candidates(a.symbol) for a in targets if a.asset_id not in exact}
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
                prov = self._provider_identity(a, accounts.get(a.asset_id, set()))
                if prov is not None:  # Anbieter-Kürzel (z. B. Bitpanda „TH“): nie über das Symbol zuordnen
                    d = prov
                elif a.asset_id in exact:
                    d = exact[a.asset_id]
                else:
                    pts = [p.price for p in fb.points(a.asset_id)]
                    ref = statistics.median(pts) if pts else None
                    d = decide(per_asset[a.asset_id], markets, chain_hints(sorted(accounts.get(a.asset_id, ()))),
                               ref)
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

    @staticmethod
    def _provider_identity(a: AssetInfo, accounts: set[str]) -> Decision | None:
        """Kürzel mit Anbieter-Identität (:mod:`app.csvimport.identity`): Nur auf Konten dieses Anbieters gebucht →
        dessen Coin („hoch“); auch auf anderen Konten gebucht → nur ein Vorschlag („niedrig“), denn dort kann das
        Kürzel einen anderen Coin bezeichnen. Bestehende Zuordnungen prüft die Suche ohnehin nicht erneut."""
        from app.csvimport.identity import PROVIDER_LABEL, identity, provider_of

        hits = {}
        other = False
        for acc in sorted(accounts):
            p = provider_of(None, None, acc)
            pa = identity(p, a.symbol) if p else None
            if pa is not None:
                hits[pa.coingecko] = pa
            else:
                other = True
        if not hits:
            return None
        if len(hits) > 1 or None in hits:
            return Decision(None, None, "Kürzel bezeichnet bei mehreren Anbietern verschiedene Coins – bitte zuordnen")
        pa = next(iter(hits.values()))
        cand = Candidate(str(pa.coingecko), pa.name, [])
        label = PROVIDER_LABEL.get(pa.provider, pa.provider)
        if other:
            return Decision(pa.coingecko, "niedrig", f"{label} führt {pa.symbol} als {pa.name}; auf anderen Konten "
                                                     f"kann {pa.symbol} ein anderer Coin sein – bitte prüfen", [cand])
        return Decision(pa.coingecko, "hoch", f"{label} führt {pa.symbol} als {pa.name} (Anbieter-Identität, nicht "
                                              "über das Symbol)", [cand])

    def _by_contract(self, catalog: Catalog, asset_id: str) -> Decision | None:
        """Eindeutiger Coin über die Contracts der Tokens, die einem Asset zugeordnet sind (Wallet-Anbindungen,
        Tabelle ``csv_symbol``) – genauer als jede Suche über das Symbol."""
        coins: dict[str, dict[str, Any]] = {}
        chains = set()
        for r in self.db.q("SELECT symbol FROM csv_symbol WHERE asset_id=?", (asset_id,)):
            tok = split_token(r["symbol"])
            if tok is not None:
                chains.add(tok[1])
                coins.update((c["id"], c) for c in catalog.for_token(tok[1], tok[2]))
        if len(coins) != 1:
            return None
        coin = next(iter(coins.values()))
        cand = Candidate(coin["id"], str(coin.get("name") or coin["id"]), Catalog.platforms(coin), chain_match=True)
        return Decision(coin["id"], "hoch", f"Contract laut CoinGecko-Katalog ({', '.join(sorted(chains))})", [cand])

    # -- Entscheidungen des Nutzers ---------------------------------------------------------------------
    def _known(self, coin_id: str) -> bool:
        cat = cached_catalog(self.cache_path)
        return cat is None or coin_id in cat.by_id  # ohne Katalog nicht prüfbar – Format ist geprüft

    def accept(self, asset_id: str, raw_id: str, reason: str = "vom Nutzer bestätigt",
               notify: bool = True) -> str | None:
        """Zuordnung übernehmen (Vorschlag oder eigene Eingabe). Rückgabe: Fehlertext oder None.

        ``notify=False``: Kursabruf erst mit :meth:`changed` anstoßen (mehrere Zuordnungen in einem Schritt)."""
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
            (asset_id, "coingecko", coin_id, "active", "user", None, reason[:200], now, now))
        log.info("Kursquelle zugeordnet: %s → CoinGecko %s", asset_id, coin_id)
        if notify:
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

    def changed(self) -> None:
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
