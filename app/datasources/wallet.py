"""Gemeinsamer Ablauf für Wallet-Anbindungen (nur lesend, nur öffentliche Daten).

Je Chain gibt es einen eigenen Adapter (``app.datasources.chains.*``); sie teilen sich hier:

* **Beobachtungsdaten** (:class:`WatchConfig`, in ``data_source.watch_json``): öffentliche Adresse(n), bei Bitcoin
  optional ein öffentlicher Kontoschlüssel (xpub/ypub/zpub) mit Adresstyp und Gap-Limit, der gewählte Anbieter und
  eine stabile Kennung des Kontos. Seed-Phrase, private Schlüssel, Signaturen oder eine Verbindung zum Gerät werden
  nie benötigt und nie angenommen.
* **Asset-Kennungen**: native Coins mit ihrem Symbol (``ETH``, ``BNB``, ``AVAX``, ``BTC``, ``SOL``, ``KAS``), Tokens
  immer über Chain und Contract/Mint/Tick (``USDC@ETH:0xa0b8…``) – nie über das Symbol allein. Die Zuordnung zu einem
  Asset in Portfolia erfolgt einmal je Token im Prüf-Stapel (wie beim CSV-Import) und gilt danach dauerhaft.
* **Stabile Kennungen** je Bewegung (``Rec.ext_id`` = Unterkennung, siehe :mod:`app.datasources.connector`).
* **Einordnung**: beobachtete Bewegungen werden nur dann direkt als Zu-/Abgang vorgeschlagen, wenn sie eindeutig
  sind; Swaps, Vertragsaufrufe mit Zu- *und* Abgang, Bridges, Staking, Rewards, mögliche Spam-Tokens und Unklares
  gehen mit Begründung in die Prüfung. Ein Eingang ist nie ein Kauf – Anschaffungskosten und -daten werden nicht
  erfunden; Transfers zwischen eigenen Konten gleicht der Prüf-Stapel ab.
* **Abdeckung**: dauerhafte Grenzen eines Adapters (``limits``) und in einem Lauf erkannte Lücken (``gaps``) werden
  angezeigt; „vollständig synchronisiert“ gilt nur ohne erkannte Lücke.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets as _secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import httpx

from app.csvimport import model as M
from app.csvimport.model import Rec
from app.datasources import connector as K
from app.datasources.chainhttp import ENDPOINTS, ChainHttp, Endpoint

ZERO = Decimal(0)
SCRIPT_TYPES = {"p2pkh": "Legacy (P2PKH, 1…)", "p2sh-p2wpkh": "Nested SegWit (P2SH-P2WPKH, 3…)",
                "p2wpkh": "Native SegWit (P2WPKH, bc1q…)", "p2tr": "Taproot (P2TR, bc1p…)"}
GAP_DEFAULT = 20
GAP_MAX = 200
MAX_ADDRESSES = 50
_WATCH_ID = re.compile(r"^[0-9a-f]{8}$")
_SPAM_WORDS = re.compile(r"(?i)(https?://|www\.|\.(com|io|net|org|xyz|app|finance|site|top|vip|link|gift)\b|"
                         r"\bvisit\b|\bclaim|\breward|\bairdrop|\bvoucher|\bbonus\b|\$\s*\d|t\.me/|telegram)")


@dataclass
class WatchConfig:
    """Was Portfolia über ein Wallet-Konto speichert – ausschließlich öffentliche Daten."""

    addresses: list[str] = field(default_factory=list)
    xpubs: list[str] = field(default_factory=list)  # Bitcoin: öffentliche Kontoschlüssel
    script: str | None = None  # Bitcoin: Adresstyp der Kontoschlüssel (SCRIPT_TYPES)
    gap: int = GAP_DEFAULT
    provider: str | None = None  # Endpoint-ID (geprüfter Katalog)
    watch_id: str = ""  # stabile Kennung des Kontos (Ereignis-IDs bei Mehr-Adress-Konten)
    tokens: bool = True  # Tokens (ERC-20/BEP-20, SPL, KRC-20) mit abrufen

    @classmethod
    def load(cls, raw: str | Mapping[str, Any] | None) -> WatchConfig:
        if not raw:
            return cls()
        d = json.loads(raw) if isinstance(raw, str) else dict(raw)
        gap = d.get("gap")
        return cls(addresses=[str(a) for a in d.get("addresses") or []][:MAX_ADDRESSES],
                   xpubs=[str(x) for x in d.get("xpubs") or []][:5],
                   script=d.get("script") if d.get("script") in SCRIPT_TYPES else None,
                   gap=min(max(int(gap), 5), GAP_MAX) if isinstance(gap, int) else GAP_DEFAULT,
                   provider=str(d["provider"]) if d.get("provider") in ENDPOINTS else None,
                   watch_id=str(d.get("watch_id") or "") if _WATCH_ID.match(str(d.get("watch_id") or "")) else "",
                   tokens=bool(d.get("tokens", True)))

    def dump(self) -> str:
        d: dict[str, Any] = {"addresses": self.addresses}
        if self.watch_id:
            d["watch_id"] = self.watch_id
        if self.xpubs:
            d["xpubs"] = self.xpubs
            d["script"] = self.script
            d["gap"] = self.gap
        if self.provider:
            d["provider"] = self.provider
        if not self.tokens:
            d["tokens"] = False
        return json.dumps(d, sort_keys=True)

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.dump())


def new_watch_id() -> str:
    return _secrets.token_hex(4)


# ----------------------------------------------------------------------------------------------------
# Asset-Kennungen
# ----------------------------------------------------------------------------------------------------

def clean_symbol(raw: str | None, fallback: str = "TOKEN") -> str:
    """Symbol laut Token-Vertrag → nur A–Z/0–9, höchstens 10 Zeichen (Spam-Symbole enthalten oft URLs/Sätze)."""
    v = re.sub(r"[^A-Z0-9]", "", (raw or "").upper())[:10]
    return v or fallback


def token_key(symbol: str | None, chain_tag: str, contract: str) -> str:
    """Kennung eines Tokens: ``<SYMBOL>@<CHAIN>:<Contract/Mint/Tick>`` – eindeutig über Chain und Contract."""
    return f"{clean_symbol(symbol)}@{chain_tag}:{contract}"


def split_token_key(key: str) -> tuple[str, str, str] | None:
    sym, at, rest = key.partition("@")
    if not at or ":" not in rest:
        return None
    chain, _, contract = rest.partition(":")
    return sym, chain, contract


def spam_reason(symbol: str | None, name: str | None) -> str | None:
    """Hinweis auf einen möglichen Spam-/Betrugs-Token (Werbung im Namen, Links, nicht-ASCII-Zeichen)."""
    text = f"{symbol or ''} {name or ''}"
    if _SPAM_WORDS.search(text):
        return "Name/Symbol enthält Werbung oder Links"
    if any(ord(ch) > 127 for ch in text):
        return "Name/Symbol mit Sonderzeichen (mögliche Nachahmung)"
    return None


def short(addr: str | None, n: int = 6) -> str:
    if not addr:
        return ""
    a = str(addr)
    return a if len(a) <= 2 * n + 1 else f"{a[:n]}…{a[-n + 1:]}"


def sub_hash(*parts: Any) -> str:
    """Kurzer, stabiler Fingerabdruck für Unterkennungen (ohne Reihenfolge-Abhängigkeit des Anbieters)."""
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]  # noqa: S324 - kein Schutzzweck


def units(raw: Any, decimals: int) -> Decimal:
    """Ganzzahlige Basiseinheiten (wei, lamports, satoshi, sompi, …) → exakte Dezimalzahl."""
    v = Decimal(str(raw).strip())
    if v != v.to_integral_value():
        raise ValueError("keine ganze Zahl")
    return v.scaleb(-int(decimals)) if v else ZERO


# ----------------------------------------------------------------------------------------------------
# Bewegungen → Zeilen
# ----------------------------------------------------------------------------------------------------

@dataclass
class Move:
    """Beobachtete Bewegung eines Assets aus Sicht des Kontos (positiv = Eingang)."""

    asset: str  # Kennung (Symbol bzw. Token-Schlüssel)
    qty: Decimal
    sub: str  # stabile Unterkennung
    note: str | None = None
    spam: str | None = None


@dataclass
class TxView:
    """Eine Transaktion aus Sicht des Kontos – Grundlage für die Einordnung."""

    chain: str  # Provider-ID (ethereum, bsc, …)
    txid: str
    ts: datetime
    moves: list[Move]
    fee: Decimal = ZERO  # vom Konto bezahlte Netzwerkgebühr (natives Asset)
    fee_asset: str = ""
    initiated: bool = False  # vom Konto ausgelöst (Absender/Unterzeichner)
    failed: bool = False
    plain: bool = True  # einfache Überweisung (kein Vertragsaufruf/keine Programmlogik)
    hint: str | None = None  # Grund, warum die Deutung nicht eindeutig ist (→ Prüfung)
    label: str | None = None  # Anzeige (z. B. Methode)
    raw: dict[str, Any] = field(default_factory=dict)


def net_moves(moves: Iterable[Move]) -> list[Move]:
    """Bewegungen je Asset: gleichgerichtete Bewegungen bleiben getrennte Zeilen (z. B. zwei Token-Eingänge in
    einem Hash), ein Asset mit Zu- *und* Abgang in derselben Transaktion (Erstattung, Hin und Zurück) wird zu einer
    Bewegung saldiert. Die Unterkennung der Saldo-Bewegung hängt nur am Asset – stabil, auch wenn der Anbieter die
    Einzelbewegungen anders ordnet."""
    by_asset: dict[str, list[Move]] = {}
    for m in sorted((m for m in moves if m.qty != 0), key=lambda m: m.sub):
        by_asset.setdefault(m.asset, []).append(m)
    out: list[Move] = []
    for asset, lst in by_asset.items():
        if any(m.qty > 0 for m in lst) and any(m.qty < 0 for m in lst):
            total = sum((m.qty for m in lst), ZERO)
            if total != 0:
                out.append(Move(asset, total, f"a:{sub_hash(asset)}", "saldiert aus " + ", ".join(
                    f"{m.qty.normalize():f}" for m in lst)[:200], next((m.spam for m in lst if m.spam), None)))
        else:
            out.extend(lst)
    return out


def classify(tx: TxView) -> list[Rec]:
    """Bewegungen einer Transaktion → Zeilen im Zwischenformat (nie geraten: Unklares geht in die Prüfung).

    * nur Gebühr (fehlgeschlagen, Freigabe, Umbuchung innerhalb des Kontos) → Gebühr
    * nur Eingänge → Zugang je Bewegung (aus eigener Vertragsinteraktion bzw. möglicher Spam: zur Prüfung)
    * nur Abgänge → Abgang je Bewegung (Vertragsaufruf statt Überweisung: zur Prüfung, z. B. Staking/Bridge)
    * genau ein Asset hinein und eines hinaus → Tausch, immer zur Prüfung (Swap/Liquidität/Bridge)
    * mehrere Assets hinein und hinaus → ungeklärt (mit Aufstellung)
    Die Gebühr hängt am ersten Abgang (sonst eigene Zeile)."""
    moves = net_moves(tx.moves)
    ins = [m for m in moves if m.qty > 0]
    outs = [m for m in moves if m.qty < 0]
    raw = {**tx.raw, "chain": tx.chain, "tx": tx.txid}
    fee = tx.fee if tx.fee > 0 else ZERO
    recs: list[Rec] = []

    def rec(kind: str, sub: str, **kw: Any) -> Rec:
        r = Rec(line=0, ts=tx.ts, kind=kind, txhash=tx.txid, raw=raw, label=tx.label, **kw)
        r.ext_id = sub
        return r

    if tx.failed:
        if fee:
            recs.append(rec(M.FEE, "fee", fee_sym=tx.fee_asset, fee_qty=fee,
                            note="fehlgeschlagene Transaktion – nur Netzwerkgebühr"))
        return recs
    if not moves:
        if fee:
            recs.append(rec(M.FEE, "fee", fee_sym=tx.fee_asset, fee_qty=fee,
                            note=tx.hint or ("Vertragsaufruf ohne Bewegung (z. B. Freigabe) – nur Netzwerkgebühr"
                                             if not tx.plain else "Transaktion ohne Wertbewegung für dieses Konto "
                                                                  "(z. B. an sich selbst) – nur Netzwerkgebühr")))
        return recs
    if ins and outs:
        a_in, a_out = {m.asset for m in ins}, {m.asset for m in outs}
        if len(a_in) == 1 and len(a_out) == 1:
            sub = "swap:" + sub_hash(*sorted(m.sub for m in moves))
            recs.append(rec(M.TRADE, sub, out_sym=outs[0].asset, out_qty=-sum((m.qty for m in outs), ZERO),
                            in_sym=ins[0].asset, in_qty=sum((m.qty for m in ins), ZERO),
                            fee_sym=tx.fee_asset if fee else None, fee_qty=fee or None,
                            review=tx.hint or "Tausch über einen Vertrag (Swap/Liquidität/Bridge?) – Art und Werte "
                                              "prüfen"))
            return recs
        note = "; ".join(f"{'+' if m.qty > 0 else ''}{m.qty.normalize():f} {m.asset}" for m in moves)[:300]
        recs.append(rec(M.REVIEW, "complex:" + sub_hash(*sorted(m.sub for m in moves)),
                        note=f"Mehrere Zu- und Abgänge in einer Transaktion ({note}) – Vorgang manuell erfassen oder "
                             "ignorieren"))
        if fee:
            recs.append(rec(M.FEE, "fee", fee_sym=tx.fee_asset, fee_qty=fee, note="Netzwerkgebühr des Vorgangs"))
        return recs
    fee_left = fee
    for m in outs:
        review = tx.hint
        if review is None and not tx.plain:
            review = "Abgang über einen Vertragsaufruf (z. B. Staking, Bridge, DEX) – Art prüfen"
        recs.append(rec(M.WITHDRAWAL, m.sub, out_sym=m.asset, out_qty=-m.qty,
                        fee_sym=tx.fee_asset if fee_left else None, fee_qty=fee_left or None, review=review,
                        note=m.note))
        fee_left = ZERO
    for m in ins:
        review = f"möglicher Spam-Token ({m.spam}) – zuordnen oder ignorieren" if m.spam else tx.hint
        if not review and tx.initiated and not tx.plain:
            review = "Eingang aus eigener Vertragsinteraktion (z. B. Claim, Unstake, Reward) – Art prüfen"
        recs.append(rec(M.DEPOSIT, m.sub, in_sym=m.asset, in_qty=m.qty, review=review or None, note=m.note))
    if fee_left:
        recs.append(rec(M.FEE, "fee", fee_sym=tx.fee_asset, fee_qty=fee_left, note="Netzwerkgebühr"))
    return recs


def event(provider: str, txid: str, owner: str, ts: datetime, recs: list[Rec], label: str | None = None) \
        -> K.SourceEvent:
    """Ereignis ``<chain>:<txid>:<eigenes Konto>`` – dieselbe Transaktion erscheint in Sender- und Empfängerkonto."""
    return K.SourceEvent(f"{provider}:{txid}:{owner}", ts, recs, label)


def ts_from_unix(v: Any) -> datetime:
    return datetime.fromtimestamp(int(v), UTC)


# ----------------------------------------------------------------------------------------------------
# Basisklasse
# ----------------------------------------------------------------------------------------------------

class WalletConnector(K.Connector):
    """Gemeinsamer Rahmen der Chain-Adapter: Anbieterwahl, HTTP-Budget, Fortschritt, Abdeckung."""

    wallet = True
    chain_label: ClassVar[str] = ""
    native: ClassVar[str] = ""
    endpoints: ClassVar[tuple[str, ...]] = ()  # erlaubte Anbieter (erster = Standard)
    limits: ClassVar[tuple[str, ...]] = ()  # dauerhafte Abdeckungsgrenzen (Anzeige)
    explorer_tx: ClassVar[str] = ""  # Link für Nutzer (öffnet der Nutzer selbst, Portfolia ruft ihn nie ab)
    explorer_addr: ClassVar[str] = ""
    explorer_token: ClassVar[str] = ""
    # nur Tests (anonymisierte Fixtures): Transport/Uhr/Wartefunktion ersetzen
    transport: ClassVar[httpx.BaseTransport | None] = None
    sleep: ClassVar[Callable[[float], None]] = staticmethod(time.sleep)
    clock: ClassVar[Callable[[], float]] = staticmethod(time.monotonic)
    # Budgets je Lauf (eine Etappe; längere Historien in mehreren Etappen)
    max_requests: ClassVar[int] = 2500
    deadline_s: ClassVar[float] = 240.0
    chain_id: ClassVar[int | None] = None
    usage: Callable[[int], None] | None = None  # vom Dienst gesetzt: Anfragen je Anbieter und Tag zählen

    def endpoint(self, cfg: K.SourceConfig) -> Endpoint:
        want = (cfg.watch or {}).get("provider")
        eid = want if want in self.endpoints else self.endpoints[0]
        return ENDPOINTS[eid]

    def http(self, cfg: K.SourceConfig, secret: K.Secret, *, usage: Callable[[int], None] | None = None,
             max_requests: int | None = None, deadline_s: float | None = None,
             network: str | None = None) -> ChainHttp:
        ep = self.endpoint(cfg)
        key = secret.reveal() if secret.present else None
        return ChainHttp(ep, key=key, chain_id=self.chain_id, network=network, transport=self.transport,
                         sleep=self.sleep, clock=self.clock, max_requests=max_requests or self.max_requests,
                         deadline_s=self.deadline_s if deadline_s is None else deadline_s, usage=usage or self.usage)

    def watch(self, cfg: K.SourceConfig) -> WatchConfig:
        return WatchConfig.load(cfg.watch)

    def coverage_limits(self, cfg: K.SourceConfig) -> list[str]:
        return list(self.limits)
