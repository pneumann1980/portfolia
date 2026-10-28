"""Bestand einer Adresse zu einem früheren Block – nur lesend, über öffentliche Archiv-RPCs (ohne Schlüssel).

Wozu: Anbieter wie Subscan (über PubFi) liefern nicht den ganzen Verlauf, oder keine internen EVM-Überweisungen. Der
**Bestand der Kette** zu einem Block ist davon unabhängig; ein Vergleich mit dem Ledger zeigt, *wann* ein Zugang oder
Abgang ohne Buchung stattfand (:mod:`app.diagnosis.chainscan`).

Geprüft (10/2026, ohne Schlüssel): peaq ``https://peaq.api.onfinality.io/public`` (Archiv, ``eth_getBalance`` bei
beliebigem Block), Polkadot Relay Chain ``rpc.polkadot.io`` und Asset Hub ``polkadot-asset-hub-rpc.polkadot.io``
(``state_getStorage`` bei Blockhash, Stand bei Block 15 000 000 lesbar). PublicNode verwirft alte Stände
(„State already discarded“) – das meldet :class:`NoArchive`, es wird nie geraten.

Zeit eines Blocks: EVM ``eth_getBlockByNumber.timestamp`` (Sekunden); Substrate ``Timestamp.Now`` (Millisekunden).
Bestand eines Substrate-Kontos: ``System.Account`` → frei + reserviert (wie die Bestandsprüfung der Anbindung).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.datasources import connector as K
from app.datasources.chainhttp import ENDPOINTS, ChainHttp, RpcError
from app.datasources.chains.codec import ss58_decode
from app.datasources.wallet import units

_SYSTEM_ACCOUNT = "26aa394eea5630e07c48ae0c9558cef7b99d880ec681799c0cf30e8886371da9"
_TIMESTAMP_NOW = "0xf0c365c3cf59d671eb72da0e7a4113c49f1f0515f462cdcf84e0f1d6045dfcbb"
# Antworten, mit denen ein Knoten meldet, dass er den alten Stand nicht (mehr) hat
_PRUNED = re.compile(r"discarded|pruned|missing trie node|state (?:is )?not available|no state available|"
                     r"header was not found|unknown ?block|historical state|not found in the database|"
                     r"required data is not available|old state|state at block", re.I)
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")


class NoArchive(Exception):
    """Kein erreichbarer Knoten kennt den Stand bei diesem Block (Knoten ohne Archiv)."""


class Unsupported(Exception):
    """Für diese Quelle gibt es keinen kostenlosen Weg zum Bestand bei einem Block."""


class Reader:
    """Bestand und Blockzeit eines Netzes (ein Konto). Unterklassen: :class:`EvmReader`, :class:`SubstrateReader`."""

    key = ""
    label = ""
    decimals = 18
    asset = ""

    def __init__(self, endpoints: tuple[str, ...], *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] | None = None, max_requests: int = 4000,
                 usage: Callable[[int], None] | None = None) -> None:
        self.endpoints = endpoints
        self._opts: dict[str, Any] = {"transport": transport, "sleep": sleep, "max_requests": max_requests,
                                      "usage": usage}
        self._http: dict[str, ChainHttp] = {}
        self._good: str | None = None
        self.calls = 0
        self._times: dict[int, int] = {}  # Block → Unix-Sekunden
        self._bal: dict[int, Decimal] = {}

    # -- Zugriff ------------------------------------------------------------------------------------------
    def _client(self, eid: str) -> ChainHttp:
        if eid not in self._http:
            self._http[eid] = ChainHttp(ENDPOINTS[eid], **{k: v for k, v in self._opts.items() if v is not None})
        return self._http[eid]

    def close(self) -> None:
        for h in self._http.values():
            h.close()
        self._http.clear()

    def __enter__(self) -> Reader:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def rpc(self, method: str, params: list[Any], what: str, *, history: bool = False) -> Any:
        """Ein RPC-Aufruf; fehlt einem Knoten der alte Stand (``history``), kommt der nächste Endpunkt dran."""
        order = [self._good, *[e for e in self.endpoints if e != self._good]] if self._good else list(self.endpoints)
        last: Exception | None = None
        pruned = 0
        for eid in order:
            try:
                out = self._client(eid).rpc(method, params, what=what)
                self.calls += 1
                self._good = eid
                return out
            except RpcError as e:
                self.calls += 1
                if history and _PRUNED.search(f"{e.rpc_message}"):
                    pruned += 1
                    last = e
                    continue
                raise K.ConnectorError("data", f"{what}: {e.rpc_message}") from None
            except K.ConnectorError as e:
                last = e
                if e.kind in ("cancelled", "config"):
                    raise
                continue
        if pruned and pruned == len(order):
            raise NoArchive(f"{self.label}: kein öffentlicher Knoten liefert den Stand bei diesem Block ({what}).")
        if isinstance(last, K.ConnectorError):
            raise last
        raise K.ConnectorError("unavailable", f"{self.label}: kein Endpunkt erreichbar ({what}).")

    # -- Schnittstelle ------------------------------------------------------------------------------------
    def tip(self) -> int:
        raise NotImplementedError

    def time(self, block: int) -> datetime:
        if block not in self._times:
            self._times[block] = int(self._fetch_time(block).timestamp())
        return datetime.fromtimestamp(self._times[block], UTC)

    def balance(self, block: int) -> Decimal:
        if block not in self._bal:
            self._bal[block] = self._fetch_balance(block)
        return self._bal[block]

    def _fetch_time(self, block: int) -> datetime:
        raise NotImplementedError

    def _fetch_balance(self, block: int) -> Decimal:
        raise NotImplementedError

    # -- Zwischenspeicher (Kettenwerte ändern sich nicht; die Aufrufer speichern sie zwischen Läufen) ------
    def known_times(self) -> dict[int, int]:
        return dict(self._times)

    def known_balances(self) -> dict[int, Decimal]:
        return dict(self._bal)

    def known_hashes(self) -> dict[int, str]:
        return {}

    def preload(self, *, times: dict[int, int], balances: dict[int, Decimal], hashes: dict[int, str]) -> None:
        self._times.update(times)
        self._bal.update(balances)

    def explorer_block(self, block: int) -> str:
        return ""


class EvmReader(Reader):
    """EVM-Adresse (``eth_getBalance`` bei Block, ``eth_getBlockByNumber`` für die Zeit)."""

    def __init__(self, address: str, endpoints: tuple[str, ...], *, key: str, label: str, asset: str,
                 decimals: int = 18, explorer: str = "", **kw: Any) -> None:
        super().__init__(endpoints, **kw)
        if not _EVM.match(address):
            raise Unsupported("Keine gültige EVM-Adresse.")
        self.address, self.key, self.label, self.asset, self.decimals = address.lower(), key, label, asset, decimals
        self._explorer = explorer

    def tip(self) -> int:
        raw = self.rpc("eth_blockNumber", [], "Blockhöhe")
        return int(str(raw), 16)

    def _fetch_time(self, block: int) -> datetime:
        blk = self.rpc("eth_getBlockByNumber", [hex(block), False], f"Block {block}", history=True)
        if not isinstance(blk, dict) or "timestamp" not in blk:
            raise K.ConnectorError("data", f"{self.label}: Block {block} nicht lesbar.")
        return datetime.fromtimestamp(int(str(blk["timestamp"]), 16), UTC)

    def _fetch_balance(self, block: int) -> Decimal:
        raw = self.rpc("eth_getBalance", [self.address, hex(block)], f"Bestand bei Block {block}", history=True)
        try:
            return units(int(str(raw), 16), self.decimals)
        except (TypeError, ValueError):
            raise K.ConnectorError("data", f"{self.label}: Bestand bei Block {block} nicht lesbar.") from None

    def explorer_block(self, block: int) -> str:
        return self._explorer.format(block) if self._explorer else ""


class SubstrateReader(Reader):
    """Substrate-Konto (``System.Account`` bei Blockhash, ``Timestamp.Now`` für die Zeit)."""

    def __init__(self, pub: bytes, endpoints: tuple[str, ...], *, key: str, label: str, asset: str,
                 decimals: int = 10, explorer: str = "", **kw: Any) -> None:
        super().__init__(endpoints, **kw)
        if len(pub) != 32:
            raise Unsupported("Kein gültiges Substrate-Konto.")
        self.pub, self.key, self.label, self.asset, self.decimals = pub, key, label, asset, decimals
        self._storage = "0x" + _SYSTEM_ACCOUNT + hashlib.blake2b(pub, digest_size=16).hexdigest() + pub.hex()
        self._hash: dict[int, str] = {}
        self._explorer = explorer

    def known_hashes(self) -> dict[int, str]:
        return dict(self._hash)

    def preload(self, *, times: dict[int, int], balances: dict[int, Decimal], hashes: dict[int, str]) -> None:
        super().preload(times=times, balances=balances, hashes=hashes)
        self._hash.update(hashes)

    def tip(self) -> int:
        hdr = self.rpc("chain_getHeader", [], "Blockhöhe")
        try:
            return int(str(hdr["number"]), 16)
        except (KeyError, TypeError, ValueError):
            raise K.ConnectorError("data", f"{self.label}: Blockhöhe nicht lesbar.") from None

    def _block_hash(self, block: int) -> str:
        if block not in self._hash:
            h = self.rpc("chain_getBlockHash", [block], f"Block {block}", history=True)
            if not isinstance(h, str) or not h.startswith("0x"):
                raise K.ConnectorError("data", f"{self.label}: Block {block} unbekannt.")
            self._hash[block] = h
        return self._hash[block]

    def _fetch_time(self, block: int) -> datetime:
        raw = self.rpc("state_getStorage", [_TIMESTAMP_NOW, self._block_hash(block)], f"Zeit von Block {block}",
                       history=True)
        if not raw:
            return datetime.fromtimestamp(0, UTC)  # Block 0 hat keinen Zeitstempel
        try:
            ms = int.from_bytes(bytes.fromhex(str(raw)[2:])[:8], "little")
        except ValueError:
            raise K.ConnectorError("data", f"{self.label}: Zeit von Block {block} nicht lesbar.") from None
        return datetime.fromtimestamp(ms / 1000, UTC)

    def _fetch_balance(self, block: int) -> Decimal:
        raw = self.rpc("state_getStorage", [self._storage, self._block_hash(block)], f"Bestand bei Block {block}",
                       history=True)
        if raw is None:
            return Decimal(0)  # Konto existierte noch nicht
        try:
            data = bytes.fromhex(str(raw)[2:])
        except ValueError:
            raise K.ConnectorError("data", f"{self.label}: Bestand bei Block {block} nicht lesbar.") from None
        # AccountInfo hat sich über die Runtimes geändert (Nonce und Zähler: 5, 12 oder 16 Byte); AccountData am Ende
        # ist immer 4 × u128 (frei, reserviert, …). Von hinten gelesen stimmt es für alle Versionen.
        if len(data) < 64 + 5:
            raise K.ConnectorError("data", f"{self.label}: Kontodaten bei Block {block} zu kurz.")
        off = len(data) - 64
        free, reserved = int.from_bytes(data[off:off + 16], "little"), int.from_bytes(data[off + 16:off + 32], "little")
        return units(free + reserved, self.decimals)

    def explorer_block(self, block: int) -> str:
        return self._explorer.format(block) if self._explorer else ""


# ----------------------------------------------------------------------------------------------------
# Zuordnung Datenquelle → Leser
# ----------------------------------------------------------------------------------------------------

def readers_for(provider: str, address: str, **kw: Any) -> list[Reader]:
    """Leser für eine Wallet-Quelle (leer, wenn es für den Anbieter keinen kostenlosen Archivweg gibt).

    * ``peaq`` mit 0x-Adresse: EVM-Archiv von OnFinality (Ausweich: weitere öffentliche Knoten – ohne Archiv
      meldet der Leser :class:`NoArchive`).
    * ``polkadot``: Relay Chain und Asset Hub (Parity), beide als Summe verglichen, weil der Ledger DOT beider
      Netze zusammenführt.
    """
    addr = (address or "").strip()
    if provider == "peaq" and _EVM.match(addr):
        return [EvmReader(addr, ("peaq_rpc", "peaq_rpc_2", "peaq_rpc_3"), key="peaq", label="peaq", asset="PEAQ",
                          explorer="https://peaq.subscan.io/block/{}", **kw)]
    if provider == "polkadot":
        try:
            _prefix, pub = ss58_decode(addr)
        except ValueError:
            return []
        return [
            SubstrateReader(pub, ("polkadot_rpc",), key="polkadot-rc", label="Polkadot Relay Chain", asset="DOT",
                            explorer="https://polkadot.subscan.io/block/{}", **kw),
            SubstrateReader(pub, ("polkadot_assethub_rpc",), key="polkadot-ah", label="Polkadot Asset Hub",
                            asset="DOT", explorer="https://assethub-polkadot.subscan.io/block/{}", **kw)]
    return []



def native_asset(provider: str, address: str) -> str:
    """Natives Asset, wenn es für die Quelle einen kostenlosen Archivweg gibt (sonst leer). Ohne Netzwerkzugriff."""
    rs = readers_for(provider, address)
    return rs[0].asset if rs else ""
