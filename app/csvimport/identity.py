"""Anbieter-Identität von Symbolen – dasselbe Kürzel kann je Anbieter einen anderen Coin bedeuten.

Symbole sind keine stabile Kennung: Bitpanda führt „TH“ für Threshold Network, CoinGecko kennt unter „th“ den Team
Heretics Fan Token. Stabil sind Anbieter-Asset-IDs (Bitpanda: Asset-UUID der API) und bei On-Chain-Token Chain +
Contract bzw. Mint (Token-Kennung ``SYMBOL@CHAIN:Contract``; ETH = EVM-Chain-ID 1, BSC = 56, AVAX = 43114, Solana-Mint,
Kaspa-Tick).

``PROVIDER_SYMBOLS`` führt bekannte Kürzel, bei denen die Auflösung über das Symbol in die Irre führen kann. Für sie
gilt beim Import: Ein Asset wird nur zugeordnet, wenn seine Kursquelle den Anbieter-Coin bestätigt oder der Nutzer
eine Zuordnung genau für diesen Anbieter gespeichert hat (Kennung ``SYMBOL@ANBIETER``, z. B. ``TH@BITPANDA``). Sonst
gilt das Symbol als mehrdeutig und der Vorgang geht in die Prüfung – nie still auf das falsche Asset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderAsset:
    provider: str  # Kennung des Anbieters (klein)
    symbol: str  # Kürzel beim Anbieter
    name: str  # Bezeichnung beim Anbieter
    coingecko: str | None  # CoinGecko-ID des gemeinten Coins
    ticker: str | None = None  # übliches Kürzel des Coins (Vorschlag für eine neue Asset-ID)


PROVIDER_SYMBOLS: dict[tuple[str, str], ProviderAsset] = {
    ("bitpanda", "TH"): ProviderAsset("bitpanda", "TH", "Threshold Network", "threshold-network-token", "T"),
}
PROVIDER_LABEL = {"bitpanda": "Bitpanda"}
_ACCOUNT_HINTS = {"bitpanda": re.compile(r"\bbitpanda\b", re.I)}
UUID_PROVIDERS = frozenset({"bitpanda"})  # Anbieter mit global eindeutigen Vorgangs-UUIDs
_DASHED_UUID = re.compile(r"(?<![0-9a-fA-F-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                          r"[0-9a-fA-F]{12}(?![0-9a-fA-F-])")


def provider_of(source: str | None = None, profile: str | None = None, account: str | None = None) -> str | None:
    """Anbieter eines Vorgangs: Datenquelle bzw. CSV-Profil (``sync:bitpanda``, ``csv:bitpanda``, ``bitpanda``),
    sonst der Kontoname (z. B. Koinly-Wallet „Bitpanda“)."""
    for v in (source, profile):
        s = (v or "").strip().lower().removeprefix("portfolia:")
        s = s.split(":", 1)[1] if s.startswith(("sync:", "csv:")) else s
        if s in PROVIDER_LABEL:
            return s
    for p, rx in _ACCOUNT_HINTS.items():
        if account and rx.search(account):
            return p
    return None


def provider_key(symbol: str, provider: str) -> str:
    """Kennung einer Zuordnung, die nur für diesen Anbieter gilt."""
    return f"{symbol.strip().upper()}@{provider.strip().upper()}"


def split_provider_key(key: str) -> tuple[str, str] | None:
    """``TH@BITPANDA`` → (TH, bitpanda); Token-Kennungen (mit Chain und Contract) zählen nicht dazu."""
    sym, at, prov = (key or "").partition("@")
    if not at or ":" in prov or prov.lower() not in PROVIDER_LABEL:
        return None
    return sym, prov.lower()


def identity(provider: str | None, symbol: str | None) -> ProviderAsset | None:
    if not provider or not symbol:
        return None
    return PROVIDER_SYMBOLS.get((provider, symbol.split(";", 1)[0].strip().upper()))


def confirms(pa: ProviderAsset, asset: Any) -> bool:
    """Bestätigt die Kursquelle des Assets den Anbieter-Coin? (nur dann darf das Symbol aufgelöst werden)"""
    if pa.coingecko is None or asset is None:
        return False
    return getattr(asset, "quote_source", None) == "coingecko" and getattr(asset, "quote_id", None) == pa.coingecko


def note_identity_keys(source: str | None, account: str | None, *texts: str | None) -> set[str]:
    """Anbieter-Kennungen in Notiz bzw. Quellkennung einer Import-Buchung: UUIDs (nur in der Schreibweise mit
    Bindestrichen) auf Konten eines Anbieters mit UUID-Kennungen (Bitpanda) – Koinly führt die
    Bitpanda-Transaktions-UUID z. B. als ``txhash=<uuid>``. UUIDs sind global eindeutig; ein Treffer gegen eine
    API-Kennung ist deshalb dasselbe Ereignis. Koinly-eigene IDs (32 Hex-Zeichen ohne Bindestriche) zählen nicht."""
    from app.csvimport.events import uuid_key

    prov = provider_of(source, None, account)
    if prov not in UUID_PROVIDERS:
        return set()
    return {k for t in texts for m in _DASHED_UUID.findall(t or "") if (k := uuid_key(prov, m))}
