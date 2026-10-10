"""Bestandsklärung, bestätigte Bestandssicht und Eröffnungsposition (M29): nur synthetische Daten.

Abgedeckt: A/B/C getrennt, Fortschreibung alter Referenzen („fortgeschrieben“ statt „verifiziert“), veraltete
Referenzen, alternative Sicht ohne Doppelzählung und ohne Nullbewertung, unbekannte Anschaffungsdaten bleiben unbekannt,
keine fiktiven FIFO-Lots, Eröffnungsposition nur mit belegtem Datum und belegten Kosten (Einzelfreigabe, Rückgängig).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis import references as R
from app.diagnosis import stock as ST
from app.diagnosis.engine import report_for
from tests.helpers import tx
from tests.test_diagnosis import fingerprint, make_ctx
from tests.test_diagnosis_audit import AUDIT_ASSETS

D = Decimal
ACC = "Börse B"


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def dep(tid: str, ts: str, qty: str, aid: str = "EUR") -> dict:
    return tx(tid, ts, "deposit", to=(ACC, aid, qty), value=qty if aid == "EUR" else "100")


def ref(ctx, qty: str, day: str, at: str = "12:00", aid: str = "EUR"):
    res = R.add(ctx, ACC, aid, qty, day, "statement", "", at, "UTC")
    assert res.ok, res.errors
    return res


def view(ctx, now: datetime | None = None) -> ST.StockView:
    return ST.build(report_for(ctx), A.decisions(ctx.db, 100), now)


def row(sv: ST.StockView, aid: str = "EUR") -> ST.StockRow:
    return next(r for r in sv.rows if r.asset == aid)


def test_abc_are_separate_and_the_unexplained_stock_changes_nothing(cfg):
    ctx = make_ctx(cfg, [dep("D1", "2026-09-01T08:00:00Z", "1000")], AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1300", "2026-09-05")  # 300 mehr als die Buchungen: nicht erklärbar
    before = fingerprint(ctx.db)
    lots_before = [(lot.acq_tx, lot.qty, lot.cost) for lot in ctx.ledger().lots]
    r = row(view(ctx))
    assert (r.ledger, r.rolled, r.diff) == (D("1000"), D("1300"), D("300"))  # A, B′, C = B′ − A
    assert r.state == "bestätigt" and r.evidence == "Ungeklärt" and r.unexplained
    assert [(lot.acq_tx, lot.qty, lot.cost) for lot in ctx.ledger().lots] == lots_before  # Test 26: kein Lot
    assert ctx.ledger().balances[(ACC, "EUR")] == D("1000")  # Ledger unverändert
    assert fingerprint(ctx.db) == before


def test_23_old_reference_is_rolled_forward_and_labelled_not_verified(cfg):
    rows = [dep("D1", "2026-09-01T08:00:00Z", "1000"), dep("D2", "2026-09-20T08:00:00Z", "50")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1300", "2026-09-05")  # Differenz +300; danach kommt D2 hinzu
    r = row(view(ctx))
    assert r.state == "fortgeschrieben" and r.n_after == 1
    assert r.rolled == D("1350") and r.ledger == D("1050") and r.diff == D("300")  # 1300 + 50 = 1350
    ref(ctx, "1050", "2026-09-30")  # neuer Nachweis zum aktuellen Stand → bestätigt
    r2 = row(view(ctx))
    assert r2.state == "bestätigt" and r2.diff == 0


def test_22_stale_reference_is_flagged_but_stays_visible(cfg):
    ctx = make_ctx(cfg, [dep("D1", "2026-01-01T08:00:00Z", "1000")], AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1000", "2026-01-10")
    now = datetime(2026, 10, 10, tzinfo=UTC)
    r = row(view(ctx, now))
    assert r.stale and r.age_days > ST.STALE_DAYS and r.state == "bestätigt"
    assert not row(view(ctx, datetime(2026, 1, 20, tzinfo=UTC))).stale


def test_21_alternative_view_has_no_double_counting_and_never_values_missing_as_zero(cfg):
    rows = [dep("D1", "2026-09-01T08:00:00Z", "1000"),
            tx("B1", "2026-09-02T08:00:00Z", "deposit", to=(ACC, "USD", "100"), value="90")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1300", "2026-09-05")
    sv = view(ctx)
    r = row(sv)
    # EUR: Wert = Menge; die Referenz ersetzt die Ledger-Menge (kein Doppelzählen von A und B)
    assert r.view_qty == D("1300") and r.view_value == D("1300.00") and r.ledger_value == D("1000.00")
    assert sv.view_total - sv.ledger_total == D("300.00")  # nur die Referenzposition weicht ab
    # USD ohne belastbaren Kurs: „unvollständig“ (None), nie 0 – und in keiner Summe als 0 gezählt
    usd = next(x for x in sv.unvalued if x.endswith("USD")) if sv.unvalued else None
    assert usd is not None and sv.n_unvalued >= 1
    assert sv.n_unreferenced >= 1


def test_page_renders_all_three_values_and_changes_nothing(cfg):
    from fastapi.testclient import TestClient

    from app.main import build_app

    ctx = make_ctx(cfg, [dep("D1", "2026-09-01T08:00:00Z", "1000")], AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1300", "2026-09-05")
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        before = fingerprint(c.app.state.ctx.db)
        page = c.get("/quality/diagnose/stock").text
        assert "A · Ledger" in page and "B · Referenz" in page and "C · Differenz" in page
        assert "kein</strong> Zugang, Kauf, Transfer, Lot, Gewinn oder Verlust" in page
        conf = c.get("/quality/diagnose/stock?view=confirmed").text
        assert "nur indikativ" in conf and "nicht bestätigt" in conf and "unvollständig" in conf
        assert fingerprint(c.app.state.ctx.db) == before


# --- Eröffnungsposition ---------------------------------------------------------------------------

def ref_finding(ctx):
    return next(f for f in report_for(ctx).findings if (f.data or {}).get("type") == "reference")


def tokx_ctx(cfg, ref_qty: str):
    ctx = make_ctx(cfg, [dep("D1", "2026-09-01T08:00:00Z", "1000", "TOKX")], AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, ref_qty, "2026-09-05", aid="TOKX")
    return ctx


def plan_for(ctx, **params):
    f = ref_finding(ctx)
    return f, A.build_plan(ctx, report_for(ctx), f, "opening", {k: [v] for k, v in params.items()}, given=True)


GOOD = {"doc_date": "2026-09-05", "doc_kind": "statement", "acq_date": "2026-03-15", "cost": "250",
        "proof": "Kontoauszug 03/2026, Seite 2"}


def test_24_25_unknown_acquisition_cost_or_date_stay_unknown_no_defaults(cfg):
    ctx = tokx_ctx(cfg, "1300")
    # keine Kosten → keine Buchung (nie 0 als Vorgabe)
    _, p = plan_for(ctx, **{**GOOD, "cost": ""})
    assert p.errors and "Anschaffungskosten" in p.errors[0] and "bleiben unbekannt" in p.errors[0] and not p.ops
    # kein Anschaffungsdatum → keine Buchung (nie „heute“ als Vorgabe)
    _, p = plan_for(ctx, **{**GOOD, "acq_date": ""})
    assert p.errors and "Anschaffungsdatum" in p.errors[0] and not p.ops
    # kein Beleg
    _, p = plan_for(ctx, **{**GOOD, "proof": ""})
    assert p.errors and "Beleg" in p.errors[0]
    # ohne Angaben gar nicht erst vorbelegt: die Optionsparameter haben außer Belegdatum/-art keine Vorgabe
    from app.diagnosis.recommend import recommend

    opt = next(o for o in recommend(report_for(ctx), ref_finding(ctx)).options if o.key == "opening")
    defaults = {p.name: p.default for p in opt.params}
    assert defaults["acq_date"] == [] and defaults["cost"] == [] and defaults["proof"] == []
    # Datum nach dem Beleg bzw. in der Zukunft
    _, p = plan_for(ctx, **{**GOOD, "acq_date": "2026-09-06"})
    assert p.errors
    _, p = plan_for(ctx, **{**GOOD, "cost": "-1"})
    assert p.errors


def test_opening_position_with_proven_data_is_single_approved_marked_and_undoable(cfg):
    ctx = tokx_ctx(cfg, "1300")
    f, p = plan_for(ctx, **GOOD)
    assert not p.errors and [o.kind for o in p.ops] == ["create"]
    t = p.ops[0].tx
    assert t.to_qty == D("300") and t.tag == "opening_balance" and t.value_eur == D("250")
    assert t.ts.date().isoformat() == "2026-03-15"  # belegtes Datum, nicht „heute“
    assert "Eröffnungsposition (Ausnahme" in (t.note or "") and "Kontoauszug 03/2026" in t.note
    res = A.apply(ctx, f.id, "opening", {k: [v] for k, v in GOOD.items()}, p.token)
    assert res.ok
    # ein Lot mit genau den belegten Angaben – nicht geschätzt
    lots = [lot for lot in ctx.ledger().lots if lot.qty == D("300")]
    assert lots and lots[0].cost == D("250") and lots[0].acq_date.isoformat() == "2026-03-15"
    # Referenz stimmt jetzt; die Buchung ist als rekonstruierter Anfangsbestand sichtbar
    assert row(view(ctx), "TOKX").diff == 0
    (vis,) = [x for x in report_for(ctx).findings if x.key.startswith("opening|")]
    assert vis.kind == "history" and "belegten Anschaffungsdaten" in vis.title and "Kontoauszug 03/2026" in vis.known[1]
    # doppelte Freigabe derselben Vorschau → nur einmal
    assert "bereits übernommen" in A.apply(ctx, f.id, "opening", {k: [v] for k, v in GOOD.items()}, p.token).message
    assert A.undo(ctx, res.decision_id).ok
    assert row(view(ctx), "TOKX").diff == D("300")  # Differenz wieder ungeklärt, kein Lot mehr
    assert not [lot for lot in ctx.ledger().lots if lot.qty == D("300")]


def test_opening_position_is_not_offered_for_a_surplus(cfg):
    ctx = tokx_ctx(cfg, "700")  # Ledger über Referenz (Dublette o. Ä.) – kein fehlender Bestand
    from app.diagnosis.recommend import recommend

    keys = [o.key for o in recommend(report_for(ctx), ref_finding(ctx)).options]
    assert "opening" not in keys and "dismiss" in keys
    r = row(view(ctx), "TOKX")
    assert r.diff == D("-300") and r.causes[0].startswith("Doppelbuchung")
