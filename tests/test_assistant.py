"""Assistent „Buchungen prüfen“: Fragen statt Lösungen, Themen-Reihenfolge, eine Zählung, Antworten mit Rückgängig,
Flow-Modus für Aufgaben mit eigener Seite, Auto-Nachprüfung."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.datastructures import Headers

from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis import autocheck as AC
from app.diagnosis import checkrun as CR
from app.diagnosis import guide as G
from app.diagnosis.engine import report_for
from app.web import flow
from tests.test_diagnosis import TOKA_ASSETS, by_kind, client_with_import, popkat_like_rows


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


def text_of(html: str) -> str:
    body = html[html.index('id="guide"'):] if 'id="guide"' in html else html
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


# ---- Aufgaben und Zählung ---------------------------------------------------------------------------------

def test_tasks_follow_the_theme_order_and_one_count_everywhere(client):
    ctx = client.app.state.ctx
    ts = G.tasks(ctx)
    assert [t.theme for t in ts] == sorted((t.theme for t in ts), key=lambda k: G.THEME_ORDER[k])
    n, themes, minutes = G.summary(ctx)
    assert n == len(ts) and themes == len({t.theme for t in ts}) and minutes >= 1
    plan = client.get("/quality/plan").text
    assert f"{n} Aufgaben in {themes} Themen" in plan and G.TITLE in plan and "Nächster Schritt" not in plan
    page = text_of(client.get("/quality/assistent?new=1").text)
    assert f"Aufgabe 1 von {n}" in page and "Thema 1 von" in page


def test_hints_only_on_request(client):
    ctx = client.app.state.ctx
    assert len(G.tasks(ctx, hints=True)) >= len(G.tasks(ctx))
    assert all(t.item is None or t.item.tier in ("A", "B") for t in G.tasks(ctx))


def test_integrity_findings_that_are_diagnosis_findings_are_not_counted_twice(client):
    from app.diagnosis import integrity as I

    ctx = client.app.state.ctx
    I.save(ctx.db, I.run(ctx, None))
    report = report_for(ctx)
    extra = G._integrity_tasks(ctx, report)
    known = {f.id for f in report.findings}
    assert all(t.key.split(":", 1)[1] not in known for t in extra)


# ---- Seiten-Aufgaben (eigene Seite, Rückkehr über den Flow-Modus) -----------------------------------------

def test_case_with_prepared_solution_is_asked_directly_and_can_be_undone(client):
    """Klärungsfall mit belegter Lösung: keine Weiterleitung, sondern die Frage des wichtigsten Befunds."""
    ctx = client.app.state.ctx
    ts = G.tasks(ctx)
    assert ts[0].kind == "fall"
    r = client.get("/quality/assistent?new=1")
    assert r.status_code == 200 and client.cookies.get("dq_flow") == "1"
    page = text_of(r.text)
    assert "Ist das derselbe Vorgang" in page and "Befund" not in page.split("Belege und Details")[0]
    assert 'href="/quality/klaerung/' not in r.text.split("Belege und Details")[0]  # keine Weiterleitung
    fields = dict(re.findall(r'name="(x_[a-z]+|ref|typ)" value="([^"]*)"', r.text))
    assert fields["typ"] == "fall" and fields["x_hyp"] and fields["x_version"]
    form = {"csrf_token": client.token, "a": "case:apply", **{k: v.replace("&#34;", '"') for k, v in fields.items()}}
    r = client.post("/quality/assistent/antwort", data=form, follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert "kl" in q, r.headers["location"]
    assert ts[0].key not in {t.key for t in G.tasks(ctx)}
    assert "Rückgängig" in client.get(r.headers["location"]).text
    r = client.post("/quality/assistent/rueckgaengig", data={"csrf_token": client.token, "kl": q["kl"][0]},
                    follow_redirects=False)
    assert "err=" not in r.headers["location"]


def test_flowbar_on_expert_pages_names_the_return_and_not_on_overview(client):
    client.get("/quality/assistent?new=1")
    assert "Zurück zur Frage" in client.get("/quality/faelle").text
    assert "Zurück zur Frage" not in client.get("/quality").text
    assert client.cookies.get("dq_flow") is None  # Übersicht beendet den Modus


# ---- Fragen und Antworten --------------------------------------------------------------------------------

def question_task(ctx, kind: str, pred=lambda f: True) -> G.Task:
    f = next(x for x in by_kind(report_for(ctx), kind) if pred(x))
    return G.detail(ctx, G.Task(key=f"befund:{f.id}", theme=G.KIND_THEME.get(kind, "bestand"), kind="frage",
                                heading="", finding_id=f.id))


def test_duplicate_is_asked_as_once_or_twice_with_effect_of_the_recommended_answer(client):
    ctx = client.app.state.ctx
    t = question_task(ctx, "duplicate", lambda f: "TOKA" in f.title)
    assert t.question == "Ist das derselbe Vorgang – also nur einmal passiert?"
    ids = [a.id for a in t.answers if not a.advanced]
    assert ids[0].startswith("opt:") and "mark:reject" in ids and ids[-1] == "mark:defer"
    yes = t.answers[0]
    assert yes.recommended and yes.token and "Bestand TOKA" in yes.effect
    assert "zweimal" in t.answers[1].text


def test_recommended_answer_applies_and_can_be_undone_from_the_next_card(client):
    ctx = client.app.state.ctx
    t = question_task(ctx, "duplicate", lambda f: "TOKA" in f.title)
    yes = t.answers[0]
    form = {"csrf_token": client.token, "f": t.finding_id, "a": yes.id, f"t_{yes.id}": yes.token, "before": "3"}
    r = client.post("/quality/assistent/antwort", data=form, follow_redirects=False)
    assert r.status_code == 303
    q = parse_qs(urlsplit(r.headers["location"]).query)
    did = int(q["done"][0])
    assert ctx.db.q1("SELECT action, status FROM diag_decision WHERE id=?", (did,))["action"] == "fix"
    page = client.get(r.headers["location"]).text
    assert "Rückgängig" in page and "Gespeichert und korrigiert" in page
    r = client.post("/quality/assistent/rueckgaengig", data={"csrf_token": client.token, "d": str(did)},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=Zur%C3%BCckgenommen" in r.headers["location"]
    assert ctx.db.q1("SELECT status FROM diag_decision WHERE id=?", (did,))["status"] == "undone"
    # zweimal zurücknehmen geht nicht
    r = client.post("/quality/assistent/rueckgaengig", data={"csrf_token": client.token, "d": str(did)},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_dont_know_defers_without_change_and_undo_reopens(client):
    ctx = client.app.state.ctx
    t = question_task(ctx, "price")
    assert t.question.startswith("Hat TOKA heute noch einen Wert?")
    r = client.post("/quality/assistent/antwort", data={"csrf_token": client.token, "f": t.finding_id,
                                                         "a": "mark:defer"}, follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    did = int(q["done"][0])
    assert ctx.db.q1("SELECT action FROM diag_decision WHERE id=?", (did,))["action"] == "defer"
    assert t.finding_id not in {x.finding_id for x in G.tasks(ctx)}  # zurückgestellt
    client.post("/quality/assistent/rueckgaengig", data={"csrf_token": client.token, "d": str(did)})
    assert t.finding_id in {x.finding_id for x in G.tasks(ctx)}


def test_answer_with_inputs_shows_the_effect_before_saving(client):
    ctx = client.app.state.ctx
    t = question_task(ctx, "price")
    yes = next(a for a in t.answers if a.params)
    assert not yes.advanced and "Kursquelle" in yes.text
    name = yes.params[0].name
    r = client.post("/quality/assistent/antwort", data={"csrf_token": client.token, "f": t.finding_id, "a": yes.id,
                                                         f"n_{yes.id}": "1", f"p_{name}": ""})
    assert r.status_code == 200 and "Bitte" in text_of(r.text)  # Eingabe fehlt → Frage mit Fehlermeldung
    before = A.decisions(ctx.db)
    r = client.post("/quality/assistent/antwort", data={"csrf_token": client.token, "f": t.finding_id, "a": yes.id,
                                                         f"n_{yes.id}": "1", f"p_{name}": "toka-token"})
    assert r.status_code == 200 and "Das ändert sich mit deiner Antwort" in r.text
    assert A.decisions(ctx.db) == before  # noch nichts gespeichert
    token = re.search(r'name="token" value="([0-9a-f]+)"', r.text).group(1)
    r = client.post("/quality/assistent/antwort", data={"csrf_token": client.token, "f": t.finding_id, "a": yes.id,
                                                         "token": token, "confirm": "1", f"p_{name}": "toka-token"},
                    follow_redirects=False)
    assert r.status_code == 303 and "done=" in r.headers["location"], r.headers["location"]


def test_finished_state_offers_hints_and_clears_the_flow(client, monkeypatch):
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [])
    page = client.get("/quality/assistent").text
    assert "Alles geprüft" in page and "Hinweise ohne Fehler" in page and client.cookies.get("dq_flow") is None


def test_skip_moves_on(client):
    ts = G.tasks(client.app.state.ctx)
    page = text_of(client.get(f"/quality/assistent?new=1&skip={ts[0].kid}").text)
    assert "Aufgabe 2 von" in page


# ---- Umleitung (rein) und Auto-Nachprüfung -------------------------------------------------------------------

def test_rewrite_only_quality_targets_and_keeps_messages():
    assert flow.rewrite("/quality/diagnose?msg=Fertig&f=3") == "/quality/assistent?msg=Fertig"
    assert flow.rewrite("/journal/pruefung?err=Nein") == "/quality/assistent?err=Nein"
    assert flow.rewrite("/quality/assistent?msg=x") is None
    assert flow.rewrite("/portfolio") is None and flow.rewrite("https://evil.example/quality") is None
    assert flow.wants_flow(Headers({"cookie": "a=b; dq_flow=1"}))
    assert not flow.wants_flow(Headers({"cookie": "xdq_flow=1"}))
    assert G.parse_skip("abcdef01,zz,ABCDEF01,12345678") == ["abcdef01", "12345678"]


def test_autocheck_runs_once_respects_gap_and_setting(client):
    ctx = client.app.state.ctx
    assert not CR.previous(ctx.db).get("finished_at")
    assert AC.job_run(ctx).get("ok") and CR.previous(ctx.db).get("finished_at")
    assert AC.job_run(ctx) == {"skipped": "letzter Lauf zu jung"}
    ctx.settings.set(AC.SETTING, False)
    assert AC.job_run(ctx) == {"skipped": "ausgeschaltet"}


def test_automatik_page_toggles_auto_check(client):
    assert "Automatische Nachprüfung" in client.get("/quality/automatik").text
    r = client.post("/quality/automatik/pruefung", data={"csrf_token": client.token, "auto": "0"},
                    follow_redirects=False)
    assert r.status_code == 303 and client.app.state.ctx.settings.get(AC.SETTING) is False
