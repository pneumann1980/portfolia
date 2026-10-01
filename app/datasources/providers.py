"""Katalog der Börsen und Chains mit Adressprüfung für öffentliche Wallet-Adressen.

Die Prüfung ist bewusst formal (Zeichensatz, Präfix, Länge) – sie verhindert Tippfehler und vor allem, dass
private Schlüssel oder Seed-Phrasen eingegeben werden. Prüfsummen (Bech32, Base58Check, EIP-55) prüft erst der
jeweilige Connector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EXCHANGE = "exchange"
WALLET = "wallet"
KIND_LABEL = {EXCHANGE: "Börse", WALLET: "Wallet-Adresse"}

_B58 = "1-9A-HJ-NP-Za-km-z"


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    kind: str
    pattern: re.Pattern[str] | None = None  # Wallets: erlaubte Adressformen (nach Normalisierung)
    example: str = ""
    lower: bool = False  # Adresse kleinschreiben (hex, bech32 – Groß-/Kleinschreibung ohne Bedeutung)
    csv_profile: str | None = None  # passendes CSV-Profil für den manuellen Weg
    bech32: tuple[str, ...] = ()  # Präfixe von Bech32-Adressen (auch in Großbuchstaben gültig, z. B. aus QR-Codes)


def _p(rx: str) -> re.Pattern[str]:
    return re.compile(rx)


_EVM = _p(r"^0x[0-9a-fA-F]{40}$")
_EVM_EX = "0x0000000000000000000000000000000000000000 (42 Zeichen, beginnt mit 0x)"

PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    # Börsen
    Provider("binance", "Binance", EXCHANGE, csv_profile="binance"),
    Provider("kraken", "Kraken", EXCHANGE, csv_profile="kraken"),
    Provider("coinbase", "Coinbase", EXCHANGE, csv_profile="coinbase"),
    Provider("bitpanda", "Bitpanda", EXCHANGE, csv_profile="bitpanda"),
    Provider("cryptocom", "Crypto.com", EXCHANGE, csv_profile="cryptocom"),
    Provider("bitvavo", "Bitvavo", EXCHANGE),
    Provider("bybit", "Bybit", EXCHANGE),
    Provider("kucoin", "KuCoin", EXCHANGE),
    Provider("okx", "OKX", EXCHANGE),
    Provider("bitget", "Bitget", EXCHANGE),
    Provider("gateio", "Gate.io", EXCHANGE),
    Provider("mexc", "MEXC", EXCHANGE),
    Provider("bitstamp", "Bitstamp", EXCHANGE),
    Provider("coinex", "CoinEx", EXCHANGE),
    Provider("bison", "BISON", EXCHANGE),
    Provider("other_exchange", "Andere Börse", EXCHANGE),
    # Chains (öffentliche Adressen)
    Provider("bitcoin", "Bitcoin", WALLET,
             _p(rf"^(bc1[0-9a-z]{{25,87}}|[13][{_B58}]{{25,34}}|[xyz]pub[{_B58}]{{100,112}})$"),
             "bc1q…, bc1p…, 1…, 3… oder xpub/ypub/zpub (öffentlicher Kontoschlüssel)", bech32=("bc1",)),
    Provider("ethereum", "Ethereum", WALLET, _EVM, _EVM_EX),
    Provider("bsc", "BNB Smart Chain", WALLET, _EVM, _EVM_EX),
    Provider("polygon", "Polygon", WALLET, _EVM, _EVM_EX, lower=True),
    Provider("arbitrum", "Arbitrum One", WALLET, _EVM, _EVM_EX, lower=True),
    Provider("optimism", "Optimism", WALLET, _EVM, _EVM_EX, lower=True),
    Provider("base", "Base", WALLET, _EVM, _EVM_EX, lower=True),
    Provider("avalanche", "Avalanche C-Chain", WALLET, _EVM, _EVM_EX),
    Provider("pulsechain", "PulseChain", WALLET, _EVM, _EVM_EX, lower=True),
    Provider("solana", "Solana", WALLET, _p(rf"^[{_B58}]{{32,44}}$"), "Base58, 32–44 Zeichen"),
    Provider("cardano", "Cardano", WALLET, _p(r"^(addr1[0-9a-z]{50,120}|stake1[0-9a-z]{50,60})$"),
             "addr1… oder stake1…", lower=True),
    Provider("kaspa", "Kaspa", WALLET, _p(r"^kaspa:[0-9a-z]{61,63}$"), "kaspa:q… (mit Präfix kaspa:)", lower=True),
    Provider("xrp", "XRP Ledger", WALLET, _p(rf"^r[{_B58}]{{24,34}}$"), "r…"),
    Provider("polkadot", "Polkadot", WALLET, _p(rf"^1[{_B58}]{{46,47}}$"), "1… (48 Zeichen)"),
    Provider("tron", "TRON", WALLET, _p(rf"^T[{_B58}]{{33}}$"), "T… (34 Zeichen)"),
    Provider("litecoin", "Litecoin", WALLET, _p(rf"^(ltc1[0-9a-z]{{25,87}}|[LM3][{_B58}]{{25,34}})$"),
             "ltc1… , L… , M…", bech32=("ltc1",)),
    Provider("dogecoin", "Dogecoin", WALLET, _p(rf"^D[{_B58}]{{33}}$"), "D… (34 Zeichen)"),
    Provider("other_chain", "Andere Chain", WALLET, _p(r"^[A-Za-z0-9:._-]{8,128}$"), "öffentliche Adresse"),
)}

INTERVALS = {0: "nur manuell", 60: "stündlich", 360: "alle 6 Stunden", 720: "alle 12 Stunden", 1440: "täglich"}

_HEX64 = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_WIF = re.compile(rf"^[5KLc9][{_B58}]{{50,51}}$")
_XPRV = re.compile(r"^[xyzt]prv", re.I)
_WORDS = re.compile(r"^[a-z]+(\s+[a-z]+){11,23}$")
_SOLANA_SECRET = re.compile(rf"^[{_B58}]{{86,90}}$")


def provider_label(pid: str) -> str:
    p = PROVIDERS.get(pid)
    return p.label if p else pid


def providers(kind: str) -> list[Provider]:
    return [p for p in PROVIDERS.values() if p.kind == kind]


def looks_secret(value: str) -> bool:
    """Privater Schlüssel oder Seed-Phrase? (Solche Eingaben werden abgelehnt und nie gespeichert.)"""
    v = value.strip()
    return bool(_HEX64.match(v) or _WIF.match(v) or _XPRV.match(v) or _WORDS.match(v.lower())
                or _SOLANA_SECRET.match(v))


def contains_secret(text: str) -> bool:
    """Enthält eine (mehrzeilige) Eingabe irgendwo einen privaten Schlüssel oder eine Seed-Phrase?"""
    t = text or ""
    return looks_secret(t) or any(looks_secret(part) for part in [*t.splitlines(), *t.split()] if part.strip())


def normalize_address(provider: Provider, raw: str) -> tuple[str | None, str | None]:
    """(normalisierte Adresse, Fehlertext). Der Fehlertext wiederholt die Eingabe nie."""
    v = re.sub(r"\s+", "", raw or "")
    if not v:
        return None, "Adresse fehlt."
    if looks_secret(raw or ""):
        return None, ("Das sieht nach einem privaten Schlüssel oder einer Seed-Phrase aus – bitte niemals eingeben. "
                      "Benötigt wird nur die öffentliche Adresse.")
    if provider.lower or (provider.bech32 and v.lower().startswith(provider.bech32)):
        v = v.lower()
    if provider.pattern is not None and not provider.pattern.match(v):
        return None, f"Keine gültige {provider.label}-Adresse (erwartet: {provider.example})."
    from app.datasources.chains.addresses import VALIDATORS

    check = VALIDATORS.get(provider.id)
    if check is not None:
        return check(re.sub(r"\s+", "", raw or ""))
    return v, None
