"""Robinhood Chain (Chain-ID 4663, Blockscout-Explorer) – anonymisierte Fixtures, ohne Netz.

Abgedeckt: Abruf über die Etherscan-kompatible Blockscout-Schnittstelle (Vorgänge, Tokens mit Chain-Kennung, Bestand),
Adressprüfung, Bot-Schutzseite des Explorers → Bestand laut öffentlichem RPC statt Totalausfall.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources.providers import PROVIDERS, normalize_address
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import (
    ETHERSCAN_KEY,
    MASTER,
    FakeEvm,
    all_rows,
    balances,
    create_wallet,
    ctx,
    load,
    make_client,
)

D = Decimal
A = "0x1111111111111111111111111111111111111111"
CHAIN = 4663


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


class Robinhood:
    """Blockscout (``robinhoodchain.blockscout.com/api``) über die Etherscan-Fixtures plus öffentlicher RPC."""

    def __init__(self, rpc_balance: int = 0) -> None:
        self.inner = FakeEvm({CHAIN: copy.deepcopy(load("evm_eth.json"))}, free_chains=(CHAIN,), key=ETHERSCAN_KEY)
        self.rpc_balance, self.challenge, self.hosts = rpc_balance, False, []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.hosts.append(req.url.host)
        if req.url.host == "rpc.mainnet.chain.robinhood.com":
            body = json.loads(req.content)
            assert body["method"] == "eth_getBalance"
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": hex(self.rpc_balance)})
        assert req.url.host == "robinhoodchain.blockscout.com" and req.url.path == "/api", req.url
        if self.challenge:  # wie live (10/2026) aus Rechenzentren: Cloudflare-Prüfseite statt API
            return httpx.Response(403, text="<html><title>Just a moment...</title></html>",
                                  headers={"cf-mitigated": "challenge", "content-type": "text/html"})
        q = dict(req.url.params)
        if q.get("module") == "block" and q.get("action") == "eth_block_number":
            q = {"module": "proxy", "action": "eth_blockNumber"}
        url = httpx.URL(f"https://api.routescan.io/v2/network/mainnet/evm/{CHAIN}/etherscan/api", params=q)
        return self.inner.handler(httpx.Request("GET", url))


@pytest.fixture
def robin(monkeypatch):
    fake = Robinhood()
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    return fake


def sync(c, sid):
    return datasource_service(ctx(c)).sync(sid, "manual")


def test_address_validation_like_other_evm_chains():
    p = PROVIDERS["robinhood"]
    assert normalize_address(p, A.upper().replace("0X", "0x"))[1] is None
    assert normalize_address(p, "0x123")[1]


def test_robinhood_sync_books_native_eth_and_tokens_with_the_chain_tag(client, robin):
    sid = create_wallet(client, "robinhood", A, name="Robinhood Wallet")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    assert rows and all(k.startswith("robinhood:") for k in rows)
    syms = {rc.rec.in_sym or rc.rec.out_sym for rc in rows.values()}
    assert "ETH" in syms and any("@ROBINHOOD:" in (s or "") for s in syms)
    assert set(robin.hosts) == {"robinhoodchain.blockscout.com"}  # kein Etherscan, keine Schlüssel


def test_robinhood_bot_protection_page_keeps_the_balance_from_the_public_rpc(client, robin):
    robin.challenge, robin.rpc_balance = True, 2 * 10**18
    sid = create_wallet(client, "robinhood", A, name="Robinhood Wallet")
    ok, msg = datasource_service(ctx(client)).check(sid)
    assert not ok and "Bestand laut öffentlichem RPC: 2 ETH" in msg
    assert balances(client, sid)["ETH"] == "2"
