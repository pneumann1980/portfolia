"""„Buchungen prüfen“ – Abnahmekriterien (PLAN_DATENQUALITAET, Assistent 2.0, Ziel 2a–f) als automatische Tests.

(a) keine Weiterleitung aus dem Assistenten (eine andere Seite nur als ausdrücklich gewählte Antwort „… ansehen“);
(b) Standardaufgabe ≤ 2 Klicks (empfohlene Antwort vorausgewählt → ein Klick auf „Antwort speichern“);
(c) Aufgabe *n von m*, Thema und Frage stehen vor den Antworten, auf schmalen Bildschirmen ohne Themenliste;
(d) eine Zählung überall (Heute = Assistent);
(e) jede Antwort ist auf der nächsten Karte rückgängig zu machen – auch für vorgemerkte Buchungen, Rechenprüfung und
    unvollständige Vorgänge;
(f) Fachbegriffe nur unter „Details“ bzw. „Andere Möglichkeiten“.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import Config, Secrets
from app.csvimport.service import csv_service
from app.diagnosis import guide as G
from app.diagnosis import integrity as I
from app.diagnosis import recheck as RC
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import PF_HEADER, TOKA_ASSETS, client_with_import, popkat_like_rows, upload

BASE = "/quality/assistent"
# Antworten, die ausdrücklich eine andere Seite öffnen (der Antworttext sagt es)
OPENING = {"go:open", "vr:yes", "stk:look"}
JARGON = ("Befund", "Klärungsfall", "Prüfstapel", "Integrität", "Dublette", "Hypothese", "Fingerprint", "Prüfsumme",
          "Stufe A", "Stufe B", "to_qty", "from_qty", "value_eur")


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


@pytest.fixture
def client(cfg):
    c = client_with_import(cfg, popkat_like_rows(), TOKA_ASSETS, valuation="2025-06-30")
    data = PF_HEADER + "N3,2025-03-13T12:00:00Z,deposit,,,,,Wallet K,TOKA,,,,,0,ohne Menge\n"
    upload(c, "unvollstaendig.csv", data)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def card(html: str) -> str:
    """Sichtbarer Hauptteil der Karte: ohne „Andere Möglichkeiten“ und ohne „Belege und Details“."""
    body = html[html.index('id="guide"'):]
    for cut in ("<details", "</main>"):
        if cut in body:
            body = body[:body.index(cut)]
    return body


def visible(html: str) -> str:
    html = re.sub(r"<(input|form)[^>]*>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def kid_of(html: str) -> str:
    return re.search(r'name="kid" value="([0-9a-f]{8})"', html).group(1)


def walk(client) -> list[tuple[str, str]]:
    """Alle Aufgaben der Reihe nach (überspringend): (Art, HTML) je Karte."""
    out = []
    r = client.get(f"{BASE}?new=1")
    for _ in range(80):
        assert r.status_code == 200, "Assistent leitet nie von selbst weiter"
        if 'name="kid"' not in r.text:
            break
        out.append((re.search(r'name="typ" value="([a-z]+)"', r.text).group(1), r.text))
        r = client.get(f"{BASE}?skip={kid_of(r.text)}")
    return out


# ---- (a), (b), (c), (f) über alle Aufgaben ----------------------------------------------------------------

def test_every_card_is_answerable_here_one_click_layout_and_plain_words(client):
    ctx = client.app.state.ctx
    cards = walk(client)
    assert len(cards) == len(G.tasks(ctx)) and len(cards) >= 3
    kinds = {k for k, _h in cards}
    assert "zeile" in kinds and "frage" in kinds
    for kind, html in cards:
        main = card(html)
        # (a) Antworten im Formular, keine Knöpfe auf andere Seiten in der Karte
        assert 'action="/quality/assistent/antwort"' in main
        assert not re.search(r'<a class="btn primary" href="(?!/quality/assistent)', main), kind
        answers = re.findall(r'name="a" value="([^"]+)"', main)
        assert answers and any(a not in OPENING for a in answers), (kind, answers)
        # (b) höchstens eine vorausgewählte Antwort; ist eine empfohlen, ist sie vorausgewählt
        checked = re.findall(r'name="a" value="([^"]+)" checked', main)
        assert len(checked) <= 1
        if "badge good" in main:
            assert checked, kind
        # (c) Reihenfolge: Aufgabe n von m → Thema → Frage → Antworten
        pos = [main.find(x) for x in ("Aufgabe ", "Thema ", 'class="guide-q"', 'class="guide-a')]
        assert all(p >= 0 for p in pos) and pos == sorted(pos), (kind, pos)
        # (f) keine Fachbegriffe im sichtbaren Hauptteil
        text = visible(main)
        bad = [w for w in JARGON if w in text]
        assert not bad, (kind, bad, text[:400])


def test_mobile_css_keeps_the_question_above_the_fold():
    css = (Path(__file__).resolve().parents[1] / "app/static/css/app.css").read_text("utf-8")
    block = css[css.index("@media (max-width: 600px) {\n  .guide"):]
    block = block[:block.index("\n}\n")]
    assert ".guide-themes { display: none; }" in block and ".guide-q" in block


# ---- (d) eine Zählung ---------------------------------------------------------------------------------------

def test_one_count_on_today_and_in_the_assistant(client):
    ctx = client.app.state.ctx
    n, themes, _m = G.summary(ctx)
    assert f"{n} Aufgaben in {themes} Themen" in client.get("/quality/plan").text
    assert f"von {n}</b>" in client.get(f"{BASE}?new=1").text


# ---- (a) ausdrücklich gewählte Seite: Ziel aus der Aufgabe, nie aus dem Formular ----------------------------

def test_open_answer_uses_the_target_of_the_task_only(client):
    ctx = client.app.state.ctx
    t = next(t for t in G.tasks(ctx) if t.kind == "zeile" and G.detail(ctx, t).answers[0].id == "go:open")
    form = {"csrf_token": client.token, "typ": "zeile", "ref": t.ref, "kid": t.kid, "a": "go:open",
            "x_href": "https://example.org/"}
    r = client.post(f"{BASE}/antwort", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == t.href and t.href.startswith("/journal/csv/")
    form["kid"] = "00000000"
    r = client.post(f"{BASE}/antwort", data=form, follow_redirects=False)
    assert r.headers["location"].startswith(BASE + "?err=")


# ---- (e) unvollständiger Vorgang: Frage, „gehört nicht dazu“ und Rückgängig -----------------------------------

def test_incomplete_row_is_a_question_in_plain_words_and_undoable(client):
    ctx = client.app.state.ctx
    t = G.detail(ctx, next(t for t in G.tasks(ctx) if t.kind == "zeile" and t.item.status == "invalid"))
    assert "fehlt eine Angabe" in t.question and "Die Menge fehlt." in t.context and "to_qty" not in t.context
    assert [a.id for a in t.answers] == ["go:open", "row:skip", "row:defer"]
    r = client.post(f"{BASE}/antwort", data={"csrf_token": client.token, "typ": "zeile", "ref": t.ref, "kid": t.kid,
                                             "a": "row:skip"}, follow_redirects=False)
    loc = r.headers["location"]
    assert "acts=" in loc and t.key not in {x.key for x in G.tasks(ctx)}
    page = client.get(loc).text
    assert "Rückgängig" in page and "Wird nicht gebucht" in page
    acts = parse_qs(urlsplit(loc).query)["acts"][0]
    r = client.post(f"{BASE}/rueckgaengig", data={"csrf_token": client.token, "acts": acts}, follow_redirects=False)
    assert "err=" not in r.headers["location"] and t.key in {x.key for x in G.tasks(ctx)}


# ---- (e) Rechenprüfung: zur Kenntnis genommen, Rückgängig, kommt bei Änderung wieder -------------------------

def test_integrity_finding_is_acknowledged_and_undone(client):
    ctx = client.app.state.ctx
    it = I.Item(id="inv-test", category="bestand", severity="kritisch", title="Summe der Zugänge passt nicht",
                cause_kind="rechenfehler", cause="Die Summe der Zugänge weicht vom Bestand ab. Weitere Details.",
                impact="± 5 EUR", accounts=["Wallet K"], assets=["TOKA"])
    I.save(ctx.db, I.Run(started_at="2025-01-01T00:00:00Z", items=[it]))
    t = next(t for t in G.tasks(ctx) if t.kind == "rechnung")
    assert t.ref == "inv-test" and t.answers[0].id == "int:ack" and t.answers[0].recommended
    assert "Integrität" not in t.question + t.context and "Wallet K" in t.heading
    r = client.post(f"{BASE}/antwort", data={"csrf_token": client.token, "typ": "rechnung", "ref": t.ref,
                                             "kid": t.kid, "a": "int:ack"}, follow_redirects=False)
    loc = r.headers["location"]
    assert "ia=inv-test" in loc and not any(x.kind == "rechnung" for x in G.tasks(ctx))
    assert "Rückgängig" in client.get(loc).text
    r = client.post(f"{BASE}/rueckgaengig", data={"csrf_token": client.token, "ia": "inv-test"},
                    follow_redirects=False)
    assert "err=" not in r.headers["location"] and any(x.kind == "rechnung" for x in G.tasks(ctx))
    # zur Kenntnis genommen, dann ändert sich der Befund → wieder offen
    assert G.ack_integrity(ctx, "inv-test") and not any(x.kind == "rechnung" for x in G.tasks(ctx))
    it.impact_eur = 99.0
    I.save(ctx.db, I.Run(started_at="2025-01-02T00:00:00Z", items=[it]))
    assert any(x.kind == "rechnung" for x in G.tasks(ctx))


# ---- (e) selbst vorgemerkte Buchung: stimmt / zurücknehmen, jeweils rückgängig ------------------------------

@pytest.fixture
def marked(cfg, monkeypatch):
    c = client_with_import(cfg, [tx("B1", "2025-01-02", "deposit", to=("Depot D", "EUR", "10"), value="10")], ASSETS)
    ctx = c.app.state.ctx
    bid = upload(c, "n.csv", PF_HEADER + "N1,2025-03-10T12:00:00Z,deposit,,,,,Depot D,EUR,25,,,,25,manuell\n")
    assert csv_service(ctx).commit(bid)["errors"] == []
    (row,) = ctx.db.q("SELECT tx_id FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))
    monkeypatch.setattr(RC, "named_by_finding", lambda ctx_, t: "Testbefund")
    assert RC.mark(ctx, row["tx_id"], "doppelt?")["errors"] == []
    try:
        yield c, ctx, row["tx_id"]
    finally:
        c.__exit__(None, None, None)


def eur(ctx) -> Decimal:
    return ctx.ledger().holdings_by_account("Depot D").get("EUR", Decimal(0))


def test_marked_booking_is_a_question_keep_and_undo(marked):
    c, ctx, tid = marked
    t = next(t for t in G.tasks(ctx) if t.kind == "erneut")
    assert t.ref == tid and [a.id for a in t.answers] == ["re:keep", "re:reopen", "skip"]
    assert "doppelt?" in t.context and "25" in t.facts[0]
    r = c.post(f"{BASE}/antwort", data={"csrf_token": c.token, "typ": "erneut", "ref": tid, "kid": t.kid,
                                        "a": "re:keep"}, follow_redirects=False)
    loc = r.headers["location"]
    assert "rm=" in loc and RC.marks(ctx.db) == {} and eur(ctx) == Decimal(35)
    assert "Rückgängig" in c.get(loc).text
    r = c.post(f"{BASE}/rueckgaengig", data={"csrf_token": c.token, "rm": tid}, follow_redirects=False)
    assert "err=" not in r.headers["location"] and RC.marks(ctx.db)[tid]["reason"] == "doppelt?"
    r = c.post(f"{BASE}/rueckgaengig", data={"csrf_token": c.token, "rm": tid}, follow_redirects=False)
    assert "err=" in r.headers["location"]  # zweimal geht nicht


def test_marked_booking_reopen_and_undo_restore_the_balance(marked):
    c, ctx, tid = marked
    t = next(t for t in G.tasks(ctx) if t.kind == "erneut")
    r = c.post(f"{BASE}/antwort", data={"csrf_token": c.token, "typ": "erneut", "ref": tid, "kid": t.kid,
                                        "a": "re:reopen"}, follow_redirects=False)
    loc = r.headers["location"]
    assert "ro=" in loc and eur(ctx) == Decimal(10)
    assert any(x.kind in ("zeile", "stapel") for x in G.tasks(ctx))  # Vorgang wieder offen → neue Frage
    assert "Rückgängig" in c.get(loc).text
    ro = parse_qs(urlsplit(loc).query)["ro"][0]
    r = c.post(f"{BASE}/rueckgaengig", data={"csrf_token": c.token, "ro": ro}, follow_redirects=False)
    assert "err=" not in r.headers["location"] and eur(ctx) == Decimal(35)


# ---- Startseite „Prüfen“ und Hinweis auf der Übersicht (0.76.0) ------------------------------------------------

def test_start_page_has_one_count_theme_bars_and_no_jargon(client):
    ctx = client.app.state.ctx
    n, themes, _m = G.overview(ctx)
    page = client.get("/quality").text
    tabs = re.findall(r'class="dq-tab[^"]*" href="[^"]+"[^>]*>([^<]+)</a>', page)
    assert tabs == ["Prüfen", "Experten", "Nachweise", "Automatik"]
    plan = client.get("/quality/plan").text
    assert f"{n} Aufgaben in {len(themes)} Themen" in plan and plan.count('role="meter"') == len(themes)
    assert all(t["label"] in plan for t in themes) and "Muss ich tun" not in plan
    text = visible(plan.split("Für Fortgeschrittene")[0])
    assert not [w for w in JARGON if w in text], text[:300]
    assert 'href="/quality/assistent?new=1"' in plan and "Neu berechnen" in plan


def test_dashboard_hint_shows_the_same_count_and_disappears_when_done(client, monkeypatch):
    from app.diagnosis import dq_web

    dq_web._SUMMARY.clear()
    n = G.summary(client.app.state.ctx)[0]
    r = client.get("/quality/hinweis")
    assert r.status_code == 200 and f"{n} Aufgaben zu deinen Buchungen offen" in r.text and "Buchungen prüfen" in r.text
    assert 'hx-get="/quality/hinweis"' in client.get("/").text
    dq_web._SUMMARY.clear()
    monkeypatch.setattr(G, "summary", lambda ctx: (0, 0, 0))
    assert client.get("/quality/hinweis").text == ""
