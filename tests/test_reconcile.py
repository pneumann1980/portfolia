"""Abgleich eines Prüf-Stapels mit dem kuratierten Import über den Transaktions-Hash (ohne Netz).

Kuratierter Import wie aus Koinly: Hash in der Notiz, Gebühren als eigene Buchung (Abgang mit Tag ``cost``),
Konto „MetaMask (ETH)“. Die Wallet-Datenquelle heißt anders („Ledger ETH“) – der Abgleich erkennt vorhandene
Vorgänge, lernt Token-Zuordnungen und stellt das Konto um, solange nichts aufgeteilt wird.
"""

from __future__ import annotations

import copy
import os
import time
from decimal import Decimal

import httpx
import pytest

from app.csvimport import model as M
from app.csvimport import reconcile as R
from app.csvimport.service import csv_service
from app.datasources import chainhttp as CH
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from tests.helpers import ASSETS, tx
from tests.wallet_fakes import (
    ETHERSCAN_KEY,
    MASTER,
    FakeEvm,
    create_wallet,
    ctx,
    load,
    make_client,
    post,
    rows_by_ext,
    set_provider_key,
)

A = "0x1111111111111111111111111111111111111111"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
FAKE = "0x9999999999999999999999999999999999999999"
MM = "MetaMask (ETH)"
CURATED_ASSETS = [*ASSETS, {"asset_id": "USDC", "name": "USD Coin", "asset_class": "crypto",
                             "quote_source": "coingecko", "quote_id": "usd-coin", "category": "Krypto: Stablecoins"}]


def H(n: int) -> str:
    return "0x" + f"{n:064x}"


def curated_rows() -> list[dict]:
    def note(n: int) -> str:
        return f"Koinly · TxHash {H(n)}"

    rows = [
        tx("K1", "2026-03-02T11:40:00Z", "deposit", to=(MM, "ETH", "1.5")),
        tx("K2", "2026-03-02T13:20:00Z", "withdrawal", frm=(MM, "ETH", "0.2")),
        tx("K2F", "2026-03-02T13:20:00Z", "withdrawal", tag="cost", frm=(MM, "ETH", "0.00063"), value="2"),
        tx("K4", "2026-03-02T16:40:00Z", "trade", frm=(MM, "ETH", "0.5"), to=(MM, "USDC", "1000"), value="1000"),
        tx("K4F", "2026-03-02T16:40:00Z", "withdrawal", tag="cost", frm=(MM, "ETH", "0.0033"), value="9"),
        tx("K5", "2026-03-02T18:20:00Z", "deposit", to=(MM, "USDC", "100")),
    ]
    for r, n in zip(rows, (1, 2, 2, 4, 4, 5), strict=True):
        r["note"] = note(n)
        r["source"], r["source_ref"] = "koinly", f"KOINLY{r['tx_id']}"
    return rows


def import_curated(c, config, rows=None, valuation="2026-03-02") -> None:
    dst = config.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=rows if rows is not None else curated_rows(), assets=CURATED_ASSETS,
              generated_at="2026-03-03T08:00:00Z", valuation_date=valuation,
              extra_tx_columns=["source", "source_ref", "note"])
    old = time.time() - 3600
    os.utime(dst, (old, old))
    assert tasks.import_check(ctx(c), "test").status == "imported"


@pytest.fixture
def evm(monkeypatch):
    eth = load("evm_eth.json")
    fake = FakeEvm({1: eth, 56: copy.deepcopy(eth), 43114: copy.deepcopy(eth)})
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


def wallet(c, account: str = "Ledger ETH") -> int:
    set_provider_key(c, "etherscan", ETHERSCAN_KEY)
    return create_wallet(c, "ethereum", A, name="Ledger ETH", account=account)


def sync(c, sid: int) -> dict:
    return datasource_service(ctx(c)).sync(sid, "manual")


def by_hash(c, bid: int) -> dict[str, list]:
    out: dict[str, list] = {}
    for rc in sorted(rows_by_ext(c, bid).values(), key=lambda r: r.idx):
        out.setdefault((rc.rec.txhash or "")[-2:], []).append(rc)
    return out


# ----------------------------------------------------------------------------------------------------
# Ende-zu-Ende: ETH-Wallet gegen kuratierten Import
# ----------------------------------------------------------------------------------------------------

def test_wallet_batch_reconciled_with_curated_import(client, evm, config):
    import_curated(client, config)
    sid = wallet(client)
    bid = int(sync(client, sid)["batch_id"])
    rs = by_hash(client, bid)
    # vorhanden: gleiche Blockchain-Transaktion, alle Beine (Gebühr als eigene Import-Buchung „cost“)
    for h, tid in (("01", "K1"), ("02", "K2"), ("04", "K4")):
        (rc,) = rs[h]
        assert rc.status == "known" and rc.recon["origin"] == "import" and rc.recon["txs"] == [tid], (h, rc.recon)
        assert MM in rc.warnings[0] and tid in rc.warnings[0]
    # zwei Token-Logs zu je 50 = eine Import-Buchung über 100; der Fake-Token derselben Transaktion fehlt dort
    real = [rc for rc in rs["05"] if USDC in (rc.rec.in_sym or "")]
    fake = [rc for rc in rs["05"] if FAKE in (rc.rec.in_sym or "")]
    assert len(real) == 2 and all(rc.status == "known" for rc in real)
    assert fake[0].status == "before" and fake[0].recon["state"] == "partial"
    assert "dort nicht gefunden: +1000 USDC@ETH:" + FAKE in fake[0].warnings[0]
    # vor dem Stichtag, nicht im Import (Freigabe, fehlgeschlagen): nur Hinweis, keine Entscheidung nötig
    for h in ("03", "06"):
        (rc,) = rs[h]
        assert rc.status == "before" and rc.recon == {"state": "missing"}
        assert "nicht im kuratierten Import gefunden" in rc.warnings[0]
    # Token-Zuordnung aus der gleichen Transaktion gelernt und gespeichert (Herkunft „abgleich“)
    sym = ctx(client).db.q1("SELECT * FROM csv_symbol WHERE symbol=?", (f"USDC@ETH:{USDC}".upper(),))
    assert (sym["asset_id"], sym["origin"]) == ("USDC", "abgleich")
    assert ctx(client).db.q1("SELECT 1 FROM csv_symbol WHERE symbol LIKE ?", (f"%{FAKE[2:].upper()}",)) is None
    # Konto der Datenquelle automatisch auf das Import-Konto umgestellt (bisheriges Konto ohne Buchungen)
    ds = datasource_service(ctx(client)).get(sid)
    assert ds.account == MM
    sw = datasource_service(ctx(client)).account_switch(sid)
    assert (sw["from"], sw["to"], sw["auto"], sw["matches"], sw["undone"]) == ("Ledger ETH", MM, True, 5, False)
    # neu nach dem Stichtag: ohne Zutun vollständig (Token zugeordnet, Konto des Imports)
    (usdc_out,) = rs["09"]
    assert usdc_out.status == "new" and not usdc_out.errors
    assert (usdc_out.row["from_account"], usdc_out.row["from_asset"], usdc_out.row["from_qty"]) == (MM, "USDC", "200")
    summ = csv_service(ctx(client)).overview(bid)
    assert summ["recon"] == {"hashes": 4, "found_import": 5, "found_app": 0, "partial": 0, "missing": 3,
                             "accounts": {MM: 5}, "learned_plain": {}}
    assert summ["unknown"] == [] and [u["symbol"] for u in summ["unknown_old"]] == [f"USDC@ETH:{FAKE}".upper()]
    page = client.get(f"/journal/csv/{bid}").text
    assert "Abgleich mit vorhandenen Buchungen" in page and "im kuratierten Import 5" in page
    assert "automatisch von „Ledger ETH“ auf „MetaMask (ETH)“ umgestellt" in page and "Rückgängig" in page
    assert "Automatisch zugeordnet" in page and "nur in Vorgängen vor dem Stichtag – optional" in page
    assert "Nichts zu tun" in page and "1 · Assets zuordnen" in page
    # Übernahme: Bestand setzt den kuratierten fort (1000 + 100 − 200 USDC auf „MetaMask (ETH)“)
    assert post(client, f"/journal/csv/{bid}/commit").status_code == 303
    held = ctx(client).ledger().holdings_by_account(MM)
    assert held["USDC"] == Decimal("900")
    assert csv_service(ctx(client)).batch(bid)["status"] == "committed"  # nichts mehr zu entscheiden


def test_account_switch_undo_and_manual_adopt(client, evm, config):
    import_curated(client, config)
    sid = wallet(client)
    bid = int(sync(client, sid)["batch_id"])
    svc = datasource_service(ctx(client))
    assert svc.get(sid).account == MM
    r = post(client, f"/journal/csv/{bid}/account-undo")
    assert r.status_code == 303 and "msg=account" in r.headers["location"]
    assert svc.get(sid).account == "Ledger ETH"
    assert csv_service(ctx(client)).batch(bid)["account"] == "Ledger ETH"
    assert svc.account_switch(sid)["undone"] is True
    # zurückgenommen: kein erneutes automatisches Umstellen, stattdessen Vorschlag mit einem Klick
    page = client.get(f"/journal/csv/{bid}").text
    assert svc.get(sid).account == "Ledger ETH"
    assert "führt 5 von 5 gefundenen Vorgängen unter <b>„MetaMask (ETH)“</b>" in page
    assert "Konto „MetaMask (ETH)“ übernehmen" in page
    (usdc_out,) = by_hash(client, bid)["09"]
    assert usdc_out.row["from_account"] == "Ledger ETH"
    r = post(client, f"/journal/csv/{bid}/account-adopt", account=MM)
    assert r.status_code == 303
    assert svc.get(sid).account == MM and svc.account_switch(sid)["auto"] is False
    (usdc_out,) = by_hash(client, bid)["09"]
    assert usdc_out.row["from_account"] == MM
    assert post(client, f"/journal/csv/{bid}/account-adopt", account="<x>").status_code == 200  # ungültig: Fehler


def test_account_kept_when_previous_account_has_bookings(client, evm, config):
    import_curated(client, config)
    r = post(client, "/journal/new", kind="deposit", date="2026-03-01", account="Ledger ETH", asset="ETH", qty="0.1")
    assert r.status_code == 303, r.text[:1500]
    sid = wallet(client)
    bid = int(sync(client, sid)["batch_id"])
    svc = datasource_service(ctx(client))
    assert svc.get(sid).account == "Ledger ETH" and svc.account_switch(sid) is None
    sug = svc.account_suggestion(svc.get(sid))
    assert (sug["account"], sug["matches"], sug["used"], sug["auto"]) == (MM, 5, 1, False)
    page = client.get(f"/journal/csv/{bid}").text
    assert "Auf „Ledger ETH“ gibt es schon 1 Buchung – sie bleiben dort" in page
    assert svc.get(sid).account == "Ledger ETH"  # auch beim Öffnen nicht automatisch


def test_without_hashes_cutoff_decides_and_old_rows_need_no_mapping(client, evm, config):
    rows = curated_rows()
    for r in rows:
        r["note"] = ""  # Import ohne Transaktions-Hashes
    import_curated(client, config, rows)
    sid = wallet(client)
    bid = int(sync(client, sid)["batch_id"])
    rs = by_hash(client, bid)
    # bis zum Stichtag „vor Stichtag“ – auch mit unbekanntem Token (früher „unvollständig“ mit Zuordnungspflicht)
    assert {rc.status for h in ("01", "02", "03", "04", "05", "06") for rc in rs[h]} == {"before"}
    swap = rs["04"][0]
    assert swap.errors and "Zum Übernehmen fehlt" in client.get(f"/journal/csv/{bid}?status=before").text
    ov = csv_service(ctx(client)).overview(bid)
    assert [u["symbol"] for u in ov["unknown"]] == [f"USDC@ETH:{USDC}".upper()]  # nur aus Zeile 009 (neu)
    assert [u["symbol"] for u in ov["unknown_old"]] == [f"USDC@ETH:{FAKE}".upper()]
    assert ov["recon"] is None and datasource_service(ctx(client)).get(sid).account == "Ledger ETH"
    page = client.get(f"/journal/csv/{bid}").text
    assert "1 · Assets zuordnen" in page and "Nichts zu tun" not in page
    # unvollständige alte Zeile lässt sich nicht „übernehmen“ wählen; Übernahme zählt nur vollständige
    assert f'name="dec_{swap.idx}"' not in page


def test_after_cutoff_same_hash_with_other_amount_is_possible_duplicate(client, evm, config):
    rows = curated_rows()
    later = tx("K9", "2026-03-03T01:00:00Z", "withdrawal", frm=(MM, "USDC", "150"))
    later["note"], later["source"], later["source_ref"] = f"TxHash {H(9)}", "koinly", "KOINLYK9"
    import_curated(client, config, [*rows, later])
    sid = wallet(client)
    bid = int(sync(client, sid)["batch_id"])
    (usdc_out,) = by_hash(client, bid)["09"]
    assert usdc_out.status == "duplicate" and not usdc_out.include()
    assert "K9" in usdc_out.warnings[0] and "dort nicht gefunden: −200" in usdc_out.warnings[0]
    assert csv_service(ctx(client)).overview(bid)["recon"]["partial"] == 1


# ----------------------------------------------------------------------------------------------------
# Regeln (ohne App)
# ----------------------------------------------------------------------------------------------------

class _Row:
    def __init__(self, idx: int, **kw) -> None:
        self.idx = idx
        self.rec = M.Rec(line=idx, ts=None, **kw)  # type: ignore[arg-type]


class _Tx:
    def __init__(self, tx_id: str, note: str, **kw) -> None:
        self.tx_id, self.note, self.source_ref = tx_id, note, None
        for k in ("type", "tag", "from_account", "from_asset", "from_qty", "to_account", "to_asset", "to_qty",
                  "fee_asset", "fee_qty"):
            setattr(self, k, kw.get(k))


def test_hashes_in_text():
    evm = "0x" + "ab" * 32
    btc = "cd" * 32
    sol = "5" + "Kq" * 43
    assert R.hashes_in(f"Koinly {evm.upper()} x", None) == {"ab" * 32}
    assert R.hashes_in(f"txid:{btc};") == {btc}
    assert R.hashes_in(f"sig {sol}") == {sol.lower()}
    assert R.hashes_in("0x" + "a" * 65, "abc", "") == set()  # zu lang: kein Hash


def test_reconcile_rules():
    h = "0x" + "11" * 32
    D = Decimal
    # Gebühr im Abgang enthalten (Import bucht Menge + Gebühr als einen Abgang)
    idx = R.HashIndex([_Tx("I1", f"hash {h}", type="withdrawal", from_account="W", from_asset="ETH",
                           from_qty=D("1.05"))], [])
    rows = [_Row(0, kind=M.WITHDRAWAL, txhash=h, out_sym="ETH", out_qty=D("1"), fee_sym="ETH", fee_qty=D("0.05"))]
    res = R.reconcile(rows, idx, lambda s: s if s == "ETH" else None, ["ETH"])
    assert res.rows[0].state == "full" and res.accounts == {"W": 1}
    # gleiche Menge unter anderem Asset → teilweise, mit Hinweis auf die Zuordnung
    idx = R.HashIndex([_Tx("I2", h, type="deposit", to_account="W", to_asset="WETH", to_qty=D("2"))], [])
    rows = [_Row(0, kind=M.DEPOSIT, txhash=h, in_sym="ETH", in_qty=D("2"))]
    res = R.reconcile(rows, idx, lambda s: s if s == "ETH" else None, ["ETH", "WETH"])
    assert res.rows[0].state == "partial" and res.rows[0].other_asset == {"ETH": "WETH"}
    # zwei unbekannte Tokens gleicher Menge: vorhanden, aber nichts gelernt (nicht eindeutig)
    idx = R.HashIndex([_Tx("I3", h, type="deposit", to_account="W", to_asset="AAA", to_qty=D("5")),
                       _Tx("I4", h, type="deposit", to_account="W", to_asset="BBB", to_qty=D("5"))], [])
    rows = [_Row(0, kind=M.DEPOSIT, txhash=h, in_sym="X@ETH:0x1", in_qty=D("5")),
            _Row(1, kind=M.DEPOSIT, txhash=h, in_sym="Y@ETH:0x2", in_qty=D("5")),
            _Row(2, kind=M.DEPOSIT, txhash=h, in_sym="Z@ETH:0x3", in_qty=D("5"))]
    res = R.reconcile(rows, idx, lambda s: None, ["AAA", "BBB"])
    # zwei Gegenbuchungen decken zwei Beine – das dritte bleibt offen; gelernt wird nichts (nicht eindeutig)
    assert sorted(r.state for r in res.rows.values()) == ["full", "full", "partial"] and res.learned == {}
    # App-Buchung (Journal) mit Hash; mehrteiliger Vorgang ohne Beine: Hash genügt
    idx = R.HashIndex([], [{"tx_id": "PF-1", "tx_hash": h[2:], "type": "deposit", "tag": None, "to_account": "W",
                            "to_asset": "ETH", "to_qty": "3", "from_account": None, "from_asset": None,
                            "from_qty": None, "fee_asset": None, "fee_qty": None}])
    rows = [_Row(0, kind=M.DEPOSIT, txhash=h, in_sym="ETH", in_qty=D("3")),
            _Row(1, kind=M.REVIEW, txhash=h, note="mehrere Bewegungen")]
    res = R.reconcile(rows, idx, lambda s: s if s == "ETH" else None, ["ETH"])
    assert (res.rows[0].state, res.rows[0].origin, res.rows[1].state) == ("full", "journal", "full")
    # unbekannter Token, eindeutig: gelernt (nur bekannte Assets)
    idx = R.HashIndex([_Tx("I5", h, type="deposit", to_account="W", to_asset="USDC", to_qty=D("7"))], [])
    rows = [_Row(0, kind=M.DEPOSIT, txhash=h, in_sym="USDC@ETH:0xabc", in_qty=D("7.00001"))]
    assert R.reconcile(rows, idx, lambda s: None, ["USDC"]).learned == {"USDC@ETH:0XABC": "USDC"}
    assert R.reconcile(rows, idx, lambda s: None, []).learned == {}


def test_reconcile_several_events_in_one_blockchain_transaction():
    """AP3: verschiedene Vorgänge derselben Blockchain-Transaktion verschmelzen nicht; jede Gegenbuchung zählt einmal."""
    h = "0x" + "22" * 32
    D = Decimal
    # Swap in einer Transaktion: Abgang USDC, Zugang ETH, Gebühr ETH – je Bein genau ein Gegenstück
    idx = R.HashIndex([_Tx("S1", h, type="trade", from_account="W", from_asset="USDC", from_qty=D("100"),
                           to_account="W", to_asset="ETH", to_qty=D("0.05"))], [])
    rows = [_Row(0, kind=M.WITHDRAWAL, txhash=h, out_sym="USDC", out_qty=D("100")),
            _Row(1, kind=M.DEPOSIT, txhash=h, in_sym="ETH", in_qty=D("0.05")),
            _Row(2, kind=M.DEPOSIT, txhash=h, in_sym="ETH", in_qty=D("0.05"))]  # zweiter, gleicher Zugang
    res = R.reconcile(rows, idx, lambda s: s if s in ("USDC", "ETH") else None, ["USDC", "ETH"])
    assert res.rows[0].state == "full"
    states = sorted(res.rows[i].state for i in (1, 2) if i in res.rows)
    # die beiden ETH-Zugänge (zusammen 0,1) passen nicht auf die eine gebuchte Seite (0,05): nicht „vorhanden“
    assert "full" not in states


def test_reconcile_two_identical_events_only_one_booked():
    """AP3: zwei gleiche Abgänge in einer Transaktion, nur einer gebucht → nicht beide als vorhanden werten."""
    h = "0x" + "33" * 32
    D = Decimal
    idx = R.HashIndex([_Tx("B1", h, type="withdrawal", from_account="W", from_asset="ETH", from_qty=D("1"))], [])
    rows = [_Row(0, kind=M.WITHDRAWAL, txhash=h, out_sym="ETH", out_qty=D("1")),
            _Row(1, kind=M.WITHDRAWAL, txhash=h, out_sym="ETH", out_qty=D("1"))]
    res = R.reconcile(rows, idx, lambda s: s if s == "ETH" else None, ["ETH"])
    full = [i for i, r in res.rows.items() if r.state == "full"]
    assert len(full) < 2  # höchstens einer gilt als vorhanden – der zweite wird nicht still verworfen
