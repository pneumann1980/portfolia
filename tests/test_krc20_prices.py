"""KRC-20-Kurse über KaspaCom (ohne Netz): Einheitenprüfung, Zuordnung ohne CoinGecko-Eintrag, Kursabruf in EUR,
Export bleibt im Datenvertrag.

Die Prüfwerte entsprechen dem live beobachteten Verhalten (10/2026): ``marketCap / (price × totalMinted)`` ergibt den
KAS/USD-Kurs – ``price`` ist KAS je Token, obwohl die Doku USD nennt.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.prices.kaspacom import KaspaComProvider, interpret
from app.prices.sources import source_service
from app.util.timeutil import iso, today_local

KAS_USD, KAS_EUR = 0.04, 0.035


def info(tick: str, price: float, *, unit: str = "KAS", supply: float = 1e9) -> dict:
    usd = price * KAS_USD if unit == "KAS" else price
    return {"ticker": tick, "price": price, "marketCap": usd * supply, "totalMinted": supply, "totalSupply": supply,
            "volumeUsd": 12.5, "state": "finished"}


def test_unit_is_checked_against_market_cap():
    r = interpret(info("KASPER", 0.00035), KAS_USD)
    assert r.unit == "KAS" and r.price_kas == pytest.approx(0.00035)
    r = interpret(info("KASPER", 0.000014, unit="USD"), KAS_USD)  # falls KaspaCom die Einheit ändert
    assert r.unit == "USD" and r.price_kas == pytest.approx(0.000014 / KAS_USD)
    bad = info("KASPER", 0.00035) | {"marketCap": 5.0}
    assert "Einheit unklar" in interpret(bad, KAS_USD)
    assert interpret({"ticker": "X1", "price": 0}, KAS_USD) == "kein Kurs"
    assert "nicht prüfbar" in interpret({"ticker": "KEI", "price": 1.0}, KAS_USD)


class FakeKC(KaspaComProvider):
    def __init__(self, data: dict[str, dict]) -> None:
        self.data = data
        self.calls: list[str] = []

    def token_info(self, tick: str):
        self.calls.append(tick)
        if tick not in self.data:
            from app.util.http import HttpError

            raise HttpError("HTTP 500", 500)
        return self.data[tick]


class FakeCG:
    def coins_list(self):
        return [{"id": "kaspa", "symbol": "kas", "name": "Kaspa", "platforms": {}}]

    def markets(self, ids):
        return {}


@pytest.fixture
def client(config):
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        yield c


def _setup(c):
    ctx = c.app.state.ctx
    d = (today_local() - timedelta(days=20)).isoformat()
    for aid in ("KASPER", "POPKAT", "KEI"):
        form = {"asset_id": aid, "name": aid, "asset_class": "crypto", "quote_source": "none", "quote_id": "",
                "category": ""}
        r = c.post("/journal/asset", data=form, follow_redirects=False)
        assert r.status_code == 303, r.text
        r = c.post("/journal/new", data={"kind": "deposit", "date": d, "account": "Kaspa (KAS)", "asset": aid,
                                         "qty": "1000", "value_eur": "1"}, follow_redirects=False)
        assert r.status_code == 303, r.text
        ctx.db.x("INSERT INTO csv_symbol(symbol, asset_id, updated_at) VALUES (?,?,?)",
                 (f"{aid}@KAS:{aid}", aid, iso(datetime.now(UTC))))
    ctx.prices.demo = None  # echte Kurswege (ohne Netz: Fakes)
    ctx.prices.cg = FakeCG()
    ctx.prices.kc = FakeKC({"KASPER": info("KASPER", 0.00035), "POPKAT": info("POPKAT", 0.00495)})
    ctx.prices.kas_rates = lambda: (KAS_EUR, KAS_USD)
    return ctx


def test_krc20_tokens_get_kaspacom_source_and_eur_quotes(client):
    ctx = _setup(client)
    res = source_service(ctx).run(force=True)
    rows = {r["asset_id"]: r for r in ctx.db.q("SELECT * FROM asset_source")}
    assert (rows["KASPER"]["quote_source"], rows["KASPER"]["quote_id"], rows["KASPER"]["status"]) == \
        ("kaspacom", "KASPER", "active")
    assert "Einheit geprüft (KAS)" in rows["KASPER"]["reason"]
    assert rows["KEI"]["status"] == "none"  # KaspaCom kennt KEI nicht (HTTP 500) → keine Zuordnung
    assert "KASPER→KASPER" in res["applied"]
    pf = ctx.portfolio()
    assert ctx.prices.series_for(pf.asset("KASPER")) == "kc:KASPER"
    out = ctx.prices.update_krc20(pf, ctx.ledger(), force=True)
    assert out.updated == 2, out.as_dict()
    q = ctx.store.latest("kc:KASPER")
    assert q["price"] == pytest.approx(0.00035 * KAS_EUR) and q["ccy"] == "EUR" and q["source"] == "kaspacom"
    # Intervall: ohne force kein erneuter Abruf
    n = len(ctx.prices.kc.calls)
    assert ctx.prices.update_krc20(pf, ctx.ledger()).skipped == "Intervall" and len(ctx.prices.kc.calls) == n
    # Übersicht ohne CoinGecko-Link für KaspaCom-Tokens
    page = client.get("/quality/sources").text
    assert "KRC-20 · KaspaCom" in page and "coingecko.com/en/coins/KASPER" not in page
    # Export (Import-ZIP) bleibt im Datenvertrag: kaspacom → none, die Zuordnung steckt im App-Zustand
    from app.journal.service import journal_service

    with zipfile.ZipFile(io.BytesIO(journal_service(ctx).export_zip())) as zf:
        assets = {r["asset_id"]: r for r in csv.DictReader(io.StringIO(zf.read("assets.csv").decode("utf-8-sig")))}
    assert (assets["KASPER"]["quote_source"], assets["KASPER"]["quote_id"]) == ("none", "")


def test_manual_coingecko_assignment_replaces_kaspacom(client):
    ctx = _setup(client)
    svc = source_service(ctx)
    svc.run(force=True)
    from app.prices import sources as S

    S.Catalog
    assert svc.accept("KASPER", "kaspa") is None
    r = ctx.db.q1("SELECT quote_source, quote_id FROM asset_source WHERE asset_id='KASPER'")
    assert (r["quote_source"], r["quote_id"]) == ("coingecko", "kaspa")
