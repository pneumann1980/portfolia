"""CSV-Import: Leser, Profile, Vorschau, Übernahme, Transfer-Abgleich, Dubletten, Rückgängig, Upload-Sicherheit."""

import io
import os
import re
import shutil
import time
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.csvimport import model as M
from app.csvimport.model import ParseOptions
from app.csvimport.profiles import PROFILES, detect, header_matcher
from app.csvimport.reader import CsvError, num, parse_ts, read_table, zone
from app.csvimport.service import csv_service, rec_from_json, rec_to_json
from app.jobs import tasks
from app.main import build_app

D = Decimal
DATA = Path(__file__).resolve().parent / "data" / "csv"
SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"


# ----------------------------------------------------------------------------------------------------
# Leser
# ----------------------------------------------------------------------------------------------------

def test_reader_encoding_delimiter_preamble_and_duplicate_headers():
    raw = "Export vom 01.02.2024\n\nDatum;Menge;Währung;Menge;Währung\n01.02.2024;1.234,56;EUR;2;BTC\n"
    t = read_table(raw.encode("cp1252"))
    assert t.delimiter == ";" and t.encoding == "cp1252" and t.header_line == 3
    assert t.keys == ["datum", "menge", "währung", "menge_2", "währung_2"]
    (ln, row), = list(t.dicts())
    assert ln == 4 and row["währung_2"] == "BTC"
    bom = read_table("﻿a,b,c\n1,2,3\n".encode())
    assert bom.keys == ["a", "b", "c"] and bom.encoding == "utf-8-sig"


@pytest.mark.parametrize("data", [b"PK\x03\x04xxxx", b"", b"   \n"])
def test_reader_rejects_zip_and_empty(data):
    with pytest.raises(CsvError):
        read_table(data)


@pytest.mark.parametrize(("raw", "decimal", "expected"), [
    ("1234.56", ".", "1234.56"), ("1.234,56", ",", "1234.56"), ("1,234.56", ".", "1234.56"),
    ("1,5", ",", "1.5"), ("1,234", ".", "1234"), ("1,234", ",", "1.234"), ("€1,234.00", ".", "1234.00"),
    ("-0.5 BTC", ".", "-0.5"), ("(12.5)", ".", "-12.5"), ("−3", ".", "-3"), ("1E-8", ".", "1E-8"),
    ("5.000", ",", "5000"), ("1 234,5", ",", "1234.5"), ("-", ".", None), ("", ".", None), ("n/a", ".", None),
])
def test_number_formats(raw, decimal, expected):
    v = num(raw, decimal)
    assert (v is None and expected is None) or v == D(expected)


@pytest.mark.parametrize(("raw", "tz", "expected", "date_only"), [
    ("2024-03-18T09:32:55Z", "UTC", "2024-03-18T09:32:55", False),
    ("2024-03-18T09:32:55+01:00", "UTC", "2024-03-18T08:32:55", False),
    ("2024-03-18 09:32:55 UTC", "Europe/Berlin", "2024-03-18T09:32:55", False),
    ("2024-03-18 10:32:55", "Europe/Berlin", "2024-03-18T09:32:55", False),
    ("2024-07-18 10:32:55", "Europe/Berlin", "2024-07-18T08:32:55", False),
    ("18.03.2024 10:32", "Europe/Berlin", "2024-03-18T09:32:00", False),
    ("03/18/2024 09:32:55", "UTC", "2024-03-18T09:32:55", False),
    ("1710754375", "UTC", "2024-03-18T09:32:55", False),
    ("1710754375000", "UTC", "2024-03-18T09:32:55", False),
    ("Mon Mar 18 2024 10:32:55 GMT+0100 (Mitteleuropäische Normalzeit)", "UTC", "2024-03-18T09:32:55", False),
    ("2024-03-18", "UTC", "2024-03-18T12:00:00", True),
])
def test_timestamps(raw, tz, expected, date_only):
    ts, only = parse_ts(raw, zone(tz))
    assert ts.astimezone(UTC).replace(tzinfo=None).isoformat() == expected
    assert only is date_only


# ----------------------------------------------------------------------------------------------------
# Profile
# ----------------------------------------------------------------------------------------------------

def parse(name, **opts):
    data = (DATA / name).read_bytes()
    t = read_table(data, header_matcher())
    p = detect(t.keys)
    assert p is not None, name
    o = ParseOptions(tz=zone(p.tz), decimal=p.decimal, account=opts.pop("account", p.account or "Konto"),
                     filename=name, **opts)
    return p, p.parse(t, o)


@pytest.mark.parametrize(("name", "pid"), [
    ("binance.csv", "binance"), ("bitpanda.csv", "bitpanda"), ("kraken.csv", "kraken"), ("coinbase.csv", "coinbase"),
    ("cryptocom.csv", "cryptocom"), ("ledger.csv", "ledger"), ("koinly.csv", "koinly"), ("electrum.csv", "electrum"),
    ("portfolia.csv", "portfolia"),
])
def test_profile_detection(name, pid):
    p, res = parse(name)
    assert p.id == pid
    assert res.recs and res.rows_read


def test_generic_file_is_not_guessed():
    t = read_table((DATA / "generic.csv").read_bytes(), header_matcher())
    assert detect(t.keys) is None


def by_line(res):
    out = {}
    for r in res.recs:
        out.setdefault(r.line, []).append(r)
    return out


def test_binance_statement():
    _, res = parse("binance.csv")
    recs = by_line(res)
    buy, = recs[3]
    assert (buy.kind, buy.out_sym, buy.out_qty, buy.in_sym, buy.in_qty, buy.fee_sym, buy.fee_qty) == \
        (M.TRADE, "EUR", D(800), "BTC", D("0.02"), "BNB", D("0.002"))
    assert recs[6][0].tag == "interest" and recs[12][0].tag == "bonus"
    assert recs[11][0].kind == M.WITHDRAWAL and recs[11][0].out_qty == D("0.0101")
    dust = recs[13]  # Kleinstbeträge: Zeilen paarweise
    assert [(r.out_sym, r.in_qty) for r in dust] == [("DOGE", D("0.003")), ("SHIB", D("0.002"))]
    assert res.skipped["Binance: interne Umbuchung (Spot/Earn/Funding/Staking)"] == 2
    assert res.skipped["Binance: Futures/Margin/Optionen (nicht unterstützt)"] == 1
    assert [ln for ln, _ in res.errors] == [18]  # unbekannte Operation wird gemeldet, nicht geraten
    assert len({r.ext_id for r in res.recs}) == len(res.recs)


def test_bitpanda_preamble_classes_and_values():
    _, res = parse("bitpanda.csv")
    recs = by_line(res)
    btc, = recs[5]
    assert btc.ts == datetime(2024, 2, 2, 9, tzinfo=UTC) and btc.fee_sym == "BEST" and btc.value == D("250.00")
    assert recs[6][0].class_hint == {"AAPL": "security"}
    reward, = recs[7]
    assert reward.kind == M.DEPOSIT and reward.tag == "reward"
    sell, = recs[8]
    assert (sell.out_sym, sell.in_sym) == ("BTC", "EUR")
    assert recs[4][0].in_sym == "EUR" and recs[4][0].value is None  # Fiat-Einzahlung


def test_kraken_groups_trades_and_normalizes_assets():
    _, res = parse("kraken.csv")
    recs = by_line(res)
    trade, = recs[3]
    assert (trade.out_sym, trade.out_qty, trade.in_sym, trade.fee_sym, trade.fee_qty) == \
        ("EUR", D("500.0000"), "BTC", "EUR", D("1.3000"))
    assert recs[5][0].in_sym == "DOT" and recs[5][0].tag == "staking"  # DOT.S → DOT
    assert recs[8][0].fee_qty == D("0.0001") and recs[8][0].out_sym == "BTC"
    assert [M.kraken_symbol(x) for x in ("DOT28.S", "XXBT", "ETH2.S", "USDC.M")] == ["DOT", "BTC", "ETH", "USDC"]
    assert res.skipped["Kraken: interne Umbuchung (Spot/Staking/Earn)"] == 2


def test_coinbase_fees_convert_and_rewards():
    _, res = parse("coinbase.csv")
    recs = by_line(res)
    buy, = recs[4]
    assert (buy.out_sym, buy.out_qty, buy.fee_sym, buy.fee_qty) == ("EUR", D("300.00"), "EUR", D("4.50"))
    conv, = recs[5]
    assert (conv.out_sym, conv.in_sym, conv.in_qty, conv.fee_qty) == ("ETH", "USDC", D("150.123"), None)
    sell, = recs[8]
    assert (sell.in_qty, sell.fee_qty) == (D("34.00"), D("0.50"))  # brutto + Gebühr = netto 33,50
    assert recs[6][0].tag == "staking" and recs[9][0].tag == "reward"


def test_cryptocom_dust_split_by_value():
    _, res = parse("cryptocom.csv")
    dust = [r for r in res.recs if r.out_sym in ("DOGE", "ADA")]
    assert [(r.out_sym, r.in_sym, r.in_qty) for r in dust] == [("DOGE", "CRO", D("3.2")), ("ADA", "CRO", D("4.8"))]
    assert sum(r.in_qty for r in dust) == D(8)


def test_ledger_fee_split_and_status():
    _, res = parse("ledger.csv")
    recs = by_line(res)
    out, = recs[3]
    assert (out.kind, out.out_qty, out.fee_qty, out.txhash) == (M.WITHDRAWAL, D("0.0029"), D("0.0001"), "hash-b")
    assert recs[4][0].kind == M.FEE and 5 not in recs  # fehlgeschlagene Operation übersprungen
    _, per_acc = parse("ledger.csv", mapping={"accounts_from_file": True})
    assert {r.account for r in per_acc.recs} == {"Bitcoin 1", "Ethereum 1", "Polkadot 1"}


def test_koinly_wallets_transfers_labels():
    _, res = parse("koinly.csv")
    recs = by_line(res)
    tr, = recs[3]
    assert (tr.kind, tr.account, tr.to_account, tr.out_qty, tr.in_qty) == \
        (M.TRANSFER, "Kraken", "Ledger", D("0.02"), D("0.0199"))
    assert recs[4][0].tag == "reward" and recs[6][0].tag == "gift"
    assert recs[2][0].value == D(1000) and recs[2][0].value_ccy == "EUR"


def test_electrum_sent_amount_excludes_fee():
    _, res = parse("electrum.csv")
    out = next(r for r in res.recs if r.kind == M.WITHDRAWAL)
    assert (out.out_qty, out.fee_qty, out.out_sym) == (D("0.0050"), D("0.0001"), "BTC")


def test_rec_roundtrip():
    _, res = parse("coinbase.csv")
    for r in res.recs:
        assert rec_from_json(rec_to_json(r)) == r


def test_all_profiles_have_hints():
    assert all(p.hint and p.label for p in PROFILES.values())


# ----------------------------------------------------------------------------------------------------
# Web: Vorschau, Übernahme, Transfers, Rückgängig
# ----------------------------------------------------------------------------------------------------

def _client(config):
    return TestClient(build_app(config, start_scheduler=False))


@pytest.fixture
def client(config):
    with _client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


@pytest.fixture
def sample_client(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with _client(config) as c:
        tasks.import_check(c.app.state.ctx, "test")
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def upload(c, name, data=None, **form):
    data = data if data is not None else (DATA / name).read_bytes()
    r = c.post("/journal/csv", data={"csrf_token": c.token, "profile": "auto", **form},
               files={"file": (name, data, "text/csv")}, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    return int(re.search(r"/journal/csv/(\d+)", r.headers["location"]).group(1)), r.headers["location"]


def post(c, url, **data):
    return c.post(url, data={"csrf_token": c.token, **data}, follow_redirects=False)


def create_unknown_assets(c, bid):
    page = c.get(f"/journal/csv/{bid}").text
    form = {}
    for i, s in re.findall(r'name="sym_(\d+)" value="([^"]+)"', page):
        qid = re.search(rf'name="qid_{i}" value="([^"]*)"', page).group(1)
        form |= {f"sym_{i}": s, f"act_{i}": "new", f"id_{i}": s, f"name_{i}": s, f"class_{i}": "crypto",
                 f"qid_{i}": qid}
    r = post(c, f"/journal/csv/{bid}/symbols", **form)
    assert r.status_code == 303, r.text[:1000]


def rows(c, bid):
    return {rc.line: rc for rc in csv_service(c.app.state.ctx).rows(bid)}


def journal(c, where="1=1"):
    return [dict(r) for r in c.app.state.ctx.db.q(f"SELECT * FROM journal_tx WHERE {where} ORDER BY id")]


def test_upload_preview_commit_and_reimport_is_idempotent(client):
    bid, loc = upload(client, "binance.csv")
    page = client.get(loc)
    assert page.status_code == 200 and "Assets zuordnen" in page.text and "Mystery Operation" in page.text
    create_unknown_assets(client, bid)
    rs = rows(client, bid)
    assert rs[3].row["type"] == "buy" and rs[3].row["value_eur"] == "800" and rs[3].value_src == "EUR"
    # Wert der Zinszeile aus dem Kurs des Kaufs derselben Datei (keine gespeicherten Kurse im Test)
    assert rs[6].row["value_eur"] == "0.4" and "Kurs aus der Datei" in rs[6].value_src
    assert rs[12].status == "invalid" and rs[12].missing_value  # USDT-Bonus ohne Kurs
    r = post(client, f"/journal/csv/{bid}/commit")
    assert r.status_code == 303 and "n=5" in r.headers["location"]
    txs = journal(client, "source='csv:binance'")
    assert len(txs) == 5 and all(t["tx_id"].startswith("PF-C-") and t["batch_id"] == bid for t in txs)
    assert csv_service(client.app.state.ctx).batch(bid)["status"] == "partial"
    # Wert nachtragen und die offene Zeile nachschieben
    r = post(client, f"/journal/csv/{bid}/rows", **{f"val_{rs[12].idx}": "0,46"})
    assert r.status_code == 303
    assert rows(client, bid)[12].status == "new"
    r = post(client, f"/journal/csv/{bid}/commit")
    assert "n=1" in r.headers["location"]
    bonus = journal(client, "to_asset='USDT'")[0]
    assert (bonus["tag"], bonus["value_eur"], bonus["value_source"]) == ("bonus", "0.46", "Eingabe")
    # dieselbe Datei erneut: alles bekannt, nichts zu übernehmen
    bid2, _ = upload(client, "binance.csv")
    st = {rc.status for rc in rows(client, bid2).values()}
    assert "new" not in st and "known" in st
    assert "bereits importiert" in client.get(f"/journal/csv/{bid2}").text
    # Buchungen erscheinen unter „Buchungen“ mit Quelle CSV und sind bearbeitbar
    page = client.get("/journal").text
    assert "CSV · Binance" in page and f'/journal/csv/{bid}"' in page
    assert client.get(f"/journal/{txs[0]['tx_id']}/edit").status_code == 200


def test_transfer_matching_across_imports_and_revert(client):
    b1, _ = upload(client, "binance.csv")
    create_unknown_assets(client, b1)
    post(client, f"/journal/csv/{b1}/commit")
    b2, _ = upload(client, "ledger.csv")
    create_unknown_assets(client, b2)
    dep = rows(client, b2)[2]
    assert dep.pair_ref and dep.pair_ref.startswith("j:PF-C-") and dep.pair_conf == "hoch"
    assert "Überträge zwischen eigenen Konten" in client.get(f"/journal/csv/{b2}").text
    r = post(client, f"/journal/csv/{b2}/commit")
    assert "t=1" in r.headers["location"]
    tr, = journal(client, "source='transfer'")
    assert (tr["from_account"], tr["to_account"], tr["from_qty"], tr["to_qty"], tr["status"]) == \
        ("Binance", "Ledger", "0.0101", "0.01", "active")
    assert {t["status"] for t in journal(client, f"merged_into='{tr['tx_id']}'")} == {"merged"}
    ctx = client.app.state.ctx
    led = ctx.ledger()
    assert led.balances[("Ledger", "BTC")] == D("0.0070")  # 0,01 − 0,0029 − 0,0001 Gebühr
    lots = list(led.lots_for("BTC", "Ledger"))
    assert lots and all(lot.acq_date.year == 2024 and lot.acq_date.month == 1 for lot in lots)  # Haltefrist ab Kauf
    # Transfer in „Buchungen“ auflösen → beide Einzelbuchungen gelten wieder
    r = post(client, f"/journal/{tr['tx_id']}/delete")
    assert r.status_code == 303 and "unpaired" in r.headers["location"]
    assert journal(client, f"tx_id='{tr['tx_id']}'")[0]["status"] == "reverted"
    assert not journal(client, "status='merged'")
    # erneut übernehmen geht nicht doppelt; Stapel 1 rückgängig → nur Ledger bleibt
    r = post(client, f"/journal/csv/{b1}/revert")
    assert r.status_code == 303
    assert {t["status"] for t in journal(client, f"batch_id={b1}")} == {"reverted"}
    assert all(t["status"] == "active" for t in journal(client, f"batch_id={b2} AND source<>'transfer'"))
    # rückgängig gemachter Stapel lässt sich wieder öffnen und erneut übernehmen
    assert post(client, f"/journal/csv/{b1}/reopen").status_code == 303
    assert rows(client, b1)[3].status == "new"
    r = post(client, f"/journal/csv/{b1}/commit")
    assert r.status_code == 303 and "t=1" in r.headers["location"]  # Transfer mit Ledger-Zugang erneut erkannt


def test_revert_of_partner_batch_dissolves_transfer(client):
    b1, _ = upload(client, "binance.csv")
    create_unknown_assets(client, b1)
    post(client, f"/journal/csv/{b1}/commit")
    b2, _ = upload(client, "ledger.csv")
    create_unknown_assets(client, b2)
    post(client, f"/journal/csv/{b2}/commit")
    r = post(client, f"/journal/csv/{b2}/revert")
    assert r.status_code == 303
    assert not journal(client, "source='transfer' AND status='active'")
    withdrawal = journal(client, f"batch_id={b1} AND type='withdrawal'")[0]
    assert withdrawal["status"] == "active" and withdrawal["merged_into"] is None


def test_duplicates_and_cutoff_against_curated_import(sample_client):
    c = sample_client
    csv = ("tx_id,datetime,type,tag,from_account,from_asset,from_qty,to_account,to_asset,to_qty,value_eur\n"
           "A1,2024-05-01T02:00:00Z,deposit,staking,,,,Börse X,ETH,0.0030,8.40\n"
           "A2,2024-05-01T04:00:00Z,deposit,staking,,,,Wallet Y,ETH,0.0030,8.40\n"
           "A3,2026-09-21T10:00:00Z,deposit,staking,,,,Börse X,ETH,0.0030,9\n")
    bid, _ = upload(c, "eigene.csv", csv.encode())
    rs = rows(c, bid)
    assert rs[2].status == "before" and rs[4].status == "new"  # Stichtag = Stand des Imports (19.09.2026)
    bid2, _ = upload(c, "eigene2.csv", csv.encode(), cutoff="", cutoff_set="1")
    rs = rows(c, bid2)
    assert rs[2].status == "duplicate" and rs[2].dup_same_account and not rs[2].include()
    assert rs[3].status == "duplicate" and not rs[3].dup_same_account and rs[3].include()  # Zeitzonenversatz 2 h
    assert "DEMO-00040" in rs[2].dup_of


def test_own_export_reimported_as_csv_creates_no_duplicates(sample_client):
    c = sample_client
    z = zipfile.ZipFile(io.BytesIO(c.get("/journal/export.zip").content))
    bid, _ = upload(c, "transactions.csv", z.read("transactions.csv"), cutoff="", cutoff_set="1")
    ov = csv_service(c.app.state.ctx).overview(bid)
    assert ov["to_commit"] == 0
    assert ov["counts"].get("duplicate", 0) > 100


def test_generic_mapping_flow(client):
    bid, loc = upload(client, "generic.csv")
    assert loc.endswith("/mapping")
    page = client.get(loc).text
    assert "Spalten zuordnen" in page and "Eingang Währung" in page
    form = {"name": "Meine Börse", "col_date": "Datum", "col_type": "Art", "col_in_qty": "Eingang",
            "col_in_sym": "Eingang Währung", "col_out_qty": "Ausgang", "col_out_sym": "Ausgang Währung",
            "col_fee_qty": "Gebühr", "col_fee_sym": "Gebühr Währung", "col_ext_id": "ID", "tz": "Europe/Berlin",
            "decimal": ",", "lbl_0": "Staking", "map_0": "deposit:staking"}
    r = post(client, f"/journal/csv/{bid}/mapping", **form)
    assert r.status_code == 303, r.text[:800]
    create_unknown_assets(client, bid)
    rs = rows(client, bid)
    assert (rs[2].row["type"], rs[2].row["to_qty"], rs[2].row["from_qty"], rs[2].row["fee_qty"]) == \
        ("buy", "0.01", "500", "0.5")
    assert rs[2].row["datetime"] == "2024-08-01T08:00:00Z"
    assert (rs[3].row["type"], rs[3].row["tag"], rs[3].row["to_qty"]) == ("deposit", "staking", "1.5")
    # gespeicherte Zuordnung erkennt dieselbe Kopfzeile beim nächsten Mal automatisch
    bid2, loc2 = upload(client, "generic.csv")
    assert not loc2.endswith("/mapping")
    assert csv_service(client.app.state.ctx).batch(bid2)["profile"].startswith("mapping:")


def test_symbol_mapping_to_existing_asset_and_ignore(sample_client):
    c = sample_client
    csv = ("Date,Sent Amount,Sent Currency,Received Amount,Received Currency,Fee Amount,Fee Currency,"
           "Net Worth Amount,Net Worth Currency,Label,Description,TxHash\n"
           "2026-09-22 10:00 UTC,,,0.5,XBTC,,,,,reward,,\n"
           "2026-09-22 11:00 UTC,,,100,SPAM,,,,,airdrop,,\n"
           "2026-09-22 12:00 UTC,100,EUR,0.001,BTC,,,,,,,\n")
    bid, _ = upload(c, "koinly-universal.csv", csv.encode(), account="Börse X")
    ov = csv_service(c.app.state.ctx).overview(bid)
    assert {u["symbol"] for u in ov["unknown"]} == {"XBTC", "SPAM"}
    r = post(c, f"/journal/csv/{bid}/symbols", sym_0="XBTC", act_0="map", asset_0="Bitcoin", sym_1="SPAM",
             act_1="ignore")
    assert r.status_code == 303
    rs = rows(c, bid)
    assert rs[2].row["to_asset"] == "BTC" and rs[3].status == "ignored"
    assert rs[4].row["type"] == "buy" and rs[4].row["to_asset"] == "BTC"  # BTC direkt über die ID erkannt
    assert "SPAM" in c.get("/journal/csv").text  # gespeicherte Zuordnungen sichtbar


def test_discard_only_without_committed_rows(client):
    bid, _ = upload(client, "electrum.csv")
    create_unknown_assets(client, bid)
    r = post(client, f"/journal/csv/{bid}/discard")
    assert r.status_code == 303
    assert csv_service(client.app.state.ctx).batch(bid) is None


def test_original_file_download(client):
    bid, _ = upload(client, "kraken.csv")
    r = client.get(f"/journal/csv/{bid}/file")
    assert r.status_code == 200 and r.content == (DATA / "kraken.csv").read_bytes()
    assert "attachment" in r.headers["content-disposition"]


def test_upload_requires_csrf_and_respects_size_limit(client, monkeypatch):
    data = (DATA / "kraken.csv").read_bytes()
    r = client.post("/journal/csv", data={"profile": "auto"}, files={"file": ("k.csv", data, "text/csv")})
    assert r.status_code == 403
    r = client.post("/journal/csv", data={"csrf_token": "falsch", "profile": "auto"},
                    files={"file": ("k.csv", data, "text/csv")})
    assert r.status_code == 403
    from app.web import security

    monkeypatch.setitem(security.UPLOAD_LIMITS, "/journal/csv", 200)
    r = client.post("/journal/csv", data={"csrf_token": client.token}, files={"file": ("k.csv", data, "text/csv")})
    assert r.status_code == 413
    monkeypatch.setitem(security.UPLOAD_LIMITS, "/journal/csv", 26 * 1024 * 1024)
    # normale Formulare bleiben auf 1 MB begrenzt
    r = client.post("/journal/new", data={"csrf_token": client.token, "note": "x" * 1_100_000})
    assert r.status_code == 413


def test_upload_rejects_non_csv(client):
    r = client.post("/journal/csv", data={"csrf_token": client.token, "profile": "auto"},
                    files={"file": ("x.zip", b"PK\x03\x04" + b"0" * 100, "application/zip")})
    assert r.status_code == 400 and "ZIP-Datei" in r.text


def test_hint_for_counterpart_in_curated_import(config):
    from app.importer.zipbuilder import build_zip
    from tests.helpers import ASSETS, tx

    rows_ = [tx("I-1", "2024-01-05T10:00:00Z", "buy", frm=("Börse X", "EUR", "800"), to=("Börse X", "BTC", "0.02"),
                value="800"),
             tx("I-2", "2024-01-08T12:00:00Z", "withdrawal", frm=("Börse X", "BTC", "0.0101"), value="404")]
    dst = config.import_dir / "imp.zip"
    build_zip(dst, transactions=rows_, assets=ASSETS, generated_at="2024-01-10T00:00:00Z", valuation_date="2024-01-09")
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with _client(config) as c:
        assert tasks.import_check(c.app.state.ctx, "test").status == "imported"
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        bid, _ = upload(c, "ledger.csv", cutoff="", cutoff_set="1")
        create_unknown_assets(c, bid)
        dep = rows(c, bid)[2]
        assert dep.pair_ref is None
        assert any("I-2" in w and "kuratierten Import" in w for w in dep.warnings)
