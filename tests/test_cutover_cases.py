"""0.104.0: Klärungsfälle nach Stichtag-Aktionen – erledigte/archivierte Fälle erscheinen nicht, Fortschritt im Fall,
Nachlauf, Gesamtprüfung beim Lesen."""

# ruff: noqa: F811
from __future__ import annotations

import json
from datetime import date

import pytest

from app.cutover import corrections as CR
from app.cutover import fix as FX
from app.cutover import service as CS
from app.cutover import settle as ST
from app.diagnosis import actions as A
from app.diagnosis import clarify as K
from app.diagnosis import guide as G
from app.diagnosis import triage as TR
from app.diagnosis import workplan as WP
from app.diagnosis.engine import report_for
from tests.test_diagnosis import TOKA_ASSETS, client_with_import, popkat_like_rows
from tests.test_review_fixes import cfg  # noqa: F401

CUT = date(2025, 1, 1)
STAMP = "2026-01-01T00:00:00Z"


def _add_case(ctx, key: str, findings: list[str], status: str = "wartet_info", title: str = "Testfall") -> None:
    ctx.db.x("DELETE FROM clarify_case WHERE case_key NOT LIKE 'kg-t%'")  # nur die Testfälle dieses Moduls
    ctx.db.x("INSERT INTO clarify_case(case_key, kind, status, title, accounts_json, assets_json, finding_ids_json, "
             "tx_ids_json, dims_json, state_json, evidence_fp, data_version, note, opened_count, created_at, "
             "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (key, "group", status, title, "[]", "[]", json.dumps(findings), "[]", "{}",
              json.dumps({"findings": [{"id": f} for f in findings]}), "fp", 1, "", 1, STAMP, STAMP))


@pytest.fixture
def cx(cfg):
    c = client_with_import(cfg, popkat_like_rows(), TOKA_ASSETS, valuation="2025-06-30")
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _ids(ctx, n=2):
    ids = [f.id for f in report_for(ctx).findings if f.parent is None][:n]
    assert len(ids) == n
    return ids


def _live(ctx):
    return {c["key"]: c for c in TR.live_cases(ctx, K.stored_cases(ctx.db))}


def test_case_with_open_findings_is_live_and_shows_progress(cx):
    ctx = cx.app.state.ctx
    a, b = _ids(ctx)
    _add_case(ctx, "kg-t1", [a, b])
    live = _live(ctx)["kg-t1"]
    assert (live["open_n"], live["total_n"]) == (2, 2)
    items = {i.key: i for i in WP.build(ctx).items}
    assert "noch 2 von 2 Befunden offen" in items["fall:kg-t1"].effect
    assert G.case_progress(ctx, a) == ("Testfall", 2, 2) and G.case_progress(ctx, "f-unbekannt") is None


def test_marking_findings_shrinks_the_case_and_the_last_one_removes_it(cx):
    ctx = cx.app.state.ctx
    a, b = _ids(ctx)
    _add_case(ctx, "kg-t2", [a, b])
    assert A.mark(ctx, a, "dismiss", "test").ok
    live = _live(ctx)["kg-t2"]
    assert (live["open_n"], live["total_n"]) == (1, 2)
    assert "noch 1 von 2 Befunden offen" in {i.key: i for i in WP.build(ctx).items}["fall:kg-t2"].effect
    assert G.case_progress(ctx, a) == ("Testfall", 1, 2)
    assert A.mark(ctx, b, "dismiss", "test").ok
    assert "kg-t2" not in _live(ctx)  # nichts mehr offen: der Fall erscheint nicht mehr
    assert "fall:kg-t2" not in {i.key for i in WP.build(ctx).items}


def test_deferred_cases_and_cases_without_findings_stay(cx):
    ctx = cx.app.state.ctx
    (a,) = _ids(ctx, 1)
    _add_case(ctx, "kg-t3", [a], status="zurueck")
    _add_case(ctx, "kg-t4", [], status="wartet_info")
    assert A.mark(ctx, a, "dismiss", "test").ok
    live = _live(ctx)
    assert "kg-t3" in live and "kg-t4" in live  # bewusst zurückgestellt / ohne Befundliste: unverändert


def test_cases_with_only_archive_findings_disappear_after_the_fix_and_return_after_the_undo(cx):
    ctx = cx.app.state.ctx
    a, _b = _ids(ctx)
    f = report_for(ctx).by_id(a)
    assert f.txs  # Befund mit Buchungsbezug (liegt im März 2025)
    _add_case(ctx, "kg-t5", [a])
    assert "kg-t5" in _live(ctx)
    ctx.db.x("INSERT INTO cutover(cutoff, cutoff_utc, status, options_json, fingerprint, created_at) "
             "VALUES (?,?,?,?,?,?)", ("2025-07-01", "2025-06-30T22:00:00Z", "active", "{}", "x", STAMP))
    ctx.invalidate_data()
    assert report_for(ctx).archived  # Archiv-Befunde
    assert "kg-t5" not in _live(ctx)  # nur Archiv-Befunde: kein offener Fall mehr
    ctx.db.x("UPDATE cutover SET status='undone'")
    ctx.invalidate_data()
    assert "kg-t5" in _live(ctx)


def test_answer_in_the_assistant_reports_the_progress_in_the_case(cx):
    ctx = cx.app.state.ctx
    a, b = _ids(ctx)
    _add_case(ctx, "kg-t6", [a, b], title="Bestände abgleichen")
    kid = next((t.kid for t in G.tasks(ctx) if t.finding_id == a), "x")
    r = cx.post("/quality/assistent/antwort", data={"csrf_token": cx.token, "f": a, "kid": kid, "a": "mark:dismiss",
                                                      "typ": "frage", "before": "5"}, follow_redirects=False)
    assert r.status_code == 303
    from urllib.parse import parse_qs, urlparse

    msg = parse_qs(urlparse(r.headers["location"]).query).get("msg", [""])[0]
    assert msg.startswith("Gespeichert: ist in Ordnung so.") and "noch 1 von 2 Befunden offen" in msg
    r2 = cx.post("/quality/assistent/antwort", data={"csrf_token": cx.token, "f": b, "kid": "x", "a": "mark:dismiss",
                                                       "typ": "frage", "before": "5"}, follow_redirects=False)
    msg2 = parse_qs(urlparse(r2.headers["location"]).query).get("msg", [""])[0]
    assert "ist damit erledigt" in msg2


def test_refresh_persists_cases_without_applying_or_logging(cx):
    ctx = cx.app.state.ctx
    before = ctx.db.scalar("SELECT COUNT(*) FROM clarify_run")
    n = K.refresh(ctx)
    assert isinstance(n, int) and ctx.db.scalar("SELECT COUNT(*) FROM clarify_run") == before
    ctx.settings.set("clarify.enabled", False)
    assert K.refresh(ctx) is None


def test_stichtag_actions_trigger_one_refresh_each(cx, monkeypatch):
    ctx = cx.app.state.ctx
    calls: list[str] = []
    monkeypatch.setattr(K, "refresh", lambda c: calls.append("r") or 0)
    ctx.db.x("INSERT INTO reference_balance(account, asset_id, qty, as_of, source, created_at) VALUES (?,?,?,?,?,?)",
             ("Wallet K", "TOKA", "1", "2025-06-01", "statement", STAMP))
    ctx.invalidate_overlay()
    pv = CS.preview(ctx, date(2025, 3, 1))
    res = FX.fix(ctx, date(2025, 3, 1), pv["token"], {a["account"] for a in pv["accounts"]})
    assert not res["errors"] and calls == ["r"]
    calls.clear()
    out = ST.apply_form(ctx, date(2025, 3, 1), {"small_all": "1", "acc_x": "all"})  # Ausgleich: einmal, nicht je Zeile
    assert len(calls) <= 1 and "errors" in out
    calls.clear()
    assert not FX.undo(ctx)["errors"] and calls == ["r"]
    calls.clear()
    r = CR.apply(ctx, date(2025, 3, 1), account="Wallet K", asset="TOKA", kind="add", qty="1", mode="cautious",
                 evidence="Annahme")
    assert not r["errors"] and calls == ["r"]
    calls.clear()
    assert not CR.undo(ctx, r["id"])["errors"] and calls == ["r"]


def test_stored_integrity_run_follows_the_cutoff_when_read(cx):
    from app.diagnosis import integrity as I

    ctx = cx.app.state.ctx
    pf = ctx.portfolio()
    old = [t.tx_id for t in pf.txs if t.date < date(2025, 3, 10)] or [pf.txs[0].tx_id]
    new = [t.tx_id for t in pf.txs if t.date >= date(2025, 3, 10)]
    mk = lambda i, ids: I.Item(id=i, category="x", severity="warnung", title="t", cause_kind="k", cause="c",  # noqa: E731
                               impact="i", tx_ids=ids)
    r = I.Run(started_at=STAMP, items=[mk("1", old), mk("2", new), mk("3", [])])
    ctx.db.x("INSERT INTO cutover(cutoff, cutoff_utc, status, options_json, fingerprint, created_at) "
             "VALUES (?,?,?,?,?,?)", ("2025-03-10", "2025-03-09T23:00:00Z", "active", "{}", "x", STAMP))
    ctx.invalidate_data()
    I.refresh_status(ctx, r)
    assert [it.status for it in r.items] == ["archiviert", "offen", "offen"]
    ctx.db.x("UPDATE cutover SET status='undone'")
    ctx.invalidate_data()
    I.refresh_status(ctx, r)
    assert [it.status for it in r.items] == ["offen", "offen", "offen"]


def test_plans_touching_the_archive_are_not_executable_and_say_so(cx):
    from app.diagnosis.actions import build_plan

    ctx = cx.app.state.ctx
    rep = report_for(ctx)
    f = next(x for x in rep.findings if x.kind == "duplicate" and x.txs)
    from app.diagnosis.recommend import recommend

    opts = [o.key for o in recommend(rep, f).fixes]
    assert opts
    plans = [build_plan(ctx, rep, f, k) for k in opts]
    assert any(p.ops and not p.errors for p in plans)  # ohne Stichtag ausführbar
    ctx.db.x("INSERT INTO cutover(cutoff, cutoff_utc, status, options_json, fingerprint, created_at) "
             "VALUES (?,?,?,?,?,?)", ("2025-07-01", "2025-06-30T22:00:00Z", "active", "{}", "x", STAMP))
    ctx.invalidate_data()
    rep2 = report_for(ctx)
    f2 = rep2.by_id(f.id)
    if f2 is None:  # reiner Archiv-Befund: erscheint gar nicht mehr (Stufe 4)
        assert f.id in {a.id for a in rep2.archived}
        return
    for k in opts:
        p = build_plan(ctx, rep2, f2, k)
        if p.ops:
            assert p.errors and "festgeschriebenen Stichtag" in p.errors[0]


def test_applying_into_the_archive_returns_a_message_not_an_error_page(cx, monkeypatch):
    from app.cutover import guard
    from app.diagnosis import actions as A

    ctx = cx.app.state.ctx
    rep = report_for(ctx)
    f = next(x for x in rep.findings if x.kind == "duplicate" and x.txs)
    from app.diagnosis.recommend import recommend

    key = next(o.key for o in recommend(rep, f).fixes)
    plan = A.build_plan(ctx, rep, f, key)
    assert not plan.errors

    def boom(*a, **k):
        raise guard.Protected("Der Stichtag 01.01.2025 ist festgeschrieben.")

    monkeypatch.setattr(A, "_exec", boom)
    res = A.apply(ctx, f.id, key, {}, plan.token)
    assert not res.ok and "festgeschrieben" in res.errors[0] and "nichts geändert" in res.errors[0]
