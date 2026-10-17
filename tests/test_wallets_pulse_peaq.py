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
        self.v2_mode = "ok"  # ok | gone (404) | down (502)
        self.v2_calls: list[httpx.Request] = []

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
            if req.url.path.startswith("/api/v2"):
                return self.v2(req)
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


def _iso(ts: str) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _party(a: str) -> dict:
    return {"hash": a, "is_contract": False, "name": None}


def _v2_tx(r: dict) -> dict:
    return {"hash": r["hash"], "block": int(r["blockNumber"]), "timestamp": _iso(r["timeStamp"]),
            "from": _party(r["from"]), "to": _party(r["to"]), "value": r["value"], "gas_used": r["gasUsed"],
            "gas_price": r["gasPrice"], "status": "ok" if r["isError"] == "0" else "error",
            "result": "success" if r["isError"] == "0" else "execution reverted", "raw_input": r.get("input", "0x"),
            "decoded_input": None}


def _v2_internal(r: dict) -> dict:
    return {"transaction_hash": r["hash"], "block": int(r["blockNumber"]), "timestamp": _iso(r["timeStamp"]),
            "from": _party(r["from"]), "to": _party(r["to"]), "value": r["value"], "success": r["isError"] == "0",
            "error": None, "index": 1}


def _v2_token(r: dict, i: int) -> dict:
    # wie live: Token-Transfers tragen keine Blocknummer
    return {"tx_hash": r["hash"], "log_index": str(i), "timestamp": _iso(r["timeStamp"]), "from": _party(r["from"]),
            "to": _party(r["to"]), "type": "token_transfer",
            "token": {"address": r["contractAddress"], "symbol": r.get("tokenSymbol"), "name": r.get("tokenName"),
                      "decimals": r.get("tokenDecimal"), "type": "ERC-20"},
            "total": {"value": r["value"], "decimals": r.get("tokenDecimal")}}


def _v2(self, req: httpx.Request) -> httpx.Response:
    """Blockscout REST v2 des PulseChain-Explorers (Seiten zu 50, neueste zuerst, ``next_page_params``)."""
    import re as _re

    self.calls.append(req)
    self.v2_calls.append(req)
    if self.v2_mode == "gone":
        return httpx.Response(404, json={"message": "Not found"})
    if self.v2_mode == "down":
        return httpx.Response(502, text="Bad Gateway")
    path = req.url.path
    q = dict(req.url.params)
    d = self.chains[369]
    m = _re.match(r"^/api/v2/(addresses/(0x[0-9a-f]{40})(?:/([a-z-]+))?|blocks/(\d+))$", path.lower())
    assert m, path
    if m.group(4):
        return httpx.Response(200, json={"timestamp": _iso(str(1_700_000_000 + int(m.group(4)) % 1000))})
    addr, what = m.group(2), m.group(3)
    if what is None:
        return httpx.Response(200, json={"coin_balance": d.get("balance", "0")})
    if what == "counters":
        return httpx.Response(200, json={"transactions_count": str(len(d.get("txlist", [])))})
    if what == "tokens":
        have = d.get("tokenbalance", {})
        return httpx.Response(200, json={"items": [{"token": {"address": c, "type": "ERC-20"}, "value": v}
                                                   for c, v in have.items()], "next_page_params": None})
    if what == "token-transfers" and q.get("type") != "ERC-20":
        return httpx.Response(200, json={"items": [], "next_page_params": None})
    if what == "token-transfers" and q.get("filter") not in ("to", "from"):
        return httpx.Response(504, text="Gateway Timeout")  # wie live: die ungefilterte Liste hängt
    def mine(r: dict) -> bool:
        return addr in (r["from"].lower(), r["to"].lower())

    if what == "transactions":
        rows = [_v2_tx(r) for r in d.get("txlist", []) if mine(r)]
    elif what == "internal-transactions":
        rows = [_v2_internal(r) for r in d.get("txlistinternal", []) if mine(r)]
    elif what == "token-transfers":
        want = q["filter"]
        rows = [_v2_token(r, i) for i, r in enumerate(d.get("tokentx", []))
                if r[want].lower() == addr]
    elif what == "coin-balance-history":
        delta: dict[int, int] = {}
        for r in d.get("txlist", []):
            if not mine(r):
                continue
            b = int(r["blockNumber"])
            if r["from"].lower() == addr:
                delta[b] = delta.get(b, 0) - int(r["gasUsed"]) * int(r["gasPrice"])
            if r["isError"] == "0":
                delta[b] = delta.get(b, 0) + int(r["value"]) * ((r["to"].lower() == addr) - (r["from"].lower() == addr))
        for b, extra in d.get("history_extra", {}).items():  # Änderungen ohne Spur beim Indexer
            delta[int(b)] = delta.get(int(b), 0) + extra
        ts = {int(r["blockNumber"]): r["timeStamp"] for r in d.get("txlist", [])}
        rows = [{"block_number": b, "block_timestamp": _iso(ts.get(b, "1700000000")), "delta": str(x), "value": "0",
                 "transaction_hash": next((r["hash"] for r in d.get("txlist", []) if int(r["blockNumber"]) == b),
                                          None)} for b, x in delta.items() if x]
    else:  # pragma: no cover
        raise AssertionError(path)
    key = "block_timestamp" if what == "coin-balance-history" else "timestamp"
    rows.sort(key=lambda r: r[key], reverse=True)
    off = int(q.get("p", 0))
    page = rows[off:off + 50]
    nxt = {"p": off + 50} if off + 50 < len(rows) else None
    return httpx.Response(200, json={"items": page, "next_page_params": nxt})


FakePulse.v2 = _v2  # type: ignore[attr-defined]


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
    assert pulse.v2_calls and not [c for c in pulse.calls if c.url.path == "/api"]  # nur REST v2
    assert len(pulse.rpc) == 1 and pulse.rpc[0]["params"] == [A, hex(FORK)]
    assert balances(client, sid)["PLS"] == "7"
    # Folgelauf: inkrementell, keine zweite Eröffnung/RPC-Abfrage, keine Dubletten
    n = len(rows)
    sync(client, sid)
    assert len(pulse.rpc) == 1 and len(all_rows(client, sid)) == n
    assert json.loads(source(client, sid)["cursor_json"])["block"] > FORK + 1


def test_pulsechain_reads_all_pages_newest_first_and_resumes_by_timestamp(client, pulse):
    """120 Transaktionen = 3 Seiten zu 50; der Folgelauf liest nur noch bis zum Fortsetzungspunkt zurück."""
    pulse.chains[369]["txlist"] = [tx(FORK + 100 + i, 100 + i, X, A, WEI // 10) for i in range(120)]
    pulse.chains[369]["balance"] = str(12 * WEI)
    sid = create_wallet(client, "pulsechain", A, name="Ledger PLS")
    assert sync(client, sid)["status"] == "synced"
    rows = all_rows(client, sid)
    assert len([k for k in rows if k.endswith("#n:in")]) == 120
    tx_pages = [c for c in pulse.v2_calls if c.url.path.endswith("/transactions")]
    assert [c.url.params.get("p", "0") for c in tx_pages] == ["0", "50", "100"]
    cur = json.loads(source(client, sid)["cursor_json"])
    assert cur["v"] == 2 and cur["ts"] > 0 and cur["block"] == FORK + 219
    pulse.v2_calls.clear()
    assert sync(client, sid)["status"] == "synced"
    assert len([c for c in pulse.v2_calls if c.url.path.endswith("/transactions")]) == 1  # Fortsetzung ab Zeitzeiger
    assert len(all_rows(client, sid)) == len(rows)  # keine Dubletten durch die Überlappung


def test_pulsechain_token_transfers_are_queried_per_direction_never_unfiltered(client, pulse):
    """Die ungefilterte Token-Liste hängt beim Explorer (Spam-lastige Adressen) – nur filter=to / filter=from."""
    tok = "0x" + "ab" * 20
    pulse.chains[369]["tokentx"] = [
        {**tx(FORK + 300, 7, X, A, 5 * 10**18), "contractAddress": tok, "tokenSymbol": "AAA", "tokenName": "Alpha",
         "tokenDecimal": "18"},
        {**tx(FORK + 310, 8, A, X, 2 * 10**18), "contractAddress": tok, "tokenSymbol": "AAA", "tokenName": "Alpha",
         "tokenDecimal": "18"}]
    pulse.chains[369]["tokenbalance"] = {tok: str(3 * 10**18)}
    sid = create_wallet(client, "pulsechain", A, name="Ledger PLS")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    tt = [c for c in pulse.v2_calls if c.url.path.endswith("/token-transfers")]
    assert tt and {c.url.params.get("filter") for c in tt} == {"to", "from"}
    assert all(c.url.params.get("type") == "ERC-20" for c in tt)
    rows = all_rows(client, sid)
    token_rows = [r for k, r in rows.items() if "AAA" in (r.rec.in_sym or "") + (r.rec.out_sym or "")]
    assert sorted((r.rec.kind, r.rec.in_qty or r.rec.out_qty) for r in token_rows) == [("deposit", D(5)),
                                                                                     ("withdrawal", D(2))]
    assert balances(client, sid)[f"AAA@PLS:{tok}"] == "3"


def test_pulsechain_balance_change_without_trace_becomes_a_review_proposal(client, pulse):
    """Der Knoten meldet eine Bestandsänderung, die keine Transaktions-/Spurliste erklärt (Beobachtung live: +20.794 PLS
    in einer Transaktion) – sie wird als eigener Vorschlag zur Prüfung geführt, damit der Bestand stimmt."""
    pulse.chains[369]["history_extra"] = {str(FORK + 200): 4 * WEI}
    sid = create_wallet(client, "pulsechain", A, name="Ledger PLS")
    assert sync(client, sid)["status"] == "synced"
    rows = all_rows(client, sid)
    key = f"pulsechain:bal-{FORK + 200}:{A}#b:{FORK + 200}"
    assert key in rows
    r = rows[key].rec
    assert (r.kind, r.in_sym, r.in_qty) == ("deposit", "PLS", D(4))
    assert "ohne zugehörige Bewegung" in r.review and H(3) in r.review
    n = len(rows)
    assert sync(client, sid)["status"] == "synced" and len(all_rows(client, sid)) == n


def test_pulsechain_pre_fork_history_returned_by_the_explorer_is_never_booked(client, pulse):
    """Der Explorer führt die kopierte Ethereum-Historie mit: Einträge vor dem ersten PulseChain-Block bzw. -Zeitpunkt
    werden nicht gebucht (auch Token-Transfers ohne Blocknummer, über den Zeitstempel)."""
    old_ts = 1_683_000_000  # 02.05.2023, vor dem Start von PulseChain
    pulse.chains[369]["txlist"].append({**tx(FORK - 900, 50, X, A, 3 * WEI), "timeStamp": str(old_ts)})
    tok = "0x" + "cd" * 20
    pulse.chains[369]["tokentx"] = [{**tx(FORK - 900, 51, X, A, 1), "timeStamp": str(old_ts), "contractAddress": tok,
                                     "tokenSymbol": "OLD", "tokenName": "Old", "tokenDecimal": "0"}]
    sid = create_wallet(client, "pulsechain", A, name="Ledger PLS")
    assert sync(client, sid)["status"] == "synced"
    rows = all_rows(client, sid)
    assert pk(50, "n:in") not in rows and not [k for k in rows if "OLD" in k or H(51) in k]
    assert {pk(2, "n:in"), pk(3, "n:out")} <= set(rows)


def test_pulsechain_falls_back_to_the_etherscan_compatible_lists_when_v2_is_gone(client, pulse):
    pulse.v2_mode = "gone"
    sid = create_wallet(client, "pulsechain", A, name="Ledger PLS")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    assert {pk(2, "n:in"), pk(3, "n:out")} <= set(all_rows(client, sid))
    assert "Etherscan-kompatiblen" in run_text(client, sid)
    assert pulse.windows  # der alte Weg mit Blockfenstern ist gelaufen


def test_pulsechain_explorer_outage_is_an_error_not_a_silent_gap(client, pulse):
    pulse.v2_mode = "down"
    sid = create_wallet(client, "pulsechain", A, name="Ledger PLS")
    res = sync(client, sid)
    assert "gestört" in res.get("error", "") and not all_rows(client, sid)


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


RPC_HOSTS = ("peaq.api.onfinality.io", "quicknode1.peaq.xyz", "peaq-rpc.publicnode.com")


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


def test_peaq_evm_address_not_found_by_subscan_uses_the_derived_substrate_account(client, subscan):
    """Die Suche über die 0x-Adresse findet nichts, das nach Frontier berechnete Konto (blake2b-256("evm:" ‖ H160))
    kennt Subscan aber: abgerufen wird dieses Konto statt „unbekannt“ (Zugänge nur über die Substrate-Seite)."""
    from app.datasources.chains.codec import evm_account_id

    derived = ss58_encode(evm_account_id(A), 1221)
    subscan.nets["peaq"]["search"] = {derived: ss58_encode(b"\x55" * 32, 42)}
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "peaq", A, name="peaq EVM")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    assert all_rows(client, sid)[qk("100-2", "tr:4")].rec.in_qty == D("12.5")
    keys = [json.loads(c.content)["key"] for c in subscan.calls if c.url.path.endswith("v2/scan/search:free")]
    assert keys[:2] == [A, derived]


def test_evm_account_id_matches_the_frontier_hashed_mapping():
    """Belegt on-chain (peaq, 10/2026): System.Account dieses Schlüssels == eth_getBalance der 0x-Adresse."""
    import hashlib

    from app.datasources.chains.codec import evm_account_id

    h160 = bytes.fromhex("11" * 20)
    assert evm_account_id("0x" + h160.hex()) == hashlib.blake2b(b"evm:" + h160, digest_size=32).digest()
    assert evm_account_id("0x" + h160.hex().upper()) == evm_account_id(A)


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


def test_peaq_evm_address_unknown_to_subscan_but_funded_on_chain_gives_balance_and_a_gap(client, subscan):
    """Subscan kennt die Adresse nicht, die Kette zeigt Bestand (nie gesendet): der Bestand wird als Ist-Bestand
    übernommen, die fehlende Historie ist eine ausgewiesene Lücke – kein Fehler, keine erfundene Buchung."""
    subscan.rpc_balance, subscan.rpc_nonce = 3 * WEI, 0
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "peaq", A, name="peaq EVM")
    res = sync(client, sid)
    assert not res.get("error"), res
    assert not all_rows(client, sid)  # nichts gebucht
    assert balances(client, sid).get("PEAQ") in ("3", "3.0")
    text = run_text(client, sid)
    assert "Historie" in text and "nicht abrufbar" in text and "Subscan direkt" in text
    assert source(client, sid)["status"] != "synced" or "Lücke" in text or "nicht abrufbar" in text


def test_peaq_evm_without_any_key_still_reads_the_balance_from_the_chain(client, subscan):
    """Kein PubFi-Schlüssel hinterlegt: „Bestand abfragen“ liefert den Kettenbestand, das Problem bleibt sichtbar."""
    subscan.rpc_balance = 13 * WEI + WEI // 2
    sid = create_wallet(client, "peaq", A, name="peaq EVM")  # kein Schlüssel
    ok, msg = datasource_service(ctx(client)).check(sid)
    assert not ok and "Bestand laut öffentlichem RPC: 13.5 PEAQ" in msg and "Schlüssel" in msg
    assert balances(client, sid) == {"PEAQ": "13.5"}
    assert not [c for c in subscan.calls if c.url.host == "api.pubfi.ai"]


def test_explorer_links_per_account_for_the_diagnosis(client, subscan):
    from app.diagnosis import live as L

    create_wallet(client, "peaq", A, name="peaq EVM")
    links = L.explorer_links(ctx(client))
    assert list(links.values()) == [f"https://peaq.subscan.io/account/{A}"]


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


def test_peaq_history_window_limit_keeps_the_chain_balance_and_reports_the_gap(client, subscan, monkeypatch):
    """Beobachtet (10/2026): die kostenlose PubFi-Route lehnt Verlaufsanfragen mit „history_window_exceeded“ ab. Für
    eine 0x-Adresse bleibt der Bestand laut öffentlichem EVM-RPC als Ist-Bestand, die Historie ist eine ausgewiesene
    Lücke – kein Fehler."""
    from app.datasources.chains import polkadot as PD

    real = subscan.handler
    routes = ("v2/scan/transfers", "v2/scan/extrinsics", "v2/scan/account/reward_slash")

    def handler(req: httpx.Request) -> httpx.Response:  # auch das jüngste Fenster wird abgelehnt
        if req.url.host == "api.pubfi.ai" and any(r in req.url.path for r in routes):
            return httpx.Response(403, json={"error": {"code": "history_window_exceeded",
                                                       "message": "history_window_exceeded"}})
        return real(req)

    monkeypatch.setattr(PD, "MIN_WINDOW", 50)
    monkeypatch.setattr(PD, "_WINDOW_CAP", {})
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(handler))
    subscan.nets["peaq"]["search"] = {A: ss58_encode(b"\x55" * 32, 42)}
    subscan.rpc_balance, subscan.rpc_nonce = 3 * WEI, 0
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "peaq", A, name="peaq EVM")
    res = sync(client, sid)
    assert not res.get("error"), res
    assert not all_rows(client, sid)
    assert balances(client, sid).get("PEAQ") in ("3", "3.0")
    text = run_text(client, sid)
    assert "Historie" in text and "nicht abrufbar" in text
