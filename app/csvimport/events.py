"""Stabile Ereignis-Kennungen für den Abgleich zwischen Quellen (CSV-Import ↔ Datenquelle).

* **Ereignis-ID** ``<anbieter>:<native ID>`` – von Connectoren geliefert, für CSV-Zeilen aus der Quellkennung
  abgeleitet, sofern das Profil eine native Börsen-ID verwendet (Kraken ``refid``/Ledger-ID, Coinbase ``ID``,
  Bitpanda ``Transaction ID``). Profile ohne IDs (z. B. Binance-Kontoauszug) erzeugen Prüfsummen – die taugen nicht
  für den Abgleich mit anderen Quellen; dort greift die unscharfe Dublettenprüfung (Zeit, Asset, Menge).
* **Transaktions-Hash** – Wallet-Exporte (Ledger Live, Trezor, Electrum, Exodus) und Wallet-Connectoren.

Ein Treffer gilt nur, wenn auch die Buchungsseite übereinstimmt (Art, Abgangs- und Zugangs-Asset): dieselbe
Blockchain-Transaktion erscheint als Abgang in der sendenden und als Zugang in der empfangenden Wallet.
"""

from __future__ import annotations

import re

NATIVE_ID_PREFIXES = ("kraken", "coinbase", "bitpanda")
HASH_PREFIXES = ("ledger", "trezor", "electrum", "exodus")
_CHECKSUM = re.compile(r"^[0-9a-f]{20}(#\d+)?$")  # Prüfsummen-Kennung (profiles._Ids), keine native ID
_HASH = re.compile(r"^(0x)?[0-9a-fA-F]{40,128}$")


def derive_event_key(external_id: str | None) -> str | None:
    """Ereignis-ID aus der Quellkennung einer CSV-Zeile (``kraken:T1:fee:ZEUR`` → ``kraken:T1``)."""
    if not external_id or ":" not in external_id:
        return None
    prefix, rest = external_id.split(":", 1)
    if prefix not in NATIVE_ID_PREFIXES:
        return None
    native = rest.split(":", 1)[0].strip()
    if not native or _CHECKSUM.match(native):
        return None
    return f"{prefix}:{native}"


def derive_tx_hash(external_id: str | None) -> str | None:
    """Transaktions-Hash aus der Quellkennung von Wallet-Exporten (``ledger:<hash>:…``)."""
    if not external_id or ":" not in external_id:
        return None
    prefix, rest = external_id.split(":", 1)
    if prefix not in HASH_PREFIXES:
        return None
    h = rest.split(":", 1)[0].strip()
    return normalize_hash(h) if _HASH.match(h) else None


def normalize_hash(h: str | None) -> str | None:
    if not h:
        return None
    v = h.strip().lower()
    return v[2:] if v.startswith("0x") else v


def match_keys(event_key: str | None, tx_hash: str | None) -> set[str]:
    out = set()
    if event_key:
        out.add(f"e:{event_key}")
    if tx_hash:
        out.add(f"h:{normalize_hash(tx_hash)}")
    return out
