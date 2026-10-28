"""Kettenbestand-Abgleich: Archiv-Leser (EVM, Substrate) und die Suche nach Bestandssprüngen ohne Buchung."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.datasources import chainhistory as H
from app.diagnosis import chainscan as CS

D = Decimal
BASE = 1_700_000_000  # Block 1
SECS = 12


class FakeReader(H.Reader):
    """Kette mit Blockzeit 12 s; Bestand = Summe der Ereignisse bis zum Block."""

    def __init__(self, events: dict[int, Decimal], tip: int = 1_000_000, key: str = "fake", pruned_before: int = 0):
        super().__init__(())
        self.key, self.label, self.asset, self.decimals = key, "Testkette", "TST", 18
        self.events, self._tip, self.pruned_before = dict(events), tip, pruned_before

    def tip(self) -> int:
        self.calls += 1
        return self._tip

    def _fetch_time(self, block: int) -> datetime:
        self.calls += 1
        if block < self.pruned_before:
            raise H.NoArchive("Testkette: Stand verworfen")
        return datetime.fromtimestamp(BASE + (block - 1) * SECS, UTC)

    def _fetch_balance(self, block: int) -> Decimal:
        self.calls += 1
        if block < self.pruned_before:
            raise H.NoArchive("Testkette: Stand verworfen")
        return sum((q for b, q in self.events.items() if b <= block), D(0))

    def explorer_block(self, block: int) -> str:
        return f"https://example.invalid/block/{block}"


def tx(block: int, qty: str, tx_id: str, sign: int = 1):
    ts = datetime.fromtimestamp(BASE + (block - 1) * SECS, UTC)
    q = D(qty)
    return SimpleNamespace(
        tx_id=tx_id, ts=ts, from_account="W" if sign < 0 else "", from_asset="TST" if sign < 0 else "",
        from_qty=q if sign < 0 else None, to_account="W" if sign > 0 else "", to_asset="TST" if sign > 0 else "",
        to_qty=q if sign > 0 else None, fee_asset="", fee_qty=None)


def run(reader, txs=(), **kw):
    return CS.scan([reader], CS.LedgerSeries(list(txs), "W", "TST"), **kw)


# ---- Suche ------------------------------------------------------------------------------------------------

def test_consistent_chain_has_no_steps():
    r = FakeReader({10: D(5)})
    res = run(r, [tx(10, "5", "a")])
    assert res.ok and res.complete and not res.steps and res.r_end == 0 and res.chain_end == 5


def test_finds_exact_blocks_and_amounts_of_unbooked_steps():
    r = FakeReader({300_001: D("1052.5"), 700_007: D("10718.83")})
    res = run(r)
    assert res.ok and res.complete and res.r_end == D("11771.33")
    assert [(s.blocks["fake"], s.delta, s.kind) for s in res.steps] == [
        (300_001, D("1052.5"), "chain"), (700_007, D("10718.83"), "chain")]
    assert res.steps[0].at == datetime.fromtimestamp(BASE + 300_000 * SECS, UTC)
    assert res.steps[0].links["fake"].endswith("/300001") and res.residual == 0
    assert res.calls < 120  # Halbierung statt Block für Block (2 Abfragen je Messpunkt)


def test_booked_step_disappears_and_only_the_missing_one_remains():
    r = FakeReader({300_001: D("1052.5"), 700_007: D("10718.83")})
    res = run(r, [tx(300_001, "1052.5", "booked")])
    assert [s.delta for s in res.steps] == [D("10718.83")]
    assert res.r_end == D("10718.83")


def test_booking_without_chain_movement_is_reported_as_ledger_step():
    r = FakeReader({})
    res = run(r, [tx(500_000, "3", "phantom")])
    assert len(res.steps) == 1 and res.steps[0].kind == "ledger" and res.steps[0].delta == D("-3")
    assert res.steps[0].ledger_tx == ["phantom"]


def test_dust_below_min_step_is_not_followed():
    r = FakeReader({100_000: D("0.0001"), 200_000: D("0.0002"), 600_000: D("50")})
    res = run(r)
    assert [s.delta for s in res.steps] == [D("50")]
    assert res.residual == D("0.0003") and res.complete


def test_budget_stops_cleanly_and_cache_makes_the_rerun_cheaper():
    ev = {200_000 + i * 40_000: D(i + 1) for i in range(8)}
    first = FakeReader(ev)
    part = run(first, max_calls=40)
    assert part.ok and not part.complete and part.pending >= 1
    saved = CS._dump([first])
    second = FakeReader(ev)
    CS._load([second], json.loads(json.dumps(saved)))
    res = run(second, max_calls=400)
    assert res.complete and len(res.steps) == 8
    cold = FakeReader(ev)
    run(cold, max_calls=400)
    assert second.calls < cold.calls


def test_max_steps_marks_search_incomplete():
    ev = {100_000 + i * 50_000: D(i + 1) for i in range(10)}
    res = run(FakeReader(ev), max_steps=3, max_calls=800)
    assert len(res.steps) == 3 and not res.complete and res.pending >= 1


def test_node_without_archive_reports_reason_and_never_guesses():
    res = run(FakeReader({10: D(1)}, pruned_before=900_000))
    assert not res.ok and "verworfen" in res.error and not res.steps


def test_two_nets_are_compared_as_sum():
    a = FakeReader({400_000: D(10)}, key="a")
    b = FakeReader({400_000: D(-10), 800_000: D(7)}, key="b")  # Migration a→b, später Zugang auf b
    res = CS.scan([a, b], CS.LedgerSeries([], "W", "TST"))
    assert res.ok and [(s.delta, set(s.blocks)) for s in res.steps] == [(D(7), {"b"})]


def test_result_roundtrips_through_json():
    r = FakeReader({300_001: D("2")})
    res = run(r)
    again = CS.Result.from_json(json.loads(json.dumps(res.to_json())))
    assert again.steps[0].delta == D(2) and again.steps[0].blocks == res.steps[0].blocks
    assert again.residual == res.residual and again.complete == res.complete


# ---- Leser (HTTP-Attrappe) --------------------------------------------------------------------------------

def rpc_transport(handler):
    def respond(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        out = handler(str(req.url), body["method"], body["params"])
        if isinstance(out, dict) and "error" in out:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": out["error"]})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": out})
    return httpx.MockTransport(respond)


ADDR = "0x1e27c175d496f315c4955fa1f31d4fd5777ed856"


def evm(handler, **kw):
    return H.EvmReader(ADDR, ("peaq_rpc", "peaq_rpc_3"), key="peaq", label="peaq", asset="PEAQ",
                       transport=rpc_transport(handler), sleep=lambda s: None, **kw)


def test_evm_reader_reads_balance_time_and_tip():
    def handler(url, method, params):
        if method == "eth_blockNumber":
            return hex(5_000_000)
        if method == "eth_getBalance":
            assert params == [ADDR, hex(1234)]
            return hex(10_718_830_000_000_000_000_000)
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(1_700_000_123)}
    with evm(handler) as r:
        assert r.tip() == 5_000_000
        assert r.balance(1234) == D("10718.83")
        assert r.time(1234) == datetime.fromtimestamp(1_700_000_123, UTC)
        assert r.balance(1234) == D("10718.83") and r.calls == 3  # zweiter Zugriff aus dem Zwischenspeicher


def test_evm_reader_falls_back_to_next_node_when_state_is_discarded():
    seen = []

    def handler(url, method, params):
        seen.append(url)
        if "onfinality" in url:
            return {"error": {"code": -32603, "message": "Fetch account balances failed: UnknownBlock(\"State already "
                                                         "discarded for 0xda6c\")"}}
        return hex(7)
    with evm(handler) as r:
        assert r.balance(10) == D(7) * D(10) ** -18
    assert any("publicnode" in u for u in seen) and any("onfinality" in u for u in seen)


def test_evm_reader_without_any_archive_raises_noarchive():
    def handler(url, method, params):
        return {"error": {"code": -32000, "message": "missing trie node abc (path ) state is not available"}}
    with evm(handler) as r, pytest.raises(H.NoArchive):
        r.balance(10)


def test_substrate_reader_uses_block_hash_system_account_and_timestamp():
    pub = bytes(range(32))
    free, reserved = 12_3456789012, 1_0000000000
    acct = bytes(16) + free.to_bytes(16, "little") + reserved.to_bytes(16, "little") + bytes(32)
    ms = 1_650_000_123_456

    def handler(url, method, params):
        if method == "chain_getHeader":
            return {"number": hex(32_196_153)}
        if method == "chain_getBlockHash":
            return "0x" + format(params[0], "064x")
        if method == "state_getStorage":
            key, at = params
            assert at == "0x" + format(20_000_000, "064x")
            if key == H._TIMESTAMP_NOW:
                return "0x" + ms.to_bytes(8, "little").hex()
            assert key.startswith("0x" + H._SYSTEM_ACCOUNT) and key.endswith(pub.hex())
            return "0x" + acct.hex()
    r = H.SubstrateReader(pub, ("polkadot_rpc",), key="polkadot-rc", label="Relay Chain", asset="DOT", decimals=10,
                          transport=rpc_transport(handler), sleep=lambda s: None)
    with r:
        assert r.tip() == 32_196_153
        assert r.balance(20_000_000) == D("13.3456789012")
        assert r.time(20_000_000) == datetime.fromtimestamp(ms // 1000, UTC)  # auf Sekunden


def test_substrate_reader_missing_account_is_zero_and_pruned_state_is_reported():
    def handler(url, method, params):
        if method == "chain_getBlockHash":
            return "0x" + "11" * 32
        if params[1] == "0x" + "11" * 32 and params[0].endswith("22" * 32):
            return None
        return {"error": {"code": 4003, "message": "Client error: UnknownBlock: State already discarded"}}
    r = H.SubstrateReader(bytes([0x22]) * 32, ("polkadot_rpc",), key="k", label="Relay", asset="DOT",
                          transport=rpc_transport(handler), sleep=lambda s: None)
    with r:
        assert r.balance(5) == 0
        with pytest.raises(H.NoArchive):
            r.time(5)


def test_readers_for_supported_providers_only():
    assert [r.key for r in H.readers_for("peaq", ADDR)] == ["peaq"]
    assert H.readers_for("peaq", "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY") == []  # SS58: kein Weg
    assert H.readers_for("ethereum", ADDR) == [] and H.readers_for("bitcoin", "bc1qxyz") == []
    dot = H.readers_for("polkadot", "15oF4uVJwmo4TdGW7VfQxNLavjCXviqxT9S1MgbjMNHr6Sp5")
    assert [r.key for r in dot] == ["polkadot-rc", "polkadot-ah"]
    assert H.readers_for("polkadot", "kein-ss58") == []


@pytest.mark.parametrize("head", [5, 12, 16])  # AccountInfo älterer und neuer Runtimes (Kopf unterschiedlich lang)
def test_substrate_balance_is_read_from_the_end_for_every_account_layout(head):
    free, reserved = 7_0000000000, 3_0000000000
    acct = bytes(head) + free.to_bytes(16, "little") + reserved.to_bytes(16, "little") + bytes(32)

    def handler(url, method, params):
        return "0x" + "ab" * 32 if method == "chain_getBlockHash" else "0x" + acct.hex()
    r = H.SubstrateReader(bytes(range(32)), ("polkadot_rpc",), key="k", label="Relay", asset="DOT",
                          transport=rpc_transport(handler), sleep=lambda s: None)
    with r:
        assert r.balance(10) == D(10)
