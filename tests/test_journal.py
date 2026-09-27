"""Manuell erfasste Buchungen (Journal): Erfassung, Prüfung, Bearbeiten/Löschen, Betrieb ohne Import, Export."""

import io
import os
import shutil
import time
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.importer.validate import validate_zip
from app.jobs import tasks
from app.ledger.engine import run_ledger
from app.main import build_app
from app.plans.service import plan_service
from app.util.timeutil import local_tz

D = Decimal
SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"
ETF = "ISIN:IE00B4L5Y983"


def _client(config):
    app = build_app(config, start_scheduler=False)
    return TestClient(app)


@pytest.fixture
def client(config):
    with _client(config) as c:
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
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
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        yield c


def post(c, url, **data):
    return c.post(url, data=data, follow_redirects=False)


def _asset(c, asset_id, name, cls="crypto", qs="coingecko", qid="", **extra):
    r = post(c, "/journal/asset", asset_id=asset_id, name=name, asset_class=cls, quote_source=qs, quote_id=qid,
             **extra)
    assert r.status_code == 303, r.text
    return r


def _ctx(c):
    return c.app.state.ctx


# -- Betrieb ohne Import --------------------------------------------------------------------------------

def test_manual_only_portfolio_and_pages(client):
    c = client
    assert "Erste Buchung erfassen" in c.get("/").text
    _asset(c, "SOL", "Solana", qid="solana", category="Krypto: Altcoins")
    r = post(c, "/journal/new", kind="deposit", date="2026-01-02", account="Börse Y", asset="EUR", qty="1.000")
    assert r.status_code == 303 and "PF-M-000001" in r.headers["location"]
    r = post(c, "/journal/new", kind="buy", date="2026-01-05", time="10:15", account="Börse Y", asset="sol",
             qty="10", amount="1.234,50", fee="1,5")
    assert r.status_code == 303 and "PF-M-000002" in r.headers["location"]
    ctx = _ctx(c)
    pf = ctx.portfolio()
    assert pf is not None and pf.import_id is None and ctx.base_portfolio() is None
    t = next(t for t in pf.txs if t.tx_id == "PF-M-000002")
    assert t.origin == "journal" and t.value_eur == D("1234.5") and t.fee_eur == D("1.5") and t.from_asset == "EUR"
    assert t.ts.astimezone(local_tz()).strftime("%H:%M") == "10:15"
    led = ctx.ledger()
    assert led.balances[("Börse Y", "SOL")] == D("10")
    assert led.balances[("Börse Y", "EUR")] == D("1000") - D("1234.5") - D("1.5")
    for url in ("/", "/positions", "/journal", "/performance", "/tax", "/plans", "/quality"):
        assert c.get(url).status_code == 200, url
    page = c.get("/journal").text
    assert "PF-M-000002" in page and "manuell" in page and "Solana" in page
    assert "Solana" in c.get("/positions").text
    # Formularseiten
    for kind in ("buy", "sell", "trade", "transfer", "income", "deposit", "withdrawal", "cost", "corporate", "expert"):
        assert c.get(f"/journal/new?kind={kind}").status_code == 200, kind
    assert c.get("/journal/PF-M-000002/edit").status_code == 200
    assert c.get("/journal/asset?id=SOL").status_code == 200


def test_validation_messages(client):
    c = client
    r = post(c, "/journal/new", kind="buy", date="2030-01-01", account="", asset="XYZ", qty="abc")
    assert r.status_code == 400
    for msg in ("Datum liegt in der Zukunft", "Konto fehlt", "„XYZ“ ist unbekannt", "Neues Asset",
                "Stückzahl: „abc“ ist keine Zahl"):
        assert msg in r.text, msg
    r = post(c, "/journal/new", kind="transfer", date="2026-02-01", from_account="A", to_account="A", asset="EUR",
             qty="5")
    assert r.status_code == 400 and "Von- und Nach-Konto sind gleich" in r.text
    r = post(c, "/journal/new", kind="income", date="2026-02-01", account="A", asset="EUR", qty="5")
    assert r.status_code == 400 and "Art fehlt" in r.text
    # Regeln des Import-Validators greifen ebenfalls (withdrawal ohne Zugangsbein etc.)
    r = post(c, "/journal/new", kind="expert", type="deposit", date="2026-02-01", from_account="A", from_asset="EUR",
             from_qty="5", to_account="A", to_asset="EUR", to_qty="5")
    assert r.status_code == 400 and "deposit darf kein Abgangsbein haben" in r.text
    assert _ctx(c).portfolio() is None
    # Assets
    r = post(c, "/journal/asset", asset_id="X Y", name="", asset_class="crypto", quote_source="coingecko",
             quote_id="Bad ID", isin="123")
    assert r.status_code == 400
    for msg in ("Kürzel/ID", "Name fehlt", "CoinGecko-ID", "ISIN ungültig"):
        assert msg in r.text, msg
    _asset(c, "SOL", "Solana", qid="solana")
    r = post(c, "/journal/asset", asset_id="SOL", name="Doppelt", asset_class="crypto", quote_source="none")
    assert r.status_code == 400 and "existiert bereits" in r.text
    r = post(c, "/journal/asset", asset_id="USD", name="Dollar-Token", asset_class="crypto", quote_source="none")
    assert r.status_code == 400 and "Währungscode" in r.text


def test_income_with_withholding_tax_edit_delete_restore(client):
    c = client
    _asset(c, ETF, "iShares Core MSCI World", cls="security", qs="yahoo", qid="EUNL.DE", tax_type="etf_equity")
    assert post(c, "/journal/new", kind="buy", date="2026-01-05", account="Depot Z", asset=ETF, qty="20",
                amount="2000").status_code == 303
    r = post(c, "/journal/new", kind="income", tag="dividend", date="2026-03-10", account="Depot Z", asset="EUR",
             qty="100", related_asset=ETF, wht="15", note="Q1")
    assert r.status_code == 303
    ctx = _ctx(c)
    rows = ctx.db.q("SELECT * FROM journal_tx ORDER BY id")
    main, wht = rows[-2], rows[-1]
    assert main["tag"] == "dividend" and main["related_asset"] == ETF and D(main["to_qty"]) == 100
    assert wht["group_ref"] == main["tx_id"] and wht["tag"] == "withholding_tax" and D(wht["from_qty"]) == 15
    led = ctx.ledger()
    assert any(e.related_asset == ETF and e.value_eur == D("100") for e in led.income)
    assert any(t.eur == D("15") and t.related_asset == ETF for t in led.taxes)
    # Bearbeiten: Quellensteuer entfernen → Teilbuchung wird gelöscht
    r = post(c, f"/journal/{main['tx_id']}/edit", kind="income", tag="dividend", date="2026-03-10",
             account="Depot Z", asset="EUR", qty="90", related_asset=ETF, wht="")
    assert r.status_code == 303
    assert ctx.db.q1("SELECT status FROM journal_tx WHERE id=?", (wht["id"],))["status"] == "replaced"
    assert D(ctx.db.q1("SELECT to_qty FROM journal_tx WHERE id=?", (main["id"],))["to_qty"]) == 90
    assert c.get(f"/journal/{wht['tx_id']}/edit").status_code == 404  # Teilbuchungen nur über die Hauptbuchung
    # Löschen und Wiederherstellen (protokolliert)
    assert post(c, f"/journal/{main['tx_id']}/delete").status_code == 303
    assert all(t.tx_id != main["tx_id"] for t in ctx.portfolio().txs)
    assert "Gelöschte Buchungen" in c.get("/journal").text
    assert post(c, f"/journal/{main['tx_id']}/restore").status_code == 303
    assert any(t.tx_id == main["tx_id"] for t in ctx.portfolio().txs)
    # die per Bearbeiten entfernte Quellensteuer kommt beim Wiederherstellen nicht zurück
    assert all(t.tx_id != wht["tx_id"] for t in ctx.portfolio().txs)
    actions = [r["action"] for r in ctx.db.q("SELECT action FROM journal_log ORDER BY id")]
    assert {"asset_create", "create", "update", "delete", "restore"} <= set(actions)
    assert post(c, "/journal/PF-M-999999/delete").status_code == 404


def test_foreign_currency_and_trade_values(client):
    c = client
    ctx = _ctx(c)
    _asset(c, "AAPL", "Apple", cls="security", qs="yahoo", qid="AAPL")
    r = post(c, "/journal/new", kind="buy", date="2026-02-02", account="Broker US", asset="AAPL", qty="2",
             amount="440", ccy="USD", fee="2,20")
    assert r.status_code == 400 and "kein Devisenkurs USD" in r.text
    ctx.db.x("INSERT INTO price_daily(series, date, close, ccy, source, fetched_at) VALUES "
             "('fx:ecb:USD', '2026-02-02', 1.10, 'USD', 'ecb', '2026-02-02T16:00:00Z')")
    r = post(c, "/journal/new", kind="buy", date="2026-02-02", account="Broker US", asset="AAPL", qty="2",
             amount="440", ccy="USD", fee="2,20")
    assert r.status_code == 303, r.text
    t = next(t for t in ctx.portfolio().txs if t.to_asset == "AAPL")
    assert t.value_eur == D("400") and t.fee_eur == D("2") and t.orig_ccy == "USD" and D(t.orig_price) == 220
    row = ctx.db.q1("SELECT value_source FROM journal_tx WHERE tx_id=?", (t.tx_id,))
    assert "Devisenkurs USD" in row["value_source"]
    # Tausch ohne Kurs → Gegenwert verlangt; mit Angabe gespeichert
    _asset(c, "ETH", "Ethereum", qid="ethereum")
    _asset(c, "SOL", "Solana", qid="solana")
    r = post(c, "/journal/new", kind="trade", date="2026-02-03", account="Wallet", from_asset="ETH", from_qty="1",
             to_asset="SOL", to_qty="20")
    assert r.status_code == 400 and "Gegenwert in EUR angeben" in r.text
    r = post(c, "/journal/new", kind="trade", date="2026-02-03", account="Wallet", from_asset="ETH", from_qty="1",
             to_asset="SOL", to_qty="20", value_eur="2.500")
    assert r.status_code == 303
    assert "negativ" in r.headers["location"]  # ETH-Bestand fehlt → Hinweis nach dem Speichern
    assert "ETH auf „Wallet“ ist negativ" in c.get(r.headers["location"]).text


# -- Zusammenspiel mit Import, Sparplänen und Export -----------------------------------------------------

def test_with_import_duplicates_and_plan_supersede(sample_client):
    c = sample_client
    ctx = _ctx(c)
    base = ctx.base_portfolio()
    imp = next(t for t in base.txs if t.to_asset == "BTC" and t.type == "buy")
    r = post(c, "/journal/new", kind="buy", date=imp.date.isoformat(), account=imp.to_account, asset="BTC",
             qty=str(imp.to_qty), amount=str(imp.value_eur))
    assert r.status_code == 303 and "Dublette" in r.headers["location"]
    page = c.get("/journal?origin=journal").text
    assert "Dublette?" in page and imp.tx_id in page
    # Sparplan-Schätzung wird durch eine passende manuell erfasste Ausführung ersetzt
    svc = plan_service(ctx)
    ctx.db.x("DELETE FROM tx_estimate")
    svc.update(today=date(2026, 9, 27), now=datetime(2026, 9, 27, 12, 0, tzinfo=local_tz()))
    est = next(e for e in svc.estimates(("estimated",)) if e["asset_id"] == "BTC")
    before = ctx.ledger().balances[("Börse X", "BTC")]
    r = post(c, "/journal/new", kind="buy", date=est["due_date"], time="08:01", account="Börse X", asset="BTC",
             qty=str(est["qty_d"]), amount="25", fee="0,25")
    assert r.status_code == 303
    row = ctx.db.q1("SELECT * FROM tx_estimate WHERE id=?", (est["id"],))
    assert row["status"] == "superseded" and "erfasste Buchung PF-M-" in row["note"]
    assert ctx.ledger().balances[("Börse X", "BTC")] == before  # Schätzung raus, echte Buchung rein


def test_export_roundtrip_via_import(sample_client, config):
    c = sample_client
    ctx = _ctx(c)
    _asset(c, "ADA", "Cardano", qid="cardano")  # nicht im Beispiel-Import enthalten
    assert post(c, "/journal/new", kind="buy", date="2026-09-20", account="Börse X", asset="ADA", qty="300",
                amount="150").status_code == 303
    r = c.get("/journal/export.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert '"schema_version": "1.1"' in zf.read("manifest.json").decode()
        assert "PF-M-000001" in zf.read("transactions.csv").decode()
    def recorded_balances():
        return {k: v for k, v in run_ledger(ctx.recorded_portfolio(), ctx.engine_options()).balances.items() if v}

    before = recorded_balances()
    dst = config.import_dir / "export.zip"
    dst.write_bytes(r.content)
    rep, parsed = validate_zip(dst)
    assert parsed is not None, [m.message for m in rep.errors]
    new_time = time.time() - 60
    os.utime(dst, (new_time, new_time))
    out = tasks.import_check(ctx, "test")
    assert out.status == "imported", out.message
    pf = ctx.portfolio()
    assert [t.origin for t in pf.txs if t.tx_id == "PF-M-000001"] == ["import"]  # nicht doppelt gezählt
    r = post(c, "/journal/PF-M-000001/edit", kind="buy", date="2026-09-20", account="Börse X", asset="ADA",
             qty="301", amount="150")
    assert r.status_code == 400 and "inzwischen im Import enthalten" in r.text
    assert recorded_balances() == before
    assert out.check["deviations"] == []


def test_manual_only_savings_plan_keeps_pending_estimates(client):
    c = client
    ctx = _ctx(c)
    _asset(c, ETF, "iShares Core MSCI World", cls="security", qs="yahoo", qid="EUNL.DE")
    for m in (1, 2, 3, 4):
        assert post(c, "/journal/new", kind="buy", date=f"2026-0{m}-15", time="09:00", account="Depot Z",
                    asset=ETF, qty="1,5", amount="150").status_code == 303
    tasks.refresh_prices(ctx, force=True)
    tasks.backfill(ctx)
    ctx.db.x("DELETE FROM tx_estimate")  # Schätzungen auf festen Stichtag
    svc = plan_service(ctx)
    svc.update(today=date(2026, 6, 20), now=datetime(2026, 6, 20, 12, 0, tzinfo=local_tz()))
    plan = next(p for p in svc.plans() if p["asset_id"] == ETF)
    assert plan["status"] == "active" and plan["freq"] == "monthly"
    pending = {e["due_date"] for e in svc.estimates(("estimated",))}
    assert pending == {"2026-05-15", "2026-06-15"}
    # eine spätere manuelle Buchung eines anderen Assets lässt den Plan nicht „ausgesetzt“ erscheinen
    _asset(c, "SOL", "Solana", qid="solana")
    assert post(c, "/journal/new", kind="buy", date="2026-06-19", account="Börse Y", asset="SOL", qty="1",
                amount="150").status_code == 303
    svc.update(today=date(2026, 6, 20), now=datetime(2026, 6, 20, 12, 0, tzinfo=local_tz()))
    svc.reconcile()
    assert {e["due_date"] for e in svc.estimates(("estimated",))} == pending
    assert next(p for p in svc.plans() if p["asset_id"] == ETF)["status"] == "active"
