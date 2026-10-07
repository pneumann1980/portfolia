"""Stabile Ereignis-Kennungen für den Abgleich zwischen Quellen (CSV-Import ↔ Datenquelle ↔ kuratierter Import).

* **Ereignis-ID** ``<anbieter>:<native ID>`` – von Connectoren geliefert, für CSV-Zeilen aus der Quellkennung
  abgeleitet, sofern das Profil eine native Börsen-ID verwendet (Kraken ``refid``/Ledger-ID, Coinbase ``ID``,
  Bitpanda ``Transaction ID``). Profile ohne IDs (z. B. Binance-Kontoauszug) erzeugen Prüfsummen – die taugen nicht
  für den Abgleich mit anderen Quellen; dort greift die unscharfe Dublettenprüfung (Zeit, Asset, Menge).
* **Aliase** – weitere IDs desselben Ereignisses. Enthält eine Kennung eine UUID (Bitpanda: ``T``/``C``… + UUID in
  der CSV, Operation-/Trade-/Transaktions-UUIDs in der API), gilt ``<anbieter>:<uuid>`` als Alias: UUIDs sind
  global eindeutig, ein Treffer ist deshalb ein exakter Treffer – egal, aus welchem Feld er stammt.
* **Transaktions-Hash** – Wallet-Exporte (Ledger Live, Trezor, Electrum, Exodus) und Wallet-Connectoren.

Treffer über Anbieter-ID/Alias bedeuten „dasselbe Ereignis“ (→ bekannt). Treffer über den Hash nur, wenn auch die
Buchungsseite übereinstimmt: dieselbe Blockchain-Transaktion ist Abgang beim Sender und Zugang beim Empfänger.
"""

from __future__ import annotations

import re

NATIVE_ID_PREFIXES = ("kraken", "coinbase", "bitpanda")
HASH_PREFIXES = ("ledger", "trezor", "electrum", "exodus")
_CHECKSUM = re.compile(r"^[0-9a-f]{20}(#\d+)?$")  # Prüfsummen-Kennung (profiles._Ids), keine native ID
_HASH = re.compile(r"^(0x)?[0-9a-fA-F]{40,128}$")
_UUID = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_LINE = re.compile(r"#\d+$")


def _split(external_id: str | None) -> tuple[str, str] | None:
    if not external_id or ":" not in external_id:
        return None
    prefix, rest = external_id.split(":", 1)
    prefix = prefix.strip().lower()
    native = _LINE.sub("", rest.split(":", 1)[0].strip())
    return (prefix, native) if native else None


def derive_event_key(external_id: str | None) -> str | None:
    """Ereignis-ID aus einer Quellkennung (``kraken:T1:fee:ZEUR`` → ``kraken:T1``, ``bitpanda:<op>#1`` →
    ``bitpanda:<op>``)."""
    sp = _split(external_id)
    if sp is None or sp[0] not in NATIVE_ID_PREFIXES or _CHECKSUM.match(sp[1]):
        return None
    return f"{sp[0]}:{sp[1]}"


def uuid_key(prefix: str, value: str | None) -> str | None:
    """``<anbieter>:<uuid>`` (klein, mit Bindestrichen) – oder None, wenn der Wert keine UUID enthält."""
    m = _UUID.search(value or "")
    if not m:
        return None
    h = re.sub("-", "", m.group(0)).lower()
    return f"{prefix}:{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def derive_aliases(external_id: str | None) -> set[str]:
    """Aliase einer Quellkennung (UUID in der nativen ID)."""
    sp = _split(external_id)
    if sp is None or sp[0] not in NATIVE_ID_PREFIXES:
        return set()
    k = uuid_key(sp[0], sp[1])
    return {k} if k else set()


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


def identity_keys(event_key: str | None, aliases: set[str] | list[str] | tuple[str, ...] = (),
                  external_id: str | None = None) -> set[str]:
    """Alle Anbieter-Kennungen eines Vorgangs (Ereignis-ID, Aliase, aus der Quellkennung abgeleitet)."""
    out = {k for k in (event_key, derive_event_key(external_id)) if k}
    out |= {a for a in aliases if a}
    out |= derive_aliases(external_id)
    for k in list(out):  # UUID-Form der Ereignis-ID selbst (Schreibweise vereinheitlichen)
        p, _, native = k.partition(":")
        # nur Anbieter mit UUID-Kennungen: aus Transaktions-Hashes (Wallets: 64 Hex-Zeichen) darf keine „UUID“
        # entstehen – sonst gälten verschiedene Vorgänge mit gleichem Hash-Anfang als dasselbe Ereignis
        u = uuid_key(p, native) if p in NATIVE_ID_PREFIXES else None
        if u:
            out.add(u)
    return out


def source_ref_keys(source: str | None, source_ref: str | None) -> set[str]:
    """Kennungen einer Buchung des kuratierten Imports (``source``/``source_ref`` laut Datenvertrag).

    Erkannt werden ``source_ref`` im Format ``<anbieter>:<ID>`` (auch Portfolia-Exporte: ``csv:bitpanda`` bzw.
    ``sync:bitpanda`` mit ``bitpanda:<ID>#<zeile>``) sowie ``source = <anbieter>`` mit nackter ID."""
    ref = (source_ref or "").strip()
    if not ref:
        return set()
    if ":" in ref and ref.split(":", 1)[0].strip().lower() in NATIVE_ID_PREFIXES:
        return identity_keys(None, (), ref)
    src = (source or "").strip().lower()
    src = src.split(":", 1)[1] if src.startswith(("csv:", "sync:")) else src
    if src in NATIVE_ID_PREFIXES:
        return identity_keys(None, (), f"{src}:{ref}")
    return set()


def match_keys(event_key: str | None, tx_hash: str | None) -> set[str]:
    out = set()
    if event_key:
        out.add(f"e:{event_key}")
    if tx_hash:
        out.add(f"h:{normalize_hash(tx_hash)}")
    return out
