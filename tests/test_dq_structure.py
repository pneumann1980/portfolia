"""Struktur der Datenqualität: Arbeitsplan (A/B/C), Prüflauf mit Fortschritt, Reiter, Fallliste. Synthetische Daten."""

from __future__ import annotations

import json
from decimal import Decimal

from app.diagnosis import checkrun as CR
from app.diagnosis import clarify as K
from app.diagnosis import workplan as WP
from app.diagnosis.engine import report_for
from tests.helpers import tx
from tests.test_case_view import _web_case, eth_rows
from tests.test_clarify import ASSETS_K, ctx_for, usdt_double_out
from tests.test_clarify import cfg as cfg
from tests.test_diagnosis import fingerprint, make_ctx
from tests.test_diagnosis_audit import AUDIT_ASSETS

D = Decimal


def test_case_item_tiers_follow_decision_state():
    base = {"key": "kg-x", "title": "T", "kind": "group", "updated_at": "", "dims": {}}
    st = {"decision": {"kind": "apply", "reason": "Herkunft belegt", "missing": [], "hyp": "h1"},
          "hyps": [{"id": "h1", "resolved": 2}], "findings": [{"id": "f-1"}], "cands": []}
    a = WP._case_item({**base, "status": "bereit", "state": st})
    assert a.tier == "A" and "übernehmen" in a.recommendation and "erledigt: 2" in a.effect
    b = WP._case_item({**base, "status": "wartet_bestand", "state": {**st, "cands": [1, 2]}})
    assert b.tier == "B" and "Bestand" in b.recommendation and b.refs == ["f-1"]
    assert WP._case_item({**base, "status": "wartet_info", "state": st}).tier == "B"
    assert WP._case_item({**base, "status": "blockiert", "state": st}).tier == "B"
    assert WP._case_item({**base, "status": "zurueck", "state": st}).tier == "C"


def test_plan_without_any_check_is_unchecked_not_done(cfg):
    ctx = ctx_for(cfg, usdt_double_out())
    plan = WP.build(ctx)
    assert plan.state in ("ungeprueft", "offen")  # nie „fertig“, solange nichts geprüft wurde
    assert plan.state != "fertig"


def test_plan_orders_a_before_b_and_never_double_counts_case_findings(cfg):
    ctx = ctx_for(cfg, usdt_double_out())
    cases = K.analyse(ctx)
    K.persist(ctx, cases)
    before = fingerprint(ctx.db)
    plan = WP.build(ctx)
    assert fingerprint(ctx.db) == before  # lesend
    keys = [i.key for i in plan.items]
    assert any(k.startswith("fall:") for k in keys)
    assert [i.tier for i in plan.items] == sorted(i.tier for i in plan.items)
    covered = {f["id"] for c in K.stored_cases(ctx.db) for f in (c["state"].get("findings") or [])}
    in_groups = {r for i in plan.items if i.kind == "gruppe" for r in i.refs}
    assert not covered & in_groups
    nxt = plan.next
    assert nxt is not None and nxt.tier in ("A", "B")


def test_missing_stock_never_reaches_tier_a(cfg):
    """Ein Fall, der nur einen Bestandsnachweis braucht, ist höchstens Stufe B – fehlender Nachweis ≠ Fehler."""
    ctx = ctx_for(cfg, usdt_double_out())
    K.persist(ctx, K.analyse(ctx))
    for i in WP.build(ctx).items:
        if i.kind == "fall":
            assert i.tier != "A"


def test_granular_plan_lists_each_finding_and_hints_are_tier_c(cfg):
    ctx = make_ctx(cfg, eth_rows(tx("D1", "2025-02-02T10:20:00Z", "deposit", to=("Wallet A", "ETH", "0.99"),
                                    value="3000")), AUDIT_ASSETS)
    rep = report_for(ctx)
    g = WP.build(ctx, rep, granular=True)
    coarse = WP.build(ctx, rep)
    assert sum(1 for i in g.items if i.kind == "befund") >= sum(1 for i in coarse.items if i.kind == "gruppe")
    assert all(i.tier == "C" for i in g.items if i.kind == "befund" and i.recommendation.startswith("Kein Handlungs"))


def test_checkrun_reports_changes_and_never_fetches(cfg, monkeypatch):
    ctx = ctx_for(cfg, usdt_double_out())
    calls: list[str] = []
    monkeypatch.setattr(K, "request_evidence", lambda *a, **k: calls.append("fetch") or [])
    first = CR.run(ctx)
    assert first["ok"] and first["first"] and first["counts"]["A"] + first["counts"]["B"] >= 1
    assert CR.previous(ctx.db)["finished_at"] and not calls  # Prüflauf ruft nichts ab (research=False)
    second = CR.run(ctx)
    assert second["ok"] and not second["first"] and second["new"] == [] and second["gone"] == []
    assert CR.running(ctx) is None  # nach dem Lauf steht nichts mehr als „läuft“


def test_checkrun_progress_is_published_per_phase(cfg):
    ctx = ctx_for(cfg, usdt_double_out())
    CR.job_run(ctx)
    st = ctx.job_status()[CR.JOB]
    p = st["progress"]
    assert p["pct"] == 100.0 and not p["running"] and p["phase"] == "done"
    assert [s["key"] for s in p["steps"]] == CR.PHASES


def test_web_structure_tabs_plan_check_and_progress(cfg):
    def check(c, ctx, case):
        page = c.get("/quality")
        assert page.status_code == 200
        t = page.text
        assert 'class="dq-tabs"' in t and "Heute" in t and "Fälle" in t and "Belege" in t and "Automatik" in t
        assert 'hx-get="/quality/plan"' in t and "Alles prüfen" in t and "Technische Einzelheiten" in t
        assert "Finanzielle Integritätsprüfung" in t and 'href="/quality/diagnose"' in t
        plan = c.get("/quality/plan")
        assert plan.status_code == 200 and "Muss ich tun" in plan.text and "Kann ich tun" in plan.text
        assert "Nichts zu tun" in plan.text and "Wie wird eingestuft?" in plan.text
        for url, word in (("/quality/faelle", "Alle Fälle"), ("/quality/belege", "Bestände und Nachweise"),
                          ("/quality/automatik", "Autopilot")):
            r = c.get(url)
            assert r.status_code == 200 and word in r.text and 'class="dq-tabs"' in r.text, url
        # Reiter auch auf den bestehenden Unterseiten
        for url in ("/quality/klaerung", f"/quality/klaerung/{case.key}", "/quality/diagnose", "/quality/integrity",
                    "/quality/autopilot", "/quality/abdeckung"):
            assert 'class="dq-tabs"' in c.get(url).text, url
        # Prüflauf: ohne Scheduler inline; danach Bericht
        token = c.get("/quality").text.split('name="csrf_token" value="')[1].split('"')[0]
        c.headers["X-CSRF-Token"] = token
        r = c.post("/quality/pruefen", data={"csrf_token": token, "back": "/quality"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/quality")
        assert CR.previous(ctx.db).get("finished_at")
        assert "Letzter Prüflauf" in c.get("/quality/plan").text
        # Fremde Rücksprungziele werden nicht übernommen
        r = c.post("/quality/pruefen", data={"csrf_token": token, "back": "https://evil.example/x"},
                   follow_redirects=False)
        assert r.headers["location"].startswith("/quality") and "evil" not in r.headers["location"]
        # laufender Job: Balken im Seitenkopf und Abfrage-Route
        ctx.db.x("INSERT INTO job_status(job, last_start, running, progress_json) VALUES (?,?,1,?) ON CONFLICT(job) "
                 "DO UPDATE SET running=1, progress_json=excluded.progress_json",
                 (CR.JOB, "2026-10-18T09:00:00+00:00", json.dumps({
                     "v": 2, "running": True, "label": "Prüflauf", "pct": 42.0, "phase": "chk_integrity",
                     "phase_label": "Integritätsprüfung", "steps": [], "updated_at": CR_NOW()})))
        busy = c.get("/quality")
        assert 'id="dq-progress"' in busy.text and "Prüflauf" in busy.text and 'aria-valuenow="42"' in busy.text
        assert c.get("/quality/pruefen/progress").status_code == 200
        ctx.db.x("UPDATE job_status SET running=0 WHERE job=?", (CR.JOB,))
        assert c.get("/quality/pruefen/progress").status_code == 204

    _web_case(cfg, usdt_double_out(), ASSETS_K, check)


def CR_NOW() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
