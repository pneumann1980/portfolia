"""Strenge Prüfung öffentlicher Adressen je Chain (Prüfsummen), bevor sie gespeichert oder abgefragt werden."""

from __future__ import annotations

import re

from app.datasources.chains.codec import (
    b58decode,
    cardano_decode,
    eip55,
    kaspa_decode,
    ss58_decode,
    ss58_encode,
    xrpl_decode,
)

_HEX40 = re.compile(r"^0x[0-9a-fA-F]{40}$")


def evm(v: str) -> tuple[str | None, str | None]:
    if not _HEX40.match(v):
        return None, "EVM-Adresse: 0x und 40 Hex-Zeichen erwartet."
    body = v[2:]
    if body != body.lower() and body != body.upper() and eip55(body) != v:
        return None, "Prüfsumme (EIP-55) stimmt nicht – Tippfehler? Adresse bitte kopieren statt abtippen."
    return v.lower(), None


def solana(v: str) -> tuple[str | None, str | None]:
    try:
        raw = b58decode(v)
    except ValueError:
        return None, "Solana-Adresse: nur Base58-Zeichen erlaubt."
    if len(raw) != 32:
        return None, "Solana-Adresse: muss 32 Byte (Base58, 32–44 Zeichen) ergeben."
    return v, None


def kaspa(v: str) -> tuple[str | None, str | None]:
    a = v.lower()
    try:
        kaspa_decode(a)
    except ValueError as e:
        return None, f"Kaspa-Adresse ungültig ({e})."
    return a, None


def bitcoin(v: str) -> tuple[str | None, str | None]:
    from app.datasources.chains.btckeys import parse_xpub, validate_address

    if v[1:4] in ("pub", "prv") and v[0] in "xyzXYZtuv":
        try:
            parse_xpub(v)
        except ValueError as e:
            return None, f"Kontoschlüssel: {e}."
        return v, None
    try:
        return validate_address(v), None
    except ValueError as e:
        return None, f"Bitcoin-Adresse ungültig ({e})."


def xrpl(v: str) -> tuple[str | None, str | None]:
    if v[:1] == "X" or v[:1] == "T":
        return None, ("X-Adresse (mit eingebettetem Destination Tag) – bitte die klassische Adresse r… angeben; den "
                      "Tag braucht Portfolia nicht.")
    try:
        xrpl_decode(v)
    except ValueError as e:
        return None, f"XRP-Ledger-Adresse ungültig ({e})."
    return v, None


def cardano(v: str) -> tuple[str | None, str | None]:
    a = v.lower()
    try:
        _, _, net, _ = cardano_decode(a)
    except ValueError as e:
        return None, f"Cardano-Adresse ungültig ({e})."
    if net != 1:
        return None, "Keine Mainnet-Adresse (Testnetz)."
    return a, None


def polkadot(v: str) -> tuple[str | None, str | None]:
    """SS58: Polkadot-Format (Präfix 0, beginnt mit 1); das generische Substrate-Format (Präfix 42, beginnt mit 5)
    wird in das Polkadot-Format umgerechnet – dasselbe Konto. Andere Netze (z. B. Kusama) werden abgelehnt."""
    try:
        prefix, acc = ss58_decode(v)
    except ValueError as e:
        return None, f"Polkadot-Adresse ungültig ({e})."
    if prefix == 0:
        return v, None
    if prefix == 42:
        return ss58_encode(acc, 0), None
    return None, (f"SS58-Adresse eines anderen Netzes (Präfix {prefix}, z. B. Kusama = 2) – bitte die Polkadot-Adresse "
                  "(beginnt mit 1) angeben.")


PEAQ_SS58 = 1221  # SS58-Registry (paritytech/ss58-registry): peaq, Symbol PEAQ, 18 Dezimalstellen


def peaq(v: str) -> tuple[str | None, str | None]:
    """peaq: EVM-Adresse (0x…, H160) oder Substrate-Konto (SS58). Das generische Format (Präfix 42, beginnt mit 5)
    wird in das peaq-Format (Präfix 1221) umgerechnet – dasselbe Konto."""
    if v.lower().startswith("0x"):
        return evm(v)
    try:
        prefix, acc = ss58_decode(v)
    except ValueError as e:
        return None, f"peaq-Adresse ungültig ({e})."
    if prefix in (PEAQ_SS58, 42):
        return ss58_encode(acc, PEAQ_SS58), None
    return None, (f"SS58-Adresse eines anderen Netzes (Präfix {prefix}) – bitte die peaq-Adresse (SS58) oder die "
                  "EVM-Adresse (0x…) angeben.")


VALIDATORS = {"ethereum": evm, "bsc": evm, "avalanche": evm, "polygon": evm, "solana": solana, "kaspa": kaspa,
              "bitcoin": bitcoin, "xrp": xrpl, "cardano": cardano, "polkadot": polkadot, "pulsechain": evm,
              "peaq": peaq}
