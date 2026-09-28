"""Kursquellen-Zuordnung: Kryptowerte ohne Kursquelle im CoinGecko-Katalog finden (ohne Netzwerk)."""

import csv
import io
import json
import zipfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.prices.coingecko import BudgetExceeded
from app.prices.sources import chain_hints, decide, parse_coin_id, source_service
from app.util.timeutil import today_local

COINS = [
    {"id": "altura", "symbol": "alu", "name": "Altura",
     "platforms": {"binance-smart-chain": "0xa1", "ethereum": "0xa2"}},
    {"id": "alu-sol", "symbol": "alu", "name": "Alu Meme", "platforms": {"solana": "So1"}},
    {"id": "xen-crypto", "symbol": "xen", "name": "XEN Crypto",
     "platforms": {"ethereum": "0x06", "binance-smart-chain": "0x2a"}},
    {"id": "xenon-fake", "symbol": "xen", "name": "Xenon", "platforms": {"ethereum": "0x99"}},
    {"id": "nacho-the-kat", "symbol": "nacho", "name": "Nacho the Kat", "platforms": {}},
    {"id": "nacho-sol", "symbol": "nacho", "name": "Nacho (Solana)", "platforms": {"solana": "Na1"}},
    {"id": "terra-luna-2", "symbol": "luna", "name": "Terra", "platforms": {}},
    {"id": "supra", "symbol": "supra", "name": "Supra", "platforms": {}},
]
MARKETS = {
    "altura": {"current_price": 0.02, "market_cap": 2e7, "ath": 0.2, "atl": 0.002},
    "alu-sol": {"current_price": 0.001, "market_cap": 1e5, "ath": 0.01, "atl": 0.0001},
    "xen-crypto": {"current_price": 1e-7, "market_cap": 5e7, "ath": 4e-4, "atl": 5e-8},
    "xenon-fake": {"current_price": 12.0, "market_cap": 1e4, "ath": 50.0, "atl": 5.0},
    "nacho-the-kat": {"current_price": 2e-5, "market_cap": 3e6, "ath": 3e-4, "atl": 5e-6},
    "nacho-sol": {"current_price": 0.01, "market_cap": 1e6, "ath": 0.1, "atl": 0.001},
    "terra-luna-2": {"current_price": 0.2, "market_cap": 1.5e8, "ath": 15.0, "atl": 0.15},
    "supra": {"current_price": 0.004, "market_cap": 8e7, "ath": 0.05, "atl": 0.001},
}


class FakeCG:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.fail = fail

    def coins_list(self):
        self.calls.append(("list", ()))
        if self.fail:
            raise self.fail
        return COINS

    def markets(self, ids):
        self.calls.append(("markets", tuple(ids)))
        return {i: {"id": i, **MARKETS[i]} for i in ids if i in MARKETS}


@pytest.fixture
def client(config):
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        yield c


def post(c, url, data):
    return c.post(url, data=data, follow_redirects=False)


def _setup(c):
    """Token ohne Kursquelle auf Wallets/Börsen, mit Werten zu realistischen Kursen, plus ein Spam-Token."""
    for aid, cat in (("ALU", ""), ("XEN", ""), ("NACHO", ""), ("LUNA", ""), ("SUPRA", ""),
                     ("SPAMX", "Krypto: Spam/Airdrop (wertlos)")):
        r = post(c, "/journal/asset", {"asset_id": aid, "name": aid, "asset_class": "crypto", "quote_source": "none",
                                       "quote_id": "", "category": cat})
        assert r.status_code == 303, r.text
    d = (today_local() - timedelta(days=40)).isoformat()
    for acc, aid, qty, value in (("MetaMask (BNB)", "ALU", "5000", "75"), ("MetaMask (ETH)", "XEN", "100000000", "44"),
                                 ("Kaspa (KAS)", "NACHO", "1000000", "13"), ("Bitpanda", "LUNA", "100000", "6.87"),
                                 ("Bitpanda", "SUPRA", "100000", "136"), ("MetaMask (ETH)", "SPAMX", "1000", "0")):
        r = post(c, "/journal/new", {"kind": "deposit", "date": d, "account": acc, "asset": aid, "qty": qty,
                                     "value_eur": value})
        assert r.status_code == 303, r.text
    ctx = c.app.state.ctx
    ctx.prices.cg = FakeCG()
    return ctx


def test_resolver_applies_only_unambiguous_matches(client):
    ctx = _setup(client)
    res = source_service(ctx).run(force=True)
    assert res["checked"] == 5  # SPAMX übersprungen
    rows = {r["asset_id"]: r for r in ctx.db.q("SELECT * FROM asset_source")}
    # ALU: nur Altura liegt auf BNB Smart Chain (Solana-Doppelgänger entfällt) → automatisch
    assert (rows["ALU"]["status"], rows["ALU"]["quote_id"], rows["ALU"]["confidence"]) == ("active", "altura", "hoch")
    # XEN: Doppelgänger passt nicht zu den eigenen Kursen → eindeutig
    assert (rows["XEN"]["status"], rows["XEN"]["quote_id"]) == ("active", "xen-crypto")
    assert (rows["SUPRA"]["status"], rows["SUPRA"]["quote_id"]) == ("active", "supra")
    # NACHO: Kaspa-Chain im Katalog nicht prüfbar → nur Vorschlag
    assert (rows["NACHO"]["status"], rows["NACHO"]["quote_id"], rows["NACHO"]["confidence"]) == \
        ("suggested", "nacho-the-kat", "mittel")
    # LUNA zu Kursen von LUNA Classic: LUNA 2.0 ist unplausibel → kein Treffer
    assert rows["LUNA"]["status"] == "none" and rows["LUNA"]["quote_id"] is None
    assert "Kursspanne" in rows["LUNA"]["reason"]
    assert "SPAMX" not in rows
    pf = ctx.recorded_portfolio()
    assert (pf.asset("ALU").quote_source, pf.asset("ALU").quote_id) == ("coingecko", "altura")
    assert pf.asset("NACHO").quote_source == "none"
    assert ctx.prices.series_for(pf.asset("XEN")).endswith("cg:xen-crypto")
    # zweiter Lauf: nichts mehr offen (frisch geprüft), Katalog aus dem Cache
    assert source_service(ctx).run()["checked"] == 0
    assert source_service(ctx).run(force=True)["checked"] == 2  # NACHO, LUNA erneut
    assert [c[0] for c in ctx.prices.cg.calls].count("list") == 1
    # Gesamtexport enthält die Zuordnung (einheitliches Import-Format)
    from app.journal.service import journal_service

    with zipfile.ZipFile(io.BytesIO(journal_service(ctx).export_zip())) as zf:
        assets = {r["asset_id"]: r for r in csv.DictReader(io.StringIO(zf.read("assets.csv").decode("utf-8-sig")))}
    assert (assets["ALU"]["quote_source"], assets["ALU"]["quote_id"]) == ("coingecko", "altura")


def test_sources_page_accept_reject_reset_and_manual_link(client):
    ctx = _setup(client)
    source_service(ctx).run(force=True)
    page = client.get("/quality/sources").text
    assert "Vorschlag" in page and "nacho-the-kat" in page and "Nacho the Kat" in page
    r = post(client, "/quality/sources/accept", {"asset_id": "NACHO", "coin_id": "nacho-the-kat"})
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert ctx.recorded_portfolio().asset("NACHO").quote_id == "nacho-the-kat"
    # Zuordnung entfernen → wieder ohne Quelle, beim nächsten Lauf neu geprüft
    assert post(client, "/quality/sources/reset", {"asset_id": "NACHO"}).status_code == 303
    assert ctx.recorded_portfolio().asset("NACHO").quote_source == "none"
    # manuell per Link (mit Sprachpfad) und unbekannte ID
    r = post(client, "/quality/sources/accept",
             {"asset_id": "LUNA", "manual": "https://www.coingecko.com/de/munzen/terra-luna-2"})
    assert r.status_code == 303 and "error" not in r.headers["location"]
    assert ctx.recorded_portfolio().asset("LUNA").quote_id == "terra-luna-2"
    r = post(client, "/quality/sources/accept", {"asset_id": "LUNA", "manual": "gibt-es-nicht"})
    assert "error=" in r.headers["location"]
    assert post(client, "/quality/sources/reject", {"asset_id": "LUNA"}).status_code == 303
    row = ctx.db.q1("SELECT * FROM asset_source WHERE asset_id='LUNA'")
    assert row["status"] == "rejected"
    assert ctx.recorded_portfolio().asset("LUNA").quote_source == "none"
    assert source_service(ctx).run(force=True)["checked"] == 1  # abgelehnte bleiben abgelehnt (nur NACHO)


def test_auto_level_setting_and_budget(client):
    ctx = _setup(client)
    ctx.settings.set("prices.auto_map", "aus")
    source_service(ctx).run(force=True)
    assert not ctx.db.q("SELECT 1 FROM asset_source WHERE status='active'")
    ctx.db.x("DELETE FROM asset_source")
    source_service(ctx).cache_path.unlink()
    ctx.prices.cg = FakeCG(fail=BudgetExceeded("Kontingent erschöpft"))
    assert "Kontingent" in source_service(ctx).run(force=True)["skipped"]
    ctx.prices.cg = None
    assert "Demo" in source_service(ctx).run(force=True)["skipped"]


def test_decide_and_helpers():
    assert chain_hints(["MetaMask (BNB)", "Binance", "Coinbase", "Solana (SOL)"]) == {"binance-smart-chain", "solana"}
    assert chain_hints(["Bitpanda", "CoinEx"]) == set()
    assert parse_coin_id("https://www.coingecko.com/en/coins/xen-crypto") == "xen-crypto"
    assert parse_coin_id(" Nacho-The-Kat ") == "nacho-the-kat"
    assert parse_coin_id("drop table;") is None
    # zwei ähnlich große Kandidaten ohne Chain-Hinweis → nur niedrig
    coins = [{"id": "a", "symbol": "x", "name": "A"}, {"id": "b", "symbol": "x", "name": "B"}]
    markets = {"a": {"current_price": 1, "market_cap": 5e6, "ath": 3, "atl": 0.5},
               "b": {"current_price": 1, "market_cap": 3e6, "ath": 3, "atl": 0.5}}
    d = decide(coins, markets, set(), 1.0)
    assert (d.coin_id, d.confidence) == ("a", "niedrig")
    markets["b"]["market_cap"] = 1e5  # klarer Marktführer
    assert decide(coins, markets, set(), 1.0).confidence == "mittel"
    assert decide(coins, markets, set(), None).confidence == "niedrig"  # ohne eigene Kurse keine Stufe höher
    assert json.dumps(decide(coins, markets, set(), 1.0).candidates[0].as_dict())
