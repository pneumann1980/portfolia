"""Buchungsprüfung über Quellen und Konten (M27) – synthetische Fälle, die bekannte Problemmuster nachbilden.

Keine echten Daten: Konten, Beträge, Kennungen und Adressen sind erfunden. Abgedeckt sind die 18 Fälle der
Anforderung (wirtschaftliche Dubletten mit Brutto/Netto/Gebühr, mehrtägiger Versatz ohne Beleg, Sparplan ohne
Finanzierung, negative Bestände, Transfers mit Gebühr bzw. über eine Bridge, Abgang an Dritte, inaktive Konten,
Datenquelle mit Fehler bzw. ohne Synchronisation, Mehrfachimport, Idempotenz, Vorschau ohne Schreibzugriff,
Übernehmen mit vollständigem Undo, unvollständige Gebühren, Soll-Ist zum Stichtag, gleichnamige Tokens auf
verschiedenen Netzwerken) sowie Referenzbestände, Gesamtexport und Oberfläche.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis import references as R
from app.diagnosis.collect import collect
from app.diagnosis.engine import diagnose, report_for
from app.diagnosis.recommend import recommend
from app.importer.loader import import_file
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import COLS, asset, by_kind, fingerprint, make_ctx
from tests.test_diagnosis_actions import data_fp

NOW = datetime.now(UTC)


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


AUDIT_ASSETS = [*ASSETS,
                asset("USDT", "Tether", "coingecko", "tether"),
                asset("USDT-BSC", "Tether (BNB Chain)", "coingecko", "tether"),
                asset("USDC", "USD Coin", "coingecko", "usd-coin"),
                asset("USDC.E", "Bridged USDC (Avalanche)", "coingecko", "usd-coin"),
                asset("TOKX", "Token X")]


def src(r: dict, source: str, ref: str | None = None) -> dict:
    r["source"], r["source_ref"] = source, ref or f"REF-{r['tx_id']}"
    return r


def koinly(r: dict) -> dict:
    return src(r, "koinly")


def api(r: dict, uuid: str) -> dict:
    """Buchung der Börsen-API nach einem Portfolia-Gesamtexport (Herkunft bleibt in ``source`` erhalten)."""
    return src(r, "portfolia:sync:bitpanda", f"bitpanda:{uuid}#0")


def journal_deposit(ctx, tx_id: str, ts: str, account: str, aid: str, qty: str, fee: str | None, uuid: str) -> None:
    """App-Buchung einer Datenquelle (Zugang brutto, Gebühr im selben Asset)."""
    stamp = "2025-06-01T00:00:00Z"
    ctx.db.x("INSERT INTO journal_tx(tx_id, source, external_id, status, ts_utc, type, to_account, to_asset, to_qty, "
             "fee_asset, fee_qty, value_eur, created_at, updated_at, event_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (tx_id, "sync:bitpanda", f"bitpanda:{uuid}#0", "active", ts, "deposit", account, aid, qty,
              aid if fee else None, fee, None, stamp, stamp, f"bitpanda:{uuid}"))
    ctx.invalidate_overlay()


def usd_rows() -> list[dict]:
    """Muster Fall A: Einzahlungen je zweimal – Steuertool netto, Börsen-API brutto mit Gebühr, 2–8 min später."""
    k = [("K1", "2023-05-03T08:27:00Z", "410.2"), ("K2", "2023-05-18T14:04:00Z", "220"),
         ("K3", "2023-06-06T11:24:00Z", "180"), ("K4", "2023-06-29T14:20:00Z", "640")]
    a = [("PF-S-000101", "2023-05-03T08:29:00Z", "416.35", "6.15"),
         ("PF-S-000102", "2023-05-18T14:08:00Z", "223.3", "3.3"),
         ("PF-S-000103", "2023-06-06T11:28:00Z", "182.7", "2.7"),
         ("PF-S-000104", "2023-06-29T14:24:00Z", "649.6", "9.6")]
    rows = [koinly(tx(i, ts, "deposit", to=("Börse B", "USD", q), value=q)) for i, ts, q in k]
    rows += [api(tx(i, ts, "deposit", to=("Börse B", "USD", q), fee=("USD", fee, ""), value=q), f"u-{n}")
             for n, (i, ts, q, fee) in enumerate(a)]
    # Käufe aus den Einzahlungen: verbrauchen genau einen Zugang
    rows += [koinly(tx(f"B{n}", ts.replace(":00Z", ":30Z").replace("T08:27", "T09:27").replace("T14:04", "T15:04")
                       .replace("T11:24", "T12:24").replace("T14:20", "T15:20"), "buy",
                       frm=("Börse B", "USD", q), to=("Börse B", "KAS", "1000"), value=q))
             for n, (_i, ts, q) in enumerate(k)]
    return rows


def econ(rep, aid: str, status: str | None = None) -> list:
    return [f for f in by_kind(rep, "duplicate") if f.key.startswith("econ|") and f"· {aid}" in f.title
            and (status is None or f.status == status)]


# ----------------------------------------------------------------------------------------------------
# 1, 11, 14, 15: wirtschaftliche Dubletten, Vorschau, Übernehmen, Undo
# ----------------------------------------------------------------------------------------------------

def test_01_usd_double_deposits_gross_net_fee_explain_the_fictitious_position(cfg):
    ctx = make_ctx(cfg, usd_rows(), AUDIT_ASSETS)
    before = fingerprint(ctx.db)
    rep = report_for(ctx)
    assert ctx.ledger().balances[("Börse B", "USD")] == Decimal("1450.2")  # Steuertool netto 0 + API netto 1.450,20
    (f,) = econ(rep, "USD")
    assert f.status == "wahrscheinlich" and f.priority == 1 and len(f.pairs) == 4
    assert {(a.tx_id, b.tx_id) for a, b, _w in f.pairs} == {("K1", "PF-S-000101"), ("K2", "PF-S-000102"),
                                                            ("K3", "PF-S-000103"), ("K4", "PF-S-000104")}
    ev = " ".join(f.evidence)
    assert "brutto 416,35, Gebühr 6,15, netto 410,2 USD" in ev
    assert any("Gebühr erklärt" in w or "netto gleich" in w for _a, _b, w in f.pairs)
    assert any("genau einen der beiden Zugänge" in w for _a, _b, w in f.pairs)  # Finanzierung der Käufe
    label, cur, alt = f.scenario.rows[0]
    assert label == "Bestand Börse B · USD" and cur == "1.450,2" and alt.startswith("0 ")
    # Zerlegung nach Quellen erklärt den Bestand vollständig
    (b,) = [x for x in by_kind(rep, "holdings") if x.key == "breakdown|Börse B|USD"]
    assert any("Koinly-Import" in k and "Saldo ±0 USD" in k for k in b.known)
    assert any("Saldo +1.450,2 USD" in k for k in b.known)
    rec = recommend(rep, f)
    assert rec.primary is None and rec.option("link_econ").params[0].default == []  # Sammelbefund: keine Vorauswahl
    assert rec.option("hide_custom") is None  # Dublette wird verknüpft, nicht frei gelöscht
    assert len(f.children) == 4  # jeder Vorgang einzeln prüf- und freigebbar
    case = rep.by_id(f.children[0])
    crec = recommend(rep, case)
    assert crec.primary.key == "link_econ" and crec.primary.params == [] and crec.option("link_econ_swap")
    assert "nicht bewiesen" in crec.text and case.data["case"]["missing"]
    assert fingerprint(ctx.db) == before


def test_11_identical_transactions_from_two_curated_imports(cfg):
    rows = [src(tx("K1", "2025-03-01T10:00:00Z", "deposit", to=("Depot S", "EUR", "500"), value="500"), "koinly"),
            src(tx("S1", "2025-03-01T10:00:30Z", "deposit", to=("Depot S", "EUR", "500"), value="500"), "scalable"),
            src(tx("S2", "2025-03-03T10:00:00Z", "deposit", to=("Depot S", "EUR", "20"), value="20"), "scalable")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    (f,) = econ(report_for(ctx), "EUR")
    assert f.status == "wahrscheinlich" and [(a.tx_id, b.tx_id) for a, b, _w in f.pairs] == [("K1", "S1")]
    assert "Import · scalable" in f.title or "Import · scalable" in " ".join(f.known)


def test_14_15_preview_writes_nothing_apply_links_as_duplicate_and_undo_restores(cfg):
    ctx = make_ctx(cfg, usd_rows(), AUDIT_ASSETS)
    before = data_fp(ctx.db)
    rep = report_for(ctx)
    (f,) = econ(rep, "USD")
    assert A.build_plan(ctx, rep, f, "link_econ", None).errors  # Sammelbearbeitung nur mit Auswahl
    sel = {"pairs": [c for c, _l in recommend(rep, f).option("link_econ").params[0].choices]}
    plan = A.build_plan(ctx, rep, f, "link_econ", sel, given=True)
    assert not plan.errors and len(plan.ops) == 4
    assert all(op.kind == "hide" and op.mode == "duplicate" and op.badge == "Doppelbuchung" for op in plan.ops)
    assert {op.target for op in plan.ops} == {"PF-S-000101", "PF-S-000102", "PF-S-000103", "PF-S-000104"}
    eff = A.preview(ctx, rep, plan)
    assert eff is not None
    assert data_fp(ctx.db) == before  # 14: Vorschau ändert nichts
    res = A.apply(ctx, f.id, "link_econ", plan.params, plan.token)
    assert res.ok
    assert ctx.ledger().balances.get(("Börse B", "USD"), Decimal(0)) == 0
    logs = [json.loads(r["after_json"]) for r in ctx.db.q("SELECT after_json FROM journal_log WHERE "
                                                           "action='import_delete'")]
    assert sorted(x["duplicate_of"] for x in logs) == ["K1", "K2", "K3", "K4"]  # verknüpft, nicht nur gelöscht
    assert ctx.db.scalar("SELECT COUNT(*) FROM tx_override WHERE action='delete'") == 4  # Import-Datei unverändert
    assert not econ(report_for(ctx), "USD")
    (d,) = A.decisions(ctx.db)
    assert A.undo(ctx, d.id).ok
    assert data_fp(ctx.db) == before  # 15: vollständig zurückgenommen
    assert ctx.ledger().balances[("Börse B", "USD")] == Decimal("1450.2")


def test_app_booking_of_data_source_is_covered_not_deleted(cfg):
    rows = [koinly(tx("K1", "2023-05-03T08:27:00Z", "deposit", to=("Börse B", "USD", "410.2"), value="380"))]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    journal_deposit(ctx, "PF-S-000001", "2023-05-03T08:29:00Z", "Börse B", "USD", "416.35", "6.15", "abc-1")
    rep = report_for(ctx)
    (f,) = econ(rep, "USD")
    plan = A.build_plan(ctx, rep, f, "link_econ", None)
    assert [(op.kind, op.target, op.link) for op in plan.ops] == [("cover", "PF-S-000001", "K1")]
    did = A.apply(ctx, f.id, "link_econ", plan.params, plan.token).decision_id
    assert ctx.db.q1("SELECT status FROM journal_tx WHERE tx_id='PF-S-000001'")["status"] == "active"
    assert ctx.ledger().balances[("Börse B", "USD")] == Decimal("410.2")
    assert A.undo(ctx, did).ok and ctx.ledger().balances[("Börse B", "USD")] == Decimal("820.4")


# ----------------------------------------------------------------------------------------------------
# 2, 16: kein Beleg → ungeklärt; unvollständige Gebühren
# ----------------------------------------------------------------------------------------------------

def test_02_eur_same_amount_days_apart_stays_unresolved(cfg):
    rows = [koinly(tx("K1", "2026-03-10T01:33:00Z", "deposit", to=("Börse B", "EUR", "1500"), value="1500")),
            api(tx("A1", "2026-03-14T01:13:00Z", "deposit", to=("Börse B", "EUR", "1500"), value="1500"), "u-1")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-04-01")
    rep = report_for(ctx)
    (f,) = econ(rep, "EUR")
    assert f.status == "hinweis" and f.priority == 3 and "ohne ausreichenden Beleg" in f.title
    assert any("kein Beleg" in u for u in f.uncertainty)
    rec = recommend(rep, f)
    assert rec.primary is None and rec.conditional  # keine empfohlene Korrektur
    opt = rec.option("link_econ")
    assert not opt.recommended and "Kein ausreichender Beleg" in opt.caution
    assert not {"K1", "A1"} & rep.index.dup_txs  # andere Regeln sehen beide als eigenständig


def test_systematic_offset_with_same_time_of_day_is_suspicion(cfg):
    """Sparplan: Einzahlung im Steuertool je 3 Tage später als in der API, gleiche Uhrzeit, regelmäßig."""
    rows = []
    for n in range(4):
        d = date(2025, 2, 3) + timedelta(days=7 * n)
        rows.append(api(tx(f"A{n}", f"{d}T09:15:00Z", "deposit", to=("Börse B", "EUR", "40"), value="40"), f"s{n}"))
        rows.append(koinly(tx(f"K{n}", f"{d + timedelta(days=3)}T09:14:00Z", "deposit", to=("Börse B", "EUR", "40"),
                              value="40")))
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2025-04-01")
    (f,) = econ(report_for(ctx), "EUR")
    assert f.status == "verdacht" and len(f.pairs) == 4
    assert all("regelmäßiger Versatz von 3 Tagen" in w for _a, _b, w in f.pairs)


def test_16_incomplete_fee_information_is_not_forced_into_a_match(cfg):
    rows = [koinly(tx("K1", "2023-05-18T14:04:00Z", "deposit", to=("Börse B", "USD", "220"), value="200")),
            api(tx("A1", "2023-05-18T14:08:00Z", "deposit", to=("Börse B", "USD", "223.3"), value="203"), "u-1")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    rep = report_for(ctx)
    assert not econ(rep, "USD")  # 220 ≠ 223,3 ohne ausgewiesene Gebühr – keine erzwungene Zuordnung
    (b,) = [x for x in by_kind(rep, "holdings") if x.key == "breakdown|Börse B|USD"]
    ev = " ".join(b.evidence)
    assert "+220 USD (K1)" in ev and "+223,3 USD (A1)" in ev  # beide als „nur in einer Quelle“ ausgewiesen


# ----------------------------------------------------------------------------------------------------
# 3, 4: negative Bestände
# ----------------------------------------------------------------------------------------------------

def test_03_plan_execution_without_funding_explains_negative_cash(cfg):
    rows = [src(tx("D1", "2026-09-01T10:00:00Z", "deposit", to=("Depot S", "EUR", "200"), value="200"), "scalable"),
            src(tx("B1", "2026-09-05T10:00:00Z", "buy", frm=("Depot S", "EUR", "199.8"),
                   to=("Depot S", "WKN:A0B1C2", "2"), value="199.8"), "scalable"),
            src(tx("I1", "2026-09-15T10:00:00Z", "deposit", tag="dividend", to=("Depot S", "EUR", "0.05"),
                   value="0.05"), "scalable"),
            src(tx("SP-1-20261001", "2026-10-01T10:00:00Z", "buy", frm=("Depot S", "EUR", "75"),
                   to=("Depot S", "WKN:A0B1C2", "1"), value="75"), "portfolia:sparplan"),
            src(tx("SP-2-20261001", "2026-10-01T10:00:00Z", "buy", frm=("Depot S", "EUR", "75"),
                   to=("Depot S", "WKN:US0001", "1"), value="75"), "portfolia:sparplan")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-02")
    rep = report_for(ctx)
    (f,) = [x for x in by_kind(rep, "history") if x.key == "issue|negative_balance|EUR|Depot S"]
    assert any("kein historischer Zwischenstand" in k for k in f.known)
    cause = " ".join(f.suspected)
    assert "2 Sparplan-Ausführung(en) ohne Finanzierung" in cause and "SP-1-20261001" in cause
    assert "endet am 15.09.2026" in cause and "ohne diese Ausführungen 0,25 EUR" in cause
    assert f.data["type"] == "negative" and recommend(rep, f).links[-1].url == "/plans"
    assert recommend(rep, f).option("adjust") is None  # keine Ausgleichsbuchung


def test_04_negative_usdt_from_missing_counterpart_and_double_withdrawal(cfg):
    rows = [koinly(tx("F1", "2024-10-01T09:00:00Z", "deposit", to=("Wallet A", "USDT", "500"), value="460")),
            koinly(tx("W1", "2024-10-02T09:00:00Z", "withdrawal", frm=("Wallet A", "USDT", "500"), value="460")),
            koinly(tx("S1", "2024-10-03T09:00:00Z", "sell", frm=("Börse B", "USDT", "480"),
                      to=("Börse B", "EUR", "440"), value="440")),
            # doppelte Auszahlung aus zwei Quellen auf einem anderen Konto
            koinly(tx("F2", "2024-10-05T09:00:00Z", "deposit", to=("Börse C", "USDT", "73.41922817"), value="68")),
            koinly(tx("X1", "2024-10-07T13:13:23Z", "withdrawal", frm=("Börse C", "USDT", "73.41922817"), value="68")),
            api(tx("X2", "2024-10-07T13:16:13Z", "withdrawal", frm=("Börse C", "USDT", "61.18"),
                   fee=("USDT", "12.23922817", ""), value="57"), "u-x")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2024-12-31")
    rep = report_for(ctx)
    (neg,) = [x for x in by_kind(rep, "history") if x.key == "issue|negative_balance|USDT|Börse B"]
    assert any("Eigenübertrags" in s and "W1" in s for s in neg.suspected)  # fehlender Eingang
    (neg2,) = [x for x in by_kind(rep, "history") if x.key == "issue|negative_balance|USDT|Börse C"]
    assert any("doppelte Auszahlung" in s and "vollständig" in s for s in neg2.suspected)
    assert neg2.scenario is not None and neg2.scenario.rows[0][2].startswith("0 ")
    (f,) = econ(rep, "USDT")
    assert f.status == "wahrscheinlich" and [(a.tx_id, b.tx_id) for a, b, _w in f.pairs] == [("X1", "X2")]


# ----------------------------------------------------------------------------------------------------
# 5, 6, 7, 18: Transfers, Bridge, Abgang an Dritte, gleichnamige Tokens
# ----------------------------------------------------------------------------------------------------

def test_05_transfer_between_own_wallets_with_network_fee_is_not_a_loss(cfg):
    rows = [tx("E0", "2025-02-01T09:00:00Z", "deposit", to=("Börse X", "ETH", "2"), value="5000"),
            tx("W1", "2025-02-02T10:00:00Z", "withdrawal", frm=("Börse X", "ETH", "1"), fee=("ETH", "0.002", "5"),
               value="2500"),
            tx("D1", "2025-02-02T10:20:00Z", "deposit", to=("Wallet B", "ETH", "1"), value="2500")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    rep = report_for(ctx)
    (t,) = by_kind(rep, "transfer")
    assert t.data["pairs"] == [["W1", "D1"]] and t.status == "verdacht"
    assert not by_kind(rep, "loss")


def test_06_bridge_with_token_change_is_a_transfer_candidate(cfg):
    w = tx("W1", "2025-05-01T10:00:00Z", "withdrawal", frm=("Wallet ETH", "USDC", "1000"), value="920")
    d = tx("D1", "2025-05-01T10:30:00Z", "deposit", to=("Wallet AVAX", "USDC.E", "998"), value="918",
           related="USDC")
    rows = [tx("F1", "2025-04-01T10:00:00Z", "deposit", to=("Wallet ETH", "USDC", "1000"), value="920"), w, d]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    rep = report_for(ctx)
    (t,) = [x for x in by_kind(rep, "transfer") if x.key.startswith("transfer-alt|")]
    ev = " ".join(t.evidence)
    assert "Tokenwechsel USDC → USDC.E (Bridge/Wrapped/Cross-Chain" in ev and "D1" in ev
    assert not re.search(r"\d+ % \(", ev) and "Matching-Score" in ev  # keine Prozent-„Wahrscheinlichkeit“
    assert t.data["pairs"] == []  # anderes Asset: keine automatische Transfer-Verknüpfung vorgeschlagen
    assert not [x for x in by_kind(rep, "loss") if "W1" in x.data.get("txs", [])]


def test_07_withdrawal_to_foreign_address_is_unresolved_not_a_proven_loss(cfg):
    rows = [tx("F1", "2025-01-01T10:00:00Z", "deposit", to=("Wallet A", "ETH", "1"), value="3000"),
            tx("W1", "2025-03-01T10:00:00Z", "withdrawal", frm=("Wallet A", "ETH", "0.4"), value="900")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    rep = report_for(ctx)
    (f,) = by_kind(rep, "loss")
    assert f.data["class"] == "B" and f.status == "verdacht" and "Ungeklärter Abgang" in f.title
    assert any("kein Nachweis" in u for u in f.uncertainty)
    assert recommend(rep, f).options[-1].dismiss


def test_compromised_account_alone_is_not_a_documented_loss(cfg):
    rows = [tx("F1", "2024-01-01T10:00:00Z", "deposit", to=("Wallet H", "ETH", "1"), value="2000"),
            tx("W1", "2024-06-15T10:00:00Z", "withdrawal", frm=("Wallet H", "ETH", "0.9"), value="2700")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    ctx.db.x("INSERT INTO accounts(import_id, account, broker, depot_group, extra_json) VALUES "
             "((SELECT MAX(id) FROM imports), 'Wallet H', 'X', 'Krypto', ?)",
             (json.dumps({"note": "kompromittiert 15.06.2024"}),))
    ctx.invalidate_data()
    rep = report_for(ctx)
    (f,) = by_kind(rep, "loss")
    assert f.data["class"] == "B" and f.status == "verdacht"  # ungeklärt – kein Verlustnachweis
    assert any("kompromittiert" in e for e in f.evidence)
    assert any("beweist keinen Diebstahl" in u for u in f.uncertainty)


def test_18_same_symbol_on_different_networks_is_never_matched(cfg):
    rows = [tx("F1", "2025-01-01T10:00:00Z", "deposit", to=("Wallet M", "USDT", "250"), value="230"),
            tx("W1", "2025-02-01T10:00:00Z", "withdrawal", frm=("Wallet M", "USDT", "250"), value="230"),
            koinly(tx("D1", "2025-02-01T10:05:00Z", "deposit", to=("Wallet M", "USDT-BSC", "250"), value="230")),
            api(tx("D2", "2025-02-01T10:06:00Z", "deposit", to=("Wallet N", "USDT-BSC", "250"), value="230"), "z")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    rep = report_for(ctx)
    assert not [x for x in by_kind(rep, "duplicate") if x.key.startswith("econ|")]  # andere Asset-ID
    for t in by_kind(rep, "transfer"):
        assert ["W1", "D1"] not in t.data["pairs"] and ["W1", "D2"] not in t.data["pairs"]


# ----------------------------------------------------------------------------------------------------
# 8, 9, 10: inaktive Konten, Datenquelle mit Fehler, fehlende Synchronisation
# ----------------------------------------------------------------------------------------------------

def add_source(ctx, sid: int, account: str, *, status: str, last_success: datetime | None, error: str | None = None,
               balances: dict[str, str] | None = None, observed: datetime | None = None) -> None:
    stamp = (observed or NOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx.db.x("INSERT INTO data_source(id, kind, provider, name, account, address, status, coverage_json, "
             "last_success_at, last_error, last_error_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (sid, "wallet", "kaspa", f"Quelle {sid}", account, f"kaspa:test{sid}", status,
              json.dumps({"complete": status == "synced", "gaps": []}),
              last_success.strftime("%Y-%m-%dT%H:%M:%SZ") if last_success else None, error,
              stamp if error else None, stamp, stamp))
    for key, q in (balances or {}).items():
        ctx.db.x("INSERT INTO ds_balance(source_id, asset_key, qty, observed_at) VALUES (?,?,?,?)",
                 (sid, key, q, stamp))


def test_08_long_inactive_wallet_with_balance_is_never_a_loss(cfg):
    rows = [tx("F1", "2022-01-10T10:00:00Z", "deposit", to=("Wallet Alt", "KAS", "5000"), value="500")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    rep = report_for(ctx)
    (f,) = by_kind(rep, "inactive")
    assert f.status == "hinweis" and "Wallet Alt" in f.title and "Inaktiv seit 10.01.2022" in f.title
    assert any("Inaktivität allein ist kein Verlust" in u for u in f.uncertainty)
    assert any("ohne Datenquelle" in s for s in f.suspected)
    assert not by_kind(rep, "loss")


def test_09_unreachable_api_does_not_report_a_loss(cfg):
    rows = [tx("F1", "2022-01-10T10:00:00Z", "deposit", to=("Wallet K", "KAS", "5000"), value="500")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    add_source(ctx, 1, "Wallet K", status="error", last_success=NOW - timedelta(days=40), error="HTTP 503",
               balances={"KAS": "0"}, observed=NOW - timedelta(days=40))
    rep = report_for(ctx)
    assert not by_kind(rep, "loss")  # veralteter Abruf mit Fehler: kein Verlust
    (row,) = [h for h in rep.holdings if h.account == "Wallet K"]
    assert row.status == "extern_unsicher"
    (f,) = by_kind(rep, "inactive")
    assert any("meldet Fehler" in s for s in f.suspected) and any("HTTP 503" in k for k in f.known)


def test_missing_balance_at_fresh_complete_check_is_unresolved_outflow(cfg):
    rows = [tx("F1", "2025-01-10T10:00:00Z", "deposit", to=("Wallet K", "KAS", "5000"), value="500")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    add_source(ctx, 1, "Wallet K", status="synced", last_success=NOW - timedelta(hours=1), balances={"KAS": "0"},
               observed=NOW - timedelta(hours=1))
    rep = report_for(ctx)
    (f,) = by_kind(rep, "loss")
    assert f.data["class"] == "B" and "fehlen bei der Bestandsprüfung" in f.title


def test_10_missing_sync_history_is_reported_separately_from_last_booking(cfg):
    rows = [tx("F1", "2023-03-01T10:00:00Z", "deposit", to=("Wallet S", "KAS", "100"), value="10")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    add_source(ctx, 1, "Wallet S", status="created", last_success=None)
    rep = report_for(ctx)
    (f,) = by_kind(rep, "inactive")
    assert any(k.startswith("Letzte Buchung: 01.03.2023") for k in f.known)
    assert any("noch nie erfolgreich" in k for k in f.known)
    assert any("noch nie erfolgreich synchronisiert" in s for s in f.suspected)
    (row,) = [h for h in rep.holdings if h.account == "Wallet S"]
    assert row.last_sync is None


# ----------------------------------------------------------------------------------------------------
# 12, 13: Mehrfachimport, wiederholte Diagnose
# ----------------------------------------------------------------------------------------------------

def test_12_13_repeated_import_and_repeated_diagnosis_are_idempotent(cfg):
    ctx = make_ctx(cfg, usd_rows(), AUDIT_ASSETS)
    sig = report_for(ctx).signature()
    before = fingerprint(ctx.db)
    for _ in range(2):
        assert diagnose(collect(ctx)).signature() == sig  # 13: deterministisch, ohne Schreibzugriff
    assert fingerprint(ctx.db) == before
    out = import_file(ctx.db, cfg.import_dir / "kuratiert.zip", ctx.engine_options())  # 12: dieselbe Datei erneut
    assert out.status in ("unchanged", "skipped", "imported")
    ctx.invalidate_data()
    rep = report_for(ctx)
    assert rep.signature() == sig and len(econ(rep, "USD")) == 1


# ----------------------------------------------------------------------------------------------------
# 17: Soll-Ist zum Stichtag, Referenzbestände
# ----------------------------------------------------------------------------------------------------

def test_17_reference_and_observed_balances_are_compared_at_the_same_date(cfg):
    rows = [tx("D1", "2026-03-01T10:00:00Z", "deposit", to=("Börse B", "EUR", "1000"), value="1000"),
            tx("D2", "2026-05-01T10:00:00Z", "deposit", to=("Börse B", "EUR", "500"), value="500"),
            tx("K1", "2025-01-01T10:00:00Z", "deposit", to=("Wallet K", "KAS", "100"), value="10"),
            tx("K2", (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "deposit",
               to=("Wallet K", "KAS", "5"), value="1")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation=NOW.date().isoformat())
    # Ist (Kontoauszug) zum 31.03. = 1.000 – stimmt mit dem Soll zum selben Stichtag, obwohl heute 1.500 gebucht sind
    assert R.add(ctx, "Börse B", "EUR", "1.000,00", "2026-03-31", "statement", "Auszug März").ok
    # Abruf vor der letzten Buchung: Soll zum Abrufzeitpunkt = 100, nicht 105
    add_source(ctx, 1, "Wallet K", status="synced", last_success=NOW - timedelta(hours=1), balances={"KAS": "100"},
               observed=NOW - timedelta(hours=1))
    rep = report_for(ctx)
    row = {(h.account, h.asset): h for h in rep.holdings}
    eur_row = row[("Börse B", "EUR")]
    assert eur_row.status == "ref_ok" and eur_row.soll_at_ref == Decimal("1000") and eur_row.ref_diff == 0
    assert eur_row.computed == Decimal("1500") and eur_row.confidence == "belegt"
    kas = row[("Wallet K", "KAS")]
    assert kas.status == "extern_ok" and kas.computed_at_obs == Decimal("100") and kas.diff == 0
    assert any("nach dem Abruf" in e for e in kas.explanations)
    # zweiter Referenzbestand (jünger) mit Abweichung → Befund, keine Ausgleichsbuchung
    assert R.add(ctx, "Börse B", "EUR", "1450", "2026-06-30").ok
    rep = report_for(ctx)
    (f,) = [x for x in by_kind(rep, "holdings") if x.key.startswith("reference|Börse B|EUR")]
    assert f.status == "belegt" and "(−50)" in f.title
    rec = recommend(rep, f)
    assert rec.option("adjust") is None and rec.options[-1].dismiss
    # fehlender Referenzbestand ist „unbekannt“, nicht 0
    assert row[("Wallet K", "KAS")].reference is None
    assert not R.add(ctx, "Börse B", "EUR", "", "2026-06-30").ok  # leer ≠ 0
    assert not R.add(ctx, "Unbekannt", "EUR", "1", "2026-06-30").ok
    assert not R.add(ctx, "Börse B", "EUR", "1", (NOW.date() + timedelta(days=2)).isoformat()).ok


def test_reference_balance_never_changes_bookings_and_travels_with_export(cfg):
    rows = [tx("D1", "2026-03-01T10:00:00Z", "deposit", to=("Börse B", "EUR", "1000"), value="1000")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-04-01")
    led = dict(ctx.ledger().balances)
    assert R.add(ctx, "Börse B", "EUR", "990", "2026-03-31").ok
    assert dict(ctx.ledger().balances) == led  # Prüfwert, keine Buchung
    from app.fullexport import collect as export_collect

    state = json.loads(export_collect(ctx, set())["state.json"].decode())
    (ref,) = state["reference_balances"]
    assert ref["qty"] == "990" and ref["as_of"] == "2026-03-31" and ref["status"] == "active"
    rid = ctx.db.scalar("SELECT id FROM reference_balance")
    assert R.remove(ctx, rid).ok and not R.remove(ctx, rid).ok
    assert ctx.db.q1("SELECT status FROM reference_balance")["status"] == "deleted"  # nachvollziehbar
    assert {r["action"] for r in ctx.db.q("SELECT action FROM journal_log")} >= {"reference_add", "reference_delete"}


# ----------------------------------------------------------------------------------------------------
# Oberfläche
# ----------------------------------------------------------------------------------------------------

def test_diagnosis_page_shows_sections_deviations_and_reference_form(cfg):
    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=usd_rows(), assets=AUDIT_ASSETS, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        page = c.get("/quality/diagnose").text
        for label in ("Bestandsabweichungen", "Mögliche Doppelbuchungen", "Ungeklärte Transfers", "Inaktive Konten",
                      "Potenzielle Verluste", "Referenzbestände"):
            assert label in page
        assert 'id="bestand"' in page and 'id="referenzen"' in page and "wirtschaftlich gleiche Zugänge" in page
        filtered = c.get("/quality/diagnose?h_asset=USD&h_conf=hinweis").text
        assert "Bestandsabweichungen (0 von 1)" in filtered  # Filter Sicherheit greift (nur Anzeige)
        assert "Bestandsabweichungen (1)" in c.get("/quality/diagnose?h_asset=USD").text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        before = data_fp(ctx.db)
        r = c.post("/quality/diagnose/reference", data={"csrf_token": token, "account": "Börse B", "asset": "USD",
                                                         "qty": "0", "as_of": "2025-06-30", "source": "statement"},
                   follow_redirects=False)
        assert r.status_code == 303 and "referenzen" in r.headers["location"]
        page = c.get("/quality/diagnose").text
        assert "Differenz zum Referenzbestand: USD auf Börse B" in page
        # Referenzbestand berührt keine Buchung
        ref_tables = {"reference_balance", "journal_log", "sqlite_sequence"}
        assert {t for (t,) in ctx.db.q("SELECT name FROM sqlite_master WHERE type='table'")} >= ref_tables
        assert ctx.ledger().balances[("Börse B", "USD")] == Decimal("1450.2")
        assert data_fp(ctx.db) != before  # nur die Referenz-Tabelle hat sich geändert
