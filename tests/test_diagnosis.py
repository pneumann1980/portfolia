"""Diagnose (nur lesend) und Schutzregeln für künftige Importe – mit synthetischen, anonymisierten Fällen.

Die Fälle bilden Muster nach (keine echten Daten): doppelte Gutschrift „manuell + Transfer“, Buchungspaare mit
gleichem Hash ohne Ereignisindex, zwei legitime Bewegungen derselben Transaktion mit verschiedenem Ereignisindex,
Anbieter-Kürzel mit falscher Kursquelle, Token-Migration mit Mengenverhältnis 10^6, rekonstruierte Buchungen,
fehlende bzw. alte Kurse, Bestandsabgleich gegen externe Bestände.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Config, Secrets
from app.context import AppContext
from app.csvimport.identity import note_identity_keys, provider_of, split_provider_key
from app.csvimport.service import CsvImportService, RowCtx, SymbolResolver, csv_service
from app.datasources.service import DataSourceService
from app.diagnosis.collect import collect
from app.diagnosis.engine import diagnose, report_for
from app.importer.loader import import_file
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.ledger.models import AssetInfo
from app.main import build_app
from app.prices.models import Quote
from app.prices.sources import SourceService
from tests.helpers import ASSETS, tx

COLS = ["source", "source_ref", "flag", "note"]
NOW = datetime.now(UTC)


def H(n: int) -> str:
    return f"{n:064x}"


def asset(aid: str, name: str | None = None, qs: str = "none", qid: str = "", **kw: str) -> dict:
    return {"asset_id": aid, "name": name or aid, "asset_class": "crypto", "quote_source": qs, "quote_id": qid,
            "category": "Krypto: Small Caps", **kw}


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    # Demo-Modus: keine Kursabrufe im Netz (auch nicht nach einem Import)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def make_ctx(cfg: Config, rows: list[dict], assets: list[dict], holdings: list[dict] | None = None,
             manual: list[dict] | None = None, valuation: str = "2025-06-30") -> AppContext:
    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=rows, assets=assets, holdings_check=holdings, manual_prices=manual,
              generated_at=f"{valuation}T20:00:00Z", valuation_date=valuation, extra_tx_columns=COLS)
    ctx = AppContext(cfg)
    ctx.startup()
    out = import_file(ctx.db, dst, ctx.engine_options())
    assert out.status == "imported", out.message
    ctx.invalidate_data()
    return ctx


def fingerprint(db) -> str:
    """Prüfsumme über alle Tabellen (ohne Protokolle, die jeder Seitenaufruf schreiben darf)."""
    h = hashlib.sha256()
    for (t,) in db.q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        if t in ("event_log", "api_usage", "job_status", "source_status"):
            continue
        for r in db.q(f"SELECT * FROM {t} ORDER BY 1"):
            h.update(repr(tuple(r)).encode())
    return h.hexdigest()


def by_kind(rep, kind: str) -> list:
    return [f for f in rep.findings if f.kind == kind]


# ----------------------------------------------------------------------------------------------------
# Dubletten
# ----------------------------------------------------------------------------------------------------

TOKA_QTY = "50123.45678901"


def popkat_like_rows() -> list[dict]:
    """Gleiche Menge zweimal gutgeschrieben: manuell (12:00 UTC, ohne Hash, ohne Wert) und als Transfer mit Hash."""
    buy = tx("B1", "2025-03-10T20:00:00Z", "trade", frm=("Wallet E", "USDT", "100"),
             to=("Wallet E", "TOKA", "50223.45678901"), value="92")
    manual = tx("M1", "2025-03-10T12:00:00Z", "deposit", to=("Wallet K", "TOKA", TOKA_QTY), value="0")
    move = tx("T1", "2025-03-10T21:30:00Z", "transfer", frm=("Wallet E", "TOKA", TOKA_QTY),
              to=("Wallet K", "TOKA", TOKA_QTY), fee=("TOKA", "100", "0"), value="0")
    fund = tx("F1", "2025-03-01T10:00:00Z", "deposit", to=("Wallet E", "USDT", "100"), value="92")
    buy["note"], move["note"] = f"txhash=0x{H(1)}", f"txhash=0x{H(2)}"
    manual["flag"] = "KOINLY_MANUAL;KOINLY_MISSING_RATE"
    for r in (buy, manual, move, fund):
        r["source"] = "koinly"
        r["source_ref"] = f"REF{r['tx_id']}"
    return [fund, buy, manual, move]


TOKA_ASSETS = [*ASSETS, asset("TOKA"), asset("USDT", "Tether", "coingecko", "tether")]


def test_manual_credit_and_transfer_with_same_quantity_is_probable_duplicate(cfg):
    holdings = [{"asset_id": "TOKA", "account": "Wallet K", "qty": "100246.91357802"}]
    ctx = make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS, holdings)
    before = fingerprint(ctx.db)
    rep = report_for(ctx)
    (f,) = [x for x in by_kind(rep, "duplicate") if "TOKA" in x.title]
    assert f.status == "wahrscheinlich" and f.priority == 1
    assert {t.tx_id for t in f.txs} == {"M1", "T1"}
    weak, strong, why = f.pairs[0]
    assert weak.tx_id == "M1" and strong.tx_id == "T1" and "exakt gleiche Menge" in why
    assert any("KOINLY_MANUAL" in e for e in f.evidence) and any("Hash" in e for e in f.evidence)
    assert any("intern konsistent" in k for k in f.known)  # Soll-Bestand enthält beide Buchungen
    # Szenario: hypothetisch, Bestand halbiert – nichts gebucht
    assert f.scenario is not None and "hypothetisch" in f.scenario.text
    label, cur, alt = f.scenario.rows[0]
    assert label == "Bestand Wallet K · TOKA" and cur == "100.246,91357802" and alt.startswith("50.123,45678901")
    # Bestandsabgleich: intern konsistent (reproduzierbar) – trotz Verdacht, mit Erklärung; kein „extern ok“
    (row,) = [h for h in rep.holdings if (h.account, h.asset) == ("Wallet K", "TOKA")]
    assert row.status == "intern_ok" and any(e.startswith("Dublettenverdacht") for e in row.explanations)
    assert ctx.ledger().balances[("Wallet K", "TOKA")] == Decimal("100246.91357802")  # unverändert
    assert fingerprint(ctx.db) == before


def kaspa_like_rows(n_pairs: int = 3) -> list[dict]:
    rows = [tx("S0", "2025-01-01T00:00:00Z", "deposit", to=("Wallet A", "KAS", "500"), value="50")]
    for i in range(n_pairs):
        ts = f"2025-02-0{i + 1}T08:0{i}:00Z"
        for copy in ("a", "b"):  # gleiche Angaben, gleicher Hash, verschiedene Kennungen, kein Ereignisindex
            if i % 2 == 0:
                r = tx(f"P{i}{copy}", ts, "deposit", to=("Wallet A", "KAS", "19.75"), value="2.5")
            else:
                r = tx(f"P{i}{copy}", ts, "withdrawal", frm=("Wallet A", "KAS", "21"), value="2.7")
            r["note"], r["source"], r["source_ref"] = f"txhash={H(100 + i)}", "koinly", f"KREF{i}{copy}"
            rows.append(r)
    return rows


def test_same_hash_pairs_without_event_index_are_suspicion_with_net_scenario(cfg):
    ctx = make_ctx(cfg, kaspa_like_rows(), ASSETS)
    rep = report_for(ctx)
    (f,) = [x for x in by_kind(rep, "duplicate") if x.key.startswith("hash|")]
    assert f.status == "verdacht" and len(f.pairs) == 3
    assert "3 Paare" in f.title and "Wallet A" in f.title
    assert all("Ereignisindex fehlt" in why for _a, _b, why in f.pairs)
    assert any("Ereignisindex" in u for u in f.uncertainty)
    # gebucht: 500 + 4 × 19,75 − 2 × 21 = 537; je Paar eine Buchung weniger: −2 × 19,75 + 21 = −18,5 (netto)
    label, cur, alt = f.scenario.rows[0]
    assert label == "Bestand Wallet A · KAS" and cur == "537" and alt == "518,5 (Δ −18,5)"
    assert rep.stats["same_hash_distinct_index"] == 0


def test_same_hash_with_distinct_event_indices_is_legitimate(cfg):
    """Zwei gleiche Bewegungen derselben Transaktion mit verschiedenem Ereignisindex: keine Dublette."""
    rows = []
    for i in (0, 1):
        r = tx(f"L{i}", "2025-04-01T09:00:00Z", "deposit", to=("Wallet E", "USDT", "50"), value="46")
        r["source"], r["source_ref"] = "portfolia:sync:ethereum", f"ethereum:0x{H(7)}#t:f00d#{i}"
        rows.append(r)
    ctx = make_ctx(cfg, rows, TOKA_ASSETS)
    rep = report_for(ctx)
    assert not by_kind(rep, "duplicate")
    assert rep.stats["same_hash_distinct_index"] == 1
    # gleicher Fall ohne Index (z. B. Steuertool-Export) wäre ein Verdacht
    for r in rows:
        r["source"], r["source_ref"] = "koinly", f"K{r['tx_id']}"
        r["note"] = f"txhash=0x{H(7)}"
    ctx2 = make_ctx(cfg, rows, TOKA_ASSETS)
    (f,) = by_kind(report_for(ctx2), "duplicate")
    assert f.status == "verdacht"


def test_provider_id_collision_between_import_note_and_app_booking(cfg):
    uuid = "0a1b2c3d-1111-4222-8333-444455556666"
    r = tx("KX", "2025-05-02T10:00:00Z", "buy", frm=("Bitpanda", "EUR", "100"), to=("Bitpanda", "BTC", "0.002"),
           value="100")
    r["source"], r["source_ref"], r["note"] = "koinly", "KOINLYX", f"txhash={uuid}"
    ctx = make_ctx(cfg, [r], ASSETS)
    stamp = "2025-06-01T00:00:00Z"
    ctx.db.x("INSERT INTO journal_tx(tx_id, source, external_id, status, ts_utc, type, from_account, from_asset, "
             "from_qty, to_account, to_asset, to_qty, value_eur, created_at, updated_at, event_key) VALUES "
             "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             ("PF-S-000001", "sync:bitpanda", f"bitpanda:{uuid}#0", "active", "2025-05-02T10:00:01Z", "buy",
              "Bitpanda", "EUR", "100", "Bitpanda", "BTC", "0.002", "100", stamp, stamp, f"bitpanda:{uuid}"))
    ctx.invalidate_overlay()
    (f,) = [x for x in by_kind(report_for(ctx), "duplicate") if x.key.startswith("id|")]
    assert f.status == "wahrscheinlich" and {t.tx_id for t in f.txs} == {"KX", "PF-S-000001"}
    assert note_identity_keys("koinly", "Bitpanda", f"txhash={uuid}") == {f"bitpanda:{uuid}"}
    assert note_identity_keys("koinly", "Bitpanda", "AC2F0000000000000000000000000ACD") == set()  # keine UUID


# ----------------------------------------------------------------------------------------------------
# Asset- und Kurszuordnung, Migration, Kurse, Schätzungen
# ----------------------------------------------------------------------------------------------------

def th_like_ctx(cfg) -> AppContext:
    rows = [tx("C1", "2025-06-01T10:00:00Z", "deposit", to=("Bitpanda", "EUR", "100"), value="100"),
            tx("C2", "2025-06-02T07:00:00Z", "buy", frm=("Bitpanda", "EUR", "30"), to=("Bitpanda", "TH", "5000"),
               value="30")]
    assets = [*ASSETS, asset("TH", "TH", "coingecko", "team-heretics-fan-token")]
    ctx = make_ctx(cfg, rows, assets, valuation="2025-06-03")
    series = ctx.prices.series_for(ctx.portfolio().assets["TH"])
    assert series is not None and series.endswith("cg:team-heretics-fan-token")
    ctx.store.upsert_quotes([Quote(series, 0.02, "EUR", NOW, "coingecko")])
    return ctx


def test_provider_symbol_with_other_coins_price_source_is_flagged(cfg):
    ctx = th_like_ctx(cfg)
    before = fingerprint(ctx.db)
    rep = report_for(ctx)
    (f,) = [x for x in by_kind(rep, "asset") if x.key.startswith("provider|")]
    assert f.status == "belegt" and "Threshold Network" in f.title
    assert any("threshold-network-token" in k for k in f.known)
    assert any("Angezeigte Bewertung: 5.000 × 0,02 € = 100,00" in k for k in f.known)
    assert any("3,33-facher Kaufkurs" in e for e in f.evidence)
    assert f.scenario is not None
    assert f.scenario.rows == [("Wert TH", "100,00\xa0€", "30,00\xa0€ (Kaufkurs 02.06.2025)")]
    assert "TH@BITPANDA" in f.identifiers
    assert f.positions == [("Bitpanda", "TH")]  # nicht EUR
    assert ctx.portfolio().assets["TH"].quote_id == "team-heretics-fan-token"  # Zuordnung unverändert
    assert fingerprint(ctx.db) == before


def test_migration_candidate_by_ratio_and_spam_status(cfg):
    rows = [tx("R1", "2025-01-10T10:00:00Z", "deposit", to=("Wallet V", "RPX", "123456789.5"), value="0"),
            tx("R2", "2025-05-10T10:00:00Z", "deposit", to=("Wallet V", "RPX#2", "123.4567895"), value="0")]
    assets = [*ASSETS, asset("RPX", koinly_id="RPX;1"),
              asset("RPX#2", "RPX (zweite ID)", koinly_id="RPX;2", status="spam",
                    category="Krypto: Spam/Airdrop (wertlos)")]
    ctx = make_ctx(cfg, rows, assets)
    (f,) = by_kind(report_for(ctx), "migration")
    assert f.status == "verdacht" and "RPX → RPX#2 (10^6 : 1)" in f.title
    assert any("Spam" in e for e in f.evidence)
    assert any("Contract-Adressen" in u for u in f.uncertainty)
    assert f.scenario is not None and "keine Abschreibung" in f.scenario.text.replace("Es erfolgt keine", "keine")
    assert ctx.portfolio().assets["RPX#2"].status == "spam"  # unverändert


def test_reconstructed_bookings_and_compensation_entry(cfg):
    rows = [tx("D0", "2025-01-02", "buy", to=("Depot D", "WKN:A0B1C2", "10"), value="100")]
    rec = [
        ("D1", "2025-02-03", "4", "40", "ausgleich|WKN:A0B1C2|2025-02-03", "RECONSTRUCTED_GAP;AVG_PRICE",
         "Ausgleichskauf: Bestand laut Depot minus belegte Käufe"),
        ("D2", "2025-03-03", "2", "20", "sparplan|WKN:A0B1C2|2025-03-03", "RECONSTRUCTED_PLAN;SAVINGS_PLAN;AVG_PRICE",
         "Sparplan rekonstruiert"),
        ("D3", "2025-04-01", "2", "20", "sparplan|WKN:A0B1C2|2025-04-01", "RECONSTRUCTED_PLAN;SAVINGS_PLAN;AVG_PRICE",
         "Sparplan rekonstruiert"),
    ]
    for tid, d, q, v, ref, flag, note in rec:
        r = tx(tid, d, "buy", to=("Depot D", "WKN:A0B1C2", q), value=v)
        r["source"], r["source_ref"], r["flag"], r["note"] = "reconstructed", ref, flag, note
        rows.append(r)
    rows.append(tx("D9", "2025-05-02", "sell", frm=("Depot D", "WKN:A0B1C2", "12"), value="150"))
    ctx = make_ctx(cfg, rows, ASSETS)
    rep = report_for(ctx)
    est = {f.title: f for f in by_kind(rep, "estimated")}
    aus = est["Ausgleichsbuchung Depot D: WKN:A0B1C2"]
    plan = next(f for t, f in est.items() if t.startswith("rekonstruierte Sparplan-Ausführungen"))
    assert aus.status == "belegt" and plan.status == "belegt" and len(plan.txs) == 2
    assert any("AVG_PRICE" in k for k in aus.known)
    # FIFO: Verkauf von 12 verbraucht 10 belegte + 2 aus der Ausgleichsbuchung → Steuerauswertung 2025 betroffen
    assert any(e.startswith("Abgänge 2025: 2 Stück aus diesen Lots") for e in aus.evidence)
    assert any("Offene Lots aus diesen Buchungen: 2 Stück" in e for e in aus.evidence)
    assert sum(len(f.txs) for f in est.values() if f.key.startswith("estimated|")) == 3


def test_missing_and_old_prices_with_source_and_age(cfg):
    rows = [tx("O1", "2025-01-05T10:00:00Z", "buy", frm=("Börse M", "EUR", "50"), to=("Börse M", "ORX", "100"),
               value="50"),
            tx("O2", "2025-01-06T10:00:00Z", "deposit", to=("Börse M", "BRX", "1000"), value="0")]
    manual = [{"asset_id": "ORX", "date": "2025-01-05", "price_eur": "0.5", "source": "manual_prices"}]
    ctx = make_ctx(cfg, rows, [*ASSETS, asset("ORX"), asset("BRX")], manual=manual)
    rep = report_for(ctx)
    prices = {f.assets[0]: f for f in by_kind(rep, "price") if f.key.startswith("price|")}
    orx, brx = prices["ORX"], prices["BRX"]
    assert orx.status == "belegt" and "0 € bewertet" in orx.title
    assert any("manueller Kurs vom 05.01.2025: 0,5 €" in k and "nicht verwendet" in k for k in orx.known)
    assert any("kein Kurspunkt" in k for k in brx.known)
    assert all("Marktwert ist unbekannt" in f.uncertainty[0] for f in (orx, brx))


# ----------------------------------------------------------------------------------------------------
# Transfers
# ----------------------------------------------------------------------------------------------------

def test_transfer_candidates_side_by_side_without_linking(cfg):
    rows = [tx("E0", "2025-02-01T09:00:00Z", "deposit", to=("Börse X", "ETH", "2"), value="5000"),
            tx("W1", "2025-02-02T10:00:00Z", "withdrawal", frm=("Börse X", "ETH", "1")),
            tx("D1", "2025-02-02T10:40:00Z", "deposit", to=("Wallet B", "ETH", "0.998"), value="2500"),
            tx("W2", "2025-02-05T10:00:00Z", "withdrawal", frm=("Wallet B", "ETH", "0.5")),
            tx("D2", "2025-02-05T10:01:00Z", "deposit", to=("Wallet C", "ETH", "0.4995"), value="1250")]
    rows[2]["note"] = f"txhash=0x{H(9)}"
    rows[3]["note"], rows[4]["note"] = f"txhash=0x{H(10)}", f"txhash=0x{H(10)}"
    ctx = make_ctx(cfg, rows, ASSETS)
    before = fingerprint(ctx.db)
    tr = {(f.status, f.accounts[0]): f for f in by_kind(report_for(ctx), "transfer")}
    hashed = tr[("wahrscheinlich", "Wallet B")]
    timed = tr[("verdacht", "Börse X")]
    (w, d, why) = timed.pairs[0]
    assert (w.tx_id, d.tx_id) == ("W1", "D1") and "99,8 %" in why and "Netzwerk" in why
    assert hashed.pairs[0][0].tx_id == "W2" and "gleicher Transaktions-Hash" in hashed.pairs[0][2]
    assert "keine Verknüpfung angelegt" in timed.scenario.text.replace("Es wird keine Verknüpfung angelegt",
                                                                       "keine Verknüpfung angelegt")
    assert fingerprint(ctx.db) == before
    assert not ctx.db.q("SELECT 1 FROM journal_tx")  # keine Verknüpfung, keine Buchung


# ----------------------------------------------------------------------------------------------------
# Bestandsabgleich gegen externe Bestände
# ----------------------------------------------------------------------------------------------------

def add_wallet(ctx: AppContext, account: str, balances: dict[str, str], *, observed: datetime, complete: bool,
               sid: int) -> None:
    stamp = observed.strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx.db.x("INSERT INTO data_source(id, kind, provider, name, account, address, status, coverage_json, created_at, "
             "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
             (sid, "wallet", "kaspa", f"Wallet {sid}", account, f"kaspa:test{sid}", "synced" if complete else
              "partial", json.dumps({"complete": complete, "gaps": []}), stamp, stamp))
    for key, q in balances.items():
        ctx.db.x("INSERT INTO ds_balance(source_id, asset_key, qty, observed_at) VALUES (?,?,?,?)",
                 (sid, key, q, stamp))


def test_holdings_reconciliation_distinguishes_internal_and_external(cfg):
    rows = [tx("H1", "2025-03-01T00:00:00Z", "deposit", to=("Wallet 1", "KAS", "100"), value="10"),
            tx("H2", "2025-03-01T00:00:00Z", "deposit", to=("Wallet 2", "KAS", "200"), value="20"),
            tx("H3", "2025-03-01T00:00:00Z", "deposit", to=("Wallet 3", "KAS", "300"), value="30"),
            tx("H4", "2025-03-01T00:00:00Z", "deposit", to=("Wallet 4", "KAS", "400"), value="40")]
    holdings = [{"asset_id": "KAS", "account": "Wallet 4", "qty": "400"}]
    ctx = make_ctx(cfg, rows, ASSETS, holdings)
    add_wallet(ctx, "Wallet 1", {"KAS": "100"}, observed=NOW - timedelta(hours=1), complete=True, sid=1)
    add_wallet(ctx, "Wallet 2", {"KAS": "199.5"}, observed=NOW - timedelta(hours=1), complete=True, sid=2)
    add_wallet(ctx, "Wallet 3", {"KAS": "300"}, observed=NOW - timedelta(days=5), complete=True, sid=3)
    before = fingerprint(ctx.db)
    rep = report_for(ctx)
    st = {h.account: h for h in rep.holdings}
    assert st["Wallet 1"].status == "extern_ok"
    assert st["Wallet 2"].status == "extern_diff" and st["Wallet 2"].diff == Decimal("-0.5")
    assert st["Wallet 3"].status == "extern_unsicher"  # gleicher Wert, aber veraltet: kein „stimmt“
    assert "Abruf älter als 48 h" in st["Wallet 3"].explanations
    assert st["Wallet 4"].status == "intern_ok" and st["Wallet 4"].observed is None
    (f,) = by_kind(rep, "holdings")
    assert f.status == "belegt" and "Wallet 2" in f.title
    assert any("Netzwerkgebühren" in s for s in f.suspected)
    assert fingerprint(ctx.db) == before
    assert ctx.db.scalar("SELECT qty FROM ds_balance WHERE source_id=2") == "199.5"  # nie ersetzt


# ----------------------------------------------------------------------------------------------------
# Nur lesend, deterministisch, Aktionen nur über Vorschau und ausdrückliches Übernehmen
# ----------------------------------------------------------------------------------------------------

def test_diagnosis_page_and_rescan_change_nothing(cfg):
    rows = popkat_like_rows() + kaspa_like_rows()
    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=rows, assets=TOKA_ASSETS, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        led = ctx.ledger()
        balances, lots = dict(led.balances), [(x.acq_tx, x.qty, x.cost) for x in led.lots]
        before = fingerprint(ctx.db)
        pages = [c.get("/quality/diagnose"), c.get("/quality/diagnose?kind=duplicate&status=verdacht"),
                 c.get("/quality/diagnose")]
        assert all(p.status_code == 200 for p in pages)
        assert fingerprint(ctx.db) == before
        reps = [report_for(ctx), diagnose(collect(ctx))]
        assert reps[0].signature() == reps[1].signature() and reps[0].signature()
        assert fingerprint(ctx.db) == before
        led2 = ctx.ledger()
        assert dict(led2.balances) == balances and [(x.acq_tx, x.qty, x.cost) for x in led2.lots] == lots
        html = pages[0].text
        body = html[html.index('class="stack diag"'):html.index("</main>") if "</main>" in html else len(html)]
        # Aktionen nur als ausdrückliche POST-Formulare der Diagnose (Vorschau ist ein GET ohne Schreibzugriff)
        assert "hx-post" not in body and "hx-delete" not in body
        actions = re.findall(r'<form method="post" action="([^"]+)"', body)
        assert actions and all(a.startswith("/quality/diagnose/") for a in actions)
        assert body.count("<form") == len(actions)
        assert "Empfehlung" in body and "/quality/diagnose/plan?f=" in body
        assert "Szenario (hypothetisch)" in body and "intern konsistent" in body
        assert "zuerst ansehen" in body
        assert "/quality/diagnose" in c.get("/quality").text


# ----------------------------------------------------------------------------------------------------
# Schutzregeln für künftige Importe und Synchronisierungen
# ----------------------------------------------------------------------------------------------------

def client_with_import(cfg, rows, assets, valuation="2025-03-31"):
    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=rows, assets=assets, generated_at=f"{valuation}T20:00:00Z", valuation_date=valuation,
              extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    c = TestClient(build_app(cfg, start_scheduler=False))
    c.__enter__()
    assert tasks.import_check(c.app.state.ctx, "test").status == "imported"
    c.get("/settings")
    c.token = c.cookies.get("portfolia_csrf")
    return c


def upload(c, name: str, data: str, **form) -> int:
    r = c.post("/journal/csv", data={"csrf_token": c.token, "profile": "auto", "cutoff": "", "cutoff_set": "1",
                                     **form}, files={"file": (name, data.encode(), "text/csv")}, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    return int(re.search(r"/journal/csv/(\d+)", r.headers["location"]).group(1))


def rows_of(c, bid: int) -> list[RowCtx]:
    return csv_service(c.app.state.ctx).rows(bid)


PF_HEADER = "tx_id,datetime,type,tag,from_account,from_asset,from_qty,to_account,to_asset,to_qty,fee_asset,fee_qty," \
            "fee_eur,value_eur,note\n"


def test_import_with_same_quantity_on_same_account_goes_to_review(cfg):
    """Neue Gutschrift ohne Hash mit exakt der Menge eines erfassten Transfers (9 h später) → Prüfung, nicht neu."""
    c = client_with_import(cfg, popkat_like_rows()[:2] + popkat_like_rows()[3:], TOKA_ASSETS)
    try:
        before = c.app.state.ctx.db.q("SELECT tx_id, from_qty, to_qty FROM tx ORDER BY tx_id")
        data = PF_HEADER + f"N1,2025-03-10T12:00:00Z,deposit,,,,,Wallet K,TOKA,{TOKA_QTY},,,,0,nachgetragen\n" \
                           "N2,2025-03-12T12:00:00Z,deposit,,,,,Wallet K,TOKA,1.5,,,,0,anderer Vorgang\n"
        bid = upload(c, "manuell.csv", data)
        r1, r2 = rows_of(c, bid)
        assert r1.status == "duplicate" and r1.dup_same_account and "T1" in r1.dup_of
        assert r1.warnings[0].startswith("gleiche Menge wie T1") and not r1.include()
        assert r2.status == "new"
        assert DataSourceService._auto_eligible([r1]) == set()  # nie automatisch übernehmen
        assert c.app.state.ctx.db.q("SELECT tx_id, from_qty, to_qty FROM tx ORDER BY tx_id") == before
    finally:
        c.__exit__(None, None, None)


def test_import_near_reconstructed_booking_goes_to_review(cfg):
    """Echte Abrechnung nahe einer rekonstruierten Sparplan-Buchung (andere Menge) → Prüfung, keine Doppelzählung."""
    rec = tx("RB1", "2025-03-03", "buy", to=("Depot D", "WKN:A0B1C2", "4.0225"), value="50")
    rec["source"], rec["source_ref"], rec["flag"] = "reconstructed", "sparplan|WKN:A0B1C2|2025-03-03", \
        "RECONSTRUCTED_PLAN;SAVINGS_PLAN;AVG_PRICE"
    c = client_with_import(cfg, [rec], ASSETS)
    try:
        data = PF_HEADER + "A1,2025-03-04T10:00:00Z,buy,,Depot D,EUR,50,Depot D,WKN:A0B1C2,3.95,,,,50,Abrechnung\n" \
                           "A2,2025-04-20T10:00:00Z,buy,,Depot D,EUR,50,Depot D,WKN:A0B1C2,3.9,,,,50,Abrechnung\n"
        near, later = rows_of(c, upload(c, "dkb.csv", data))
        assert near.status == "duplicate" and near.dup_of == ["RB1"] and not near.include()
        assert near.warnings[0].startswith("rekonstruierte Buchung RB1 (03.03.2025)")
        assert later.status == "new"
        assert c.app.state.ctx.db.scalar("SELECT COUNT(*) FROM journal_tx") == 0
    finally:
        c.__exit__(None, None, None)


def test_same_hash_two_legitimate_events_from_same_source_are_both_new(cfg):
    """Zweite Bewegung derselben Transaktion (anderer Ereignisindex) wird nicht als „vorhanden“ verschluckt."""
    from app.csvimport import model as M
    from app.csvimport.model import Rec

    c = client_with_import(cfg, [tx("Z0", "2025-01-01T00:00:00Z", "deposit", to=("Wallet E", "ETH", "1"),
                                    value="2000")], TOKA_ASSETS)
    try:
        svc: CsvImportService = csv_service(c.app.state.ctx)
        h = "0x" + H(42)

        def rec(sub: int) -> Rec:
            r = Rec(line=0, ts=datetime(2025, 5, 1, 9, 0, tzinfo=UTC), kind=M.DEPOSIT, in_sym="USDT",
                    in_qty=Decimal("50"), account="Wallet E", txhash=h, value=Decimal("46"), value_ccy="EUR")
            r.ext_id, r.event_key, r.event_line = f"ethereum:{h}#t:f00d#{sub}", f"ethereum:{h}", sub
            return r

        kw = {"source": "sync:ethereum", "profile": "ethereum", "account": "Wallet E", "label": "Test",
              "datasource_id": None, "payload": b"{}", "options": {"cutoff": ""}}
        b1 = svc.ingest([rec(0)], **kw)
        out = svc.commit(b1)
        assert out["created"] == 1, out
        b2 = svc.ingest([rec(0), rec(1)], **kw)
        first, second = rows_of(c, b2)
        assert first.status == "known"  # gleiche Kennung derselben Quelle
        assert second.status == "new", second.warnings  # gleicher Hash, anderer Ereignisindex → eigener Vorgang
    finally:
        c.__exit__(None, None, None)


def koinly_csv(lines: list[str]) -> str:
    head = ("Date,Type,Label,Sending Wallet,Sent Amount,Sent Currency,Receiving Wallet,Received Amount,"
            "Received Currency,Fee Amount,Fee Currency,Net Value (EUR),TxHash,Description,ID\n")
    return head + "".join(x + "\n" for x in lines)


def test_provider_symbol_never_maps_silently_to_other_coin(cfg):
    assets = [*ASSETS, asset("TH", "TH", "coingecko", "team-heretics-fan-token")]
    c = client_with_import(cfg, [tx("Q0", "2025-01-01T00:00:00Z", "deposit", to=("Bitpanda", "EUR", "100"),
                                    value="100")], assets)
    try:
        data = koinly_csv(["2025-05-03 10:00:00 UTC,buy,,Bitpanda,30,EUR,Bitpanda,5000,TH,,,30,,,ID-TH-1",
                           "2025-05-03 11:00:00 UTC,buy,,Wallet Z,30,EUR,Wallet Z,700,TH,,,30,,,ID-TH-2"])
        bid = upload(c, "koinly.csv", data)
        bp, other = sorted(rows_of(c, bid), key=lambda r: r.rec.ext_id)
        assert bp.status == "invalid" and bp.row is None
        assert "„TH“ bei Bitpanda ist Threshold Network" in bp.errors[0] and "TH@BITPANDA" in bp.errors[0]
        assert bp.symbols == {"EUR": "EUR", "TH@BITPANDA": "ambiguous"}
        assert other.status == "new" and other.row["to_asset"] == "TH"  # anderes Konto: Symbol wie bisher
        ov = csv_service(c.app.state.ctx).overview(bid)
        (u,) = [x for x in ov["unknown"] if x["symbol"] == "TH@BITPANDA"]
        assert u["provider"] == "bitpanda"
        page = c.get(f"/journal/csv/{bid}").text
        assert "TH · Bitpanda" in page and "Kürzel des Anbieters" in page
        m = re.search(r'name="sym_(\d+)" value="TH@BITPANDA"', page)
        i = m.group(1)
        assert re.search(rf'name="qid_{i}" value="threshold-network-token"', page)  # Vorschlag: eigenes Asset
        assert re.search(rf'name="id_{i}" value="T"', page)
        # Nutzer legt das Asset für Bitpanda an → Zuordnung gilt nur dort
        r = c.post(f"/journal/csv/{bid}/symbols", data={
            "csrf_token": c.token, f"sym_{i}": "TH@BITPANDA", f"act_{i}": "new", f"id_{i}": "T",
            f"name_{i}": "Threshold Network", f"class_{i}": "crypto", f"qid_{i}": "threshold-network-token"},
            follow_redirects=False)
        assert r.status_code == 303
        bp, other = sorted(rows_of(c, bid), key=lambda r: r.rec.ext_id)
        assert bp.status == "new" and bp.row["to_asset"] == "T"
        assert other.row["to_asset"] == "TH"
        assert c.app.state.ctx.portfolio().assets["TH"].quote_id == "team-heretics-fan-token"  # unverändert
    finally:
        c.__exit__(None, None, None)


def test_resolver_uses_confirmed_asset_and_provider_keys():
    th = AssetInfo("TH", "TH", "crypto", "coingecko", "team-heretics-fan-token")
    t = AssetInfo("T", "Threshold Network", "crypto", "coingecko", "threshold-network-token")
    r = SymbolResolver({"TH": th}, {})
    assert r.resolve_for("TH", "bitpanda") == (None, "ambiguous", "TH@BITPANDA")
    assert r.resolve_for("TH", None) == ("TH", "id", "TH")
    r2 = SymbolResolver({"TH": th, "T": t}, {})
    assert r2.resolve_for("TH", "bitpanda") == ("T", "provider", "TH@BITPANDA")
    r3 = SymbolResolver({"TH": th}, {"TH@BITPANDA": None})
    assert r3.resolve_for("th", "bitpanda") == (None, "ignored", "TH@BITPANDA")
    assert provider_of("csv:bitpanda") == "bitpanda" and provider_of("sync:bitpanda") == "bitpanda"
    assert provider_of("koinly", None, "Bitpanda") == "bitpanda" and provider_of("koinly", None, "Kraken") is None
    assert split_provider_key("TH@BITPANDA") == ("TH", "bitpanda")
    assert split_provider_key("USDC@ETH:0xabc") is None


def test_koinly_id_and_bitpanda_uuid_from_curated_import_are_known(cfg):
    uuid = "0a1b2c3d-2222-4333-8444-555566667777"
    k1 = tx("KK1", "2025-02-01T10:00:00Z", "buy", frm=("Wallet Q", "EUR", "40"), to=("Wallet Q", "ETH", "0.02"),
            value="40")
    k1["source"], k1["source_ref"] = "koinly", "ABCDEF0123456789ABCDEF0123456789"
    k2 = tx("KK2", "2025-02-02T10:00:00Z", "buy", frm=("Bitpanda", "EUR", "40"), to=("Bitpanda", "BTC", "0.001"),
            value="40")
    k2["source"], k2["source_ref"], k2["note"] = "koinly", "FEDCBA9876543210FEDCBA9876543210", f"txhash={uuid}"
    c = client_with_import(cfg, [k1, k2], ASSETS)
    try:
        data = koinly_csv(["2025-02-01 10:00:00 UTC,buy,,Wallet Q,40,EUR,Wallet Q,0.02,ETH,,,40,,,"
                           "abcdef0123456789abcdef0123456789"])
        (r,) = rows_of(c, upload(c, "koinly.csv", data))
        assert r.status == "known" and r.dup_of == ["KK1"] and "koinly-ID" in r.warnings[0]
        svc = csv_service(c.app.state.ctx)
        from app.csvimport import model as M
        from app.csvimport.model import Rec

        rec = Rec(line=0, ts=datetime(2025, 2, 2, 10, 0, tzinfo=UTC), kind=M.TRADE, out_sym="EUR",
                  out_qty=Decimal("40"), in_sym="BTC", in_qty=Decimal("0.001"), account="Bitpanda")
        rec.ext_id, rec.event_key, rec.event_line = f"bitpanda:{uuid}#0", f"bitpanda:{uuid}", 0
        bid = svc.ingest([rec], source="sync:bitpanda", profile="bitpanda", account="Bitpanda", label="Test",
                         datasource_id=None, payload=b"{}", options={"cutoff": ""})
        (r,) = rows_of(c, bid)
        assert r.status == "known" and r.dup_of == ["KK2"] and "Anbieter-ID" in r.warnings[0]
    finally:
        c.__exit__(None, None, None)


def test_unclear_transfers_are_never_auto_committed():
    from app.csvimport import model as M
    from app.csvimport.model import Rec

    def rc(idx: int, **kw) -> RowCtx:
        rec = Rec(line=idx, ts=datetime(2025, 1, 1, tzinfo=UTC), kind=M.WITHDRAWAL, out_sym="ETH",
                  out_qty=Decimal("1"))
        rec.event_key = f"ev{idx}"
        base = {"id": idx, "idx": idx, "line": idx, "rec": rec, "status": "new", "decision": None, "value_in": None,
                "fee_in": None, "pair_ref": None, "pair_conf": None, "pair_ok": None, "tx_id": None,
                "row": {"type": "withdrawal"}}
        base.update(kw)
        return RowCtx(**base)

    plain = rc(1)
    medium = rc(2, pair_ref="b:9", pair_conf="mittel")
    confirmed = rc(3, pair_ref="b:9", pair_conf="mittel", pair_ok=1)
    high = rc(4, pair_ref="b:9", pair_conf="hoch")
    counterpart = rc(5)
    counterpart.counterpart = "I-7"
    assert not plain.transfer_unclear and medium.transfer_unclear and not confirmed.transfer_unclear
    assert not high.transfer_unclear and counterpart.transfer_unclear
    assert DataSourceService._auto_eligible([plain, medium, confirmed, high, counterpart]) == {1, 3, 4}


def test_automatic_price_source_respects_provider_identity():
    th = AssetInfo("TH", "TH", "crypto")
    d = SourceService._provider_identity(th, {"Bitpanda"})
    assert d is not None and d.coin_id == "threshold-network-token" and d.confidence == "hoch"
    d2 = SourceService._provider_identity(th, {"Bitpanda", "Wallet Z"})
    assert d2 is not None and d2.coin_id == "threshold-network-token" and d2.confidence == "niedrig"
    assert SourceService._provider_identity(th, {"Wallet Z"}) is None
    assert SourceService._provider_identity(AssetInfo("BTC", "Bitcoin", "crypto"), {"Bitpanda"}) is None


def test_transfer_excess_is_reported_with_position_and_booking(cfg):
    """AP8: Transfer mit mehr Eingang als Ausgang → eigener Befund (Konto/Asset, Buchung, Ursache, Maßnahme)."""
    rows = [tx("b", "2025-01-10", "buy", frm=("Börse", "EUR", "100"), to=("Börse", "TOKA", "10"), value=100)]
    ctx = make_ctx(cfg, rows, [*ASSETS, asset("TOKA")])
    # Import und Journal lehnen das inzwischen ab – Altbuchung aus einer früheren Version direkt in der Test-DB
    ctx.db.x("INSERT INTO journal_tx(tx_id, source, status, ts_utc, date_only, type, from_account, from_asset, "
             "from_qty, to_account, to_asset, to_qty, created_at, updated_at) VALUES ('PF-M-000001', 'manual', "
             "'active', '2025-02-01T10:00:00Z', 0, 'transfer', 'Börse', 'TOKA', '10', 'Wallet', 'TOKA', '12', "
             "'2025-02-01', '2025-02-01')")
    ctx.invalidate_overlay()
    before = fingerprint(ctx.db)
    rep = report_for(ctx)
    f = next(f for f in by_kind(rep, "history") if f.title.startswith("Transfer: mehr empfangen als gesendet"))
    assert f.status == "belegt" and ("Wallet", "TOKA") in f.positions
    assert len(f.txs) == 1 and f.txs[0].type == "transfer" and any("2 mehr empfangen" in k for k in f.known)
    assert f.decision and "nichts automatisch" in f.decision
    assert fingerprint(ctx.db) == before  # read-only
