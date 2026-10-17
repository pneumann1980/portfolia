"""Blockscout REST v2 (``…/api/v2``) – Adresshistorie im Etherscan-Format, nur lesend.

Warum v2? Der PulseChain-Explorer (Blockscout) beantwortet die Etherscan-kompatiblen Listen ``txlist``/``tokentx``
für Adressen mit vielen Spam-Token-Transfers nicht mehr in angemessener Zeit (Zeitüberschreitung bzw. HTTP 502, Stand
10/2026), während dieselben Daten über die REST-API v2 in unter zwei Sekunden kommen – sofern Token-Transfers **je
Richtung** (``filter=to`` / ``filter=from``) abgefragt werden; die ungefilterte Liste hängt.

Aufbau
    * ``/addresses/<a>/transactions`` – normale Transaktionen (neueste zuerst, Schlüsselfolge ``next_page_params``)
    * ``/addresses/<a>/internal-transactions`` – interne Wertbewegungen (Trace)
    * ``/addresses/<a>/token-transfers?type=ERC-20&filter=to|from`` – ERC-20-Transfers je Richtung
    * ``/addresses/<a>`` und ``/addresses/<a>/tokens?type=ERC-20`` – Bestände

Die Zeilen werden in die Felder der Etherscan-kompatiblen API übersetzt (``blockNumber``, ``timeStamp``, ``hash``,
``from``, ``to``, ``value``, ``gasUsed``, ``gasPrice``, ``contractAddress`` …) – der gemeinsame EVM-Adapter wertet
sie unverändert aus, Kennungen der Bewegungen bleiben identisch zu denen des Etherscan-Wegs.

Grenzen
    Token-Transfers tragen in v2 keine Blocknummer; sie ist nur bekannt, wenn dieselbe Transaktion in den anderen
    Listen steht, sonst ``0`` (nur Anzeige – maßgeblich ist der Zeitstempel). Die Listen kommen neueste zuerst; ein
    Abruf endet deshalb erst, wenn die Seiten bis zum Fortsetzungspunkt gelesen sind (bei Erstabruf: bis zum Anfang).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.datasources import connector as K
from app.datasources.chainhttp import ChainHttp

MAX_PAGES = 400  # je Liste und Lauf (≈ 20.000 Einträge zu 50)


def unix(ts: Any) -> int:
    """ISO-8601-Zeitstempel von Blockscout (``2023-11-03T18:29:15.000000Z``) → Unix-Sekunden; 0 bei Unlesbarem."""
    try:
        return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(UTC).timestamp())
    except (TypeError, ValueError):
        return 0


def _hash(v: Any) -> str:
    return str(v.get("hash") or "").lower() if isinstance(v, dict) else str(v or "").lower()


def _block(it: dict[str, Any]) -> str:
    b = it.get("block_number", it.get("block"))
    return str(b) if isinstance(b, int | str) and str(b).isdigit() else "0"


def tx_row(it: dict[str, Any]) -> dict[str, Any]:
    ok = str(it.get("status") or "ok") == "ok" and str(it.get("result") or "success") == "success"
    raw = str(it.get("raw_input") or "0x")
    dec = it.get("decoded_input") if isinstance(it.get("decoded_input"), dict) else {}
    mid = str(dec.get("method_id") or "")
    return {"hash": str(it.get("hash") or "").lower(), "blockNumber": _block(it),
            "timeStamp": str(unix(it.get("timestamp"))),
            "from": _hash(it.get("from")), "to": _hash(it.get("to")) or _hash(it.get("created_contract")),
            "value": str(it.get("value") or "0"), "gasUsed": str(it.get("gas_used") or "0"),
            "gasPrice": str(it.get("gas_price") or "0"), "isError": "0" if ok else "1",
            "txreceipt_status": "1" if ok else "0",
            "input": raw if len(raw) <= 10 else raw[:10],  # nur ob/welche Methode – nie die Nutzdaten
            "methodId": ("0x" + mid) if mid and not mid.startswith("0x") else (mid or raw[:10]),
            "functionName": str(dec.get("method_call") or "")}


def internal_row(it: dict[str, Any]) -> dict[str, Any]:
    h = str(it.get("transaction_hash") or it.get("transaction") or "").lower()
    return {"hash": h, "transactionHash": h, "blockNumber": _block(it), "timeStamp": str(unix(it.get("timestamp"))),
            "from": _hash(it.get("from")), "to": _hash(it.get("to")) or _hash(it.get("created_contract")),
            "value": str(it.get("value") or "0"),
            "isError": "0" if it.get("success", True) and not it.get("error") else "1"}


def token_row(it: dict[str, Any]) -> dict[str, Any]:
    tok = it.get("token") if isinstance(it.get("token"), dict) else {}
    tot = it.get("total") if isinstance(it.get("total"), dict) else {}
    h = str(it.get("tx_hash") or it.get("transaction_hash") or "").lower()
    dec = tot.get("decimals") if tot.get("decimals") is not None else tok.get("decimals")
    return {"hash": h, "transactionHash": h, "blockNumber": _block(it), "timeStamp": str(unix(it.get("timestamp"))),
            "from": _hash(it.get("from")), "to": _hash(it.get("to")),
            "contractAddress": str(tok.get("address") or tok.get("address_hash") or "").lower(),
            "value": str(tot.get("value") or "0"), "tokenSymbol": tok.get("symbol"), "tokenName": tok.get("name"),
            "tokenDecimal": "" if dec is None else str(dec), "logIndex": str(it.get("log_index") or "")}


class BlockscoutV2:
    """Lesezugriff auf die REST-API v2 einer Blockscout-Instanz für eine Adresse."""

    def __init__(self, http: ChainHttp, addr: str, report: Callable[[str], None] | None = None) -> None:
        self.http, self.addr = http, addr
        self._report = report or (lambda _t: None)
        self.capped: list[str] = []  # Listen, die an MAX_PAGES endeten (unvollständig)

    def _get(self, path: str, params: dict[str, Any] | None, what: str) -> dict[str, Any]:
        body = self.http.get(path, params, what=what)
        if not isinstance(body, dict):
            raise K.ConnectorError("data", f"Unerwartete Antwort von {self.http.ep.label} ({what}).")
        return body

    def _list(self, path: str, params: dict[str, Any], what: str, since_ts: int,
              ts_key: str = "timestamp") -> list[dict[str, Any]]:
        """Alle Einträge neuer als ``since_ts`` (Unix-Sekunden), neueste zuerst gelesen."""
        out: list[dict[str, Any]] = []
        query = dict(params)
        for page in range(MAX_PAGES):
            self._report(f"{what} (Seite {page + 1})")
            body = self._get(path, query, what)
            items = [i for i in body.get("items") or [] if isinstance(i, dict)]
            done = False
            for it in items:
                if unix(it.get(ts_key)) <= since_ts:
                    done = True
                    break
                out.append(it)
            nxt = body.get("next_page_params")
            if done or not items or not isinstance(nxt, dict) or not nxt:
                return out
            query = {**params, **nxt}
        self.capped.append(what)
        return out

    def transactions(self, since_ts: int) -> list[dict[str, Any]]:
        return [tx_row(i) for i in self._list(f"/addresses/{self.addr}/transactions", {}, "Transaktionen", since_ts)]

    def internal(self, since_ts: int) -> list[dict[str, Any]]:
        return [internal_row(i) for i in self._list(f"/addresses/{self.addr}/internal-transactions", {},
                                                    "interne Bewegungen", since_ts)]

    def tokens(self, since_ts: int) -> list[dict[str, Any]]:
        """ERC-20-Transfers je Richtung (die ungefilterte Liste hängt bei Spam-lastigen Adressen); Selbstüberweisungen
        stehen in beiden Richtungen und werden über (Transaktion, Log-Index) nur einmal geführt."""
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for direction in ("to", "from"):
            for it in self._list(f"/addresses/{self.addr}/token-transfers", {"type": "ERC-20", "filter": direction},
                                 "Token-Transfers", since_ts):
                key = (str(it.get("tx_hash") or "").lower(), str(it.get("log_index") or ""))
                if key in seen:
                    continue
                seen.add(key)
                out.append(token_row(it))
        return out

    def balance_history(self, since_ts: int) -> list[dict[str, Any]]:
        """Änderungen des nativen Bestands je Block laut Knoten (``delta`` in kleinster Einheit, ``block_number``,
        ``block_timestamp``, ``transaction_hash`` bzw. ``None``) – unabhängig von der Spurliste des Indexers."""
        return self._list(f"/addresses/{self.addr}/coin-balance-history", {}, "Bestandsverlauf", since_ts,
                          "block_timestamp")

    def native_balance(self) -> int:
        body = self._get(f"/addresses/{self.addr}", None, "Bestand")
        try:
            return int(str(body.get("coin_balance") or "0"))
        except ValueError:
            raise K.ConnectorError("data", f"{self.http.ep.label}: Bestand nicht lesbar.") from None

    def token_balances(self) -> dict[str, int]:
        """Contract (klein) → Menge in kleinster Einheit; höchstens ``MAX_PAGES`` Seiten."""
        out: dict[str, int] = {}
        query: dict[str, Any] = {"type": "ERC-20"}
        for _ in range(MAX_PAGES):
            body = self._get(f"/addresses/{self.addr}/tokens", query, "Token-Bestände")
            for it in body.get("items") or []:
                tok = it.get("token") if isinstance(it, dict) and isinstance(it.get("token"), dict) else None
                if tok is None:
                    continue
                try:
                    out[str(tok.get("address") or tok.get("address_hash") or "").lower()] = int(str(it.get("value")))
                except ValueError:
                    continue
            nxt = body.get("next_page_params")
            if not isinstance(nxt, dict) or not nxt:
                break
            query = {"type": "ERC-20", **nxt}
        return out

    def counters(self) -> dict[str, Any]:
        return self._get(f"/addresses/{self.addr}/counters", None, "Zähler")

    def has_nft(self) -> bool:
        for kind in ("ERC-721", "ERC-1155"):
            body = self._get(f"/addresses/{self.addr}/token-transfers", {"type": kind, "filter": "to"},
                             "NFT-Transfers")
            if body.get("items"):
                return True
        return False

    def block_ts(self, number: int) -> int:
        return unix(self._get(f"/blocks/{int(number)}", None, "Block").get("timestamp"))
