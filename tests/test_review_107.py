"""0.107.0 – Prüfung 0.106.0: Bestätigung an die Anzeige gebunden (N34), einmalige Ausführung (N35), atomare
Korrekturen (N36), Bindung an den Stichtagslauf (N37), Wiederherstellung mit Lücken (N30), Einstellungen im Lesestand
(N31), Stapelrücknahme belebt keine verworfene Eingabe (N32) und die Regel zu N33 (bestätigte Bewertung)."""

# ruff: noqa: F811
from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from app import fullexport as FE
from app.cutover import corrections as CR
from app.cutover import settle as ST
from app.diagnosis import guide as G
from app.diagnosis.engine import report_for
from tests.test_cutover_cases import _add_case
from tests.test_cutover_fix import _fix_all
from tests.test_cutover_settle import CUT, _holdings, _plan, _ref, cx  # noqa: F401
from tests.test_review_fixes import c, cfg, form_values, post  # noqa: F401

D = Decimal


def _settle_form(page: str) -> dict[str, list[str]]:
    return form_values(page, "/settings/stichtag/ausgleich")


# ---------------------------------------------------------------------------------------------------------- N34

def test_settle_page_carries_the_displayed_fingerprint_and_books_it(cx):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")
    assert not _fix_all(ctx)[1]["errors"]
    row = next(r for r in _plan(ctx)["rows"] if r["asset"] == "ETH")
    page = cx.get("/settings/stichtag?settle=1").text
    fields = _settle_form(page)
    assert fields[f"fp_{row['id']}"] == [row["fp"]]  # angezeigter Stand im Formular
    fields[f"dec_{row['id']}"] = ["settle"]
    r = post(cx, "/settings/stichtag/ausgleich", {**fields, "do": ["book"]})
    assert r.status_code == 200 and _holdings(ctx, "Wallet", "ETH") == D("1.5")


def test_settle_page_form_is_rejected_after_the_evidence_changed(cx):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")
    assert not _fix_all(ctx)[1]["errors"]
    row = next(r for r in _plan(ctx)["rows"] if r["asset"] == "ETH")
    fields = _settle_form(cx.get("/settings/stichtag?settle=1").text)
    fields[f"dec_{row['id']}"] = ["settle"]
    ctx.db.x("UPDATE reference_balance SET qty='0.5' WHERE account='Wallet' AND asset_id='ETH'")
    ctx.invalidate_overlay()
    r = post(cx, "/settings/stichtag/ausgleich", {**fields, "do": ["book"]})
    assert r.status_code == 409 and "hat sich seit der Anzeige geändert" in r.text
    assert _holdings(ctx, "Wallet", "ETH") == D(2) and not ctx.db.scalar("SELECT COUNT(*) FROM cutover_correction")


def test_custom_target_needs_the_summary_and_then_books_once(cx):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")
    assert not _fix_all(ctx)[1]["errors"]
    row = next(r for r in _plan(ctx)["rows"] if r["asset"] == "ETH")
    fields = _settle_form(cx.get("/settings/stichtag?settle=1").text)
    fields[f"dec_{row['id']}"], fields[f"tgt_{row['id']}"] = ["custom"], ["1.8"]
    direct = post(cx, "/settings/stichtag/ausgleich", {**fields, "do": ["book"]})
    assert direct.status_code == 409 and "Auswahl prüfen" in direct.text  # eigener Zielbestand war nicht angezeigt
    summary = post(cx, "/settings/stichtag/ausgleich", {**fields, "do": ["preview"]})
    assert summary.status_code == 200
    fields2 = _settle_form(summary.text)
    ok = post(cx, "/settings/stichtag/ausgleich", {**fields2, "do": ["book"]})
    assert ok.status_code == 200 and _holdings(ctx, "Wallet", "ETH") == D("1.8")
    again = post(cx, "/settings/stichtag/ausgleich", {**fields2, "do": ["book"]})  # doppelt abgesendet
    assert again.status_code in (409, 422) and _holdings(ctx, "Wallet", "ETH") == D("1.8")
    assert ctx.db.scalar("SELECT COUNT(*) FROM cutover_correction WHERE status='active'") == 1


def test_assistant_settle_answer_carries_a_token_bound_to_the_rows(cx):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")
    assert not _fix_all(ctx)[1]["errors"]
    fid = next(f.id for f in report_for(ctx).findings if ("Wallet", "ETH") in {tuple(p) for p in f.positions})
    _add_case(ctx, "kg-t-107", [fid])
    t = G.detail(ctx, next(x for x in G.tasks(ctx) if x.key == "fall:kg-t-107"))
    a = next(x for x in t.answers if x.id == "case:settle")
    assert a.token == ST.digest(ST.rows_for_case(ctx, [fid]))
    wrong = cx.post("/quality/assistent/antwort", data={"csrf_token": cx.token, "kid": t.kid, "a": "case:settle",
                                                         "typ": "seite", "t_case:settle": "0" * 24},
                    follow_redirects=False)
    err = parse_qs(urlparse(wrong.headers["location"]).query).get("err", [""])[0]
    assert "hat sich seit der Anzeige geändert" in err and _holdings(ctx, "Wallet", "ETH") == D(2)
    ok = cx.post("/quality/assistent/antwort", data={"csrf_token": cx.token, "kid": t.kid, "a": "case:settle",
                                                      "typ": "seite", "t_case:settle": a.token},
                 follow_redirects=False)
    assert ok.status_code == 303 and _holdings(ctx, "Wallet", "ETH") == D("1.5")


# ---------------------------------------------------------------------------------------------------------- N35

def test_a_stale_version_books_nothing(cx):
    ctx = cx.app.state.ctx
    assert not _fix_all(ctx)[1]["errors"]
    m = __import__("app.cutover.core", fromlist=["models"]).models(ctx, CUT)[0]
    v, errs = CR.validate(ctx, m, account="Wallet", asset="ETH", kind="add", qty="1", mode="cautious",
                          evidence="t", acq_date=None, cost_eur=None)
    assert v is not None and not errs
    out = CR.book(ctx, CUT, m, [v], "veraltet")
    assert out["errors"] == [CR.STALE] and _holdings(ctx, "Wallet", "ETH") == D(2)


# ---------------------------------------------------------------------------------------------------------- N36/N37

def test_undo_keeps_booking_and_audit_together(cx):
    ctx = cx.app.state.ctx
    assert not _fix_all(ctx)[1]["errors"]
    res = CR.apply(ctx, CUT, account="Wallet", asset="ETH", kind="add", qty="1", mode="cautious", evidence="t")
    assert not res["errors"] and _holdings(ctx, "Wallet", "ETH") == D(3)
    assert CR.undo(ctx, res["id"]) == {"errors": []} and _holdings(ctx, "Wallet", "ETH") == D(2)
    assert CR.undo(ctx, res["id"])["errors"]  # zweite Rücknahme ändert nichts


def test_corrections_of_another_cutoff_are_rejected_while_one_is_fixed(cx):
    ctx = cx.app.state.ctx
    res = CR.apply(ctx, date(2024, 1, 1), account="Wallet", asset="ETH", kind="add", qty="1", mode="cautious",
                   evidence="Vorbereitung")
    assert not res["errors"]  # ohne festgeschriebenen Stichtag: jedes Datum (Vorbereitung)
    assert not _fix_all(ctx)[1]["errors"]
    assert "Festgeschrieben ist der Stichtag 01.01.2025" in CR.undo(ctx, res["id"])["errors"][0]
    out = ST.apply_form(ctx, date(2024, 1, 1), {"dec_x": "settle"})
    assert out["errors"] and "veraltete Seite" in out["errors"][0]


# ---------------------------------------------------------------------------------------------------------- N30

def test_restore_with_an_ambiguous_observation_is_reported_as_gap(c):
    cx = c.app.state.ctx
    base = {"provider": "ethereum", "kind": "wallet", "account": "Wallet K", "name": "Gleich", "enabled": 1,
            "sync_interval_min": 1440, "auto_commit": 0}
    state = {"datasources": [{**base, "address": "0x" + "1" * 40}, {**base, "address": "0x" + "2" * 40}],
             "ds_balances": [{"provider": "ethereum", "account": "Wallet K", "name": "Gleich", "asset_key": "KAS",
                              "qty": "12", "observed_at": "2026-11-05T06:00:00Z"}]}
    iid = cx.active_import_id()
    counts = FE._apply_db(cx, iid, state, {}, "2026-11-05T07:00:00Z")
    st = cx.db.get_state(f"restore.{iid}")
    assert st["state"] == "applied_gaps" and counts["ds_balances_unassigned"] == 1
    assert st["gaps"][0]["asset_key"] == "KAS" and "nicht eindeutig" in st["gaps"][0]["reason"]
    tpl = c.app.state.templates.env.get_template("partials/restore_gaps.html")
    html = tpl.render(restore={**st, "gaps": st["gaps"]})
    assert "Wiederherstellung mit Lücken" in html and "KAS 12" in html


def test_gaps_survive_the_file_step_of_a_restore(c):
    """Wiederherstellung mit Dateien: der Abschluss (``_finish``) behält die Lücken des Datenbankteils bei."""
    cx = c.app.state.ctx
    base = {"provider": "ethereum", "kind": "wallet", "account": "Wallet K", "name": "Gleich", "enabled": 1,
            "sync_interval_min": 1440, "auto_commit": 0}
    state = {"datasources": [{**base, "address": "0x" + "1" * 40}, {**base, "address": "0x" + "2" * 40}],
             "ds_balances": [{"provider": "ethereum", "account": "Wallet K", "name": "Gleich", "asset_key": "KAS",
                              "qty": "12", "observed_at": "2026-11-05T06:00:00Z"}]}
    iid = cx.active_import_id()
    journal = {"id": "r107", "import_id": iid, "at": "2026-11-05T07:00:00Z", "attempts": 0,
               "files": [{"name": "x", "target": "/nonexistent/none", "tmp": "/nonexistent/tmp", "bak": "/x",
                          "sha": "0"}]}
    FE._apply_db(cx, iid, state, {}, "2026-11-05T07:00:00Z", journal)
    assert cx.db.get_state(f"restore.{iid}")["state"] == "incomplete"
    FE._finish(cx, {**journal, "files": []})  # Dateien inzwischen ersetzt; Journal aus dem Speicher (ohne Lücken)
    st = cx.db.get_state(f"restore.{iid}")
    assert st["state"] == "applied_gaps" and len(st["gaps"]) == 1


# ---------------------------------------------------------------------------------------------------------- N31

def test_settings_read_in_a_write_transaction_are_not_cached(c):
    cx = c.app.state.ctx
    cx.settings.set("ledger.scope", "global")
    try:
        with cx.db.transaction() as conn:
            conn.execute("UPDATE settings SET value_json='\"account\"' WHERE key='ledger.scope'")
            assert cx.settings.get("ledger.scope") == "account"  # eigene, ungeschriebene Änderung
            raise RuntimeError("zurücknehmen")
    except RuntimeError:
        pass
    assert cx.settings.get("ledger.scope") == "global"  # nichts aus der zurückgenommenen Transaktion behalten


# ---------------------------------------------------------------------------------------------------------- N33

def test_n33_the_confirmed_valuation_is_booked_with_its_origin(c, monkeypatch):
    """Regel (0.107.0): Bestätigt wird der in der Vorschau gezeigte EUR-Wert samt Herkunft. Ändert sich danach nur die
    Kurszuordnung des Assets, bucht die Bestätigung diesen Wert (nachvollziehbar in ``value_source``); neue Planungen
    bewerten nach der neuen Zuordnung. Buchungsrelevante Daten (Buchungen, Zuordnungen, Belege) bleiben gebunden."""
    from app.csvimport import batch as BA
    from app.csvimport.service import csv_service
    from app.journal.service import journal_service
    from app.prices.sources import SourceService
    from tests.test_diagnosis import PF_HEADER, upload
    from tests.test_review_092 import _run_editor

    cx = c.app.state.ctx
    assert not journal_service(cx).save_asset({"asset_id": "N33TOK", "name": "N33", "asset_class": "crypto",
                                               "quote_source": "none", "quote_id": ""}).errors
    assert SourceService(cx).accept("N33TOK", "kaspa") is None
    bid = upload(c, "n33.csv", PF_HEADER + "N33A,2025-03-12T12:00:00Z,deposit,,,,,Other,N33TOK,3,,,,,\n")
    svc = csv_service(cx)
    row = svc.rows(bid)[0]
    shown = Decimal(row.row["value_eur"])
    assert shown > 0
    BA.set_selection(cx.db, bid, {row.idx})
    fields = form_values(c.get(f"/journal/csv/{bid}/preview?action=include").text, f"/journal/csv/{bid}/execute")
    original = BA._execute_tx

    def before_tx(*args, **kwargs):
        _run_editor(cx, lambda: SourceService(cx).accept("N33TOK", "bitcoin"))
        return original(*args, **kwargs)

    monkeypatch.setattr(BA, "_execute_tx", before_tx)
    r = post(c, f"/journal/csv/{bid}/execute", fields)
    monkeypatch.undo()
    booked = cx.db.q1("SELECT value_eur, value_source FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))
    assert r.status_code == 303 and booked is not None
    assert Decimal(booked["value_eur"]) == shown and booked["value_source"]
