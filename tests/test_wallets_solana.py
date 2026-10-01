"""Solana-Wallets (SOL + SPL) – synthetische Fixtures, ohne Netz.

Abgedeckt: eingehende Token-Transfers, die nur über das Token-Konto auffindbar sind (aktuelle und inzwischen
geschlossene Konten), Miete (Rent) als Eigentum der Wallet, Gebühren, fehlgeschlagene Transaktionen, Swap über ein
Programm, unbekannte Tokens, NFTs, Paginierung der Signaturen, blockweise Fortsetzung nach Abbruch, nicht abrufbare
Transaktion als sichtbare Lücke, Drosselung (HTTP 429, JSON-RPC −32005), Helius mit Schlüssel.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources.chains import solana as SO
from app.datasources.chains.codec import b58encode
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import (
    MASTER,
    FakeSolana,
    all_rows,
    balances,
    create_wallet,
    ctx,
    make_client,
    set_provider_key,
    source,
)

D = Decimal
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM = "11111111111111111111111111111111"
ATA_PROG = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEX = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


def pk(n: int) -> str:
    return b58encode(bytes([n]) * 32)


W, S = pk(1), pk(2)  # eigene Wallet, Absender
ATA_USDC, ATA_OLD, ATA_NFT, S_ATA = pk(3), pk(4), pk(5), pk(6)
MINT_M, MINT_NFT = pk(7), pk(8)
RENT = 2_039_280


def sig(n: int) -> str:
    return b58encode(bytes([n % 256]) * 63 + bytes([n // 256 + 1]))


class Chain:
    """Baut konsistente Transaktionen (Vor-/Nach-Bestände) für die Nachbildung."""

    def __init__(self) -> None:
        self.lam: dict[str, int] = {W: 0, S: 100_000_000_000}
        self.tok: dict[str, tuple[str, str, int, int]] = {}  # Konto → (Mint, Besitzer, Menge, Dezimalen)
        self.txs: list[dict[str, Any]] = []

    def tx(self, n: int, slot: int, keys: list[tuple[str, bool]], lam: dict[str, int], fee: int = 5000,
           tok: dict[str, int] | None = None, new_tok: dict[str, tuple[str, str, int]] | None = None,
           close: tuple[str, ...] = (), err: Any = None, programs: tuple[str, ...] = (SYSTEM,)) -> str:
        names = [k for k, _ in keys]
        for p in programs:
            if p not in names:
                keys.append((p, False))
                names.append(p)
        pre = [self.lam.get(k, 0) for k in names]
        pre_tok = [self._tb(i, k) for i, k in enumerate(names) if k in self.tok]
        payer = names[0]
        if err is None:
            for k, d in lam.items():
                self.lam[k] = self.lam.get(k, 0) + d
            for k, (mint, owner, dec) in (new_tok or {}).items():
                self.tok[k] = (mint, owner, 0, dec)
            for k, d in (tok or {}).items():
                mint, owner, amt, dec = self.tok[k]
                self.tok[k] = (mint, owner, amt + d, dec)
        self.lam[payer] = self.lam.get(payer, 0) - fee
        post = [self.lam.get(k, 0) for k in names]
        post_tok = [self._tb(i, k) for i, k in enumerate(names) if k in self.tok]
        for k in close:
            self.tok.pop(k, None)
        post_tok = [t for t in post_tok if names[t["accountIndex"]] not in close]
        s = sig(n)
        self.txs.append({"slot": slot, "blockTime": 1772445600 + slot, "meta": {
            "err": err, "fee": fee, "preBalances": pre, "postBalances": post, "preTokenBalances": pre_tok,
            "postTokenBalances": post_tok, "loadedAddresses": {"writable": [], "readonly": []}},
            "transaction": {"signatures": [s], "message": {
                "accountKeys": [{"pubkey": k, "signer": sg, "writable": True, "source": "transaction"}
                                for k, sg in keys],
                "instructions": [{"programId": p, "program": "x", "parsed": {}} for p in programs]}}})
        return s

    def _tb(self, i: int, k: str) -> dict[str, Any]:
        mint, owner, amt, dec = self.tok[k]
        return {"accountIndex": i, "mint": mint, "owner": owner, "programId": TOKEN,
                "uiTokenAmount": {"amount": str(amt), "decimals": dec, "uiAmountString": "x"}}


def scenario() -> tuple[Chain, dict[str, str]]:
    c = Chain()
    s: dict[str, str] = {}
    s["sol_in"] = c.tx(1, 100, [(S, True), (W, False)], {S: -2_000_000_000, W: 2_000_000_000})
    s["create_ata"] = c.tx(2, 200, [(W, True), (ATA_USDC, False), (USDC, False)], {W: -RENT, ATA_USDC: RENT},
                           new_tok={ATA_USDC: (USDC, W, 6)}, programs=(SYSTEM, ATA_PROG, TOKEN))
    s["usdc_in"] = c.tx(3, 300, [(S, True), (S_ATA, False), (ATA_USDC, False)], {}, tok={ATA_USDC: 100_000_000},
                        programs=(TOKEN,))  # Wallet-Adresse nicht beteiligt – nur das Token-Konto
    s["swap"] = c.tx(4, 400, [(W, True), (ATA_USDC, False)], {W: -1_000_000_000}, tok={ATA_USDC: 150_000_000},
                     programs=(DEX, TOKEN))
    s["m_create"] = c.tx(5, 500, [(S, True), (ATA_OLD, False), (W, False), (MINT_M, False)],
                         {S: -RENT, ATA_OLD: RENT}, new_tok={ATA_OLD: (MINT_M, W, 0)}, programs=(ATA_PROG, TOKEN))
    c.tok[ATA_OLD] = (MINT_M, W, 5000, 0)  # mit Erstellung 5.000 M (0 Dezimalen, kein NFT da > 1)
    c.txs[-1]["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = "5000"
    s["m_in_only_ata"] = c.tx(6, 600, [(S, True), (ATA_OLD, False)], {}, tok={ATA_OLD: 1000}, programs=(TOKEN,))
    s["m_out_close"] = c.tx(7, 700, [(W, True), (ATA_OLD, False), (S_ATA, False)], {ATA_OLD: -RENT, W: RENT},
                           tok={ATA_OLD: -6000}, close=(ATA_OLD,), programs=(TOKEN,))
    s["failed"] = c.tx(8, 800, [(W, True), (S, False)], {W: -1, S: 1}, err={"InstructionError": [0, "Custom"]})
    c.tok[ATA_NFT] = (MINT_NFT, W, 0, 0)
    c.lam[ATA_NFT] = RENT
    s["nft"] = c.tx(9, 900, [(S, True), (ATA_NFT, False)], {}, tok={ATA_NFT: 1}, programs=(TOKEN,))
    return c, s


@pytest.fixture
def sol(monkeypatch):
    chain, sigs = scenario()
    tas = [{"pubkey": ATA_USDC, "mint": USDC, "amount": "250000000", "decimals": 6, "lamports": RENT},
           {"pubkey": ATA_NFT, "mint": MINT_NFT, "amount": "1", "decimals": 0, "lamports": RENT}]
    fake = FakeSolana(chain.txs, tas, balance=chain.lam[W])
    fake.chain, fake.sigs = chain, sigs  # type: ignore[attr-defined]
    sleeps: list[float] = []
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(sleeps.append))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    monkeypatch.setattr(SO.SolanaConnector, "workers", 1)
    fake.sleeps = sleeps  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        from app.journal.service import journal_service
        journal_service(ctx(c)).save_asset({"asset_id": "SOL", "name": "Solana", "asset_class": "crypto",
                                            "quote_source": "none"})
        yield c


def sync(c, sid):
    return datasource_service(ctx(c)).sync(sid, "manual")


def rows_of(c, sid, s: str) -> list:
    return [v for k, v in all_rows(c, sid).items() if k.startswith(f"solana:{s}:{W}#")]


def test_token_accounts_closed_accounts_rent_fees_and_programs(client, sol):
    sid = create_wallet(client, "solana", W, name="Phantom SOL", group="Phantom")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    s = sol.sigs
    (dep,) = rows_of(client, sid, s["sol_in"])
    assert (dep.rec.kind, dep.rec.in_sym, dep.rec.in_qty) == ("deposit", "SOL", D("2"))
    (fee,) = rows_of(client, sid, s["create_ata"])  # Miete bleibt Eigentum der Wallet → nur Gebühr
    assert fee.rec.kind == "fee" and fee.rec.fee_qty == D("0.000005")
    (usdc,) = rows_of(client, sid, s["usdc_in"])  # nur über das Token-Konto gefunden
    assert (usdc.rec.kind, usdc.rec.in_sym, usdc.rec.in_qty, usdc.rec.review) == \
        ("deposit", f"USDC@SOL:{USDC}", D("100"), None)
    (swap,) = rows_of(client, sid, s["swap"])
    assert swap.rec.kind == "trade" and swap.rec.out_qty == D("1") and swap.rec.in_qty == D("150") and \
        swap.rec.review and swap.rec.label == "Programm JUP6LkbZ"
    m_rows = rows_of(client, sid, s["m_create"])
    assert {(r.rec.in_sym, r.rec.in_qty) for r in m_rows} == {(f"SPL@SOL:{MINT_M}", D("5000")),
                                                               ("SOL", D("0.00203928"))}
    assert any(r.rec.review and "Spam" in r.rec.review for r in m_rows if r.rec.in_sym != "SOL")
    (later,) = rows_of(client, sid, s["m_in_only_ata"])  # nur über das inzwischen geschlossene Konto auffindbar
    assert later.rec.in_qty == D("1000")
    (out,) = rows_of(client, sid, s["m_out_close"])  # Rückgabe der Miete gleicht sich aus
    assert (out.rec.kind, out.rec.out_sym, out.rec.out_qty) == ("withdrawal", f"SPL@SOL:{MINT_M}", D("6000"))
    (failed,) = rows_of(client, sid, s["failed"])
    assert failed.rec.kind == "fee" and "fehlgeschlagen" in failed.rec.note
    assert not rows_of(client, sid, s["nft"]) and "NFT" in res["message"]
    cur = json.loads(source(client, sid)["cursor_json"])
    assert ATA_OLD in cur["tas"] and all(v["done"] for v in cur["addrs"].values())
    bal = balances(client, sid)
    assert bal[f"USDC@SOL:{USDC}"] == "250" and D(bal["SOL"]) == (D(sol.chain.lam[W]) + 2 * RENT) / D(10**9)
    assert ds_state(client, sid) == ("vollständig synchronisiert", "good")


def ds_state(c, sid):
    return datasource_service(ctx(c)).get(sid).sync_state


def test_incremental_uses_until_and_is_idempotent(client, sol):
    sid = create_wallet(client, "solana", W, name="Phantom SOL")
    sync(client, sid)
    n = len(all_rows(client, sid))
    new = sol.chain.tx(20, 2000, [(S, True), (W, False)], {S: -1_000_000_000, W: 1_000_000_000})
    sol.txs[new] = sol.chain.txs[-1]
    sol.calls.clear()
    res = sync(client, sid)
    assert res.get("new", 0) == 1 and len(all_rows(client, sid)) == n + 1
    untils = [json.loads(r.content)["params"][1].get("until") for r in sol.calls
              if json.loads(r.content)["method"] == "getSignaturesForAddress"]
    assert untils and all(untils)
    sol.calls.clear()
    assert sync(client, sid).get("new", 0) == 0
    assert not [r for r in sol.calls if json.loads(r.content)["method"] == "getTransaction"]


def test_pagination_chunks_and_interrupted_backfill(client, sol, monkeypatch):
    monkeypatch.setattr(SO, "SIG_PAGE", 3)
    monkeypatch.setattr(SO, "CHUNK", 2)
    for i in range(30):
        s = sol.chain.tx(100 + i, 10_000 + i, [(S, True), (W, False)], {S: -1000 - i, W: 1000 + i}, fee=5000)
        sol.txs[s] = sol.chain.txs[-1]
    sid = create_wallet(client, "solana", W, name="Viele Vorgänge")
    monkeypatch.setattr(SO.SolanaConnector, "max_requests", 12)
    sync(client, sid)
    st = source(client, sid)
    assert st["status"] == "partial" and json.loads(st["coverage_json"])["resume"]
    first = set(all_rows(client, sid))
    assert first
    monkeypatch.setattr(SO.SolanaConnector, "max_requests", 2500)
    rounds = 0
    while datasource_service(ctx(client)).get(sid).backfill_pending:
        rounds += 1
        assert rounds < 30
        sync(client, sid)
    rows = all_rows(client, sid)
    assert first <= set(rows) and len([k for k in rows if k.endswith("#sol")]) == 30 + 2  # + sol_in, Miete M
    assert len(set(rows)) == len(rows)


def test_missing_transaction_is_a_visible_gap_not_a_loss(client, sol):
    sol.missing.add(sol.sigs["usdc_in"])
    sid = create_wallet(client, "solana", W, name="Phantom SOL")
    sync(client, sid)
    st = source(client, sid)
    cov = json.loads(st["coverage_json"])
    assert st["status"] == "partial" and cov["gaps"] and not cov["complete"]
    assert ds_state(client, sid)[0] != "vollständig synchronisiert"
    assert not rows_of(client, sid, sol.sigs["usdc_in"])
    sol.missing.clear()
    sync(client, sid)
    assert rows_of(client, sid, sol.sigs["usdc_in"]) and source(client, sid)["status"] == "synced"


def test_throttling_is_retried(client, sol):
    sid = create_wallet(client, "solana", W, name="Phantom SOL")
    sol.fail_next(httpx.Response(429, headers={"Retry-After": "4"}),
                  lambda r: json.loads(r.content)["method"] == "getTransaction")
    sol.fail_next(httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005,
                                                                                  "message": "rate limited"}}),
                  lambda r: json.loads(r.content)["method"] == "getSignaturesForAddress")
    assert sync(client, sid)["status"] == "synced"
    assert 4.0 in sol.sleeps


def test_helius_needs_key_and_receives_it_only_as_documented(client, sol):
    sid = create_wallet(client, "solana", W, name="Phantom SOL", chain_provider="helius")
    res = sync(client, sid)
    assert res["kind"] == "config" and not sol.calls
    key = "helius-test-key-0123456789"
    set_provider_key(client, "helius", key)
    assert sync(client, sid)["status"] == "synced"
    assert {r.url.host for r in sol.calls} == {"mainnet.helius-rpc.com"}
    assert all(r.url.params.get("api-key") == key for r in sol.calls)
    assert key not in json.dumps(source(client, sid), default=str)
