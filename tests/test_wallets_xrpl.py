"""XRP Ledger – Saldoänderungen validierter Ledger (synthetische Daten, ohne Netz).

Abgedeckt: Adressprüfung (Prüfsumme, X-Adresse), Kontoeröffnung, Zahlung mit Destination Tag und Gebühr,
fehlgeschlagene Transaktion (nur Gebühr), Trustline-Tokens mit gleicher Währung verschiedener Emittenten, DEX-Tausch
zur Prüfung, Kleinstbetrag mit Memo, Paginierung über ``marker``, abgebrochener Lauf mitten in einem Ledger mit
Fortsetzung ohne Doppelungen, Lücke bei fehlender Kontoeröffnung, Reserve im Bestand, Serverfehler.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources.chains import xrpl as X
from app.datasources.chains.codec import xrpl_encode
from app.datasources.providers import PROVIDERS, normalize_address
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import MASTER, FakeXrpl, all_rows, balances, create_wallet, ctx, make_client, post, source

D = Decimal
ME, EXT, EXCH, ISS, ISS2 = (xrpl_encode(bytes([b]) * 20) for b in (0x11, 0x22, 0x33, 0x44, 0x55))
T0 = datetime(2024, 1, 1, tzinfo=UTC).timestamp()


def root(acc: str, before: int, after: int, created: bool = False) -> dict:
    if created:
        return {"CreatedNode": {"LedgerEntryType": "AccountRoot", "NewFields": {"Account": acc, "Balance": str(after)}}}
    return {"ModifiedNode": {"LedgerEntryType": "AccountRoot", "FinalFields": {"Account": acc, "Balance": str(after)},
                             "PreviousFields": {"Balance": str(before)}}}


def trust(low: str, high: str, cur: str, before: str, after: str, created: bool = False) -> dict:
    final = {"Balance": {"currency": cur, "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji", "value": after},
             "LowLimit": {"currency": cur, "issuer": low, "value": "1000"},
             "HighLimit": {"currency": cur, "issuer": high, "value": "0"}}
    if created:
        return {"CreatedNode": {"LedgerEntryType": "RippleState", "NewFields": final}}
    return {"ModifiedNode": {"LedgerEntryType": "RippleState", "FinalFields": final,
                             "PreviousFields": {"Balance": {"currency": cur, "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",
                                                            "value": before}}}}


def xtx(n: int, ledger: int, idx: int, ttype: str, account: str, fee: int, nodes: list, result: str = "tesSUCCESS",
        **fields) -> dict:
    when = datetime.fromtimestamp(T0 + ledger * 60, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"close_time_iso": when, "hash": f"{n:064X}", "ledger_index": ledger, "validated": True,
            "meta": {"AffectedNodes": nodes, "TransactionIndex": idx, "TransactionResult": result},
            "tx_json": {"Account": account, "Fee": str(fee), "TransactionType": ttype, "ledger_index": ledger,
                        "Flags": 0, **fields}}


def history() -> list[dict]:
    return [
        xtx(1, 100, 0, "Payment", EXT, 12, [root(EXT, 100_000_000, 49_999_988), root(ME, 0, 50_000_000, True)],
            Destination=ME),
        xtx(2, 110, 0, "Payment", ME, 12, [root(ME, 50_000_000, 39_999_988), root(EXCH, 5 * 10**8, 51 * 10**7)],
            Destination=EXCH, DestinationTag=12345),
        xtx(3, 120, 0, "TrustSet", ME, 10, [root(ME, 39_999_988, 39_999_978), trust(ME, ISS, "USD", "0", "0", True)]),
        xtx(4, 130, 0, "Payment", ISS, 10, [root(ISS, 10**9, 10**9 - 10), trust(ME, ISS, "USD", "0", "100")],
            Destination=ME),
        xtx(5, 135, 0, "Payment", ISS2, 10, [root(ISS2, 10**9, 10**9 - 10), trust(ME, ISS2, "USD", "0", "7", True)],
            Destination=ME),
        xtx(6, 140, 0, "OfferCreate", ME, 10, [root(ME, 39_999_978, 19_999_968), trust(ME, ISS, "USD", "100", "130")]),
        xtx(7, 150, 0, "Payment", ME, 10, [root(ME, 19_999_968, 19_999_958)], result="tecUNFUNDED_PAYMENT",
            Destination=EXT),
        xtx(8, 150, 1, "Payment", EXT, 10, [root(EXT, 10**8, 10**8 - 11), root(ME, 19_999_958, 19_999_959)],
            Destination=ME, Memos=[{"Memo": {"MemoData": "68747470733a2f2f7370616d2e78797a"}}]),
        xtx(9, 160, 0, "Payment", EXT, 10, [root(EXT, 10**8, 10**8 - 10), root(ME, 19_999_959, 20_999_959)],
            Destination=ME),
        xtx(10, 160, 1, "Payment", EXT, 10, [root(EXT, 10**8, 10**8 - 10), root(ME, 20_999_959, 21_999_959)],
            Destination=ME),
        xtx(11, 160, 2, "Payment", EXT, 10, [root(EXT, 10**8, 10**8 - 10), root(ME, 21_999_959, 22_999_959)],
            Destination=ME),
    ]


@pytest.fixture
def ledger(monkeypatch):
    fake = FakeXrpl(history(), tip=200, accounts={ME: {"Balance": "22999959", "OwnerCount": 2}},
                    lines={ME: [{"account": ISS, "balance": "130", "currency": "USD"},
                                {"account": ISS2, "balance": "7", "currency": "USD"}]}, first_ledger=50)
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    monkeypatch.setattr(X, "PAGE", 2)  # Paginierung mit kleinen Seiten prüfen
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


def k(n: int, sub: str) -> str:
    return f"xrp:{n:064X}:{ME}#{sub}"


def test_address_validation():
    p = PROVIDERS["xrp"]
    assert normalize_address(p, ME) == (ME, None)
    bad = ME[:-1] + ("a" if ME[-1] != "a" else "b")
    assert normalize_address(p, bad)[0] is None and "Prüfsumme" in normalize_address(p, bad)[1]
    x_addr = "X7AcgcsBL6XDcUb289X4mJ8djcdyKaB5hJDWMArnXr61cqZ"
    assert "X-Adresse" in normalize_address(p, x_addr)[1]


def test_history_from_balance_changes(client, ledger):
    sid = create_wallet(client, "xrp", ME, name="Ledger XRP")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    r = rows[k(1, "xrp")].rec
    assert (r.kind, r.in_sym, r.in_qty, r.review) == ("deposit", "XRP", D(50), None)
    r = rows[k(2, "xrp")].rec
    assert (r.kind, r.out_qty, r.fee_qty, r.fee_sym) == ("withdrawal", D(10), D("0.000012"), "XRP")
    assert "Destination Tag 12345" in r.note and r.raw["DestinationTag"] == 12345
    r = rows[k(3, "fee")].rec
    assert r.kind == "fee" and r.fee_qty == D("0.00001")
    usd1 = rows[k(4, f"t:USD.{ISS}")].rec
    usd2 = rows[k(5, f"t:USD.{ISS2}")].rec
    assert usd1.in_sym == f"USD@XRPL:USD.{ISS}" and usd1.in_qty == D(100)
    assert usd2.in_sym == f"USD@XRPL:USD.{ISS2}" and usd2.in_sym != usd1.in_sym  # gleiche Währung, anderer Emittent
    swap = next(v.rec for x, v in rows.items() if x.startswith(f"xrp:{6:064X}:") and v.rec.kind == "trade")
    assert (swap.out_sym, swap.out_qty, swap.in_sym, swap.in_qty) == ("XRP", D(20), f"USD@XRPL:USD.{ISS}", D(30))
    assert "DEX" in swap.review
    r = rows[k(7, "fee")].rec
    assert r.kind == "fee" and "fehlgeschlagen" in r.note
    r = rows[k(8, "xrp")].rec
    assert r.in_qty == D("0.000001") and "Spam" in r.review
    for n in (9, 10, 11):
        assert rows[k(n, "xrp")].rec.in_qty == D(1)
    b = balances(client, sid)
    assert b["XRP"] == "22.999959" and b[f"USD@XRPL:USD.{ISS}"] == "130"
    note = ctx(client).db.scalar("SELECT note FROM ds_balance WHERE source_id=? AND asset_key='XRP'", (sid,))
    assert "1.4 XRP Kontoreserve" in note
    # Seiten zu 2 Einträgen über marker
    assert sum(1 for c in ledger.calls if json.loads(c.content)["method"] == "account_tx") >= 6
    # erneuter Lauf: nichts Neues
    before = len(all_rows(client, sid))
    sync(client, sid)
    assert len(all_rows(client, sid)) == before


def test_interrupted_run_resumes_inside_ledger_without_duplicates(client, ledger, monkeypatch):
    sid = create_wallet(client, "xrp", ME, name="Ledger XRP")
    monkeypatch.setattr(WalletConnector, "max_requests", 5)  # server_info + 4 Seiten → Abbruch mitten in Ledger 150/160
    res = sync(client, sid)
    assert res["status"] == "partial", res
    cur = json.loads(source(client, sid)["cursor_json"])
    first = all_rows(client, sid)
    assert cur["ledger"] <= 160 and all(v.rec.raw["ledger"] < cur["ledger"] for v in first.values())
    monkeypatch.setattr(WalletConnector, "max_requests", 2500)
    res = sync(client, sid)
    rows = all_rows(client, sid)
    assert len({x.split(":")[1] for x in rows}) == 11  # jede Transaktion genau einmal (auch TrustSet: Gebühr)
    for n in (9, 10, 11):
        assert k(n, "xrp") in rows


def test_missing_account_creation_is_a_gap_and_server_errors_keep_cursor(client, ledger):
    ledger.txs = history()[1:]  # Server ohne die Kontoeröffnung
    sid = create_wallet(client, "xrp", ME, name="Ledger XRP")
    res = sync(client, sid)
    assert res["status"] == "partial"
    assert "Kontoeröffnung" in source(client, sid)["coverage_json"]
    cursor = source(client, sid)["cursor_json"]
    ledger.fail_next(httpx.Response(200, json={"result": {"error": "tooBusy", "status": "error"}}))
    ledger.fail_next(httpx.Response(200, json={"result": {"error": "tooBusy", "status": "error"}}))
    ledger.fail_next(httpx.Response(200, json={"result": {"error": "tooBusy", "status": "error"}}))
    ledger.fail_next(httpx.Response(200, json={"result": {"error": "tooBusy", "status": "error"}}))
    res = sync(client, sid)
    assert "error" in res and source(client, sid)["cursor_json"] == cursor


def test_check_reports_reserve_and_lines(client, ledger):
    sid = create_wallet(client, "xrp", ME, name="Ledger XRP")
    r = post(client, f"/settings/datasources/{sid}/check")
    assert "msg=" in r.headers["location"], r.headers["location"]
    details = json.loads(source(client, sid)["last_check_json"])["details"]
    assert "Kontoreserve" in details["balance"]["text"] and "2 Trustline" in details["tokens"]["text"]
