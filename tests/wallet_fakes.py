"""Testhilfen für Wallet-Anbindungen: nachgebildete Anbieter-APIs (ohne Netz) und App-Helfer.

Die Nachbildungen filtern und blättern wie die Originale (Etherscan: Blockbereich, Seite × Einträge, sortiert;
Esplora: 25 je Seite ab ``last_seen_txid``; Solana: Signaturen absteigend mit ``before``/``until``; Kaspa:
Blockzeit-Cursor), damit Paginierung, Abbruch und Fortsetzung realistisch geprüft werden. Alle Daten sind
anonymisiert bzw. synthetisch.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.config import Config
from app.csvimport.service import csv_service
from app.main import build_app

DATA = Path(__file__).resolve().parent / "data" / "wallets"
MASTER = base64.b64encode(bytes(range(32))).decode()
ETHERSCAN_KEY = "ES_test_key_0123456789ABCDEFGHIJ"
NO_TX = {"status": "0", "message": "No transactions found", "result": []}


def load(name: str) -> dict[str, Any]:
    return json.loads((DATA / name).read_text())


class Recorder:
    """Gemeinsame Basis: Anfragen aufzeichnen, einmalige Störungen einspeisen."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.inject: list[tuple[Callable[[httpx.Request], bool], httpx.Response]] = []

    def injected(self, req: httpx.Request) -> httpx.Response | None:
        for i, (pred, resp) in enumerate(self.inject):
            if pred(req):
                del self.inject[i]
                return resp
        return None

    def fail_next(self, resp: httpx.Response, pred: Callable[[httpx.Request], bool] = lambda r: True) -> None:
        self.inject.append((pred, resp))


class FakeEvm(Recorder):
    """Etherscan API V2 und Routescan (Etherscan-kompatibel) für mehrere Chains."""

    def __init__(self, chains: dict[int, dict[str, Any]], *, free_chains: tuple[int, ...] = (1,),
                 key: str = ETHERSCAN_KEY, paid: bool = False) -> None:
        super().__init__()
        self.chains = chains
        self.free_chains = free_chains
        self.key = key
        self.paid = paid

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.method == "GET", "nur lesende Aufrufe"
        p = req.url.params
        if req.url.host == "api.etherscan.io":
            assert req.url.path == "/v2/api"
            chain = int(p["chainid"])
            if p.get("apikey") != self.key:
                return httpx.Response(200, json={"status": "0", "message": "NOTOK", "result": "Invalid API Key"})
            if chain not in self.free_chains and not self.paid:
                return httpx.Response(200, json={"status": "0", "message": "NOTOK", "result":
                                                 "Free API access is not supported for this chain. Please upgrade "
                                                 "your api plan for full chain coverage. https://etherscan.io/apis"})
        elif req.url.host == "api.routescan.io":
            m = re.match(r"^/v2/network/mainnet/evm/(\d+)/etherscan/api$", req.url.path)
            assert m, req.url.path
            assert "chainid" not in p
            chain = int(m.group(1))
        else:  # pragma: no cover - darf nie passieren
            raise AssertionError(f"unerwarteter Host {req.url.host}")
        inj = self.injected(req)
        if inj is not None:
            return inj
        d = self.chains[chain]
        mod, act = p["module"], p["action"]
        if mod == "proxy" and act == "eth_blockNumber":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 83, "result": hex(d["tip"])})
        if act == "balance":
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": d.get("balance", "0")})
        if act == "tokenbalance":
            res = d.get("tokenbalance", {}).get(p["contractaddress"].lower(), "0")
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": res})
        if act in ("tokennfttx", "token1155tx"):
            rows = d.get(act, [])
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": rows[:1]} if rows else NO_TX)
        if act in ("txlist", "txlistinternal", "tokentx"):
            addr = p["address"].lower()
            start, end = int(p["startblock"]), int(p["endblock"])
            page, off = int(p["page"]), int(p["offset"])
            if off > 1000 or page * off > 10000:
                return httpx.Response(200, json={"status": "0", "message": "NOTOK",
                                                 "result": "Result window is too large"})
            rows = [r for r in d.get(act, []) if start <= int(r["blockNumber"]) <= end
                    and addr in (r["from"].lower(), r["to"].lower())]
            rows.sort(key=lambda r: int(r["blockNumber"]), reverse=p.get("sort") == "desc")
            chunk = rows[(page - 1) * off: page * off]
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": chunk} if chunk else NO_TX)
        return httpx.Response(200, json={"status": "0", "message": "NOTOK", "result": "Error! Invalid action"})

    def requests(self, action: str) -> list[httpx.Request]:
        return [c for c in self.calls if c.url.params.get("action") == action]


def make_client(config: Config) -> TestClient:
    return TestClient(build_app(config, start_scheduler=False))


def post(c: TestClient, url: str, **data: Any) -> httpx.Response:
    return c.post(url, data={"csrf_token": c.token, **data}, follow_redirects=False)  # type: ignore[attr-defined]


def ctx(c: TestClient) -> Any:
    return c.app.state.ctx  # type: ignore[attr-defined]


def create_wallet(c: TestClient, provider: str, address: str, *, name: str | None = None, group: str = "Ledger",
                  account: str | None = None, **form: Any) -> int:
    data = {"kind": "wallet", "provider": provider, "name": name or f"{group} {provider}", "address": address,
            "account": account or name or f"{group} {provider}", "wallet_group": group, "sync_interval_min": "0",
            **form}
    r = post(c, "/settings/datasources", **data)
    assert r.status_code == 303, r.text[:1500]
    return int(re.search(r"/settings/datasources/(\d+)", r.headers["location"]).group(1))


def set_provider_key(c: TestClient, provider: str, key: str) -> None:
    r = post(c, f"/settings/datasources/provider-keys/{provider}", api_key=key)
    assert r.status_code == 303 and "error=" not in r.headers["location"], r.headers["location"]


def source(c: TestClient, sid: int) -> dict[str, Any]:
    r = ctx(c).db.q1("SELECT * FROM data_source WHERE id=?", (sid,))
    return dict(r) if r else {}


def rows_by_ext(c: TestClient, bid: int) -> dict[str, Any]:
    return {rc.rec.ext_id: rc for rc in csv_service(ctx(c)).rows(bid)}


def all_rows(c: TestClient, sid: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for b in ctx(c).db.q("SELECT id FROM csv_batch WHERE datasource_id=? ORDER BY id", (sid,)):
        out.update(rows_by_ext(c, int(b["id"])))
    return out


def balances(c: TestClient, sid: int) -> dict[str, str]:
    return {r["asset_key"]: r["qty"] for r in ctx(c).db.q("SELECT asset_key, qty FROM ds_balance WHERE source_id=?",
                                                          (sid,))}


class FakeEsplora(Recorder):
    """Esplora-API (mempool.space/Blockstream): Statistik je Adresse aus den Transaktionen berechnet, Historie
    neueste zuerst in Seiten zu 25 ab ``last_seen_txid``."""

    def __init__(self, txs: list[dict[str, Any]], tip: int, mempool: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.txs = txs
        self.tip = tip
        self.mempool = mempool or []

    @staticmethod
    def _involves(t: dict[str, Any], a: str) -> bool:
        return any((v.get("prevout") or {}).get("scriptpubkey_address") == a for v in t["vin"]) or \
            any(o.get("scriptpubkey_address") == a for o in t["vout"])

    def _stats(self, txs: list[dict[str, Any]], a: str) -> dict[str, int]:
        funded = sum(o["value"] for t in txs for o in t["vout"] if o.get("scriptpubkey_address") == a)
        spent = sum(v["prevout"]["value"] for t in txs for v in t["vin"]
                    if (v.get("prevout") or {}).get("scriptpubkey_address") == a)
        return {"tx_count": sum(1 for t in txs if self._involves(t, a)), "funded_txo_sum": funded,
                "spent_txo_sum": spent, "funded_txo_count": 0, "spent_txo_count": 0}

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.method == "GET" and req.url.host in ("mempool.space", "blockstream.info")
        assert not req.url.params, "Esplora braucht keine Parameter (kein Schlüssel)"
        inj = self.injected(req)
        if inj is not None:
            return inj
        path = req.url.path.removeprefix("/api")
        if path == "/blocks/tip/height":
            return httpx.Response(200, text=str(self.tip))
        m = re.match(r"^/address/([a-zA-Z0-9]+)$", path)
        if m:
            a = m.group(1)
            return httpx.Response(200, json={"address": a, "chain_stats": self._stats(self.txs, a),
                                             "mempool_stats": self._stats(self.mempool, a)})
        m = re.match(r"^/address/([a-zA-Z0-9]+)/txs/chain(?:/([0-9a-f]{64}))?$", path)
        if m:
            a, last = m.group(1), m.group(2)
            mine = sorted((t for t in self.txs if self._involves(t, a)),
                          key=lambda t: (-t["status"]["block_height"], t["txid"]))
            if last:
                idx = next(i for i, t in enumerate(mine) if t["txid"] == last)
                mine = mine[idx + 1:]
            return httpx.Response(200, json=mine[:25])
        return httpx.Response(404, text="not found")


class FakeSolana(Recorder):
    """Solana JSON-RPC (öffentlicher RPC bzw. Helius): Signaturen je Adresse absteigend nach Slot mit
    ``before``/``until``/``limit``, Transaktionen jsonParsed, Token-Konten je Programm."""

    def __init__(self, txs: list[dict[str, Any]], token_accounts: list[dict[str, Any]], balance: int) -> None:
        super().__init__()
        self.txs = {t["transaction"]["signatures"][0]: t for t in txs}
        self.token_accounts = token_accounts
        self.balance = balance
        self.missing: set[str] = set()

    @staticmethod
    def keys(t: dict[str, Any]) -> list[str]:
        return [k["pubkey"] for k in t["transaction"]["message"]["accountKeys"]]

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.method == "POST" and req.url.host in ("api.mainnet-beta.solana.com", "mainnet.helius-rpc.com")
        inj = self.injected(req)
        if inj is not None:
            return inj
        body = json.loads(req.content)
        m, p = body["method"], body["params"]

        def ok(result: Any) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        if m == "getSlot":
            return ok(300_000_000)
        if m == "getBalance":
            return ok({"context": {"slot": 1}, "value": self.balance})
        if m == "getTokenAccountsByOwner":
            prog = p[1]["programId"]
            return ok({"context": {"slot": 1}, "value": [
                {"pubkey": t["pubkey"], "account": {"lamports": t["lamports"], "owner": prog, "data": {
                    "program": "spl-token", "parsed": {"type": "account", "info": {
                        "mint": t["mint"], "owner": p[0], "tokenAmount": {
                            "amount": t["amount"], "decimals": t["decimals"], "uiAmountString": "x"}}}}}}
                for t in self.token_accounts
                if t.get("program", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA") == prog]})
        if m == "getSignaturesForAddress":
            addr, opts = p[0], p[1]
            rows = sorted(((t["slot"], s) for s, t in self.txs.items() if addr in self.keys(t)), reverse=True)
            sigs = [s for _, s in rows]
            if opts.get("before"):
                sigs = sigs[sigs.index(opts["before"]) + 1:]
            if opts.get("until"):
                sigs = sigs[:sigs.index(opts["until"])] if opts["until"] in sigs else sigs
            sigs = sigs[:opts.get("limit", 1000)]
            return ok([{"signature": s, "slot": self.txs[s]["slot"], "err": self.txs[s]["meta"]["err"], "memo": None,
                        "blockTime": self.txs[s]["blockTime"], "confirmationStatus": "finalized"} for s in sigs])
        if m == "getTransaction":
            assert p[1]["encoding"] == "jsonParsed" and p[1]["commitment"] == "finalized"
            return ok(None if p[0] in self.missing else self.txs.get(p[0]))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "error": {"code": -32601, "message": "Method not found"}})


class FakeKaspa(Recorder):
    """api.kaspa.org (Blockzeit-Seiten, Grenzzeitpunkte vollständig) und api.kasplex.org (KRC-20, opScore-Cursor)."""

    def __init__(self, txs: list[dict[str, Any]], balances: dict[str, int], ops: list[dict[str, Any]],
                 tokenlist: list[dict[str, Any]], decimals: dict[str, int]) -> None:
        super().__init__()
        self.txs, self.bal, self.ops, self.tokenlist, self.decimals = txs, balances, ops, tokenlist, decimals
        self.krc_status = "synced"

    @staticmethod
    def _involves(t: dict[str, Any], a: str) -> bool:
        return any(i.get("previous_outpoint_address") == a for i in t["inputs"]) or \
            any(o.get("script_public_key_address") == a for o in t["outputs"])

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.method == "GET" and req.url.host in ("api.kaspa.org", "api.kasplex.org")
        inj = self.injected(req)
        if inj is not None:
            return inj
        p, path = req.url.params, req.url.path
        if req.url.host == "api.kaspa.org":
            m = re.match(r"^/addresses/(kaspa:[a-z0-9]+)/balance$", path)
            if m:
                return httpx.Response(200, json={"address": m.group(1), "balance": self.bal.get(m.group(1), 0)})
            m = re.match(r"^/addresses/(kaspa:[a-z0-9]+)/full-transactions-page$", path)
            assert m, path
            a, after, limit = m.group(1), int(p["after"]), int(p["limit"])
            assert p["resolve_previous_outpoints"] == "light" and p["acceptance"] == "accepted"
            rows = sorted((t for t in self.txs if self._involves(t, a) and t["block_time"] > after),
                          key=lambda t: (t["block_time"], t["transaction_id"]))
            page = rows[:limit]
            if len(page) == limit:  # Grenzzeitpunkt vollständig mitliefern (wie der Server)
                last = page[-1]["block_time"]
                page += [t for t in rows[limit:] if t["block_time"] == last]
            return httpx.Response(200, json=sorted(page, key=lambda t: -t["block_time"]))
        path = path.removeprefix("/v1")
        if self.krc_status == "unsynced403":  # API 3.x (go-krc20d): jede Abfrage 403 „unsynced“, auch /info
            return httpx.Response(403, json={"message": "unsynced", "result": None})
        if path == "/info":
            return httpx.Response(200, json={"message": self.krc_status, "result": {"daaScore": "1"}})
        m = re.match(r"^/krc20/address/(kaspa:[a-z0-9]+)/tokenlist$", path)
        if m:
            return httpx.Response(200, json={"message": "successful", "prev": None, "next": None,
                                             "result": self.tokenlist})
        m = re.match(r"^/krc20/token/([A-Za-z0-9]+)$", path)
        if m:
            return httpx.Response(200, json={"message": "successful", "result": [
                {"tick": m.group(1), "dec": str(self.decimals.get(m.group(1).upper(), 8))}]})
        if path == "/krc20/oplist":
            a = p["address"]
            mine = [o for o in self.ops if a in (o["from"], o["to"])]
            if p.get("prev"):
                page = sorted((o for o in mine if int(o["opScore"]) > int(p["prev"])),
                              key=lambda o: int(o["opScore"]))[:50]
                page.reverse()
            else:
                nxt = int(p.get("next") or 9199999999999999999)
                page = sorted((o for o in mine if int(o["opScore"]) < nxt), key=lambda o: -int(o["opScore"]))[:50]
            return httpx.Response(200, json={"message": "successful", "result": page,
                                             "prev": page[0]["opScore"] if page else None,
                                             "next": page[-1]["opScore"] if page else None})
        return httpx.Response(404, json={"message": "not found"})


class FakeBlockscout(Recorder):
    """Blockscout Polygon (Etherscan-kompatibel, ohne Key): ``block/eth_block_number``, ``getblocknobytime`` als
    Objekt, ``proxy`` unbekannt; ``status`` 2 für interne Transaktionen auf Wunsch."""

    def __init__(self, data: dict[str, Any], internal_partial: bool = False) -> None:
        super().__init__()
        self.d = data
        self.internal_partial = internal_partial

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.method == "GET" and req.url.host == "polygon.blockscout.com" and req.url.path == "/api"
        assert "apikey" not in req.url.params and "chainid" not in req.url.params
        inj = self.injected(req)
        if inj is not None:
            return inj
        p = req.url.params
        mod, act = p["module"], p["action"]
        if mod == "proxy":
            return httpx.Response(200, json={"message": "Unknown module", "result": None, "status": "0"})
        if mod == "block" and act == "eth_block_number":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": hex(self.d["tip"]), "id": 1})
        if act == "balance":
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": self.d.get("balance", "0")})
        if act == "tokenbalance":
            res = self.d.get("tokenbalance", {}).get(p["contractaddress"].lower(), "0")
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": res})
        if act in ("tokennfttx", "token1155tx"):
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": []})
        if act in ("txlist", "txlistinternal", "tokentx"):
            addr = p["address"].lower()
            start, end = int(p["startblock"]), int(p["endblock"])
            page, off = int(p["page"]), int(p["offset"])
            rows = sorted((r for r in self.d.get(act, []) if start <= int(r["blockNumber"]) <= end
                           and addr in (r["from"].lower(), r["to"].lower())), key=lambda r: int(r["blockNumber"]))
            chunk = rows[(page - 1) * off: page * off]
            if act == "txlistinternal" and self.internal_partial:
                return httpx.Response(200, json={"message": "Some internal transactions within this block range have "
                                                            "not yet been processed", "result": chunk, "status": "2"})
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": chunk} if chunk else NO_TX)
        return httpx.Response(200, json={"status": "0", "message": "NOTOK", "result": "Error! Invalid action"})


class FakeXrpl(Recorder):
    """rippled JSON-RPC (xrplcluster.com bzw. s2.ripple.com): ``account_tx`` aufsteigend mit ``marker``,
    ``account_info``/``account_lines``, ``server_info``."""

    def __init__(self, txs: list[dict[str, Any]], tip: int, accounts: dict[str, dict[str, Any]],
                 lines: dict[str, list[dict[str, Any]]] | None = None, first_ledger: int = 32570) -> None:
        super().__init__()
        self.txs, self.tip, self.accounts, self.lines = txs, tip, accounts, lines or {}
        self.first_ledger = first_ledger

    @staticmethod
    def involves(t: dict[str, Any], a: str) -> bool:
        return a in json.dumps(t)

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.method == "POST" and req.url.host in ("xrplcluster.com", "s2.ripple.com")
        inj = self.injected(req)
        if inj is not None:
            return inj
        body = json.loads(req.content)
        m, p = body["method"], body["params"][0]
        assert p.get("api_version") == 2

        def ok(result: dict[str, Any]) -> httpx.Response:
            return httpx.Response(200, json={"result": {**result, "status": "success"}})

        if m == "server_info":
            return ok({"info": {"complete_ledgers": f"{self.first_ledger}-{self.tip}", "validated_ledger": {
                "seq": self.tip, "reserve_base_xrp": 1, "reserve_inc_xrp": 0.2}}})
        a = p.get("account")
        if m in ("account_info", "account_lines") and a not in self.accounts:
            return httpx.Response(200, json={"result": {"error": "actNotFound", "status": "error",
                                                        "error_message": "Account not found."}})
        if m == "account_info":
            return ok({"account_data": {"Account": a, **self.accounts[a]}, "validated": True})
        if m == "account_lines":
            return ok({"account": a, "lines": self.lines.get(a, [])})
        if m == "account_tx":
            lo, hi = int(p["ledger_index_min"]), int(p["ledger_index_max"])
            lo = self.first_ledger if lo == -1 else lo
            rows = [t for t in self.txs if lo <= t["ledger_index"] <= hi and self.involves(t, a)]
            rows.sort(key=lambda t: (t["ledger_index"], t["meta"]["TransactionIndex"]), reverse=not p.get("forward"))
            start = int((p.get("marker") or {}).get("i", 0))
            limit = int(p.get("limit") or 200)
            chunk = rows[start:start + limit]
            res: dict[str, Any] = {"account": a, "ledger_index_min": lo, "ledger_index_max": hi, "limit": limit,
                                   "transactions": chunk, "validated": True}
            if start + limit < len(rows):
                res["marker"] = {"i": start + limit}
            return ok(res)
        return httpx.Response(200, json={"result": {"error": "unknownCmd", "status": "error"}})


class FakeKoios(Recorder):
    """Koios v1 (api.koios.rest): PostgREST-Seiten über ``offset``/``limit``, ``order`` nach Blockhöhe."""

    def __init__(self, txs: list[dict[str, Any]], tip: dict[str, Any], rewards: list[dict[str, Any]] | None = None,
                 accounts: dict[str, dict[str, Any]] | None = None, assets: list[dict[str, Any]] | None = None,
                 key: str | None = None) -> None:
        super().__init__()
        self.txs = {t["tx_hash"]: t for t in txs}
        self.tip, self.rewards, self.accounts, self.assets = tip, rewards or [], accounts or {}, assets or []
        self.key = key

    @staticmethod
    def _touch(t: dict[str, Any], stake: str | None = None, addrs: set[str] | None = None) -> bool:
        for io in [*t["inputs"], *t["outputs"]]:
            if stake and io.get("stake_addr") == stake:
                return True
            if addrs and (io.get("payment_addr") or {}).get("bech32") in addrs:
                return True
        return any(stake and w.get("stake_addr") == stake for w in t.get("withdrawals") or [])

    def _page(self, rows: list[Any], q: Any) -> httpx.Response:
        off, lim = int(q.get("offset", 0)), int(q.get("limit", 1000))
        assert lim <= 1000
        return httpx.Response(200, json=rows[off:off + lim])

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.url.host == "api.koios.rest" and req.url.path.startswith("/api/v1/")
        auth = req.headers.get("authorization")
        if self.key:
            assert auth == f"Bearer {self.key}"
        else:
            assert auth is None
        inj = self.injected(req)
        if inj is not None:
            return inj
        path, q = req.url.path.removeprefix("/api/v1"), req.url.params
        body = json.loads(req.content) if req.content else {}
        if path == "/tip":
            return httpx.Response(200, json=[self.tip])
        if path in ("/account_txs", "/address_txs"):
            if path == "/account_txs":
                stake, addrs, after = q["_stake_address"], None, int(q.get("_after_block_height", 0))
            else:
                stake, addrs, after = None, set(body["_addresses"]), int(body.get("_after_block_height", 0))
            assert q.get("order") == "block_height.asc,tx_hash.asc"
            rows = sorted(({"tx_hash": h, "epoch_no": t["epoch_no"], "block_height": t["block_height"],
                            "block_time": t["tx_timestamp"]} for h, t in self.txs.items()
                           if t["block_height"] >= after and self._touch(t, stake, addrs)),
                          key=lambda r: (r["block_height"], r["tx_hash"]))
            return self._page(rows, q)
        if path == "/tx_info":
            assert body["_inputs"] and body["_assets"] and body["_withdrawals"] and body["_certs"]
            return httpx.Response(200, json=[self.txs[h] for h in body["_tx_hashes"] if h in self.txs])
        if path == "/account_reward_history":
            return self._page([r for r in self.rewards if r["stake_address"] in body["_stake_addresses"]], q)
        if path == "/account_info":
            return httpx.Response(200, json=[{"stake_address": s, **self.accounts[s]}
                                             for s in body["_stake_addresses"] if s in self.accounts])
        if path == "/account_assets":
            return self._page([a for a in self.assets if a.get("stake_address") in body["_stake_addresses"]], q)
        if path == "/account_addresses":
            return httpx.Response(200, json=[{"stake_address": s, "addresses": sorted({
                (io.get("payment_addr") or {}).get("bech32") for t in self.txs.values()
                for io in [*t["inputs"], *t["outputs"]] if io.get("stake_addr") == s})}
                for s in body["_stake_addresses"]])
        if path in ("/address_info", "/address_assets"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "not found"})


class FakeSubscan(Recorder):
    """Subscan über das PubFi-Gateway (``/v1/gateway/subscan/<netz>/api/…:free``, Bearer) bzw. direkt
    (``<netz>.api.subscan.io``, ``X-API-Key``): Listen im Blockbereich, aufsteigend, ``page``/``row``."""

    def __init__(self, nets: dict[str, dict[str, Any]], key: str) -> None:
        super().__init__()
        self.nets, self.key = nets, key

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.method == "POST" and not req.url.params, "PubFi verbietet Query-Parameter"
        if req.url.host == "api.pubfi.ai":
            m = re.match(r"^/v1/gateway/subscan/([a-z-]+)/api/(.+):free$", req.url.path)
            assert m, req.url.path
            assert req.headers.get("authorization") == f"Bearer {self.key}"
            net, route = m.group(1), m.group(2)
        else:
            m = re.match(r"^([a-z-]+)\.api\.subscan\.io$", req.url.host)
            assert m and req.url.path.startswith("/api/") and not req.url.path.endswith(":free")
            assert req.headers.get("x-api-key") == self.key
            net, route = m.group(1), req.url.path.removeprefix("/api/")
        inj = self.injected(req)
        if inj is not None:
            return inj
        d = self.nets[net]
        body = json.loads(req.content or b"{}")

        def ok(data: Any) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "message": "Success", "generated_at": 1, "data": data})

        if route == "scan/metadata":
            return ok({"blockNum": str(d["tip"] + 5), "finalized_blockNum": str(d["tip"])})
        addr = body.get("address")
        lo, _, hi = str(body.get("block_range") or "0-999999999").partition("-")
        row, page = int(body.get("row") or 10), int(body.get("page") or 0)
        assert row <= 100

        def sel(rows: list[dict[str, Any]], who: Any) -> list[dict[str, Any]]:
            out = [r for r in rows if who(r) and int(lo) <= int(r.get("block_num") or 0) <= int(hi)]
            out.sort(key=lambda r: (int(r.get("block_num") or 0), str(r.get("extrinsic_index") or r.get(
                "event_index") or "")))
            return out[page * row:(page + 1) * row]

        if route == "v2/scan/transfers":
            rows = sel(d.get("transfers", []), lambda r: addr in (r["from"], r["to"]))
            return ok({"count": len(rows), "transfers": rows or None})
        if route == "v2/scan/extrinsics":
            return ok({"count": 0, "extrinsics": sel(d.get("extrinsics", []), lambda r: r["signer"] == addr)})
        if route == "v2/scan/account/reward_slash":
            return ok({"count": 0, "list": sel(d.get("rewards", []), lambda r: r["account"] == addr)})
        if route == "v2/scan/account/tokens":
            return ok({"count": len(d.get("tokens", [])), "list": d.get("tokens", [])})
        return httpx.Response(404, json={"code": 404, "message": "not found"})
