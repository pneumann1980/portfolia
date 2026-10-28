"""„Buchungen prüfen“ 0.74: für alle gleich beantworten, Quellenvorrang, Kettenbestand als Antwort, Protokoll."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import Config, Secrets
from app.diagnosis import guide as G
from app.diagnosis.engine import report_for
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import asset, by_kind, client_with_import

NOW = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


@pytest.fixture
def client(cfg):
    rows = [tx("A1", "2025-03-01T00:00:00Z", "deposit", to=("Wallet Z", "TOKA", "10"), value="5"),
            tx("A2", "2025-03-02T00:00:00Z", "deposit", to=("Wallet Z", "TOKB", "20"), value="6"),
            tx("A3", "2025-03-03T00:00:00Z", "deposit", to=("Wallet Z", "TOKC", "30"), value="7"),
            tx("A4", "2025-03-04T00:00:00Z", "deposit", to=("Wallet P", "PEAQ", "3"), value="1")]
    assets = [*ASSETS, asset("TOKA"), asset("TOKB"), asset("TOKC"), asset("PEAQ", qs="coingecko", qid="peaq-2")]
    c = client_with_import(cfg, rows, assets, valuation="2025-06-30")
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def price_tasks(ctx):
    return [t for t in G.tasks(ctx) if t.kind == "frage" and t.theme == "wert"]


def test_same_answer_for_all_similar_questions_and_undo_for_all(client):
    ctx = client.app.state.ctx
    ts = price_tasks(ctx)
    assert len(ts) == 3
    r = client.post("/quality/assistent/antwort", data={"csrf_token": client.token, "typ": "frage",
                                                         "ref": ts[0].finding_id, "a": "mark:dismiss",
                                                         "l_mark:dismiss": "Nein, der Token ist wertlos"},
                    follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["sa"] == ["mark:dismiss"] and q["sig"][0].startswith("price:")
    page = client.get(r.headers["location"]).text
    assert "2 weitere Fragen dieser Art" in page and "Alle 2 gleich beantworten" in page
    r = client.post("/quality/assistent/alle", data={"csrf_token": client.token, "sig": q["sig"][0],
                                                      "a": "mark:dismiss"}, follow_redirects=False)
    q2 = parse_qs(urlsplit(r.headers["location"]).query)
    assert "2 Fragen gleich beantwortet" in q2["msg"][0] and len(q2["dids"][0].split(",")) == 2
    assert price_tasks(ctx) == []
    r = client.post("/quality/assistent/rueckgaengig", data={"csrf_token": client.token, "dids": q2["dids"][0]},
                    follow_redirects=False)
    assert "err=" not in r.headers["location"] and len(price_tasks(ctx)) == 2


def test_log_page_lists_answers_and_takes_them_back(client):
    ctx = client.app.state.ctx
    t = price_tasks(ctx)[0]
    client.post("/quality/assistent/antwort", data={"csrf_token": client.token, "typ": "frage", "ref": t.finding_id,
                                                     "a": "mark:dismiss"})
    page = client.get("/quality/assistent/protokoll").text
    assert "Was ich geändert habe" in page and "Zurücknehmen" in page
    did = ctx.db.scalar("SELECT id FROM diag_decision WHERE status='active' ORDER BY id DESC LIMIT 1")
    r = client.post("/quality/assistent/rueckgaengig", data={"csrf_token": client.token, "d": str(did),
                                                              "back": "protokoll"}, follow_redirects=False)
    assert r.headers["location"] == "/quality/assistent/protokoll"
    assert "zurückgenommen" in client.get("/quality/assistent/protokoll").text


def add_source(ctx, provider: str, account: str, address: str | None, balances: dict[str, str]) -> int:
    sid = ctx.db.x("INSERT INTO data_source(kind, provider, name, account, address, status, coverage_json, created_at, "
                   "updated_at) VALUES (?,?,?,?,?,'synced',?,?,?)",
                   ("wallet" if address else "exchange", provider, f"{provider} Quelle", account, address,
                    json.dumps({"complete": True, "gaps": []}), NOW, NOW)).lastrowid
    for k, q in balances.items():
        ctx.db.x("INSERT INTO ds_balance(source_id, asset_key, qty, observed_at) VALUES (?,?,?,?)", (sid, k, q, NOW))
    ctx.invalidate_data()
    return int(sid)


def test_source_priority_is_one_question_per_account(client):
    ctx = client.app.state.ctx
    sid = add_source(ctx, "binance", "Wallet Z", None, {"TOKA": "12"})
    assert any(f.data.get("account") == "Wallet Z" for f in by_kind(report_for(ctx), "holdings"))
    t = next(x for x in G.tasks(ctx) if x.kind == "vorrang")
    assert t.theme == "doppelt" and "maßgeblich" in t.question and t.ref == str(sid)
    page = client.get("/quality/assistent?new=1").text
    assert "Soll Binance" in page or "maßgeblich sein?" in page
    r = client.post("/quality/assistent/antwort", data={"csrf_token": client.token, "typ": "vorrang", "ref": str(sid),
                                                         "a": "vr:yes"}, follow_redirects=False)
    assert r.headers["location"] == f"/settings/datasources/{sid}/uebernahme"
    client.post("/quality/assistent/antwort", data={"csrf_token": client.token, "typ": "vorrang", "ref": str(sid),
                                                     "a": "vr:no"})
    assert not [x for x in G.tasks(ctx) if x.kind == "vorrang"]


def test_holdings_question_offers_the_blockchain_search_and_runs_it(client, monkeypatch):
    from app.diagnosis import chainscan as CS

    ctx = client.app.state.ctx
    rows_acc = "Wallet P"
    add_source(ctx, "peaq", rows_acc, "0x1e27c175d496f315c4955fa1f31d4fd5777ed856", {"PEAQ": "5"})
    import app.datasources.chains.peaq  # noqa: F401  (Anbindung registrieren)

    f = next(x for x in by_kind(report_for(ctx), "holdings") if (x.data or {}).get("asset") == "PEAQ")
    t = G.detail(ctx, G.Task(key=f"befund:{f.id}", theme="bestand", kind="frage", heading="", finding_id=f.id,
                             ref=f.id))
    assert any(a.id == "chain:scan" for a in t.answers)
    seen = {}
    monkeypatch.setattr(CS, "job_run", lambda ctx_, acc, asset: seen.update(acc=acc, asset=asset) or {"ok": True})
    r = client.post("/quality/assistent/antwort", data={"csrf_token": client.token, "typ": "frage", "ref": f.id,
                                                         "a": "chain:scan"}, follow_redirects=False)
    assert seen == {"acc": rows_acc, "asset": "PEAQ"} and "msg=" in r.headers["location"]
