"""Strenge Prüfung öffentlicher Adressen je Chain (Prüfsummen), bevor sie gespeichert oder abgefragt werden."""

from __future__ import annotations

import re

from app.datasources.chains.codec import b58decode, eip55, kaspa_decode

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


VALIDATORS = {"ethereum": evm, "bsc": evm, "avalanche": evm, "solana": solana, "kaspa": kaspa, "bitcoin": bitcoin}
