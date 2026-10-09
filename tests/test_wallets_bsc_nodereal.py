"""BNB Chain über NodeReal BSCTrace (``nr_getAssetTransfers``) – strenger Fake nach der NodeReal-Referenz, ohne Netz.

Abgedeckt: Schlüssel als Pfadsegment (nie in Query/Antwort), Blockfenster ≤ 100.000 (hier verkleinert), Seiten über
``pageKey``, getrennte Abfragen für ``fromAddress``/``toAddress`` (Eigenüberweisung nur einmal), Gebühr eigener
Token-Transaktionen über ``eth_getTransactionReceipt`` (kein „external“-Eintrag), fremd ausgelöste interne Eingänge
ohne Gebühr, inkrementeller Folgelauf ohne Dubletten, Routescan nicht mehr angeboten.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources.chains import evm as E
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import MASTER, all_rows, balances, create_wallet, ctx, make_client, set_provider_key, source

D = Decimal
KEY = "nodereal0123456789abcdef"
A = "0x1111111111111111111111111111111111111111"
X = "0x3333333333333333333333333333333333333333"
USDT = "0x55d398326f99059ff775485246999027b3197955"
WEI = 10**18


def H(n: int) -> str:
    return "0x" + f"{n:064x}"


def xfer(cat: str, block: int, n: int, frm: str, to: str, value: int, **kw) -> dict:
    return {"category": cat, "blockNum": hex(block), "from": frm, "to": to, "value": hex(value),
            "asset": kw.pop("asset", "BNB"), "hash": H(n), "blockTimeStamp": 1_700_000_000 + block, **kw}


class FakeNodeReal:
    def __init__(self) -> None:
        self.tip = 300
        self.transfers = [
            xfer("external", 100, 1, X, A, 1 * WEI, gasPrice=1_000_000_000, gasUsed=21000, receiptsStatus=1),
            xfer("external", 120, 2, A, X, WEI // 2, gasPrice=3_000_000_000, gasUsed=21000, receiptsStatus=1),
            xfer("20", 150, 3, A, X, 10 * WEI, asset="USDT", contractAddress=USDT, decimal="18"),  # eigener Token-Tx
            xfer("20", 160, 4, X, A, 5 * WEI, asset="USDT", contractAddress=USDT, decimal="18"),
            xfer("internal", 210, 5, X, A, WEI // 10),  # fremd ausgelöst
            xfer("external", 250, 6, A, A, 0, gasPrice=1_000_000_000, gasUsed=30000, receiptsStatus=1),  # an sich
        ]
        gp = hex(2_000_000_000)
        self.txs = {H(3): {"from": A, "to": USDT, "value": "0x0", "gasPrice": gp, "input": "0xa9059cbb"},
                    H(4): {"from": X, "to": USDT, "value": "0x0", "gasPrice": "0x1", "input": "0xa9059cbb"},
                    H(5): {"from": X, "to": "0x" + "44" * 20, "value": "0x0", "gasPrice": "0x1", "input": "0x"}}
        self.receipts = {H(3): {"status": "0x1", "gasUsed": hex(50000), "effectiveGasPrice": hex(2_000_000_000)}}
        self.calls: list[dict] = []
        self.windows: list[tuple[int, int]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.method == "POST" and req.url.host == "bsc-mainnet.nodereal.io"
        assert req.url.path == f"/v1/{KEY}" and not req.url.params, "Schlüssel nur als Pfadsegment"
        body = json.loads(req.content)
        self.calls.append(body)
        m, p = body["method"], body["params"]

        def ok(result):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        if m == "eth_blockNumber":
            return ok(hex(self.tip))
        if m == "eth_getBalance":
            return ok(hex(WEI // 2))
        if m == "eth_getTransactionByHash":
            return ok(self.txs.get(p[0]))
        if m == "eth_getTransactionReceipt":
            return ok(self.receipts.get(p[0]))
        assert m == "nr_getAssetTransfers"
        q = p[0]
        a, b = int(q["fromBlock"], 16), int(q["toBlock"], 16)
        assert 0 <= b - a < 100_000, "Blockbereich je Abfrage ≤ 100.000"
        assert ("fromAddress" in q) != ("toAddress" in q)
        self.windows.append((a, b))
        rows = [t for t in self.transfers if a <= int(t["blockNum"], 16) <= b and t["category"] in q["category"]
                and (t["from"] == q.get("fromAddress") or t["to"] == q.get("toAddress"))]
        size = int(q["maxCount"], 16)
        assert size <= 0x3E8
        start = int(q.get("pageKey") or 0)
        page = rows[start:start + size]
        nxt = str(start + size) if start + size < len(rows) else None
        return ok({"transfers": page, **({"pageKey": nxt} if nxt else {})})


@pytest.fixture
def nr(monkeypatch):
    fake = FakeNodeReal()
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    monkeypatch.setattr(E.BscConnector, "nr_window", 100)
    monkeypatch.setattr(E, "NR_PAGE", 1)
    monkeypatch.setattr(E.BscConnector, "confirmations", 0)
    return fake


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def k(n: int, sub: str) -> str:
    return f"bsc:{H(n)}:{A}#{sub}"


def test_bsc_over_nodereal_maps_history_with_fees(client, nr):
    set_provider_key(client, "nodereal", KEY)
    sid = create_wallet(client, "bsc", A, name="MetaMask BNB")  # Standard: NodeReal
    res = datasource_service(ctx(client)).sync(sid, "manual")
    assert res.get("status") == "synced", res
    rows = {key: rc.rec for key, rc in all_rows(client, sid).items()}
    assert rows[k(1, "n:in")].in_qty == D(1)
    out = rows[k(2, "n:out")]
    assert out.out_qty == D("0.5") and out.fee_qty == D("0.000063") and out.fee_sym == "BNB"
    tok = next(r for key, r in rows.items() if key.startswith(f"bsc:{H(3)}:") and r.out_sym)
    assert tok.out_sym == f"USDT@BSC:{USDT}" and tok.out_qty == D(10) and tok.fee_qty == D("0.0001")
    assert any(key.startswith(f"bsc:{H(4)}:") and r.in_qty == D(5) for key, r in rows.items())
    internal = [r for key, r in rows.items() if key.startswith(f"bsc:{H(5)}:")]
    assert internal and all(not r.fee_qty for r in internal) and internal[0].in_qty == D("0.1")
    assert len([key for key in rows if key.startswith(f"bsc:{H(6)}:")]) == 1  # Eigenüberweisung einmal (nur Gebühr)
    assert balances(client, sid)["BNB"] == "0.5"
    # Fenster: 0–99, 100–199, 200–299, 300 je zwei Richtungen, Seiten zu 1 über pageKey
    assert set(nr.windows) >= {(0, 99), (100, 199), (200, 299), (300, 300)}
    receipts = [c for c in nr.calls if c["method"] == "eth_getTransactionReceipt"]
    assert [c["params"][0] for c in receipts] == [H(3)]  # nur eigene Tx ohne external-Eintrag
    cur = json.loads(source(client, sid)["cursor_json"])
    assert cur["block"] == 301
    # Folgelauf: nur neue Blöcke, keine Dubletten
    n = len(rows)
    nr.calls.clear()
    nr.tip = 320
    nr.transfers.append(xfer("external", 310, 7, X, A, 2 * WEI, gasPrice=1, gasUsed=1, receiptsStatus=1))
    assert datasource_service(ctx(client)).sync(sid, "manual").get("status") == "synced"
    assert len(all_rows(client, sid)) == n + 1
    assert all(int(c["params"][0]["fromBlock"], 16) >= 301 for c in nr.calls if c["method"] == "nr_getAssetTransfers")


def test_bsc_nodereal_key_required_and_never_leaked(client, nr):
    sid = create_wallet(client, "bsc", A, name="MetaMask BNB")
    res = datasource_service(ctx(client)).sync(sid, "manual")
    assert "Schlüssel" in res["error"] and not nr.calls
    page = client.get("/settings/datasources/new?kind=wallet&provider=bsc").text
    assert "NodeReal" in page and "Routescan" not in page.split("chain_provider")[1][:2000]
