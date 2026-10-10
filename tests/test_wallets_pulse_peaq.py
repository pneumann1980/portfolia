"""PulseChain (Blockscout + Fork-Bestand) und peaq (Subscan: SS58 über PubFi, EVM über direkten Subscan-Key) –
synthetische Daten, ohne Netz.

Abgedeckt: Historie erst ab dem ersten PulseChain-Block (Ethereum-Vorgeschichte nie abgefragt), kopierter
PLS-Bestand am Fork-Block als prüfpflichtige Eröffnung (nur Erstabruf, RPC ``eth_getBalance`` am Block 17.233.000),
Fehler des RPC nur als Warnung; SS58-Codec mit Zwei-Byte-Präfix (peaq 1221), Adressprüfung (generisch 42 → 1221,
fremdes Netz abgelehnt, 0x → EVM), peaq-Substrate über das PubFi-Gateway (18 Nachkommastellen, eigene Kennung,
Rewards-Route optional), peaq-EVM nur mit direktem Subscan-Key (X-API-Key, Etherscan-kompatibel) und klare
Ablehnung über PubFi.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources.chains import evm as E
from app.datasources.chains.codec import ss58_decode, ss58_encode
from app.datasources.providers import PROVIDERS, normalize_address
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import (
    MASTER,
    FakeEvm,
    FakeSubscan,
    all_rows,
    balances,
    create_wallet,
    ctx,
    make_client,
    set_provider_key,
    source,
)

D = Decimal
A = "0x1111111111111111111111111111111111111111"
X = "0x3333333333333333333333333333333333333333"
KEY = "pubfi_test_key_0123456789abcdef"
SUBSCAN_KEY = "subscan_test_key_0123456789abcd"
FORK = 17_233_000
WEI = 10**18


def H(n: int) -> str:
    return "0x" + f"{n:064x}"


def tx(block: int, n: int, frm: str, to: str, value: int, ts: int = 1_700_000_000) -> dict:
    return {"blockNumber": str(block), "timeStamp": str(ts + block % 1000), "hash": H(n), "nonce": "1",
            "blockHash": H(n + 10_000), "transactionIndex": "0", "from": frm, "to": to, "value": str(value),
            "gas": "21000", "gasPrice": "1000000000", "isError": "0", "txreceipt_status": "1", "input": "0x",
            "contractAddress": "", "cumulativeGasUsed": "21000", "gasUsed": "21000", "confirmations": "100",
            "methodId": "0x", "functionName": ""}


class FakePulse(FakeEvm):
    """Blockscout-API des PulseChain-Explorers (``api.scan.pulsechain.com/api``) und PulseChain-RPC."""

    def __init__(self, data: dict, fork_balance: int | None) -> None:
        super().__init__({369: data, 3338: copy.deepcopy(data)})
        self.fork_balance = fork_balance
        self.rpc: list[dict] = []
        self.subscan_key = SUBSCAN_KEY
        self.windows: list[tuple] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if host == "rpc.pulsechain.com":
            assert req.method == "POST" and req.url.path in ("", "/")
            body = json.loads(req.content)
            self.rpc.append(body)
            assert body["method"] == "eth_getBalance" and body["params"][1] == hex(FORK)
            if self.fork_balance is None:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                                 "error": {"code": -32000, "message": "missing trie node"}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": hex(self.fork_balance)})
        if host == "api.scan.pulsechain.com":
            assert req.url.path == "/api" and "apikey" not in req.url.params
            chain = 369
        elif host == "peaq.api.subscan.io":
            assert req.url.path == "/api/scan/evm/etherscan"
            assert req.headers.get("x-api-key") == self.subscan_key and "apikey" not in req.url.params
            chain = 3338
            if req.url.params.get("module") == "proxy":
                return httpx.Response(200, json={"status": "0", "message": "NOTOK", "result": "Unknown module"})
        else:  # pragma: no cover
            raise AssertionError(f"unerwarteter Host {host}")
        params = dict(req.url.params)
        if chain == 369 and "startblock" in params:  # wie live: nur start_block/end_block wirken
            self.windows.append((params.get("start_block"), params.get("end_block")))
            params["startblock"] = params.pop("start_block", "0")
            params["endblock"] = params.pop("end_block", "999999999")
        if params.get("module") == "block" and params.get("action") == "eth_block_number":
            params = {"module": "proxy", "action": "eth_blockNumber"}
        if params.get("module") == "block" and params.get("action") == "getblocknobytime":
            self.calls.append(req)
            return httpx.Response(200, json={"status": "1", "message": "OK",
                                             "result": str(self.chains[chain]["tip"])})
        fwd = httpx.Request("GET", f"https://api.routescan.io/v2/network/mainnet/evm/{chain}/etherscan/api",
                            params=params)
        resp = super().handler(fwd)
        self.calls[-1] = req
        return resp


def pulse_data() -> dict:
    return {"tip": FORK + 20_000, "balance": str(7 * WEI),
            "txlist": [tx(FORK - 500, 1, X, A, 99 * WEI),  # Ethereum-Vorgeschichte – nie abgefragt
                       tx(FORK + 100, 2, X, A, 5 * WEI),
                       tx(FORK + 200, 3, A, X, 1 * WEI)],
            "txlistinternal": [], "tokentx": [], "tokenbalance": {}}


@pytest.fixture
def pulse(monkeypatch):
    fake = FakePulse(pulse_data(), fork_balance=3 * WEI)
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    return fake


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def sync(c, sid):
    return datasource_service(ctx(c)).sync(sid, "manual")


def run_text(c, sid) -> str:
    run = ctx(c).db.q1("SELECT message, detail_json FROM data_source_run WHERE source_id=? ORDER BY id DESC", (sid,))
    return f"{run['message'] or ''} {run['detail_json'] or ''}"


def pk(n: int, sub: str) -> str:
    return f"pulsechain:{H(n)}:{A}#{sub}"


FORK_KEY = f"pulsechain:fork-{FORK}:{A}#fork"


def test_pulsechain_starts_at_first_block_and_offers_fork_opening(client, pulse):
    sid = create_wallet(client, "pulsechain", A, name="Ledger PLS")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    assert set(rows) == {FORK_KEY, pk(2, "n:in"), pk(3, "n:out")}  # Tx vor dem Fork (Ethereum) fehlt bewusst
    op = rows[FORK_KEY].rec
    assert (op.kind, op.in_sym, op.in_qty, op.tag) == ("deposit", "PLS", D(3), "fork")
    assert "Fork" in op.review and op.ts == E.PULSE_START  # nie automatisch übernommen
    assert rows[pk(2, "n:in")].rec.in_qty == D(5) and rows[pk(3, "n:out")].rec.out_qty == D(1)
    lists = [c for c in pulse.calls if c.url.params.get("action") in ("txlist", "txlistinternal", "tokentx")]
    assert lists and all(int(c.url.params["startblock"]) == FORK + 1 for c in lists)
    assert len(pulse.rpc) == 1 and pulse.rpc[0]["params"] == [A, hex(FORK)]
    assert balances(client, sid)["PLS"] == "7"
    # Folgelauf: inkrementell, keine zweite Eröffnung/RPC-Abfrage, keine Dubletten
    n = len(rows)
    sync(client, sid)
    assert len(pulse.rpc) == 1 and len(all_rows(client, sid)) == n
    assert json.loads(source(client, sid)["cursor_json"])["block"] > FORK + 1


def test_pulsechain_queries_block_windows_with_live_parameter_names(client, pulse, monkeypatch):
    monkeypatch.setattr(E.PulseChainConnector, "block_window", 5_000)
    sid = create_wallet(client, "pulsechain", A, name="Ledger PLS")
    assert sync(client, sid)["status"] == "synced"
    assert {pk(2, "n:in"), pk(3, "n:out")} <= set(all_rows(client, sid))
    starts = sorted({int(a) for a, _ in pulse.windows if a is not None})
    assert starts[0] == FORK + 1 and len(starts) >= 4  # 20.000 Blöcke in Fenstern zu 5.000
    assert all(int(b) - int(a) < 5_000 for a, b in pulse.windows if a is not None)


def test_pulsechain_provider_ignoring_block_range_books_nothing(client, pulse, monkeypatch):
    """Ignoriert ein Explorer den Blockbereich, kämen Ethereum-Vorgänge vor dem Fork – Abbruch statt Buchung."""
    from app.datasources import chainhttp as CHm

    ep = CHm.ENDPOINTS["blockscout_pulsechain"]
    monkeypatch.setitem(CHm.ENDPOINTS, "blockscout_pulsechain",
                        __import__("dataclasses").replace(ep, block_param_alias=False))
    sid = create_wallet(client, "pulsechain", A, name="Ledger PLS")
    res = sync(client, sid)
    assert "außerhalb des abgefragten Blockbereichs" in res.get("error", "")
    assert not all_rows(client, sid)


def test_pulsechain_fork_balance_error_is_only_a_warning(client, pulse):
    pulse.fork_balance = None
    sid = create_wallet(client, "pulsechain", A, name="Ledger PLS")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    assert FORK_KEY not in rows and pk(2, "n:in") in rows
    assert "Fork-Block" in run_text(client, sid)


def test_pulsechain_zero_fork_balance_creates_no_opening(client, pulse):
    pulse.fork_balance = 0
    sid = create_wallet(client, "pulsechain", A, name="Ledger PLS")
    sync(client, sid)
    assert FORK_KEY not in all_rows(client, sid)


ME_PEAQ = ss58_encode(b"\x55" * 32, 1221)
OTHER = ss58_encode(b"\x66" * 32, 1221)


def str_(block: int, idx: str, ev: int, frm: str, to: str, amount: str, v2: str) -> dict:
    return {"block_num": block, "block_timestamp": 1_760_000_000 + block, "extrinsic_index": idx, "event_idx": ev,
            "from": frm, "to": to, "amount": amount, "amount_v2": v2, "asset_symbol": "PEAQ", "asset_unique_id": "PEAQ",
            "module": "balances", "success": True, "hash": H(block), "fee": "0"}


def peaq_nets() -> dict:
    return {"peaq": {"tip": 5000,
                     "transfers": [str_(100, "100-2", 4, OTHER, ME_PEAQ, "12.5", str(125 * 10**17)),
                                   str_(200, "200-1", 3, ME_PEAQ, OTHER, "2", str(2 * WEI))],
                     "extrinsics": [{"block_num": 200, "block_timestamp": 1_760_000_200, "extrinsic_index": "200-1",
                                     "extrinsic_hash": H(200), "call_module": "balances",
                                     "call_module_function": "transfer_keep_alive", "fee": str(10**16),
                                     "fee_used": str(10**16), "success": True, "signer": ME_PEAQ}],
                     "rewards": [],
                     "tokens": [{"symbol": "PEAQ", "unique_id": "PEAQ", "decimals": 18, "balance": str(105 * 10**17)}]}}


RPC_HOSTS = ("peaq.api.onfinality.io", "quicknode1.peaq.xyz")


class FakePeaqRpc(FakeSubscan):
    """Subscan (PubFi) plus öffentlicher peaq-EVM-RPC für ``eth_getBalance`` / ``eth_getTransactionCount``."""

    def __init__(self, nets: dict, key: str) -> None:
        super().__init__(nets, key)
        self.rpc_balance, self.rpc_nonce, self.rpc_down = 0, 0, False
        self.rpc_calls: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.host not in RPC_HOSTS:
            return super().handler(req)
        assert req.method == "POST" and not req.headers.get("authorization"), "RPC: kein Schlüssel"
        body = json.loads(req.content)
        self.rpc_calls.append(body)
        if self.rpc_down:
            return httpx.Response(503, text="down")
        result = {"eth_getBalance": hex(self.rpc_balance),
                  "eth_getTransactionCount": hex(self.rpc_nonce)}[body["method"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def subscan(monkeypatch):
    fake = FakePeaqRpc(peaq_nets(), KEY)
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    return fake


def qk(idx: str, sub: str) -> str:
    return f"peaq:pq-{idx}:{ME_PEAQ}#{sub}"


def test_ss58_codec_two_byte_prefixes_roundtrip():
    pub = bytes(range(32))
    for prefix in (0, 2, 42, 63, 64, 1221, 16383):
        got_prefix, got_pub = ss58_decode(ss58_encode(pub, prefix))
        assert (got_prefix, got_pub) == (prefix, pub)


def test_peaq_address_validation():
    p = PROVIDERS["peaq"]
    assert normalize_address(p, ME_PEAQ) == (ME_PEAQ, None)
    assert normalize_address(p, ss58_encode(b"\x55" * 32, 42)) == (ME_PEAQ, None)  # generisch → peaq-Format
    assert normalize_address(p, ss58_encode(b"\x55" * 32, 0))[1]  # Polkadot-Konto: anderes Netz
    assert normalize_address(p, "0xabcdef0123456789abcdef0123456789abcdef01")[1] is None
    assert "Prüfsumme" in normalize_address(p, "0xAbCdEf0123456789abcdef0123456789ABCDEF01")[1]
    assert normalize_address(p, "0x" + "ab" * 32)[1]  # privater Schlüssel abgelehnt


def test_peaq_substrate_over_pubfi(client, subscan):
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "peaq", ME_PEAQ, name="peaq")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    r = rows[qk("100-2", "tr:4")].rec
    assert (r.kind, r.in_sym, r.in_qty) == ("deposit", "PEAQ", D("12.5"))
    r = rows[qk("200-1", "tr:3")].rec
    assert (r.kind, r.out_sym, r.out_qty, r.fee_sym, r.fee_qty) == ("withdrawal", "PEAQ", D(2), "PEAQ", D("0.01"))
    assert balances(client, sid) == {"PEAQ": "10.5"}  # 18 Nachkommastellen
    assert all(c.url.path.startswith("/v1/gateway/subscan/peaq/api/") and c.url.path.endswith(":free")
               for c in subscan.calls)
    assert json.loads(source(client, sid)["cursor_json"])["nets"]["peaq"]["block"] == 5001


def test_peaq_rewards_route_error_is_a_note_not_a_failure(client, subscan):
    subscan.fail_next(httpx.Response(200, json={"code": 10001, "message": "Not supported network", "data": None}),
                      lambda r: "reward_slash" in r.url.path)
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "peaq", ME_PEAQ, name="peaq")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    assert qk("100-2", "tr:4") in all_rows(client, sid)
    assert "Rewards" in run_text(client, sid)


def test_peaq_evm_address_over_pubfi_resolves_substrate_account(client, subscan):
    """0x-Adresse mit kostenlosem PubFi-Key: Subscan löst das Substrate-Konto auf, abgerufen wird dieses Konto –
    nie die Meldung „Subscan-Key nötig“."""
    subscan.nets["peaq"]["search"] = {A: ss58_encode(b"\x55" * 32, 42)}  # generisches Format → peaq-Präfix
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "peaq", A, name="peaq EVM")  # Standard-Anbieter: PubFi
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    assert rows[qk("100-2", "tr:4")].rec.in_qty == D("12.5")
    assert "ERC-20" in run_text(client, sid)
    search = [c for c in subscan.calls if c.url.path.endswith("v2/scan/search:free")]
    assert search and json.loads(search[0].content) == {"key": A}
    assert all(c.url.host == "api.pubfi.ai" for c in subscan.calls)


def test_peaq_evm_address_unknown_to_subscan_is_explained_when_rpc_is_down(client, subscan):
    subscan.rpc_down = True
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "peaq", A, name="peaq EVM")
    res = sync(client, sid)
    assert "kein peaq-Konto" in res["error"] and "Subscan direkt" in res["error"]


def test_peaq_evm_address_unknown_to_subscan_and_empty_on_chain_is_a_clean_empty_account(client, subscan):
    """Subscan kennt die Adresse nicht, die Kette zeigt Bestand 0 und nie eine Transaktion: kein Fehler, Bestand 0."""
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "peaq", A, name="peaq EVM")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    assert not all_rows(client, sid) and balances(client, sid).get("PEAQ") in ("0", None)
    assert "leer" in run_text(client, sid)
    assert [c["method"] for c in subscan.rpc_calls] == ["eth_getBalance", "eth_getTransactionCount"]
    # Später entsteht das Konto bei Subscan → regulärer Abruf ab Block 0, nichts geht verloren
    subscan.nets["peaq"]["search"] = {A: ss58_encode(b"\x55" * 32, 42)}
    res = sync(client, sid)
    assert res["status"] == "synced", res
    assert all_rows(client, sid)[qk("100-2", "tr:4")].rec.in_qty == D("12.5")


def test_peaq_evm_address_unknown_to_subscan_but_funded_on_chain_is_not_called_empty(client, subscan):
    subscan.rpc_balance, subscan.rpc_nonce = 3 * WEI, 2
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "peaq", A, name="peaq EVM")
    res = sync(client, sid)
    assert res.get("status") != "synced"
    assert "3 PEAQ" in res["error"] and "Subscan direkt" in res["error"]
    assert not all_rows(client, sid)


def test_peaq_evm_with_direct_subscan_key(client, pulse):
    pulse.chains[3338] = pulse_data() | {"tip": FORK + 20_000}
    sid2 = create_wallet(client, "peaq", A, name="peaq EVM 2", chain_provider="subscan")
    res = sync(client, sid2)
    assert "Schlüssel" in res["error"] and not pulse.calls
    set_provider_key(client, "subscan", SUBSCAN_KEY)
    res = sync(client, sid2)
    assert res.get("status") == "synced", res
    rows = all_rows(client, sid2)
    assert f"peaq:{H(2)}:{A}#n:in" in rows and f"peaq:{H(1)}:{A}#n:in" in rows  # keine Fork-Grenze auf peaq
    assert rows[f"peaq:{H(2)}:{A}#n:in"].rec.in_sym == "PEAQ"
    assert {c.url.host for c in pulse.calls} == {"peaq.api.subscan.io"}
    assert not any(c.url.params.get("module") == "proxy" for c in pulse.calls)
