"""Visuelle Fallklärung – Darstellungsadapter (nur Lesen). Synthetische Fälle, keine echten Daten.

Geprüft wird, was die Oberfläche nicht verfälschen darf: benannte Orte statt IDs, unbekannt bleibt unbekannt,
Vorschläge sind nicht gebucht, Gebühr „nicht geliefert“ ≠ 0, Bestände nur mit Zeitpunkt/Quelle/Umfang, die Simulation
einer Hypothese verändert nichts, und der Adapter schreibt nie.
"""

from __future__ import annotations

from decimal import Decimal

from app.diagnosis import case_view as V
from app.diagnosis import clarify as K
from app.diagnosis import stockconfirm as SC
from app.diagnosis.engine import report_for
from tests.helpers import tx
from tests.test_clarify import cfg as cfg
from tests.test_clarify import ctx_for, one_case, sand_rows, usdt_double_out
from tests.test_diagnosis import fingerprint, make_ctx
from tests.test_diagnosis_audit import AUDIT_ASSETS

D = Decimal


def view(ctx, case, **kw):
    return V.build(ctx, case, report_for(ctx), **kw)


def eth_rows(*deposits):
    return [tx("E0", "2025-02-01T09:00:00Z", "deposit", to=("Börse X", "ETH", "3"), value="9000"),
            tx("W1", "2025-02-02T10:00:00Z", "withdrawal", frm=("Börse X", "ETH", "1"), value="3000"), *deposits]


def test_named_places_keep_names_and_unknown_counterparty_stays_unknown(cfg):
    ctx = ctx_for(cfg, usdt_double_out())
    case = one_case(ctx)
    v = view(ctx, case)
    named = [p for p in v.places if not p.unknown]
    assert [p.name for p in named] == ["Bybit"] and named[0].mark == "A" and named[0].own is True
    # kein belegter Anbieter-/Netzwerk-/Unterkontoname erfunden
    assert named[0].network == "" and named[0].sub == "" and named[0].addresses == []
    unknown = [p for p in v.places if p.unknown]
    assert len(unknown) == 3  # je Vorgang ein eigener unbekannter Ort – nicht dieselbe Gegenpartei
    assert {p.name for p in unknown} == {"Gegenkonto nicht bekannt"} and all(p.own is None for p in unknown)
    assert all(m.state == "booked" and m.line == "solid" and m.status_text == "gebucht" for m in v.moves)
    assert any("weder eigene Wallet noch externe Auszahlung belegt" in q for q in v.questions)
    # Beträge exakt (deutsches Format, keine Rundung)
    assert {m.out.qty for m in v.moves} == {"97,58213201", "81,317079", "16,26505301"}


def test_fee_not_delivered_is_not_zero_and_booked_fee_is_labelled(cfg):
    ctx = ctx_for(cfg, sand_rows())
    v = view(ctx, one_case(ctx))
    by = {m.tx_ids[0]: m for m in v.moves}
    assert by["K1"].fee.basis == "keine Gebühr geliefert" and by["K1"].fee.qty is None
    assert "0" not in by["K1"].fee.text.replace("keine Gebühr geliefert", "")
    assert by["A1"].fee.basis == "zusätzlich gebucht" and by["A1"].fee.qty == "12,189"
    assert "belastet Bybit" in by["A1"].fee.text


def test_proposal_is_dashed_and_not_booked_and_gives_concrete_reasons(cfg):
    ctx = make_ctx(cfg, eth_rows(tx("D1", "2025-02-02T10:20:00Z", "deposit", to=("Wallet A", "ETH", "0.99"),
                                    value="3000")), AUDIT_ASSETS)
    (case,) = K.analyse(ctx)
    v = view(ctx, case)
    prop = [m for m in v.moves if m.state == "proposed"]
    assert len(prop) == 1 and prop[0].line == "dashed" and "nicht gebucht" in prop[0].status_text
    names = {p.pid: p.name for p in v.places}
    assert (names[prop[0].src], names[prop[0].dst]) == ("Börse X", "Wallet A")
    why = " ".join(prop[0].reason)
    assert "Kein gemeinsamer Hash" in why and "0,01 ETH" in why and "nur dann eine Gebühr, wenn sie belegt ist" in why
    assert "%" not in why  # keine Prozent-Sicherheit
    assert prop[0].fee is None
    # gebuchte Bewegungen bleiben daneben unverändert bestehen
    assert {m.tx_ids[0] for m in v.moves if m.state == "booked"} == {"W1", "D1"}


def test_competing_candidates_are_shown_as_alternatives_without_choosing(cfg):
    ctx = make_ctx(cfg, eth_rows(tx("D1", "2025-02-02T10:20:00Z", "deposit", to=("Wallet A", "ETH", "1"), value="3000"),
                                 tx("D2", "2025-02-02T10:25:00Z", "deposit", to=("Wallet B", "ETH", "1"),
                                    value="3000")), AUDIT_ASSETS)
    (case,) = K.analyse(ctx)
    v = view(ctx, case)
    prop = [m for m in v.moves if m.state == "proposed"]
    assert len(prop) == 2 and all("konkurrierend" in m.type_label for m in prop)
    assert all("wählt keinen aus" in " ".join(m.reason) for m in prop)
    assert {v.place(m.dst).name for m in prop} == {"Wallet A", "Wallet B"}
    assert v.action is not None and v.action.key == "compare" and v.action.mode == "information"


def test_simulation_marks_removed_without_changing_the_report(cfg):
    ctx = ctx_for(cfg, usdt_double_out())
    case = one_case(ctx)
    before = fingerprint(ctx.db)
    rep = report_for(ctx)
    n_refs = len(rep.index._refs)
    hyp = next(h for h in case.hyps if not h.base)
    now = V.build(ctx, case, rep)
    sim = V.build(ctx, case, rep, hyp_id=hyp.id)
    assert not now.simulated and "Aktueller Stand" in now.mode_text
    assert sim.simulated and sim.hyp_id == hyp.id and "nichts davon ist gebucht" in sim.mode_text
    assert {m.tx_ids[0]: m.state for m in sim.moves} == {"K1": "removed", "A1": "booked", "A2": "booked"}
    assert next(m for m in sim.moves if m.state == "removed").line == "struck"
    assert fingerprint(ctx.db) == before and len(rep.index._refs) == n_refs  # lesend, ohne Nebenwirkung
    assert V.build(ctx, case, rep, hyp_id="h-unbekannt").simulated is False  # unbekannte Hypothese: aktueller Stand


def test_facts_carry_time_source_and_scope_and_missing_proof_is_unknown(cfg):
    ctx = ctx_for(cfg, usdt_double_out())
    case = one_case(ctx)
    place = next(p for p in view(ctx, case).places if p.name == "Bybit")
    led = next(f for f in place.facts if f.label == "Ledger-Bestand")
    assert led.at and led.source and led.scope == "Konto gesamt"
    miss = next(f for f in place.facts if f.label == "Externer Nachweis")
    assert miss.qty is None and "unbekannt, nicht 0" in miss.note
    res = SC.submit(ctx, case.key, "Bybit", "USDT", "804,83573598", "2025-06-01", "12:00", "UTC", "api", apply=True)
    assert res.ok
    c2 = one_case(ctx)
    place = next(p for p in view(ctx, c2).places if p.name == "Bybit")
    ref = next(f for f in place.facts if f.label == "Nachweis (Nutzer)")
    assert ref.qty == "804,83573598" and ref.at.startswith("01.06.2025") and ref.scope
    assert "Ledger zum selben Zeitpunkt" in ref.note


def test_quality_dimensions_are_separate_and_coverage_is_not_proof(cfg):
    ctx = ctx_for(cfg, usdt_double_out())
    v = view(ctx, one_case(ctx))
    assert set(v.quality) == {"Datenqualität", "Importabdeckung", "Offene Konflikte"}
    assert v.quality["Datenqualität"][1] == "offen"
    cov = v.quality["Importabdeckung"]
    assert cov[1] == "unbelegt" and "Vollständigkeit unbelegt" in cov[0]
    assert v.quality["Offene Konflikte"][1] == "keine"


def test_selection_by_place_and_move_and_unknown_selection_is_ignored(cfg):
    ctx = ctx_for(cfg, usdt_double_out())
    case = one_case(ctx)
    v0 = view(ctx, case)
    pid = next(p.pid for p in v0.places if p.name == "Bybit")
    sel = view(ctx, case, sel=pid).selected
    assert sel is not None and sel["kind"] == "place" and len(sel["moves"]) == 3
    mv = view(ctx, case, sel="m-K1").selected
    assert mv is not None and mv["kind"] == "move" and mv["src"].name == "Bybit" and mv["dst"].unknown
    assert view(ctx, case, sel="p-gibtesnicht").selected is None


def test_view_is_deterministic_and_never_writes(cfg):
    ctx = ctx_for(cfg, sand_rows())
    case = one_case(ctx)
    before = fingerprint(ctx.db)
    a, b = view(ctx, case), view(ctx, case)
    assert before == fingerprint(ctx.db)
    assert [(p.pid, p.mark, p.name) for p in a.places] == [(p.pid, p.mark, p.name) for p in b.places]
    assert [(m.mid, m.state) for m in a.moves] == [(m.mid, m.state) for m in b.moves]


# ----------------------------------------------------------------------------------------------------
# Oberfläche (Seite im Gesamtsystem)
# ----------------------------------------------------------------------------------------------------

def _web_case(cfg, rows, assets, fn):
    import os
    import time

    from fastapi.testclient import TestClient

    from app.importer.zipbuilder import build_zip
    from app.jobs import tasks
    from app.main import build_app
    from tests.test_diagnosis import COLS

    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=rows, assets=assets, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        case = one_case(ctx)
        fn(c, ctx, case)


def test_page_renders_money_path_with_names_legend_and_text_alternative(cfg):
    from tests.test_clarify import ASSETS_K

    def check(c, ctx, case):
        before = fingerprint(ctx.db)
        r = c.get(f"/quality/klaerung/{case.key}")
        assert r.status_code == 200
        t = r.text
        assert 'id="geldweg"' in t and "Aktueller Stand" in t and "Legende" in t
        assert "Als Tabelle (Textalternative)" in t
        assert ">Bybit<" in t or "Bybit" in t
        assert "Herkunft nicht bekannt" in t and "weder eigene Wallet noch externe Auszahlung belegt" in t
        assert "keine Gebühr geliefert" in t and 'class="sens-real"' in t and 'class="sens-mask"' in t
        assert "gebucht" in t and "Importabdeckung" in t and "Vollständigkeit unbelegt" in t
        # Auswahl und Simulation sind Links (ohne Skript), ändern nichts
        pid = next(p.pid for p in view(ctx, case).places if p.name == "Bybit")
        s = c.get(f"/quality/klaerung/{case.key}?sel={pid}")
        assert (s.status_code == 200 and "Vollständige Adresse" in s.text) or "Interne Kontokennung" in s.text
        mv = c.get(f"/quality/klaerung/{case.key}?sel=m-K1")
        assert mv.status_code == 200 and "<details class=\"vk-more\" open>" in mv.text
        assert c.get(f"/quality/klaerung/{case.key}?hyp=h-unbekannt&sel=%3Cscript%3E").status_code == 200
        assert "<script>" not in c.get(f"/quality/klaerung/{case.key}?sel=%3Cscript%3E").text
        assert fingerprint(ctx.db) == before  # GET schreibt nichts

    _web_case(cfg, sand_rows(), ASSETS_K, check)


def test_case_page_embeds_window_chart_and_get_stays_read_only(cfg):
    from app.diagnosis import research as RS
    from tests.test_clarify import ASSETS_K

    def check(c, ctx, case):
        # ohne Referenz: ausdrücklich „unbekannt“, kein Diagramm
        t0 = c.get(f"/quality/klaerung/{case.key}").text
        assert 'id="fenster"' in t0 and "unbekannt</b> (nicht 0)" in t0 and 'data-chart="devwindows"' not in t0
        res = SC.submit(ctx, case.key, "Bybit", "SAND", "104,638", "2025-06-01", "12:00", "UTC", "api", apply=False)
        assert res.ok, res.errors
        # ein hängengebliebener Lauf: Seitenaufrufe zeigen ihn als abgebrochen, schreiben aber nichts
        ctx.db.x("INSERT INTO diag_research(source_id, status, started_at) "
                 "VALUES (999, 'running', '2025-01-01T00:00:00')")
        before = fingerprint(ctx.db)
        t1 = c.get(f"/quality/klaerung/{case.key}").text
        assert 'data-chart="devwindows"' in t1 and 'id="kwd-1"' in t1
        w = c.get("/quality/diagnose/windows?all=1")
        assert w.status_code == 200 and "Lauf nicht mehr aktiv" in w.text
        run_state = "SELECT status FROM diag_research WHERE source_id=999"
        assert ctx.db.scalar(run_state) == "running"  # der Seitenaufruf hat nichts geändert
        assert fingerprint(ctx.db) == before
        # erst ein Schreibvorgang bereinigt
        RS.reconcile_stale(ctx.db)
        assert ctx.db.scalar(run_state) == "cancelled"

    _web_case(cfg, sand_rows(), ASSETS_K, check)
