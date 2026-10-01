"""Vorschläge für unbekannte Symbole im Prüf-Stapel – Abgleich mit Datenbasis und lokalem CoinGecko-Katalog.

Ohne Netz: Katalog als Cache-Datei, EVM-Anbieter nachgebildet (siehe ``tests/wallet_fakes.py``).
"""

from __future__ import annotations

import copy
import gzip
import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.csvimport import model as M
from app.csvimport.service import csv_service
from app.csvimport.suggest import Basis, Suggester
from app.datasources import chainhttp as CH
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from app.ledger.models import AssetInfo
from app.prices.sources import Catalog, catalog_path, catalog_state, source_service, split_token
from app.util.timeutil import iso
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
USDC_AVAX = "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e"
FAKE = "0x9999999999999999999999999999999999999999"
RIO = "0x94a8b4ee5cd64c79d0ee816f467ea73009f51aa0"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

COINS = [
    {"id": "usd-coin", "symbol": "usdc", "name": "USDC",
     "platforms": {"ethereum": USDC, "avalanche": USDC_AVAX, "solana": MINT}},
    {"id": "realio-network", "symbol": "rio", "name": "Realio Network", "platforms": {"ethereum": RIO}},
    {"id": "rio-defi", "symbol": "rio", "name": "RioDeFi", "platforms": {"binance-smart-chain": "0x" + "ab" * 20}},
    {"id": "nacho-the-kat", "symbol": "nacho", "name": "Nacho the Kat", "platforms": {"kaspa": "NACHO"}},
    {"id": "supra", "symbol": "supra", "name": "Supra", "platforms": {}},
    {"id": "best-a", "symbol": "best", "name": "Best A", "platforms": {}},
    {"id": "best-b", "symbol": "best", "name": "Best B", "platforms": {"binance-smart-chain": "0x" + "cd" * 20}},
]


def asset(aid: str, name: str = "", cls: str = "crypto", qs: str = "none", qid: str | None = None,
          **kw: object) -> AssetInfo:
    return AssetInfo(aid, name or aid, cls, quote_source=qs, quote_id=qid, **kw)  # type: ignore[arg-type]


def entry(key: str, *, count: int = 1, spam: bool = False, ambiguous: bool = False, dirs: tuple = ("in",),
          kinds: tuple = (M.DEPOSIT,), hint: str | None = None) -> dict:
    return {"symbol": key.upper(), "display": key, "count": count, "ambiguous": ambiguous, "hint": hint,
            "spam": spam, "dirs": set(dirs), "kinds": set(kinds)}


def run(unknown, known=(), saved=None, rows=None, catalog=True, names=None, accounts=()):
    basis = Basis({a.asset_id: a for a in known}, saved or {}, rows or {})
    cat = Catalog(COINS, datetime.now(UTC)) if catalog else None
    return Suggester(basis, cat, names, accounts).run(unknown)


def write_catalog(c, coins=COINS, age: timedelta = timedelta(0)) -> None:
    path = catalog_path(ctx(c))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps({"fetched_at": iso(datetime.now(UTC) - age), "coins": coins}).encode()))


# ----------------------------------------------------------------------------------------------------
# Regeln (ohne App)
# ----------------------------------------------------------------------------------------------------

def test_token_resolved_by_contract_never_by_symbol():
    known = [asset("USDC", "USD Coin", qs="coingecko", qid="usd-coin")]
    out = run([entry(f"USDC@ETH:{USDC}"), entry(f"USDC@ETH:{FAKE}"), entry(f"USDC@ETH:{FAKE[:-1]}8", spam=True),
               entry(f"USDC@ETH:{FAKE[:-1]}7", dirs=("in", "out"), kinds=(M.TRADE, M.REVIEW))], known)
    real = out[f"USDC@ETH:{USDC}".upper()]
    assert (real.action, real.asset_id, real.confidence, real.prefill) == ("map", "USDC", "hoch", True)
    assert "usd-coin" in real.reason and "Ethereum" in real.reason
    # gleiches Symbol, unbekannter Contract: nie dem echten USDC zugeordnet
    only_in = out[f"USDC@ETH:{FAKE}".upper()]
    assert (only_in.action, only_in.confidence, only_in.asset_id) == ("ignore", "mittel", "")
    assert "nur erhalten" in only_in.reason
    spam = out[f"USDC@ETH:{FAKE[:-1]}8".upper()]
    assert (spam.action, spam.confidence) == ("ignore", "hoch")
    moved = out[f"USDC@ETH:{FAKE[:-1]}7".upper()]
    assert (moved.action, moved.prefill) == ("", False) and "Nicht bei CoinGecko gelistet" in moved.reason


def test_token_new_asset_and_second_chain_maps_to_it():
    out = run([entry(f"USDC@ETH:{USDC}", count=5), entry(f"USDC@AVAX:{USDC_AVAX}"),
               entry(f"USDC@SOL:{MINT}")])
    first = out[f"USDC@ETH:{USDC}".upper()]
    assert (first.action, first.new_id, first.name, first.qid) == ("new", "USDC", "USDC", "usd-coin")
    for key in (f"USDC@AVAX:{USDC_AVAX}", f"USDC@SOL:{MINT}"):
        s = out[key.upper()]
        assert (s.action, s.asset_id, s.confidence) == ("map", "USDC", "hoch"), key


def test_token_matches_existing_asset_without_source_and_conflicts():
    # gleiches Symbol ohne Kursquelle → zuordnen, Kursquelle übernehmen (mittel)
    out = run([entry(f"RIO@ETH:{RIO}")], [asset("RIO", "Realio")])
    s = out[f"RIO@ETH:{RIO}".upper()]
    assert (s.action, s.asset_id, s.source_id, s.confidence) == ("map", "RIO", "realio-network", "mittel")
    # Felder für „neu anlegen“ vorbereitet, falls das vorhandene Asset doch ein anderer Token ist
    assert (s.new_id, s.name, s.qid, s.verified) == ("RIO#2", "Realio Network", "realio-network", True)
    # gleiches Symbol, aber anderer Coin → eigenes Asset RIO#2 mit Warnung
    out = run([entry(f"RIO@ETH:{RIO}")], [asset("RIO", "RioDeFi", qs="coingecko", qid="rio-defi")])
    s = out[f"RIO@ETH:{RIO}".upper()]
    assert (s.action, s.new_id, s.qid) == ("new", "RIO#2", "realio-network") and "rio-defi" in s.warning
    # offener Kursquellen-Vorschlag wird durch den Contract bestätigt
    rows = {"RIOX": {"status": "suggested", "quote_id": "realio-network"}}
    out = run([entry(f"RIO@ETH:{RIO}")], [asset("RIOX", "Realio alt")], rows=rows)
    s = out[f"RIO@ETH:{RIO}".upper()]
    assert (s.action, s.asset_id, s.source_id, s.confidence) == ("map", "RIOX", "realio-network", "hoch")
    # Spam-Asset gleichen Symbols wird nicht vorgeschlagen
    out = run([entry(f"RIO@ETH:{RIO}")], [asset("RIO", "Spam", status="spam")])
    s = out[f"RIO@ETH:{RIO}".upper()]
    assert (s.action, s.new_id) == ("new", "RIO#2")


def test_saved_contract_and_case_insensitive_mints():
    saved = {f"OLDRIO@ETH:{RIO}".upper(): "RIO", f"JUNK@ETH:{FAKE}".upper(): None}
    out = run([entry(f"RIO@ETH:{RIO}"), entry(f"NEWJUNK@ETH:{FAKE}", dirs=("in", "out"))], [asset("RIO")],
              saved=saved)
    assert (out[f"RIO@ETH:{RIO}".upper()].action, out[f"RIO@ETH:{RIO}".upper()].asset_id) == ("map", "RIO")
    assert out[f"NEWJUNK@ETH:{FAKE}".upper()].action == "ignore"
    # Solana-Mint aus einer gespeicherten (großgeschriebenen) Kennung wird trotzdem gefunden
    cat = Catalog(COINS, datetime.now(UTC))
    assert [c["id"] for c in cat.for_token("SOL", MINT.upper())] == ["usd-coin"]
    assert [c["id"] for c in cat.for_token("KAS", "nacho")] == ["nacho-the-kat"]
    assert cat.for_token("BSC", USDC) == [] and cat.for_token("XYZ", USDC) == []
    assert split_token(f"usdc@eth:{USDC}") == ("usdc", "ETH", USDC) and split_token("BTC") is None


def test_plain_symbols():
    known = [asset("BEST", "Best A", qs="coingecko", qid="best-a"), asset("BEST#2", "Best Spam", status="spam"),
             asset("XBTC", "Bitcoin Asset", aliases=["XBT"])]
    out = run([entry("BEST", ambiguous=True), entry("SUPRA"), entry("BTC"), entry("RIO;123"), entry("WAT"),
               entry("AAPL", hint="security"), entry("FOO")], known, saved={"FOO;9": "XBTC"})
    assert (out["BEST"].action, out["BEST"].asset_id, out["BEST"].confidence) == ("map", "BEST", "mittel")
    assert (out["SUPRA"].action, out["SUPRA"].qid, out["SUPRA"].confidence) == ("new", "supra", "mittel")
    assert (out["BTC"].action, out["BTC"].qid, out["BTC"].confidence) == ("new", "bitcoin", "hoch")
    rio = out["RIO;123"]  # zwei Coins mit dem Symbol → Auswahl, keine Vorbelegung
    assert (rio.action, rio.qid, rio.prefill) == ("new", "", False) and {o[0] for o in rio.options} == \
        {"realio-network", "rio-defi"}
    assert out["WAT"].confidence == "niedrig" and "Kein Coin" in out["WAT"].reason
    assert out["AAPL"].action == "" and out["AAPL"].reason == ""
    assert (out["FOO"].action, out["FOO"].asset_id, out["FOO"].confidence) == ("map", "XBTC", "mittel")
    # Chain-Hinweis aus dem Kontonamen sortiert die Auswahl
    out = run([entry("RIO")], accounts=["MetaMask (BNB)"])
    assert out["RIO"].options[0][0] == "rio-defi"
    # ohne Katalog: nur Datenbasis, Hinweis statt Vermutung
    out = run([entry("SUPRA"), entry(f"RIO@ETH:{RIO}"), entry(f"X@ETH:{FAKE}", spam=True)], catalog=False)
    assert out["SUPRA"].prefill is False and "nicht geladen" in out["SUPRA"].reason
    assert out[f"RIO@ETH:{RIO}".upper()].action == ""
    assert out[f"X@ETH:{FAKE}".upper()].action == "ignore"


def test_free_ids_respect_existing_assets_and_currencies():
    basis = Basis({"usdc": asset("usdc")}, {}, {})
    assert basis.free_id("USDC") == "USDC#2"
    assert basis.free_id("USDC") == "USDC#3"
    assert basis.free_id("EUR") == "EUR#2"
    assert basis.free_id("W$T!") == "WT"
    assert basis.free_id("") == "TOKEN"


# ----------------------------------------------------------------------------------------------------
# Prüf-Stapel einer ETH-Wallet
# ----------------------------------------------------------------------------------------------------

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


class FakeCG:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def coins_list(self):
        self.calls.append("list")
        return COINS

    def markets(self, ids):
        self.calls.append("markets")
        return {}


def eth_batch(c) -> int:
    from app.journal.service import journal_service

    set_provider_key(c, "etherscan", ETHERSCAN_KEY)
    js = journal_service(ctx(c))
    for aid in ("ETH", "BNB", "AVAX"):
        assert not js.save_asset({"asset_id": aid, "name": aid, "asset_class": "crypto", "quote_source": "none"}).errors
    sid = create_wallet(c, "ethereum", A, name="Ledger ETH")
    return int(datasource_service(ctx(c)).sync(sid, "manual")["batch_id"])


def form_from_page(page: str) -> dict[str, str]:
    """Formular so absenden, wie der Browser es mit den Vorbelegungen täte."""
    sec = page[page.index('id="assets"'):]
    sec = sec[:sec.index("</form>")]
    data: dict[str, str] = {}
    for name, value in re.findall(r'<input type="hidden" name="(sym_\d+)" value="([^"]*)"', sec):
        data[name] = value
    for m in re.finditer(r'<input type="text" name="((?:asset|id|name|qid)_\d+)" value="([^"]*)"', sec):
        data[m.group(1)] = m.group(2)
    for m in re.finditer(r'<input type="checkbox" name="(src_\d+)" value="([^"]*)" checked', sec):
        data[m.group(1)] = m.group(2)
    for m in re.finditer(r'<select name="((?:act|class)_\d+)"[^>]*>(.*?)</select>', sec, re.S):
        sel = re.search(r'<option value="([^"]*)"[^>]*selected', m.group(2))
        data[m.group(1)] = sel.group(1) if sel else ""
    return {k: v.replace("&#34;", '"').replace("&amp;", "&") for k, v in data.items()}


def test_batch_prefills_new_asset_from_catalog_and_ignores_fake(client, evm):
    write_catalog(client)
    bid = eth_batch(client)
    page = client.get(f"/journal/csv/{bid}").text
    assert "2 von 2 vorbelegt" in page and "lokal durchsucht" in page and "Quelle:" not in page
    assert "Vorschlag · hoch" in page and "Contract (Ethereum) laut CoinGecko-Katalog: USDC (usd-coin)" in page
    data = form_from_page(page)
    by_sym = {v: k[4:] for k, v in data.items() if k.startswith("sym_")}
    real, fake = by_sym[f"USDC@ETH:{USDC}".upper()], by_sym[f"USDC@ETH:{FAKE}".upper()]
    assert (data[f"act_{real}"], data[f"id_{real}"], data[f"name_{real}"], data[f"qid_{real}"]) == \
        ("new", "USDC", "USDC", "usd-coin")
    assert data[f"act_{fake}"] == "ignore"
    # Fake-Token: keine Vorbelegung aus der Liste bekannter Coins (sonst Kurs des echten USDC), Warnung bleibt
    assert (data[f"qid_{fake}"], data[f"name_{fake}"]) == ("", "USDC")
    assert page.count("das Symbol kann gefälscht sein") == 1  # nur der nicht bestätigte Contract
    r = post(client, f"/journal/csv/{bid}/symbols", **data)
    assert r.status_code == 303, r.text[:2000]
    from app.journal.service import journal_service

    a = journal_service(ctx(client)).known_assets()["USDC"]
    assert (a.quote_source, a.quote_id) == ("coingecko", "usd-coin")
    rows = rows_by_ext(client, bid)
    assert all(v.status == "ignored" for v in rows.values() if v.rec.in_sym == f"USDC@ETH:{FAKE}")
    assert all(v.row and v.row["to_asset"] == "USDC" for v in rows.values()
               if v.rec.in_sym == f"USDC@ETH:{USDC}" and v.rec.kind == M.DEPOSIT)


def test_batch_maps_to_existing_asset_and_confirms_price_source(client, evm):
    write_catalog(client)
    from app.journal.service import journal_service

    js = journal_service(ctx(client))
    assert not js.save_asset({"asset_id": "USDC", "name": "USD Coin", "asset_class": "crypto",
                              "quote_source": "none"}).errors
    # Asset muss gebucht sein, damit es als Kryptowert ohne Kursquelle zählt
    r = post(client, "/journal/new", kind="deposit", date="2026-01-05", account="Bitpanda", asset="USDC", qty="10",
             value_eur="9")
    assert r.status_code == 303, r.text[:1500]
    bid = eth_batch(client)
    page = client.get(f"/journal/csv/{bid}").text
    data = form_from_page(page)
    real = {v: k[4:] for k, v in data.items() if k.startswith("sym_")}[f"USDC@ETH:{USDC}".upper()]
    assert (data[f"act_{real}"], data[f"asset_{real}"], data[f"src_{real}"]) == ("map", "USDC", "usd-coin")
    assert "Vorschlag · mittel" in page and "Kursquelle CoinGecko „usd-coin“ für USDC übernehmen" in page
    # Häkchen entfernt: nur die Zuordnung, keine Kursquelle
    unchecked = {k: v for k, v in data.items() if k != f"src_{real}"}
    assert post(client, f"/journal/csv/{bid}/symbols", **unchecked).status_code == 303
    assert csv_service(ctx(client)).saved_symbols()[f"USDC@ETH:{USDC}".upper()] == "USDC"
    assert ctx(client).db.q1("SELECT * FROM asset_source WHERE asset_id='USDC'") is None
    assert 'id="assets"' not in client.get(f"/journal/csv/{bid}").text  # Symbol jetzt bekannt
    # mit Häkchen: Kursquelle aus dem Contract übernommen
    csv_service(ctx(client)).delete_symbol(f"USDC@ETH:{USDC}".upper())
    csv_service(ctx(client)).evaluate(bid)
    data = form_from_page(client.get(f"/journal/csv/{bid}").text)  # Fake-Token bereits ignoriert
    real = {v: k[4:] for k, v in data.items() if k.startswith("sym_")}[f"USDC@ETH:{USDC}".upper()]
    assert len([k for k in data if k.startswith("sym_")]) == 1 and data[f"src_{real}"] == "usd-coin"
    r = post(client, f"/journal/csv/{bid}/symbols", **data)
    assert r.status_code == 303, r.text[:2000]
    row = ctx(client).db.q1("SELECT * FROM asset_source WHERE asset_id='USDC'")
    assert (row["status"], row["quote_id"], row["origin"]) == ("active", "usd-coin", "user")
    assert "Contract laut CoinGecko-Katalog" in row["reason"]
    assert ctx(client).recorded_portfolio().asset("USDC").quote_id == "usd-coin"


def test_catalog_state_loads_in_background_once(client, evm, monkeypatch):
    c = ctx(client)
    triggered: list[str] = []

    class Sched:
        def trigger(self, name, delay=1.0, **kw):
            triggered.append(name)
            return True

        def shutdown(self):
            pass

    monkeypatch.setattr(c, "scheduler", Sched())
    c.prices.cg = None
    st = catalog_state(c, start=True)
    assert (st["catalog"], st["available"], st["loading"], triggered) == (None, False, False, [])
    c.prices.cg = FakeCG()
    st = catalog_state(c, start=True)
    assert st["loading"] and triggered == ["coingecko_catalog"]
    # Seite zeigt den Ladezustand und fragt den Status ab
    bid = eth_batch(client)
    page = client.get(f"/journal/csv/{bid}").text
    assert "CoinGecko-Katalog wird geladen" in page and f'hx-get="/journal/csv/{bid}/catalog"' in page
    # Job läuft: Status-Fragment pollt weiter; danach Seite neu laden
    from app.prices.sources import refresh_catalog

    c.job_start("coingecko_catalog")
    assert "every 3s" in client.get(f"/journal/csv/{bid}/catalog").text
    c.job_end("coingecko_catalog", True, result=refresh_catalog(c))
    r = client.get(f"/journal/csv/{bid}/catalog")
    assert r.status_code == 204 and r.headers["HX-Redirect"] == f"/journal/csv/{bid}#assets"
    assert c.prices.cg.calls == ["list"]
    # frischer Katalog: kein erneuter Abruf, Vorschläge ohne Netzaufruf
    triggered.clear()
    page = client.get(f"/journal/csv/{bid}").text
    assert triggered == [] and c.prices.cg.calls == ["list"] and "Vorschlag · hoch" in page
    # veralteter Katalog wird weiter genutzt und im Hintergrund erneuert – höchstens alle 30 Minuten ein Versuch
    write_catalog(client, age=timedelta(days=9))
    page = client.get(f"/journal/csv/{bid}").text
    assert triggered == [] and "Vorschlag · hoch" in page  # letzter Lauf gerade eben
    c.db.x("UPDATE job_status SET last_end=? WHERE job='coingecko_catalog'",
           (iso(datetime.now(UTC) - timedelta(hours=1)),))
    page = client.get(f"/journal/csv/{bid}").text
    assert triggered == ["coingecko_catalog"] and "Vorschlag · hoch" in page
    assert "CoinGecko-Katalog wird geladen" not in page
    # Fehler: Hinweis, kein erneuter Versuch bei jedem Seitenaufruf
    catalog_path(c).unlink()
    c.job_start("coingecko_catalog")
    c.job_end("coingecko_catalog", False, error="BudgetExceeded: CoinGecko-Monatskontingent erschöpft (10000/10000)")
    triggered.clear()
    page = client.get(f"/journal/csv/{bid}").text
    assert triggered == [] and "Monatskontingent erschöpft" in page


def test_source_search_uses_token_contract(client, evm):
    """Asset aus einem Token ohne Kurs-ID: die Kursquellen-Suche findet den Coin über den Contract."""
    write_catalog(client)
    c = ctx(client)
    c.prices.cg = FakeCG()
    bid = eth_batch(client)
    page = client.get(f"/journal/csv/{bid}").text
    data = form_from_page(page)
    real = {v: k[4:] for k, v in data.items() if k.startswith("sym_")}[f"USDC@ETH:{USDC}".upper()]
    data[f"qid_{real}"] = ""  # Nutzer legt ohne Kurs-ID an
    data[f"id_{real}"] = "MYUSDC"
    assert post(client, f"/journal/csv/{bid}/symbols", **data).status_code == 303
    assert post(client, f"/journal/csv/{bid}/commit").status_code == 303
    res = source_service(c).run(force=True)
    assert res["applied"] == ["MYUSDC→usd-coin"], res
    row = c.db.q1("SELECT * FROM asset_source WHERE asset_id='MYUSDC'")
    assert (row["confidence"], row["status"]) == ("hoch", "active") and "Contract" in row["reason"]
    assert "markets" not in c.prices.cg.calls  # ohne Marktdaten-Abruf
