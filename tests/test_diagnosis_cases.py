"""Einzelvorgänge und transaktionsgenaue Freigabe (M28) – synthetische Fälle.

Abgedeckt (Nummern wie in der Anforderung): 1/2 Einzelkorrektur aus einer Gruppe von zehn, 3 ungleiche Beträge ohne
Gebühr, 4 unabhängige gleich hohe Einzahlungen, 5 1:n und n:m, 6 mehrdeutige Transfers (und eindeutig durch
Ausschluss), 7/8 kompromittierte Wallet mit Rettungsüberweisung bzw. dokumentiertem Diebstahl, 9/10 Ablehnen und
erneute Diagnose, 11 Einzelfreigabe mit Undo, 12 Sammelfreigabe mit konkurrierenden Korrekturen, 13 zwei Sitzungen,
14 doppelte Anfrage, 15 veraltete Vorschau nach Synchronisation, 16/17 Referenzbestand mit Zeitpunkt und
Rückwärtskompatibilität, 18 Fiat centgenau, 19 sehr kleine Tokenmengen, 20 Steuer/FIFO, 21 Integrität nach
Apply/Undo, 22 kein Schreibzugriff durch Diagnose, Prüfansicht oder Vorschau.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis import bulk as B
from app.diagnosis import references as R
from app.diagnosis.engine import report_for
from app.diagnosis.recommend import recommend
from tests.helpers import tx
from tests.test_diagnosis import by_kind, fingerprint, make_ctx
from tests.test_diagnosis_actions import data_fp
from tests.test_diagnosis_audit import AUDIT_ASSETS, api, journal_deposit, koinly

NOW = datetime.now(UTC)


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def ten_rows() -> list[dict]:
    """Zehn Einzahlungen je zweimal (Steuertool netto, API brutto mit Gebühr), je ≤ 5 min Abstand."""
    rows = []
    for n in range(10):
        day = f"2024-0{1 + n // 5}-{10 + n % 5 * 3:02d}"
        net = Decimal(100 + 37 * n) + Decimal("0.25")
        fee = Decimal("1.5")
        rows.append(koinly(tx(f"K{n}", f"{day}T09:00:00Z", "deposit", to=("Börse B", "USD", str(net)), value=str(net))))
        rows.append(api(tx(f"A{n}", f"{day}T09:04:00Z", "deposit", to=("Börse B", "USD", str(net + fee)),
                           fee=("USD", str(fee), ""), value=str(net)), f"u{n}"))
    return rows


def group(rep):
    (f,) = [x for x in by_kind(rep, "duplicate") if x.key.startswith("econ|") and "USD" in x.title]
    return f


def children(rep, f):
    return [rep.by_id(c) for c in f.children]


# ----------------------------------------------------------------------------------------------------
# 1, 2, 11, 21, 22: Einzelkorrektur aus einer Gruppe, Undo, Integrität, kein Schreibzugriff
# ----------------------------------------------------------------------------------------------------

def test_01_02_11_21_22_single_case_from_group_of_ten(cfg):
    ctx = make_ctx(cfg, ten_rows(), AUDIT_ASSETS)
    before, led = data_fp(ctx.db), dict(ctx.ledger().balances)
    lots = [(x.acq_tx, x.qty, x.cost) for x in ctx.ledger().lots]
    rep = report_for(ctx)
    f = group(rep)
    cases = children(rep, f)
    assert len(cases) == 10 and all(c.parent == f.id and c.state == "offen" for c in cases)
    ids_before = {c.id for c in cases}
    target = cases[3]
    assert {r.tx_id for r in target.txs} == {"K3", "A3"}
    # 22: Diagnose, Empfehlung und Vorschau schreiben nichts
    rec = recommend(rep, target)
    assert rec.primary.key == "link_econ" and rec.primary.params == []
    plan = A.build_plan(ctx, rep, target, "link_econ", None)
    assert [(op.kind, op.target, op.link, op.mode) for op in plan.ops] == [("hide", "A3", "K3", "duplicate")]
    eff = A.preview(ctx, rep, plan)
    assert eff.total is not None and eff.positions and eff.positions[0].delta < 0
    assert data_fp(ctx.db) == before
    # 1: nur dieser Vorgang wird übernommen
    res = A.apply(ctx, target.id, "link_econ", plan.params, plan.token)
    assert res.ok
    assert [r["tx_id"] for r in ctx.db.q("SELECT tx_id FROM tx_override WHERE action='delete'")] == ["A3"]
    row = ctx.db.q1("SELECT * FROM diag_decision WHERE id=?", (res.decision_id,))
    assert row["token"] == plan.token and set(json.loads(row["tx_ids_json"])) >= {"K3", "A3"}
    assert row["data_version"] and json.loads(row["effects_json"])["positions"]
    # 2: die übrigen neun Vorgänge bestehen unverändert (gleiche Kennungen, offen)
    rep2 = report_for(ctx)
    rest = children(rep2, group(rep2))
    assert len(rest) == 9 and {c.id for c in rest} == ids_before - {target.id}
    assert all(c.state == "offen" for c in rest)
    # 11/21: vollständige Rücknahme, Bestände und Lots exakt wie vorher
    assert A.undo(ctx, res.decision_id).ok
    assert data_fp(ctx.db) == before and dict(ctx.ledger().balances) == led
    assert [(x.acq_tx, x.qty, x.cost) for x in ctx.ledger().lots] == lots
    assert len(children(report_for(ctx), group(report_for(ctx)))) == 10


# ----------------------------------------------------------------------------------------------------
# 3, 4, 5: Evidenz, unabhängige Vorgänge, mehrteilige Darstellung
# ----------------------------------------------------------------------------------------------------

def test_03_amount_match_without_identity_is_never_proven(cfg):
    rows = [koinly(tx("K1", "2024-02-01T09:00:00Z", "deposit", to=("Börse B", "USD", "412"), value="280")),
            api(tx("A1", "2024-02-01T09:03:00Z", "deposit", to=("Börse B", "USD", "418.18"), fee=("USD", "6.18", ""),
                   value="280"), "x1"),
            koinly(tx("K2", "2024-03-01T09:00:00Z", "deposit", to=("Börse B", "USD", "235"), value="230")),
            api(tx("A2", "2024-03-01T09:03:00Z", "deposit", to=("Börse B", "USD", "238.6"), value="233"), "x2")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    rep = report_for(ctx)
    (f,) = [x for x in by_kind(rep, "duplicate") if x.key.startswith("econ|")]
    assert f.status == "wahrscheinlich" and "Möglicherweise doppelt" in f.title  # Gebühr belegt, Identität nicht
    case = f.data["cases"][0]
    assert any("Gebühr 6,18 USD in A1 ausgewiesen" in p for p in case["pro"])
    assert any("nicht identisch" in c for c in case["contra"])
    assert any("Anbieterreferenz" in m for m in case["missing"])
    # 238,6 ohne ausgewiesene Gebühr passt nicht zu 235 → keine Zuordnung
    assert all("K2" not in json.dumps(x.data) for x in by_kind(rep, "duplicate") if x.key.startswith("econ"))


def test_shared_provider_reference_is_proven_duplicate(cfg):
    uuid = "0a1b2c3d-1111-4222-8333-444455556666"
    k = koinly(tx("K1", "2024-02-01T09:00:00Z", "deposit", to=("Bitpanda", "USD", "412"), value="280"))
    k["note"] = f"txhash={uuid}"
    rows = [k, api(tx("A1", "2024-02-01T09:03:00Z", "deposit", to=("Bitpanda", "USD", "418.18"),
                      fee=("USD", "6.18", ""), value="280"), uuid)]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    (f,) = [x for x in by_kind(report_for(ctx), "duplicate") if x.key.startswith(("econ|", "id|"))]
    assert f.status in ("belegt", "wahrscheinlich")
    if f.key.startswith("econ|"):
        assert f.status == "belegt" and "Nachgewiesen" in f.title


def test_04_independent_equal_deposits_stay_separate(cfg):
    rows = [koinly(tx("K1", "2024-02-01T09:00:00Z", "deposit", to=("Börse B", "EUR", "500"), value="500")),
            koinly(tx("K2", "2024-02-01T09:10:00Z", "deposit", to=("Börse B", "EUR", "500"), value="500"))]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    rep = report_for(ctx)
    assert not [x for x in by_kind(rep, "duplicate") if x.key.startswith("econ")]  # gleiche Quelle → kein Vorschlag
    assert ctx.ledger().balances[("Börse B", "EUR")] == Decimal("1000")


def test_05_one_to_many_and_many_to_many_are_shown_but_not_recommended(cfg):
    rows = [api(tx("A1", "2024-02-01T09:00:00Z", "deposit", to=("Börse B", "EUR", "1000"), value="1000"), "m1"),
            koinly(tx("K1", "2024-02-01T09:10:00Z", "deposit", to=("Börse B", "EUR", "600"), value="600")),
            koinly(tx("K2", "2024-02-01T09:12:00Z", "deposit", to=("Börse B", "EUR", "400"), value="400")),
            # n:m: 300 + 200 (Steuertool) vs. 250 + 250 (API)
            koinly(tx("K3", "2024-04-01T09:00:00Z", "deposit", to=("Börse B", "EUR", "300"), value="300")),
            koinly(tx("K4", "2024-04-01T09:20:00Z", "deposit", to=("Börse B", "EUR", "200"), value="200")),
            api(tx("A3", "2024-04-01T09:05:00Z", "deposit", to=("Börse B", "EUR", "250"), value="250"), "m3"),
            api(tx("A4", "2024-04-01T09:25:00Z", "deposit", to=("Börse B", "EUR", "250"), value="250"), "m4")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    before = data_fp(ctx.db)
    rep = report_for(ctx)
    (one,) = [x for x in by_kind(rep, "duplicate") if x.key.startswith("econ-multi|")]
    assert one.status == "hinweis" and "(1:2)" in one.title and {r.tx_id for r in one.txs} == {"A1", "K1", "K2"}
    assert recommend(rep, one).primary is None  # erkannt, aber nicht automatisch empfohlen
    (nm,) = [x for x in by_kind(rep, "duplicate") if x.key.startswith("econ-nm|")]
    assert "(2:2)" in nm.title and nm.data["pairs"] == [] and not recommend(rep, nm).fixes
    # 1:n nur mit ausdrücklicher Wahl: keine Buchung doppelt verwendet
    plan = A.build_plan(ctx, rep, one, "link_econ", None)
    assert len({op.target for op in plan.ops}) == len(plan.ops)
    assert data_fp(ctx.db) == before


# ----------------------------------------------------------------------------------------------------
# 6: Transfers – Mehrdeutigkeit, eindeutig durch Ausschluss, keine Prozent-Wahrscheinlichkeiten
# ----------------------------------------------------------------------------------------------------

def test_06_ambiguous_transfer_candidates_are_not_assigned(cfg):
    rows = [tx("E0", "2025-02-01T09:00:00Z", "deposit", to=("Börse X", "ETH", "3"), value="9000"),
            tx("W1", "2025-02-02T10:00:00Z", "withdrawal", frm=("Börse X", "ETH", "1"), value="3000"),
            tx("D1", "2025-02-02T10:20:00Z", "deposit", to=("Wallet A", "ETH", "1"), value="3000"),
            tx("D2", "2025-02-02T10:25:00Z", "deposit", to=("Wallet B", "ETH", "1"), value="3000")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    rep = report_for(ctx)
    (t,) = by_kind(rep, "transfer")
    assert t.data.get("ambiguous") and t.data["pairs"] == [] and "Mehrdeutige Transfer-Zuordnung" in t.title
    assert recommend(rep, t).primary is None
    assert any("starke Übereinstimmung" in e or "plausibler Kandidat" in e for e in t.evidence)
    assert not any(re.search(r"\d+ % \(", e) for e in t.evidence)


def test_06b_unique_assignment_by_exclusion_is_explained(cfg):
    rows = [tx("E0", "2025-02-01T09:00:00Z", "deposit", to=("Börse X", "ETH", "3"), value="9000"),
            tx("W1", "2025-02-02T10:00:00Z", "withdrawal", frm=("Börse X", "ETH", "1"), value="3000"),
            tx("W2", "2025-02-02T10:01:00Z", "withdrawal", frm=("Börse X", "ETH", "1.05"), value="3150"),
            tx("D1", "2025-02-02T10:20:00Z", "deposit", to=("Wallet A", "ETH", "1"), value="3000"),
            tx("D2", "2025-02-02T10:21:00Z", "deposit", to=("Wallet A", "ETH", "1.04"), value="3120")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    rep = report_for(ctx)
    pairs = sorted(p for t in by_kind(rep, "transfer") if t.parent is None for p in t.data["pairs"])
    assert pairs == [["W1", "D1"], ["W2", "D2"]]
    assert any("durch Ausschluss" in w for t in by_kind(rep, "transfer") for _a, _b, w in t.pairs)


# ----------------------------------------------------------------------------------------------------
# 7, 8: kompromittierte Wallet
# ----------------------------------------------------------------------------------------------------

def compromise(ctx, account: str, note: str) -> None:
    ctx.db.x("INSERT INTO accounts(import_id, account, broker, depot_group, extra_json) VALUES "
             "((SELECT MAX(id) FROM imports), ?, 'X', 'Krypto', ?)", (account, json.dumps({"note": note})))
    ctx.invalidate_data()


def test_07_compromised_wallet_rescue_transfer_is_not_a_loss(cfg):
    rows = [tx("F1", "2024-01-01T10:00:00Z", "deposit", to=("Wallet H", "ETH", "1"), value="2000"),
            tx("W1", "2024-06-15T10:00:00Z", "withdrawal", frm=("Wallet H", "ETH", "0.9"), value="2700"),
            tx("D1", "2024-06-15T10:12:00Z", "deposit", to=("Wallet Neu", "ETH", "0.899"), value="2697")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    compromise(ctx, "Wallet H", "kompromittiert 15.06.2024")
    rep = report_for(ctx)
    assert not by_kind(rep, "loss")
    (t,) = by_kind(rep, "transfer")
    assert t.data["pairs"] == [["W1", "D1"]]


def test_08_compromised_wallet_with_documented_theft(cfg):
    rows = [tx("F1", "2024-01-01T10:00:00Z", "deposit", to=("Wallet H", "ETH", "1"), value="2000"),
            tx("W1", "2024-06-15T10:00:00Z", "withdrawal", tag="stolen", frm=("Wallet H", "ETH", "0.9"),
               value="2700")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    compromise(ctx, "Wallet H", "kompromittiert 15.06.2024")
    (f,) = by_kind(report_for(ctx), "loss")
    assert f.data["class"] == "C" and f.data.get("documented")
    assert any("Benutzerklassifikation" in k and "nicht extern unabhängig verifiziert" in k for k in f.known)


# ----------------------------------------------------------------------------------------------------
# 9, 10: Ablehnen, erneute Diagnose, geänderte Evidenz
# ----------------------------------------------------------------------------------------------------

def test_09_10_reject_survives_rescan_and_reopens_when_evidence_changes(cfg):
    ctx = make_ctx(cfg, ten_rows(), AUDIT_ASSETS)
    rep = report_for(ctx)
    f = group(rep)
    target = children(rep, f)[0]
    before = data_fp(ctx.db)
    bal = ctx.ledger().balances[("Börse B", "USD")]
    res = A.mark(ctx, target.id, "reject", "laut Kontoauszug zwei Einzahlungen")
    assert res.ok and data_fp(ctx.db) == before  # nur die Entscheidung (Protokoll), keine Buchung
    assert ctx.ledger().balances[("Börse B", "USD")] == bal
    # 9: unveränderte Daten → derselbe Vorgang bleibt abgelehnt, nicht als neuer Fall
    rep2 = report_for(ctx)
    again = rep2.by_id(target.id)
    assert again is not None and again.state == "abgelehnt"
    sammel = recommend(rep2, group(rep2)).option("link_econ")
    assert all(v != "K0|A0" for v, _l in sammel.params[0].choices)  # nicht in der Sammelbearbeitung
    assert A.mark(ctx, target.id, "reject").ok  # wiederholt: keine zweite Entscheidung
    assert ctx.db.scalar("SELECT COUNT(*) FROM diag_decision WHERE status='active'") == 1
    # 10: neue Evidenz (weiterer möglicher Partner aus dritter Quelle) → überholt, erneut zu prüfen
    journal_deposit(ctx, "PF-S-000900", "2024-01-10T09:30:00Z", "Börse B", "USD", "100.25", None, "neu-1")
    rep3 = report_for(ctx)
    changed = rep3.by_id(target.id)
    assert changed is not None and changed.state == "ueberholt"


def test_defer_keeps_case_open_and_marked(cfg):
    ctx = make_ctx(cfg, ten_rows(), AUDIT_ASSETS)
    rep = report_for(ctx)
    target = children(rep, group(rep))[1]
    assert A.mark(ctx, target.id, "defer").ok
    assert report_for(ctx).by_id(target.id).state == "spaeter"
    assert not A.mark(ctx, target.id, "explode").ok


# ----------------------------------------------------------------------------------------------------
# 12, 13, 14, 15: Sammelfreigabe, zwei Sitzungen, doppelte Anfrage, veraltete Vorschau
# ----------------------------------------------------------------------------------------------------

def test_12_bulk_uses_cases_never_group_and_case_together(cfg):
    ctx = make_ctx(cfg, ten_rows(), AUDIT_ASSETS)
    rep = report_for(ctx)
    f = group(rep)
    cs = children(rep, f)
    cand = {x.id for x, _c, _l in B.candidates(rep)}
    assert f.id not in cand and {c.id for c in cs} <= cand
    bp = B.plan(ctx, [f.id, cs[0].id, cs[1].id], include_review=True)
    reasons = {it.finding.id: it.reason for it in bp.items}
    assert reasons[f.id] and not reasons[cs[0].id] and not reasons[cs[1].id]
    assert any("gemeinsam berechnet" in n for n in bp.notes)
    before = data_fp(ctx.db)
    res = B.execute(ctx, [f.id, cs[0].id, cs[1].id], bp.token, include_review=True)
    assert res.ok
    assert sorted(r["tx_id"] for r in ctx.db.q("SELECT tx_id FROM tx_override WHERE action='delete'")) == ["A0", "A1"]
    assert B.undo(ctx, res.bulk_id).ok and data_fp(ctx.db) == before


def test_13_14_two_sessions_and_repeated_request_execute_once(cfg):
    ctx = make_ctx(cfg, ten_rows(), AUDIT_ASSETS)
    rep = report_for(ctx)
    case = children(rep, group(rep))[0]
    p1 = A.build_plan(ctx, rep, case, "link_econ", None)
    p2 = A.build_plan(ctx, rep, case, "link_econ", None)  # zweite Sitzung, gleiche Vorschau
    p3 = A.build_plan(ctx, rep, case, "link_econ_swap", None)  # zweite Sitzung, andere Lösung
    r1 = A.apply(ctx, case.id, "link_econ", p1.params, p1.token)
    after = data_fp(ctx.db)
    r2 = A.apply(ctx, case.id, "link_econ", p2.params, p2.token)
    assert r1.ok and r2.ok and r2.decision_id == r1.decision_id and "bereits übernommen" in r2.message
    r3 = A.apply(ctx, case.id, "link_econ_swap", p3.params, p3.token)
    assert not r3.ok
    assert data_fp(ctx.db) == after
    assert ctx.db.scalar("SELECT COUNT(*) FROM diag_decision WHERE action='fix'") == 1


def test_15_stale_preview_after_new_sync_is_rejected(cfg):
    ctx = make_ctx(cfg, ten_rows(), AUDIT_ASSETS)
    rep = report_for(ctx)
    case = children(rep, group(rep))[2]
    plan = A.build_plan(ctx, rep, case, "link_econ", None)
    journal_deposit(ctx, "PF-S-000901", "2025-01-01T09:00:00Z", "Börse B", "USD", "5", None, "sync-1")  # neuer Abruf
    before = data_fp(ctx.db)
    res = A.apply(ctx, case.id, "link_econ", plan.params, plan.token)
    assert not res.ok and "nicht mehr aktuell" in " ".join(res.errors)
    assert data_fp(ctx.db) == before


def test_undo_refused_when_later_correction_depends_on_it(cfg):
    rows = [koinly(tx("K1", "2024-02-01T09:00:00Z", "deposit", to=("Börse B", "USD", "412"), value="280")),
            api(tx("A1", "2024-02-01T09:03:00Z", "deposit", to=("Börse B", "USD", "418.18"), fee=("USD", "6.18", ""),
                   value="280"), "x1")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS)
    rep = report_for(ctx)
    (f,) = [x for x in by_kind(rep, "duplicate") if x.key.startswith("econ|")]
    plan = A.build_plan(ctx, rep, f, "link_econ", None)
    first = A.apply(ctx, f.id, "link_econ", plan.params, plan.token).decision_id
    # zweite Korrektur auf derselben Buchung (eigene Auswahl: K1 ausblenden) – liegt zeitlich danach
    with ctx.db.transaction() as c:
        c.execute("INSERT INTO diag_decision(finding_id, kind, title, action, option, ops_json, status, created_at) "
                  "VALUES ('x', 'duplicate', 't', 'fix', 'hide_custom', ?, 'active', '2099-01-01T00:00:00Z')",
                  (json.dumps([{"kind": "hide", "target": "A1", "origin": "import"}]),))
    res = A.undo(ctx, first)
    assert not res.ok and "spätere Korrekturen" in res.errors[0]


# ----------------------------------------------------------------------------------------------------
# 16–19: Referenzbestände
# ----------------------------------------------------------------------------------------------------

def test_16_reference_with_exact_timestamp(cfg):
    rows = [tx("D1", "2026-03-31T08:00:00Z", "deposit", to=("Börse B", "EUR", "1000"), value="1000"),
            tx("D2", "2026-03-31T15:00:00Z", "deposit", to=("Börse B", "EUR", "500"), value="500")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-04-01")
    assert R.add(ctx, "Börse B", "EUR", "1000", "2026-03-31", "api", "Abruf mittags", "12:00", "UTC").ok
    (row,) = [h for h in report_for(ctx).holdings if (h.account, h.asset) == ("Börse B", "EUR")]
    assert row.reference_ts == datetime(2026, 3, 31, 12, 0, tzinfo=UTC)
    assert row.soll_at_ref == Decimal("1000") and row.status == "ref_ok"  # D2 (15 Uhr) zählt nicht
    assert not R.add(ctx, "Börse B", "EUR", "1", "2026-03-31", time_of_day="25:00").ok
    assert not R.add(ctx, "Börse B", "EUR", "1", "2026-03-31", tz="Mars/Base").ok


def test_17_legacy_day_end_reference_still_works(cfg):
    rows = [tx("D1", "2026-03-31T08:00:00Z", "deposit", to=("Börse B", "EUR", "1000"), value="1000"),
            tx("D2", "2026-04-01T08:00:00Z", "deposit", to=("Börse B", "EUR", "500"), value="500")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-04-02")
    # Eintrag aus 0.24.0 (ohne Zeitpunkt/Zeitzone)
    ctx.db.x("INSERT INTO reference_balance(account, asset_id, qty, as_of, source, status, created_at) VALUES "
             "('Börse B', 'EUR', '1000', '2026-03-31', 'statement', 'active', '2026-04-01T00:00:00Z')")
    (row,) = [h for h in report_for(ctx).holdings if (h.account, h.asset) == ("Börse B", "EUR")]
    assert row.reference_ts is None and row.soll_at_ref == Decimal("1000") and row.status == "ref_ok"
    from app.fullexport import collect as export_collect

    (ref,) = json.loads(export_collect(ctx, set())["state.json"].decode())["reference_balances"]
    assert ref["as_of_ts"] is None and "tz" in ref  # neue Spalten reisen mit, alte Einträge bleiben lesbar


def test_decisions_and_timed_references_survive_export_and_restore(cfg):
    """Abgelehnt/später prüfen sowie Referenz mit Zeitpunkt reisen mit dem Gesamtexport; ältere Exporte (nur
    „dismiss“ ohne ``action``) bleiben lesbar."""
    from app.fullexport import _state, apply, collect

    ctx = make_ctx(cfg, ten_rows(), AUDIT_ASSETS)
    rep = report_for(ctx)
    c0, c1 = children(rep, group(rep))[:2]
    assert A.mark(ctx, c0.id, "reject", "Kontoauszug: zwei Einzahlungen").ok
    assert A.mark(ctx, c1.id, "defer").ok
    assert R.add(ctx, "Börse B", "USD", "0", "2024-03-01", "statement", "", "18:30", "Europe/Berlin", "value").ok
    st = _state(collect(ctx, set()))
    assert {d["action"] for d in st["diag_dismissed"]} == {"reject", "defer"}
    (ref,) = st["reference_balances"]
    assert ref["as_of_ts"].startswith("2024-03-01T17:30")
    assert ref["tz"] == "Europe/Berlin" and ref["basis"] == "value"
    ctx.db.x("DELETE FROM diag_decision")
    ctx.db.x("DELETE FROM reference_balance")
    iid = ctx.active_import_id()
    ctx.db.x("INSERT OR REPLACE INTO import_extra(import_id, name, data) VALUES (?,?,?)",
             (iid, "state.json", json.dumps(st).encode()))
    counts = apply(ctx, iid)
    assert counts["checked"] == 2 and counts["references"] == 1
    marks = A.active_marks(ctx.db)
    assert marks[c0.id].action == "reject" and marks[c1.id].action == "defer"
    rep2 = report_for(ctx)
    assert rep2.by_id(c0.id).state == "abgelehnt" and rep2.by_id(c1.id).state == "spaeter"
    (row,) = [h for h in rep2.holdings if (h.account, h.asset) == ("Börse B", "USD")]
    assert row.reference_ts == datetime(2024, 3, 1, 17, 30, tzinfo=UTC)
    assert apply(ctx, iid)["checked"] == 0  # nicht doppelt
    # Export aus 0.24.0: nur „geprüft“ ohne action-Feld
    ctx.db.x("DELETE FROM diag_decision")
    old = {"finding_id": c0.id, "kind": "duplicate", "title": c0.title, "fingerprint": "alt", "note": None,
           "created_at": "2026-01-01T00:00:00Z"}
    legacy = {**st, "reference_balances": [], "diag_dismissed": [old]}
    ctx.db.x("INSERT OR REPLACE INTO import_extra(import_id, name, data) VALUES (?,?,?)",
             (iid, "state.json", json.dumps(legacy).encode()))
    assert apply(ctx, iid)["checked"] == 1 and A.active_marks(ctx.db)[c0.id].action == "dismiss"


def test_18_fiat_reference_is_compared_to_the_cent(cfg):
    rows = [tx("D1", "2026-03-01T08:00:00Z", "deposit", to=("Börse B", "EUR", "1734.50"), value="1734.5")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-04-01")
    assert R.add(ctx, "Börse B", "EUR", "1.734,50", "2026-03-31").ok
    (row,) = [h for h in report_for(ctx).holdings if h.asset == "EUR"]
    assert row.status == "ref_ok"
    assert R.add(ctx, "Börse B", "EUR", "1.734,49", "2026-03-31", time_of_day="23:00").ok
    (row,) = [h for h in report_for(ctx).holdings if h.asset == "EUR"]
    assert row.status == "ref_diff" and row.ref_diff == Decimal("-0.01")


def test_19_tiny_token_amounts_keep_full_precision(cfg):
    q = "0.000001234567890123456789"  # 24 Nachkommastellen (Ledger: Bestände unter 1e-12 gelten als Staub)
    rows = [tx("D1", "2026-03-01T08:00:00Z", "deposit", to=("Wallet K", "TOKX", q), value="0")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-04-01")
    assert R.add(ctx, "Wallet K", "TOKX", q, "2026-03-31").ok
    (row,) = [h for h in report_for(ctx).holdings if h.asset == "TOKX"]
    assert row.status == "ref_ok"
    assert R.add(ctx, "Wallet K", "TOKX", "0.000001234567890123456790", "2026-03-31", time_of_day="22:00").ok
    (row,) = [h for h in report_for(ctx).holdings if h.asset == "TOKX"]
    assert row.status == "ref_diff" and row.ref_diff == Decimal("1E-24")


# ----------------------------------------------------------------------------------------------------
# 20: Steuer- und FIFO-Auswirkungen sichtbar vor der Freigabe
# ----------------------------------------------------------------------------------------------------

def test_20_tax_and_fifo_effects_are_shown_before_release(cfg):
    rows = [api(tx("A1", "2024-01-10T09:00:00Z", "deposit", to=("Börse B", "BTC", "1"), value="1200"), "b1"),
            koinly(tx("K1", "2024-01-10T09:20:00Z", "deposit", to=("Börse B", "BTC", "1"), value="1000")),
            koinly(tx("S1", "2024-06-01T09:00:00Z", "sell", frm=("Börse B", "BTC", "1"), to=("Börse B", "EUR", "3000"),
                      value="3000"))]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2024-12-31")
    rep = report_for(ctx)
    (f,) = [x for x in by_kind(rep, "duplicate") if x.key.startswith("econ|")]
    plan = A.build_plan(ctx, rep, f, "link_econ", None)
    assert [op.target for op in plan.ops] == ["A1"]
    eff = A.preview(ctx, rep, plan)
    (y,) = [y for y in eff.years if y.year == 2024]
    assert y.realized == (Decimal("1800.00"), Decimal("2000.00"))  # FIFO: Lot von A1 (1.200) → Lot von K1 (1.000)
    assert any(p.asset == "BTC" and p.bal == (Decimal("1"), Decimal("0")) for p in eff.positions)


# ----------------------------------------------------------------------------------------------------
# Oberfläche: Prüfansicht je Vorgang, Entscheidungen, keine Schreibzugriffe durch GET
# ----------------------------------------------------------------------------------------------------

def test_case_page_and_decisions_via_web(cfg):
    from tests.test_diagnosis import client_with_import

    c = client_with_import(cfg, ten_rows(), AUDIT_ASSETS, valuation="2024-12-31")
    ctx = c.app.state.ctx
    try:
        rep = report_for(ctx)
        f = group(rep)
        case = children(rep, f)[4]
        before = fingerprint(ctx.db)
        main = c.get("/quality/diagnose").text
        assert "10 Einzelvorgänge" in main and f"/quality/diagnose/case/{case.id}" in main
        page = c.get(f"/quality/diagnose/case/{case.id}")
        assert page.status_code == 200
        html = page.text
        for part in ("A · Beteiligte Buchungen (2)", "B · Diagnose", "C · Entscheidung",
                     "Wirkung ansehen und übernehmen",
                     "Ablehnen", "Später prüfen", "Ungeklärt lassen", "Originalfelder und Herkunft", "K4", "A4"):
            assert part in html, part
        assert "Wahrscheinlich – nicht bewiesen" in html
        prev = c.get(f"/quality/diagnose/plan?f={case.id}&o=link_econ")
        assert prev.status_code == 200
        assert "Gesamtwert und Allokation" in prev.text and "Zurück zum Vorgang" in prev.text
        assert fingerprint(ctx.db) == before  # GET schreibt nichts
        r = c.post("/quality/diagnose/mark", data={"f": case.id, "action": "reject", "back": "case",
                                                   "note": "zwei Vorgänge", "csrf_token": c.token},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/quality/diagnose/case/{case.id}")
        assert "abgelehnt" in c.get(r.headers["location"]).text
        assert c.get("/quality/diagnose/case/f-doesnotexist").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_no_write_on_repeated_diagnosis_with_cases(cfg):
    ctx = make_ctx(cfg, ten_rows(), AUDIT_ASSETS)
    before = fingerprint(ctx.db)
    sigs = [report_for(ctx).signature() for _ in range(3)]
    assert sigs[0] == sigs[1] == sigs[2] and fingerprint(ctx.db) == before
    assert sum(1 for s in sigs[0] if s[1] == "duplicate") >= 11  # Sammelbefund + 10 Einzelvorgänge
    _ = timedelta  # Hilfsimport (Lesbarkeit der Zeitangaben oben)
