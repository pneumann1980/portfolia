"""Öffentliche Blockchain-Explorer für Belege (M25/AP3 Stufe 4) – nur mit Freigabe, nur der Transaktions-Hash.

Genutzt werden die bereits eingebundenen, schlüsselfreien Endpunkte des Wallet-Frameworks
(:mod:`app.datasources.chainhttp`, mit Ratenlimit, Anfrage-/Zeitbudget und Abbruch):

* Bitcoin – mempool.space Esplora-API ``GET /tx/{txid}`` (Gebühr in Satoshi, Blockzeit, Bestätigung)
* Kaspa – api.kaspa.org ``GET /transactions/{id}?resolve_previous_outpoints=light`` (Blockzeit, Annahme, Gebühr =
  Eingänge − Ausgänge)

EVM-Ketten brauchen einen Anbieter-Schlüssel (Etherscan) bzw. einen RPC-Knoten – für Belege bewusst nicht angebunden
(dokumentierte Grenze). Übertragen wird ausschließlich der Hash; keine Adressen, Namen, Mengen oder Kontodaten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

HEX64 = re.compile(r"^[0-9a-f]{64}$")


class LookupError(Exception):
    """Explorer nicht erreichbar, gedrosselt oder Antwort unbrauchbar (Text ohne Geheimnisse)."""


@dataclass(frozen=True)
class TxInfo:
    explorer: str
    confirmed: bool
    block_time: datetime | None
    fee: Decimal | None
    fee_sym: str


def chain_for(symbol: str | None, chain: str | None, h: str) -> str | None:
    s, c = (symbol or "").upper(), (chain or "").lower()
    if not HEX64.match(h):
        return None
    if s == "BTC" or "bitcoin" in c:
        return "bitcoin"
    if s == "KAS" or "kaspa" in c:
        return "kaspa"
    return None


def lookup(chain: str, h: str, *, transport: Any = None) -> TxInfo | None:
    from app.datasources import connector as K
    from app.datasources.chainhttp import ENDPOINTS, ChainHttp, Stop

    if not HEX64.match(h):
        raise LookupError("Kein gültiger Transaktions-Hash.")
    ep = ENDPOINTS["mempool" if chain == "bitcoin" else "kaspa"]
    try:
        with ChainHttp(ep, transport=transport, max_requests=2, wait_budget_s=8.0, deadline_s=20.0) as http:
            if chain == "bitcoin":
                body = http.get(f"/tx/{h}", what="Transaktion", allow_status=(400, 404))
                if not isinstance(body, dict):
                    return None
                st = body.get("status") or {}
                bt = st.get("block_time")
                return TxInfo(ep.label, bool(st.get("confirmed")),
                              datetime.fromtimestamp(int(bt), UTC) if isinstance(bt, int) else None,
                              (Decimal(int(body["fee"])) / Decimal(100_000_000)) if isinstance(body.get("fee"), int)
                              else None, "BTC")
            body = http.get(f"/transactions/{h}", {"resolve_previous_outpoints": "light"}, what="Transaktion",
                            allow_status=(400, 404))
            if not isinstance(body, dict):
                return None
            ins = body.get("inputs") or []
            outs = body.get("outputs") or []
            fee = None
            if ins and all(i.get("previous_outpoint_amount") is not None for i in ins):
                fee = (Decimal(sum(int(i["previous_outpoint_amount"]) for i in ins))
                       - Decimal(sum(int(o.get("amount") or 0) for o in outs))) / Decimal(100_000_000)
            bt = body.get("block_time")
            return TxInfo(ep.label, bool(body.get("is_accepted")),
                          datetime.fromtimestamp(int(bt) / 1000, UTC) if isinstance(bt, int) else None,
                          fee if fee is None or fee >= 0 else None, "KAS")
    except Stop as e:
        raise LookupError(f"{ep.label}: Budget erschöpft ({e}).") from None
    except K.ConnectorError as e:
        raise LookupError(f"{ep.label}: {e.message}") from None
    except (ValueError, KeyError, TypeError) as e:
        raise LookupError(f"{ep.label}: unerwartete Antwort ({type(e).__name__}).") from None
