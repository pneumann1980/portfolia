"""„Buchungen prüfen“: neue Vorgänge aus dem Prüfstapel als Fragen – sichere gesammelt, übrige einzeln; Rückgängig."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import Config, Secrets
from app.diagnosis import guide as G
from tests.test_diagnosis import (
    PF_HEADER,
    TOKA_ASSETS,
    TOKA_QTY,
    client_with_import,
    popkat_like_rows,
    rows_of,
    upload,
)


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


@pytest.fixture
def stack(cfg):
    c = client_with_import(cfg, popkat_like_rows()[:2] + popkat_like_rows()[3:], TOKA_ASSETS)
    data = PF_HEADER + f"N1,2025-03-10T12:00:00Z,deposit,,,,,Wallet K,TOKA,{TOKA_QTY},,,,0,nachgetragen\n" \
                       "N2,2025-03-12T12:00:00Z,deposit,,,,,Wallet K,TOKA,1.5,,,,0,anderer Vorgang\n"
    bid = upload(c, "manuell.csv", data)
    try:
        yield c, bid
    finally:
        c.__exit__(None, None, None)


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[html.index('id="guide"'):]))


def fields(html: str) -> dict[str, str]:
    return {k: v.replace("&#34;", '"') for k, v in re.findall(r'name="(x_[a-z]+|ref|typ|kid)" value="([^"]*)"', html)}


def test_new_transactions_come_first_safe_ones_as_one_question(stack):
    c, _bid = stack
    ts = G.tasks(c.app.state.ctx)
    assert [t.kind for t in ts[:2]] == ["stapel", "zeile"] and ts[0].theme == ts[1].theme == "import"
    page = text_of(c.get("/quality/assistent?new=1").text)
    assert "1 neuer Vorgang ist eindeutig zugeordnet. Übernehmen?" in page
    assert "Bestand TOKA auf Wallet K: 50.123,45678901 → 50.124,95678901" in page


def test_accepting_all_safe_books_them_and_undo_reverts(stack):
    c, bid = stack
    ctx = c.app.state.ctx
    html = c.get("/quality/assistent?new=1").text
    f = fields(html)
    r = c.post("/quality/assistent/antwort", data={"csrf_token": c.token, "a": "stk:all", **f}, follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert "acts" in q, r.headers["location"]
    r1, r2 = rows_of(c, bid)
    assert r2.status == "committed" and r1.status == "duplicate"
    assert "stapel:sicher" not in {t.key for t in G.tasks(ctx)}
    r = c.post("/quality/assistent/rueckgaengig", data={"csrf_token": c.token, "acts": q["acts"][0]},
               follow_redirects=False)
    assert "err=" not in r.headers["location"]
    assert rows_of(c, bid)[1].status != "committed"


def test_single_transaction_is_asked_whether_it_is_already_booked(stack):
    c, _bid = stack
    ctx = c.app.state.ctx
    t = next(x for x in G.tasks(ctx) if x.kind == "zeile")
    html = c.get(f"/quality/assistent?new=1&skip={G.tasks(ctx)[0].kid}").text
    page = text_of(html)
    assert "Ist dieser Vorgang schon gebucht?" in page and "Übertrag am 10.03.2025" in page
    assert "+50.123,45678901 TOKA" in page
    f = fields(html)
    assert f["ref"] == t.ref
    r = c.post("/quality/assistent/antwort", data={"csrf_token": c.token, "a": "row:link", **f},
               follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert "acts" in q and "Verkn" in q["msg"][0], r.headers["location"]
    assert t.key not in {x.key for x in G.tasks(ctx)}
    c.post("/quality/assistent/rueckgaengig", data={"csrf_token": c.token, "acts": q["acts"][0]})
    assert t.key in {x.key for x in G.tasks(ctx)}


def test_dont_know_defers_the_transaction(stack):
    c, _bid = stack
    ctx = c.app.state.ctx
    t = next(x for x in G.tasks(ctx) if x.kind == "zeile")
    r = c.post("/quality/assistent/antwort", data={"csrf_token": c.token, "a": "row:defer", "typ": "zeile",
                                                    "ref": t.ref}, follow_redirects=False)
    assert "err=" not in r.headers["location"], r.headers["location"]
    assert t.key not in {x.key for x in G.tasks(ctx)}  # zurückgestellt → keine Frage mehr


def test_answer_to_a_changed_transaction_changes_nothing(stack):
    c, _bid = stack
    r = c.post("/quality/assistent/antwort", data={"csrf_token": c.token, "a": "row:include", "typ": "zeile",
                                                    "ref": "999:#0"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
