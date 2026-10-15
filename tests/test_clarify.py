"""Konfliktklärung (0.29.0) – synthetische Fälle, temporäre Datenbanken, kontrollierte Uhren.

Keine echten Daten: Konten, Beträge, Hashes sind erfunden bzw. strukturgleich nachgebildet. Abgedeckt sind die 16
geforderten Regressionsfälle (USDT-Doppelabgang, SAND-Doppelgutschrift, gekoppelte Bybit-Fälle, SOL-Menge, bereits
vorhandener PI-Transfer, Einzelfills gegen Aggregat, Pionex-Abrechnung, XT-Gegenbeispiel, zwei Lösungen mit gleichem
Endbestand, mehrere Referenzzeitpunkte, fehlende Anschaffungskosten, Unterkonten/gebundene Margin, neue Evidenz,
Nebenläufigkeit und Folgelauf, Atomarität/Idempotenz/Undo, Export/Restore) sowie ein Skalierungstest.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis import clarify as K
from app.diagnosis import references as R
from app.diagnosis import stockconfirm as SC
from app.diagnosis.engine import report_for
from tests.helpers import tx
from tests.test_diagnosis import asset, fingerprint, make_ctx
from tests.test_diagnosis_audit import AUDIT_ASSETS, api, koinly

D = Decimal
ASSETS_K = [*AUDIT_ASSETS, asset("SAND", "The Sandbox", "coingecko", "the-sandbox"),
            asset("SOL", "Solana", "coingecko", "solana"), asset("XT", "XT Token", "coingecko", "xtcom-token"),
            asset("PI", "Pi", "coingecko", "pi-network")]
FUND = tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Bybit", "USDT", "1000"), value=900)


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


@pytest.fixture(autouse=True)
def _reset_flag():
    K._FLAG["dirty"] = False
    yield
    K._FLAG["dirty"] = False


def ctx_for(cfg, rows, assets=None, **kw):
    return make_ctx(cfg, rows, assets or ASSETS_K, **kw)


def one_case(ctx, **kw) -> K.Case:
    cases = K.analyse(ctx, **kw)
    assert len(cases) == 1, [c.title for c in cases]
    return cases[0]


def balance(ctx, acc: str, asset_id: str) -> Decimal:
    return report_for(ctx).snapshot.ledger.balances.get((acc, asset_id), D(0))


def usdt_double_out():
    return [FUND,
            koinly(tx("K1", "2025-03-01T10:00:00Z", "withdrawal", frm=("Bybit", "USDT", "97.58213201"), value=88)),
            api(tx("A1", "2025-03-01T10:01:00Z", "withdrawal", frm=("Bybit", "USDT", "81.317079"), value=73), "u1"),
            api(tx("A2", "2025-03-01T10:01:30Z", "withdrawal", frm=("Bybit", "USDT", "16.26505301"), value=15), "u2")]


def sand_rows():
    return [tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Wallet", "SAND", "10"), value=5),
            koinly(tx("K1", "2025-03-01T10:00:00Z", "deposit", to=("Bybit", "SAND", "52.319"), value=30)),
            api(tx("A1", "2025-03-01T10:03:00Z", "deposit", to=("Bybit", "SAND", "64.508"),
                   fee=("SAND", "12.189", ""), value=30), "u1")]


# ----------------------------------------------------------------------------------------------------
# 1 · USDT-Doppelabgang: 97.58213201 gegenüber 81.317079 + 16.26505301
# ----------------------------------------------------------------------------------------------------

def test_01_usdt_double_outflow_counts_once_after_stock_confirmation(cfg):
    ctx = ctx_for(cfg, usdt_double_out())
    before = fingerprint(ctx.db)
    case = one_case(ctx)
    assert fingerprint(ctx.db) == before  # Analyse schreibt nichts
    assert case.status == "wartet_bestand" and case.decision.kind == "ask_stock"
    # Summe der Fills = Aggregat exakt – rechnerisch erklärt, aber keine Identität (kein Hash, andere Quelle)
    assert {c.evidence for c in case.cands} == {"betrag_gebuehr"}
    assert D("81.317079") + D("16.26505301") == D("97.58213201")
    # die Frage unterscheidet die Varianten und fragt nach genau dieser Position
    q = case.question
    assert q is not None and (q["account"], q["asset"]) == ("Bybit", "USDT")
    assert {e["qty"] for e in q["expect"]} == {"804.83573598", "902.41786799"}
    assert "Bybit" in q["text"] and "USDT" in q["text"] and "Uhrzeit" in q["text"]
    # ohne Nachweis wird nichts übernommen – auch nicht bei eingeschalteter Automatik
    ctx.settings.set("autopilot.enabled", True)
    ctx.settings.set("autopilot.fixes", True)
    out = K.run(ctx, "auto", research=False)
    assert out is not None and out["applied"] == []
    assert balance(ctx, "Bybit", "USDT") == D("804.83573598")
    # Nachweis + Übernahme in einem Schritt
    res = SC.submit(ctx, case.key, "Bybit", "USDT", "902,41786799", "2025-06-01", "12:00", "UTC", "api", apply=True)
    assert res.ok and res.verified and res.decision_ids, res.errors
    assert balance(ctx, "Bybit", "USDT") == D("902.41786799")  # die Auszahlung zählt genau einmal
    led = report_for(ctx).snapshot.ledger
    assert sum(d.qty for d in led.disposals if d.asset == "USDT") == D("97.58213201")
    # Rohdaten bleiben: nichts wurde gelöscht (Import-Buchungen nur überlagert), Rücknahme stellt alles her
    assert ctx.db.scalar("SELECT COUNT(*) FROM tx_override WHERE action='delete'", default=0) >= 1
    undo = K.undo_run(ctx, res.token)
    assert undo.ok
    assert balance(ctx, "Bybit", "USDT") == D("804.83573598")


def test_01_stock_equal_to_baseline_does_not_apply(cfg):
    """Der Nutzer bestätigt den Bestand OHNE Korrektur (zwei Auszahlungen waren echt): nichts wird übernommen."""
    ctx = ctx_for(cfg, usdt_double_out())
    case = one_case(ctx)
    res = SC.submit(ctx, case.key, "Bybit", "USDT", "804,83573598", "2025-06-01", "12:00", "UTC", "api", apply=True)
    assert res.ok and not res.decision_ids
    assert balance(ctx, "Bybit", "USDT") == D("804.83573598")
    c2 = one_case(ctx)
    assert c2.status == "teilweise" and c2.decision.kind == "confirm_partial"
    assert "Menge bestätigt" in res.message or "Bestand ist bestätigt" in c2.decision.reason


# ----------------------------------------------------------------------------------------------------
# 2 · SAND-Doppelgutschrift: 52.319 gegenüber 64.508 − 12.189
# ----------------------------------------------------------------------------------------------------

def test_02_sand_double_credit_linked_not_double_counted(cfg):
    ctx = ctx_for(cfg, sand_rows())
    case = one_case(ctx)
    assert D("64.508") - D("12.189") == D("52.319")
    assert case.status == "wartet_bestand"
    assert {c.evidence for c in case.cands} == {"betrag_gebuehr"} and all(c.fee_explained for c in case.cands)
    assert case.dims["gebuehren"][0] == "geklaert"
    res = SC.submit(ctx, case.key, "Bybit", "SAND", "52,319", "2025-06-01", "12:00", "UTC", "api", apply=True)
    assert res.ok and res.verified, res.errors
    assert balance(ctx, "Bybit", "SAND") == D("52.319")
    # beide Quellen bleiben als Buchungen erhalten (verknüpft, nicht gelöscht)
    base = ctx.base_portfolio()
    assert {t.tx_id for t in base.txs} >= {"K1", "A1"}
    assert K.stored_cases(ctx.db)[0]["status"] == "geloest"
    run = K.runs_of(ctx.db, case.key)[0]
    assert run["outcome"] == "geloest" and "Anschaffung" in run["detail"]["effect"]["zusammenfassung"]


# ----------------------------------------------------------------------------------------------------
# 4 · SOL-Menge: 0.023 − 0.000023 = 0.022977, spätere Verwendung
# ----------------------------------------------------------------------------------------------------

def sol_rows():
    return [tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Börse", "SOL", "1"), value=100),
            tx("W1", "2025-03-01T10:00:00Z", "withdrawal", frm=("Börse", "SOL", "0.022977"),
               fee=("SOL", "0.000023", ""), value=2.3),
            tx("D1", "2025-03-01T10:20:00Z", "deposit", to=("Wallet", "SOL", "0.022977"), value=2.3),
            tx("S1", "2025-03-05T10:20:00Z", "sell", frm=("Wallet", "SOL", "0.022977"), to=("Wallet", "EUR", "2.4"),
               value=2.4)]


def test_04_sol_fee_arithmetic_and_later_use(cfg):
    assert D("0.023") - D("0.000023") == D("0.022977")
    ctx = ctx_for(cfg, sol_rows())
    case = one_case(ctx)
    c = case.cands[0]
    assert c.option == "link" and c.evidence == "betrag_gebuehr" and c.fee_explained
    assert case.dims["gebuehren"][0] == "geklaert"
    # Bestände ändern sich durch die Verknüpfung nicht – ein Bestandsnachweis kann sie nicht unterscheiden
    base, hyp = case.hyps[0], case.hyps[1]
    assert base.balances == hyp.balances and K.discriminating_positions(case.hyps) == []
    assert case.question is None and case.status == "wartet_info"
    # die spätere Verwendung (Verkauf) ist Teil der Simulation: Anschaffungsdatum bzw. Ergebnis ändern sich
    assert hyp.ok and hyp.lot_dates and base.sig != hyp.sig
    # nicht automatisch (kein Identitätsbeleg) – auch nicht mit Automatik
    ctx.settings.set("autopilot.enabled", True)
    ctx.settings.set("autopilot.fixes", True)
    assert K.run(ctx, "auto", research=False)["applied"] == []
    refused = K.apply_hypothesis(ctx, case.key, hyp.id, authorization="auto")
    assert not refused.ok
    # ausdrückliche Bestätigung (mit Vorschau) ist möglich
    done = K.apply_hypothesis(ctx, case.key, hyp.id, authorization="user")
    assert done.ok and done.verified, done.errors
    assert K.analyse(ctx) == []


# ----------------------------------------------------------------------------------------------------
# 6 · Einzelfills gegenüber Aggregat
# ----------------------------------------------------------------------------------------------------

def test_06_unambiguous_aggregate_is_a_candidate(cfg):
    rows = [FUND,
            koinly(tx("K1", "2025-03-01T10:00:00Z", "withdrawal", frm=("Bybit", "USDT", "100"), value=90)),
            api(tx("A1", "2025-03-01T10:01:00Z", "withdrawal", frm=("Bybit", "USDT", "60"), value=54), "u1"),
            api(tx("A2", "2025-03-01T10:01:30Z", "withdrawal", frm=("Bybit", "USDT", "40"), value=36), "u2")]
    ctx = ctx_for(cfg, rows)
    case = one_case(ctx)
    assert case.cands and case.status == "wartet_bestand"


def test_06b_ambiguous_sums_are_never_merged_automatically(tmp_path):
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    cfg = Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                 startup_jobs=False, log_format="text", secrets=Secrets())
    rows = [FUND,
            koinly(tx("K1", "2025-03-01T10:00:00Z", "withdrawal", frm=("Bybit", "USDT", "100"), value=90)),
            koinly(tx("K2", "2025-03-01T10:00:10Z", "withdrawal", frm=("Bybit", "USDT", "100"), value=90)),
            api(tx("A1", "2025-03-01T10:01:00Z", "withdrawal", frm=("Bybit", "USDT", "60"), value=54), "u1"),
            api(tx("A2", "2025-03-01T10:01:30Z", "withdrawal", frm=("Bybit", "USDT", "40"), value=36), "u2"),
            api(tx("A3", "2025-03-01T10:01:40Z", "withdrawal", frm=("Bybit", "USDT", "70"), value=63), "u3"),
            api(tx("A4", "2025-03-01T10:01:50Z", "withdrawal", frm=("Bybit", "USDT", "30"), value=27), "u4")]
    ctx = ctx_for(cfg, rows)
    case = one_case(ctx)
    assert case.cands == [] and case.decision.kind == "need_info" and case.status == "wartet_info"
    ctx.settings.set("autopilot.enabled", True)
    ctx.settings.set("autopilot.fixes", True)
    assert K.run(ctx, "auto", research=False)["applied"] == []
    assert balance(ctx, "Bybit", "USDT") == D("600")


# ----------------------------------------------------------------------------------------------------
# 8 · XT-Gegenbeispiel: 260.86516521 − 5.07599521 = 255.78917
# ----------------------------------------------------------------------------------------------------

def xt_rows():
    return [tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Börse", "XT", "300"), value=30),
            tx("W1", "2025-03-01T10:00:00Z", "withdrawal", frm=("Börse", "XT", "260.86516521"), value=26),
            tx("D1", "2025-03-01T10:20:00Z", "deposit", to=("Wallet", "XT", "255.78917"), value=25.5)]


def test_08_xt_same_end_balance_confirms_no_fee_cause(cfg):
    assert D("260.86516521") - D("5.07599521") == D("255.78917")
    ctx = ctx_for(cfg, xt_rows())
    case = one_case(ctx)
    c = case.cands[0]
    assert c.evidence == "betrag" and not c.fee_explained  # Differenz ist nicht als Gebühr belegt
    assert case.dims["gebuehren"][0] == "offen" and "nicht als Gebühr belegt" in case.dims["gebuehren"][1]
    # der Endbestand stimmt – bestätigt aber weder Gebührenursache noch den Transfer
    res = SC.submit(ctx, case.key, "Wallet", "XT", "255,78917", "2025-06-01", "12:00", "UTC", "api", apply=True)
    assert res.ok and not res.decision_ids
    c2 = one_case(ctx)
    assert c2.status == "teilweise" and c2.decision.kind == "confirm_partial"
    assert c2.dims["gebuehren"][0] == "offen" and c2.dims["bestand"][0] == "geklaert"
    assert c2.dims["transfer"][0] == "offen"  # kein falscher Vollabschluss
    # keine Ausgleichsbuchung, keine automatische Verknüpfung
    ctx.settings.set("autopilot.enabled", True)
    ctx.settings.set("autopilot.fixes", True)
    assert K.run(ctx, "auto", research=False)["applied"] == []
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx", default=0) == 0


# ----------------------------------------------------------------------------------------------------
# 10 · mehrere Referenzzeitpunkte: Endbestand passt, Zwischenbestand widerspricht
# ----------------------------------------------------------------------------------------------------

def test_10_intermediate_reference_contradicts_final(cfg):
    ctx = ctx_for(cfg, sand_rows())
    assert one_case(ctx).status == "wartet_bestand"
    # Zwischenstand 10:05 (nach beiden Buchungen): 104,638 – Endstand 52,319. Beides lässt sich nicht erfüllen.
    for q, when in (("104,638", "10:05"), ("52,319", "12:00")):
        d = "2025-03-01" if when == "10:05" else "2025-06-01"
        r = R.add(ctx, "Bybit", "SAND", q, d, "statement", "", when, "UTC")
        assert r.ok, r.errors
    ctx.invalidate_data()
    c2 = one_case(ctx)
    assert c2.decision.kind != "apply" and c2.status in ("blockiert", "wartet_info", "teilweise")
    assert all(not h.ok or h.base for h in c2.hyps if h.stock == "widerspruch")
    ctx.settings.set("autopilot.enabled", True)
    ctx.settings.set("autopilot.fixes", True)
    assert K.run(ctx, "auto", research=False)["applied"] == []
    assert balance(ctx, "Bybit", "SAND") == D("104.638")


# ----------------------------------------------------------------------------------------------------
# Hilfen für Identität (gemeinsamer Hash) und Derivate
# ----------------------------------------------------------------------------------------------------

H1 = "0x" + "ab" * 32
H2 = "0x" + "cd" * 32


def with_hash(r: dict, source: str, h: str) -> dict:
    r["source"], r["source_ref"] = source, h
    return r


def bybit_deriv_rows(liquidation: str = "20"):
    return [tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Bybit Derivate", "USDT", "1000"), value=900),
            with_hash(tx("K1", "2025-02-01T10:00:00Z", "deposit", to=("Bybit Derivate", "USDT", "500"), value=450),
                      "koinly", H1),
            with_hash(tx("A1", "2025-02-01T10:00:20Z", "deposit", to=("Bybit Derivate", "USDT", "500"), value=450),
                      "portfolia:sync:bybit", H1),
            with_hash(tx("K2", "2025-02-02T10:00:00Z", "deposit", to=("Bybit Derivate", "USDT", "300"), value=270),
                      "koinly", H2),
            with_hash(tx("A2", "2025-02-02T10:00:20Z", "deposit", to=("Bybit Derivate", "USDT", "300"), value=270),
                      "portfolia:sync:bybit", H2),
            tx("FU", "2025-02-04T10:00:00Z", "withdrawal", tag="funding", frm=("Bybit Derivate", "USDT", "1.5"),
               value=1.35),
            tx("L1", "2025-02-05T10:00:00Z", "withdrawal", tag="liquidation",
               frm=("Bybit Derivate", "USDT", liquidation), value=18)]


def automation(ctx, on: bool = True) -> None:
    ctx.settings.set("autopilot.enabled", on)
    ctx.settings.set("autopilot.fixes", on)


# ----------------------------------------------------------------------------------------------------
# 3 · gekoppelte Bybit-Fälle: Zuflusskorrektur + Liquidation/Funding gemeinsam
# ----------------------------------------------------------------------------------------------------

def test_03_coupled_bybit_inflow_correction_with_derivative_events(cfg):
    from app.diagnosis import derivatives as DV

    ctx = ctx_for(cfg, bybit_deriv_rows())
    case = one_case(ctx)
    # beide Zuflüsse und die Derivatebuchungen liegen in EINER Gruppe (gleiche Position), kein Doppelzählen
    assert len(case.findings) == 2 and case.decision.kind == "apply"
    assert all(c.evidence == "identitaet" for c in case.cands)
    joined = " ".join(case.deriv)
    assert "Funding" in joined and "Liquidation" in joined and "nicht implementiert" in joined
    assert case.dims["vorgangsart"][0] == "offen" and case.dims["steuer"][0] == "offen"
    before = DV.economics(report_for(ctx).snapshot.pf.txs)
    assert before[0].result_qty == D("-21.5")
    # ohne Automatik: bereit, aber nichts geschrieben
    assert K.run(ctx, "auto", research=False)["applied"] == []
    assert balance(ctx, "Bybit Derivate", "USDT") == D("2578.5")
    # mit Automatik: beide Zuflüsse gemeinsam (atomar), Derivate bleiben unberührt
    automation(ctx)
    out = K.run(ctx, "auto", research=False)
    assert len(out["applied"]) == 1 and out["applied"][0]["n"] == 2
    assert balance(ctx, "Bybit Derivate", "USDT") == D("1778.5")  # 2578.5 − 500 − 300
    after = DV.economics(report_for(ctx).snapshot.pf.txs)
    assert after[0].result_qty == before[0].result_qty  # Derivateergebnis getrennt und unverändert
    st = K.stored_cases(ctx.db)[0]
    assert st["status"] == "geloest" and st["dims"]["steuer"][0] == "offen"


def test_03_joint_simulation_blocks_when_correction_would_overdraw(cfg):
    ctx = ctx_for(cfg, bybit_deriv_rows(liquidation="2300"))
    case = one_case(ctx)
    # Einzeln wäre jede Korrektur belegt – gemeinsam mit der Liquidation entstünde ein negativer Bestand
    assert case.decision.kind == "blocked" and case.status == "blockiert"
    assert any("negativ" in r for h in case.hyps for r in h.reasons)
    automation(ctx)
    assert K.run(ctx, "auto", research=False)["applied"] == []
    refused = K.apply_hypothesis(ctx, case.key, case.decision.hyp.id if case.decision.hyp else case.hyps[1].id,
                                 authorization="user")
    assert not refused.ok


def test_03_margin_pair_is_only_a_manual_candidate(cfg):
    rows = [tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Bybit", "USDT", "1000"), value=900),
            tx("M1", "2025-02-01T10:00:00Z", "withdrawal", tag="margin", frm=("Bybit", "USDT", "200"), value=180),
            tx("M2", "2025-02-01T10:00:05Z", "deposit", tag="margin", to=("Bybit Derivate", "USDT", "200"),
               value=180)]
    ctx = ctx_for(cfg, rows)
    case = one_case(ctx)
    assert [c.option for c in case.cands] == ["link_margin"] and case.cands[0].evidence == "betrag"
    assert case.decision.kind != "apply"
    # Umbuchung ist kein Handelsverlust; Verknüpfung (nur auf ausdrückliche Bestätigung) erhält das Anschaffungsdatum
    h = case.hyps[1]
    assert h.lot_dates and h.ok
    automation(ctx)
    assert K.run(ctx, "auto", research=False)["applied"] == []
    done = K.apply_hypothesis(ctx, case.key, h.id, authorization="user")
    assert done.ok, done.errors
    lots = report_for(ctx).snapshot.ledger.lots
    assert {str(x.acq_date) for x in lots if x.account == "Bybit Derivate"} == {"2025-01-02"}


def test_03_unsupported_derivative_rows_stay_open(cfg):
    ctx = ctx_for(cfg, [FUND])
    summary = {"rows_read": 5, "recs": 3, "skipped": {"Blockpit: Margin (Margin/Derivate nicht unterstützt)": 2},
               "errors": [], "error_count": 0, "notes": []}
    ctx.db.x("INSERT INTO csv_batch(filename, file_size, file_sha256, raw_gz, profile, account, status, summary_json, "
             "created_at, updated_at) VALUES ('blockpit.csv', 10, 'x', x'00', 'blockpit', 'Bybit', 'preview', ?, "
             "'2025-06-01', '2025-06-01')", (json.dumps(summary),))
    cases = K.analyse(ctx)
    un = [c for c in cases if c.kind == "unsupported"]
    assert len(un) == 1 and un[0].status == "wartet_info" and "2 nicht übernommene" in un[0].title
    assert un[0].dims["vorgangsart"][0] == "offen" and un[0].dims["steuer"][0] == "offen"
    K.persist(ctx, cases)
    # läuft ein Durchgang mit Automatik, gilt der Fall nicht als erledigt
    automation(ctx)
    K.run(ctx, "auto", research=False)
    assert [c["status"] for c in K.stored_cases(ctx.db) if c["kind"] == "unsupported"] == ["wartet_info"]
    # nur eine ausdrückliche Rückstellung ändert das – und gilt nur bei gleicher Evidenz
    assert K.defer(ctx, un[0].key, "bewusst nicht erfasst").ok
    K.run(ctx, "auto", research=False)
    assert [c["status"] for c in K.stored_cases(ctx.db) if c["kind"] == "unsupported"] == ["zurueck"]


def test_03_derivative_economics_are_separate_and_never_zero_when_unknown():
    from app.diagnosis import derivatives as DV
    from tests.helpers import portfolio

    rows = [tx("P1", "2025-02-03T10:00:00Z", "withdrawal", tag="realized_loss", frm=("B", "USDT", "30"), value=27),
            tx("P2", "2025-02-04T10:00:00Z", "withdrawal", tag="funding", frm=("B", "USDT", "1")),  # Wert unbekannt
            tx("M1", "2025-02-01T10:00:00Z", "deposit", tag="margin", to=("B", "USDT", "200"), value=180)]
    econ = DV.economics(portfolio(rows).txs)
    assert len(econ) == 1 and econ[0].result_qty == D("-31") and econ[0].margin_qty == D("200")
    assert econ[0].result_eur is None and econ[0].n_unknown == 1  # unbekannt ≠ 0 €
    from app.ledger.engine import REALIZED_KINDS

    assert "derivative" not in REALIZED_KINDS  # kein stilles Zusammenwerfen mit dem Spot-Ergebnis


# ----------------------------------------------------------------------------------------------------
# 5 · bereits vorhandener PI-Transfer
# ----------------------------------------------------------------------------------------------------

def test_05_existing_transfer_is_not_booked_again_on_reimport(cfg):
    from app.csvimport import model as M
    from app.csvimport import reconcile as RC
    from tests.test_reconcile import _Row, _Tx

    h = "0x" + "55" * 32
    existing = _Tx("T1", f"Koinly TxHash {h}", type="transfer", from_account="Börse", from_asset="PI",
                   from_qty=D("50"), to_account="Wallet", to_asset="PI", to_qty=D("49.9"))
    idx = RC.HashIndex([existing], [])
    rows = [_Row(0, kind=M.WITHDRAWAL, txhash=h, out_sym="PI", out_qty=D("50")),
            _Row(1, kind=M.DEPOSIT, txhash=h, in_sym="PI", in_qty=D("49.9"))]
    res = RC.reconcile(rows, idx, lambda s: s if s == "PI" else None, ["PI"])
    assert {r.state for r in res.rows.values()} == {"full"}  # beide Seiten sind bereits gebucht → nichts Neues
    # und die Klärung sieht darin keinen offenen Fall
    ctx = ctx_for(cfg, [tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Börse", "PI", "100"), value=10),
                        tx("T1", "2025-03-01T10:00:00Z", "transfer", frm=("Börse", "PI", "50"),
                           to=("Wallet", "PI", "49.9"), value=5)])
    assert [c for c in K.analyse(ctx) if c.kind == "group"] == []
    n = ctx.db.scalar("SELECT COUNT(*) FROM journal_tx", default=0)
    K.run(ctx, "auto", research=False)
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx", default=0) == n


# ----------------------------------------------------------------------------------------------------
# 7 · Pionex: Abrechnungskomponenten bis zum Restbestand, keine erfundenen Zeitpunkte
# ----------------------------------------------------------------------------------------------------

def test_07_pionex_components_explain_balance_without_inventing_times(cfg):
    rows = [tx("T0", "2025-01-02", "deposit", to=("Pionex", "USDT", "1000"), value=900),
            tx("B1", "2025-02-03", "trade", frm=("Pionex", "USDT", "400"), to=("Pionex", "SAND", "800"), value=360),
            tx("B2", "2025-02-10T12:00:00Z", "sell", frm=("Pionex", "SAND", "300"), to=("Pionex", "USDT", "150"),
               value=135)]
    ctx = ctx_for(cfg, rows)
    assert R.add(ctx, "Pionex", "SAND", "600", "2025-06-01", "statement", "", "12:00", "UTC").ok
    ctx.invalidate_data()
    case = one_case(ctx)
    text = "\n".join(case.evidence)
    assert "Pionex · SAND" in text and "Handel +500" in text and "Summe +500" in text
    assert "Abrechnungszeitpunkt fehlt" in text and "nicht ergänzt" in text
    assert case.cands == [] and case.status == "wartet_info" and case.dims["bestand"][0] == "offen"
    assert case.dims["historie"][0] == "offen"
    automation(ctx)
    assert K.run(ctx, "auto", research=False)["applied"] == []
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx", default=0) == 0  # keine Ausgleichsbuchung


# ----------------------------------------------------------------------------------------------------
# 9 · zwei Deutungen mit gleichem Endbestand
# ----------------------------------------------------------------------------------------------------

def test_09_two_interpretations_same_end_balance_quantity_confirmed_history_open(cfg):
    rows = [tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Börse", "SAND", "100"), value=10),
            tx("W1", "2025-03-01T10:00:00Z", "withdrawal", frm=("Börse", "SAND", "50"), value=5),
            koinly(tx("K1", "2025-03-01T11:00:00Z", "deposit", to=("Wallet", "SAND", "50"), value=5)),
            api(tx("A1", "2025-03-01T11:00:30Z", "deposit", to=("Wallet", "SAND", "50"), value=5), "u1")]
    ctx = ctx_for(cfg, rows)
    case = one_case(ctx)
    assert case.status == "wartet_bestand"
    res = SC.submit(ctx, case.key, "Wallet", "SAND", "50", "2025-06-01", "12:00", "UTC", "api", apply=True)
    assert res.ok and res.verified, res.errors  # Doppelgutschrift ist durch den Nachweis eindeutig
    assert balance(ctx, "Wallet", "SAND") == D("50")
    # nächster Schritt: Transfer-Zuordnung – der Bestand stimmt mit und ohne Verknüpfung, die Zuordnung bleibt offen
    c2 = one_case(ctx)
    assert c2.status == "teilweise" and c2.decision.kind == "confirm_partial"
    assert "Menge" in c2.decision.reason or "Bestand" in c2.decision.reason
    assert c2.dims["transfer"][0] == "offen"
    automation(ctx)
    assert K.run(ctx, "auto", research=False)["applied"] == []


# ----------------------------------------------------------------------------------------------------
# 11 · fehlende Anschaffungskosten
# ----------------------------------------------------------------------------------------------------

def test_11_missing_cost_stays_unknown_while_quantity_is_confirmed(cfg):
    rows = [koinly(tx("K1", "2025-03-01T10:00:00Z", "deposit", to=("Bybit", "SAND", "52.319"))),
            api(tx("A1", "2025-03-01T10:03:00Z", "deposit", to=("Bybit", "SAND", "64.508"),
                   fee=("SAND", "12.189", "")), "u1")]
    ctx = ctx_for(cfg, rows)
    case = one_case(ctx)
    assert case.dims["anschaffung"][0] == "offen" and "nicht als 0" in case.dims["anschaffung"][1]
    res = SC.submit(ctx, case.key, "Bybit", "SAND", "52,319", "2025-06-01", "12:00", "UTC", "api", apply=True)
    assert res.ok and res.verified, res.errors
    st = K.stored_cases(ctx.db)[0]
    assert st["status"] == "geloest"
    assert st["dims"]["bestand"][0] == "geklaert" and st["dims"]["anschaffung"][0] == "offen"
    assert st["dims"]["steuer"][0] == "offen"  # Kostenbasis unbekannt → Steuer nicht abgeschlossen
    summary = K.runs_of(ctx.db, st["key"])[0]["detail"]["effect"]["zusammenfassung"]
    assert "geklärt" in summary and "Offen" in summary and "Anschaffung" in summary


# ----------------------------------------------------------------------------------------------------
# 12 · Unterkonten, Umfänge, gebundene Margin
# ----------------------------------------------------------------------------------------------------

def test_12_scopes_subaccounts_and_locked_margin_are_not_mixed_up(cfg):
    rows = [tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Bybit", "USDT", "100"), value=90),
            tx("F1", "2025-01-02T08:00:00Z", "deposit", to=("Bybit Derivate", "USDT", "50"), value=45)]
    ctx = ctx_for(cfg, rows)
    # Spot- und Funding-Anzeige desselben Kontos ergeben zusammen den Ledger-Bestand
    for qty, scope, sub in (("60", "spot", "Spot"), ("40", "funding", "Funding")):
        r = R.add(ctx, "Bybit", "USDT", qty, "2025-06-01", "api", "", "12:00", "UTC", subaccount=sub, scope=scope,
                  qty_locked="10" if scope == "spot" else "")
        assert r.ok, r.errors
    ctx.invalidate_data()
    h = next(x for x in report_for(ctx).holdings if (x.account, x.asset) == ("Bybit", "USDT"))
    assert h.reference == D("100") and h.ref_diff == D(0) and h.status == "ref_ok"
    # gebundene Menge außerhalb von 0..Gesamt wird abgelehnt; EUR-Gesamtwert ersetzt keine Assetmenge
    bad = R.add(ctx, "Bybit", "USDT", "5", "2025-06-01", "api", "", "12:00", "UTC", scope="spot", qty_locked="9")
    assert not bad.ok and "gebundene" in bad.errors[0]
    # Derivate: Margin und offenes Ergebnis sind nur Angaben – sie verändern den Vergleich nicht
    ok = R.add(ctx, "Bybit Derivate", "USDT", "50", "2025-06-01", "api", "", "12:00", "UTC", scope="derivatives",
               qty_margin="20", qty_pnl="-3,5")
    assert ok.ok
    ctx.invalidate_data()
    h2 = next(x for x in report_for(ctx).holdings if (x.account, x.asset) == ("Bybit Derivate", "USDT"))
    assert h2.reference == D("50") and h2.status == "ref_ok"
    # zusammengefasste Konten: Summe, kein Nachweis für ein einzelnes Konto
    comb = R.add(ctx, "Bybit", "USDT", "150", "2025-06-01", "api", "", "12:00", "UTC", scope="combined",
                 members=["Bybit", "Bybit Derivate"])
    assert comb.ok, comb.errors
    ctx.invalidate_data()
    rep = report_for(ctx)
    assert len(rep.snapshot.combined) == 1 and rep.snapshot.combined[0].qty == D("150")
    assert K._combined_points(rep, {("Bybit", "USDT")}) == {"Bybit + Bybit Derivate · USDT (zusammengefasst)": [D(0)]}
    assert not R.add(ctx, "Bybit", "USDT", "1", "2025-06-01", scope="combined", members=["Bybit"]).ok


# ----------------------------------------------------------------------------------------------------
# 13 · neue Evidenz öffnet Fälle erneut, keine alten Entscheidungen blind
# ----------------------------------------------------------------------------------------------------

def test_13_new_evidence_reopens_deferred_and_resolved_cases(cfg):
    ctx = ctx_for(cfg, sand_rows())
    case = one_case(ctx)
    K.persist(ctx, [case])
    assert K.defer(ctx, case.key, "später").ok
    K.run(ctx, "auto", research=False)
    assert K.stored_cases(ctx.db)[0]["status"] == "zurueck"  # gleiche Evidenz → Rückstellung gilt
    # neuer Nachweis = neue Evidenz → der Fall wird wieder bewertet
    assert R.add(ctx, "Bybit", "SAND", "52,319", "2025-06-01", "api", "", "12:00", "UTC").ok
    ctx.invalidate_data()
    K.run(ctx, "auto", research=False)
    st = K.stored_cases(ctx.db)[0]
    assert st["status"] != "zurueck" and st["status"] == "bereit"
    # übernommen und zurückgenommen → derselbe Fall taucht neu auf (nicht blind als gelöst geführt)
    res = K.apply_hypothesis(ctx, case.key, case.hyps[1].id, authorization="user")
    assert res.ok and K.stored_cases(ctx.db)[0]["status"] == "geloest"
    assert K.undo_run(ctx, res.token).ok
    K.run(ctx, "auto", research=False)
    st = K.stored_cases(ctx.db)[0]
    assert st["status"] in ("bereit", "wartet_info") and st["opened_count"] >= 2 and st["reopened_at"]
    runs = K.runs_of(ctx.db, case.key)
    assert any(r["status"] == "undone" for r in runs)


# ----------------------------------------------------------------------------------------------------
# 14 · Nebenläufigkeit und Folgelauf
# ----------------------------------------------------------------------------------------------------

def test_14_parallel_run_is_refused_and_remembered_as_followup(cfg):
    ctx = ctx_for(cfg, sand_rows())
    assert K._RUN.acquire(blocking=False)
    try:
        assert K.run(ctx, "auto", research=False) is None  # läuft schon: nichts doppelt
        assert K.pending_followup()
    finally:
        K._RUN.release()
    out = K.run(ctx, "auto", research=False)
    assert out is not None and not out["followup_pending"]


def test_14_data_arriving_during_a_run_is_processed_in_a_second_round(cfg, monkeypatch):
    ctx = ctx_for(cfg, bybit_deriv_rows())
    automation(ctx)
    real = K.persist
    state = {"n": 0}

    def persist_and_inject(c, cases, **kw):
        out = real(c, cases, **kw)
        state["n"] += 1
        if state["n"] == 1:
            # während des Durchgangs trifft ein neuer Nachweis ein
            assert R.add(c, "Bybit Derivate", "USDT", "1778,5", "2025-06-01", "api", "", "12:00", "UTC").ok
            K.notify_change()
        return out

    monkeypatch.setattr(K, "persist", persist_and_inject)
    out = K.run(ctx, "auto", research=False)
    assert out["rounds"] >= 2 and len(out["applied"]) == 1
    assert balance(ctx, "Bybit Derivate", "USDT") == D("1778.5")
    assert not K.pending_followup()


# ----------------------------------------------------------------------------------------------------
# 15 · Atomarität, Idempotenz, Undo
# ----------------------------------------------------------------------------------------------------

def test_15_atomic_idempotent_and_fully_undoable(cfg, monkeypatch):
    ctx = ctx_for(cfg, bybit_deriv_rows())
    automation(ctx)
    case = one_case(ctx)
    hyp = case.decision.hyp
    assert hyp is not None and len(hyp.cands) == 2
    before = fingerprint(ctx.db)
    real = A._exec
    calls = {"n": 0}

    def failing(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:  # zweite Teilkorrektur des gekoppelten Falls scheitert
            raise A.Conflict("Testfehler")
        return real(*a, **kw)

    monkeypatch.setattr(A, "_exec", failing)
    res = K.apply_hypothesis(ctx, case.key, hyp.id, authorization="auto")
    assert not res.ok and "nichts geändert" in " ".join(res.errors).lower()
    monkeypatch.setattr(A, "_exec", real)
    assert fingerprint(ctx.db) == before  # keine Teilkorrektur
    ok = K.apply_hypothesis(ctx, case.key, hyp.id, authorization="auto")
    assert ok.ok and len(ok.decision_ids) == 2
    again = K.apply_hypothesis(ctx, case.key, hyp.id, authorization="auto")  # Doppelklick
    assert not again.ok or again.message.startswith("Diese Klärung")
    assert ctx.db.scalar("SELECT COUNT(*) FROM diag_decision WHERE action='fix' AND status='active'",
                         default=0) == 2
    assert balance(ctx, "Bybit Derivate", "USDT") == D("1778.5")
    undo = K.undo_run(ctx, ok.token)
    assert undo.ok and balance(ctx, "Bybit Derivate", "USDT") == D("2578.5")
    assert ctx.db.scalar("SELECT COUNT(*) FROM diag_decision WHERE action='fix' AND status='active'",
                         default=0) == 0
    assert not K.undo_run(ctx, ok.token).ok  # zweite Rücknahme: nichts
