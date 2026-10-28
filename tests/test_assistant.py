"""Assistent der Datenqualität: nächste Aufgabe, Flow-Modus (Weiterleitung), Überspringen, Auto-Nachprüfung."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.datastructures import Headers

from app.config import Config, Secrets
from app.diagnosis import assistant as AS
from app.diagnosis import autocheck as AC
from app.diagnosis import checkrun as CR
from app.web import flow
from tests.test_diagnosis import TOKA_ASSETS, client_with_import, popkat_like_rows


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


@pytest.fixture
def client(cfg):
    c = client_with_import(cfg, popkat_like_rows(), TOKA_ASSETS, valuation="2025-06-30")
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


# ---- Umleitung (rein) -------------------------------------------------------------------------------------

def test_rewrite_only_quality_targets_and_keeps_messages():
    assert flow.rewrite("/quality/diagnose?msg=Fertig&f=3") == "/quality/assistent?msg=Fertig"
    assert flow.rewrite("/journal/pruefung?err=Nein") == "/quality/assistent?err=Nein"
    assert flow.rewrite("/quality/klaerung/abc") == "/quality/assistent"
    assert flow.rewrite("/quality/assistent?msg=x") is None  # schon am Ziel
    assert flow.rewrite("/portfolio") is None and flow.rewrite("https://evil.example/quality") is None
    assert flow.rewrite("//evil.example/quality") is None


def test_flow_cookie_detection():
    assert flow.wants_flow(Headers({"cookie": "a=b; dq_flow=1"}))
    assert not flow.wants_flow(Headers({"cookie": "dq_flow=0"})) and not flow.wants_flow(Headers({}))
    assert not flow.wants_flow(Headers({"cookie": "xdq_flow=1"}))


def test_skip_parsing_accepts_only_short_ids():
    assert AS.parse_skip("abcdef01,zz,ABCDEF01,12345678") == ["abcdef01", "12345678"]
    assert len(AS.parse_skip(",".join(["abcdef01"] * 100))) == AS.MAX_SKIP


# ---- Seite und Flow ---------------------------------------------------------------------------------------

def first_form(html: str) -> dict[str, str]:
    return dict(re.findall(r'name="(f|o|token)" value="([^"]*)"', html))


def test_assistant_opens_case_pages_and_shows_findings_with_progress(client):
    ctx = client.app.state.ctx
    tasks = AS.open_tasks(ctx)
    assert tasks[0].kind == "fall"  # Klärungsfall hat eine eigene Seite → Weiterleitung, Flow-Modus beginnt
    r = client.get("/quality/assistent?new=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == tasks[0].href
    assert client.cookies.get("dq_flow") == "1"
    r = client.get(f"/quality/assistent?skip={AS.kid(tasks[0].key)}", follow_redirects=False)
    assert r.status_code == 200, r.text[:300]
    assert f"Aufgabe 2 von {len(tasks)}" in r.text
    assert "Überspringen" in r.text and tasks[1].title in r.text


def test_assistant_reports_everything_done_without_tasks(client, monkeypatch):
    monkeypatch.setattr(AS, "open_tasks", lambda ctx: [])
    r = client.get("/quality/assistent", follow_redirects=False)
    assert r.status_code == 200 and "Alles erledigt" in r.text
    assert client.cookies.get("dq_flow") is None


def test_actions_return_to_assistant_only_in_flow_mode(client):
    ctx = client.app.state.ctx
    from app.diagnosis.engine import report_for
    from tests.test_diagnosis import by_kind

    f = next(x for x in by_kind(report_for(ctx), "duplicate") if "TOKA" in x.title)
    prev = client.get(f"/quality/diagnose/plan?f={f.id}&o=hide_weak")
    token = re.search(r'name="token" value="([0-9a-f]+)"', prev.text).group(1)
    form = {"f": f.id, "o": "hide_weak", "token": token, "p__set": "1", "csrf_token": client.token}

    client.cookies.delete("dq_flow")
    # Ohne Flow: dismiss führt wie bisher zur Diagnose
    r = client.post("/quality/diagnose/dismiss", data={"csrf_token": client.token, "f": f.id, "note": "x"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/quality/diagnose")
    did = None
    from app.diagnosis import actions as A

    did = A.active_dismissals(ctx.db)[f.id].id
    client.post(f"/quality/diagnose/decision/{did}/reopen", data={"csrf_token": client.token})

    # Mit Flow: Übernehmen endet beim Assistenten, Meldung bleibt erhalten
    client.cookies.set("dq_flow", "1")
    r = client.post("/quality/diagnose/apply", data=form, follow_redirects=False)
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    assert loc.path == "/quality/assistent" and "msg" in parse_qs(loc.query)
    page = client.get(r.headers["location"], follow_redirects=False)
    assert page.status_code in (200, 303)
    if page.status_code == 200:
        assert "Übernommen" in page.text


def test_overview_ends_flow_mode(client):
    client.get("/quality/assistent")
    assert client.cookies.get("dq_flow") == "1"
    r = client.get("/quality")
    assert r.status_code == 200 and client.cookies.get("dq_flow") is None


def test_skip_moves_on_and_end_clears_cookies(client):
    ctx = client.app.state.ctx
    tasks = AS.open_tasks(ctx)
    assert tasks
    first = AS.kid(tasks[0].key)
    step = AS.next_step(ctx, [first])
    assert step.skipped == 1 and (step.item is None or AS.kid(step.item.key) != first)
    r = client.get("/quality/assistent/ende", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/quality"
    assert client.cookies.get("dq_flow") is None


def test_summary_counts_tasks_and_minutes(client):
    n, minutes = AS.summary(client.app.state.ctx)
    assert n == len(AS.open_tasks(client.app.state.ctx)) and (minutes >= 1 if n else minutes == 0)
    plan = client.get("/quality/plan").text
    assert ("Weiter mit dem Assistenten" in plan) == bool(n)


def test_flow_bar_only_inside_flow_mode(client):
    client.cookies.delete("dq_flow")
    assert "dq-flow" not in client.get("/quality/faelle").text
    client.cookies.set("dq_flow", "1")
    assert "dq-flow" in client.get("/quality/faelle").text


# ---- Auto-Nachprüfung -------------------------------------------------------------------------------------

def test_autocheck_runs_once_respects_gap_and_setting(client):
    ctx = client.app.state.ctx
    assert not CR.previous(ctx.db).get("finished_at")
    out = AC.job_run(ctx)
    assert out.get("ok") and CR.previous(ctx.db).get("finished_at")
    assert AC.job_run(ctx) == {"skipped": "letzter Lauf zu jung"}
    ctx.settings.set(AC.SETTING, False)
    assert AC.job_run(ctx) == {"skipped": "ausgeschaltet"}


def test_autocheck_listener_is_installed_once_and_debounces(client):
    ctx = client.app.state.ctx
    AC.install(ctx)
    AC.install(ctx)
    assert sum(1 for fn in ctx.change_listeners if getattr(fn, "_portfolia_autocheck", False)) == 1

    calls = []

    class Sched:
        def debounce(self, name, delay, **kw):
            calls.append((name, delay))
            return True

    old = ctx.scheduler
    ctx.scheduler = Sched()
    try:
        ctx._changed("data")
        ctx._changed("prices")
        ctx.settings.set(AC.SETTING, False)
        ctx._changed("data")
    finally:
        ctx.scheduler = old
    assert (AC.JOB, AC.DEBOUNCE_S) in calls and calls.count((AC.JOB, AC.DEBOUNCE_S)) == 1


def test_automatik_page_toggles_auto_check(client):
    page = client.get("/quality/automatik").text
    assert "Automatische Nachprüfung" in page and "eingeschaltet" in page
    r = client.post("/quality/automatik/pruefung", data={"csrf_token": client.token, "auto": "0"},
                    follow_redirects=False)
    assert r.status_code == 303 and client.app.state.ctx.settings.get(AC.SETTING) is False
    assert "ausgeschaltet" in client.get("/quality/automatik").text
