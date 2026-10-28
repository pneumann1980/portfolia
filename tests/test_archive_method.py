"""Methode „Kettenbestand“ (archive_rpc) für peaq und Polkadot: kein Aufruf von Subscan/PubFi, nur Bestand per RPC."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx

from app.datasources import connector as K
from app.datasources.chainhttp import ENDPOINTS
from app.datasources.chains import peaq as PQ
from app.datasources.chains import polkadot as PD
from app.datasources.chains.codec import ss58_encode
from app.datasources.wallet import WatchConfig

D = Decimal
DOT = ss58_encode(bytes([0x55]) * 32, 0)
EVM = "0x1e27c175d496f315c4955fa1f31d4fd5777ed856"


def cfg(provider: str, address: str, method: str | None) -> K.SourceConfig:
    return K.SourceConfig(1, "wallet", provider, "W", "Konto", address, watch={"provider": method} if method else {})


def transport(seen: list[str], dot_free: int = 12_3456789012):
    def respond(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        assert req.url.host not in ("api.pubfi.ai", "polkadot.api.subscan.io", "peaq.api.subscan.io"), req.url
        body = json.loads(req.content)
        m = body["method"]
        if m == "state_getStorage":
            acct = bytes(16) + dot_free.to_bytes(16, "little") + bytes(16) + bytes(32)
            res = "0x" + acct.hex() if "polkadot-asset-hub" not in req.url.host else None
        elif m == "eth_getBalance":
            res = hex(5 * 10**18)
        elif m == "eth_getTransactionCount":
            res = "0x3"
        else:
            res = None
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": res})
    return httpx.MockTransport(respond)


def prepared(conn, seen):
    conn.transport, conn.sleep = transport(seen), lambda s: None
    return conn


def test_archive_endpoint_is_a_selectable_method_for_peaq_and_polkadot():
    assert "archive_rpc" in ENDPOINTS and not ENDPOINTS["archive_rpc"].key_required
    assert PD.PolkadotConnector.endpoints[0] == "pubfi" and PD.PolkadotConnector.endpoints[-1] == "archive_rpc"
    assert PQ.PeaqConnector.endpoints[-1] == "archive_rpc"
    assert WatchConfig.load({"addresses": [DOT], "provider": "archive_rpc"}).provider == "archive_rpc"


def test_polkadot_archive_method_never_calls_the_indexer():
    seen: list[str] = []
    conn = prepared(PD.PolkadotConnector(), seen)
    c = cfg("polkadot", DOT, "archive_rpc")
    res = conn.check(c, K.Secret(None))
    assert res.ok and res.balances[0].qty == D("12.3456789012") and "Kettenbestand" in res.message
    fetched = conn.fetch(c, K.Secret(None), None)
    assert fetched.events == [] and fetched.balances[0].qty == D("12.3456789012")
    assert not fetched.complete and "Kettenbestand" in fetched.gaps[0]
    assert fetched.coverage["mode"].startswith("Kettenbestand")
    assert set(seen) == {"rpc.polkadot.io", "polkadot-asset-hub-rpc.polkadot.io"}


def test_peaq_archive_method_never_calls_the_indexer_and_needs_an_evm_address():
    seen: list[str] = []
    conn = prepared(PQ.PeaqConnector(), seen)
    res = conn.check(cfg("peaq", EVM, "archive_rpc"), K.Secret(None))
    assert res.ok and res.balances[0].qty == D(5)
    fetched = conn.fetch(cfg("peaq", EVM, "archive_rpc"), K.Secret(None), {"x": 1})
    assert fetched.events == [] and fetched.balances[0].qty == D(5) and fetched.cursor == {"x": 1}
    assert set(seen) <= {"peaq.api.onfinality.io", "quicknode1.peaq.xyz", "peaq-rpc.publicnode.com"}
    ss58 = ss58_encode(bytes([0x11]) * 32, PQ.PEAQ_SS58)
    try:
        conn.check(cfg("peaq", ss58, "archive_rpc"), K.Secret(None))
    except K.ConnectorError as e:
        assert e.kind == "config" and "0x-Adressen" in e.message
    else:
        raise AssertionError("SS58 ohne Indexer muss abgelehnt werden")


def test_default_method_is_unchanged():
    conn = PD.PolkadotConnector()
    assert conn.endpoint(cfg("polkadot", DOT, None)).id == "pubfi"
    assert conn.endpoint(cfg("polkadot", DOT, "archive_rpc")).id == "archive_rpc"
