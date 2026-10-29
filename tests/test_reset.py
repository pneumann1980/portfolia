"""Von vorn beginnen: alle Korrekturen und Antworten zurücknehmen – Rundreise (danach gleicher Stand wie vorher),
Umfang, Prüfsumme der Vorschau, Anhalten bei einem Fehler, doppelte Bestätigung in der Oberfläche."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import Config, Secrets
from app.diagnosis import guide as G
from app.diagnosis import recheck as RC
from app.diagnosis import reset as RS
from app.jobs.maintenance import list_backups
from tests.test_diagnosis import PF_HEADER, TOKA_ASSETS, TOKA_QTY, client_with_import, popkat_like_rows, upload

BASE = "/quality/assistent"
OPENING = {"go:open", "vr:yes", "stk:look", "skip", "chain:scan"}


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


@pytest.fixture
def client(cfg):
    c = client_with_import(cfg, popkat_like_rows(), TOKA_ASSETS, valuation="2025-06-30")
    data = PF_HEADER + f"N1,2025-03-10T12:00:00Z,deposit,,,,,Wallet K,TOKA,{TOKA_QTY},,,,0,nachgetragen\n" \
                       "N2,2025-03-12T12:00:00Z,deposit,,,,,Wallet K,TOKA,1.5,,,,0,anderer Vorgang\n"
    upload(c, "manuell.csv", data)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def snapshot(ctx) -> tuple:
    """Wirksamer Stand: Buchungen des Portfolios und Bestände je Konto."""
    ctx.invalidate_data()
    pf = ctx.portfolio()
    txs = sorted((t.tx_id, t.type, str(t.from_qty or ""), t.from_asset or "", str(t.to_qty or ""), t.to_asset or "",
                  t.from_account or "", t.to_account or "") for t in pf.txs)
    led = ctx.ledger()
    holds = {acc: {a: str(q) for a, q in led.holdings_by_account(acc).items() if q} for acc in pf.all_accounts()}
    return txs, holds


def active(ctx) -> tuple[int, int]:
    return (int(ctx.db.scalar("SELECT COUNT(*) FROM diag_decision WHERE status='active'", default=0)),
            int(ctx.db.scalar("SELECT COUNT(*) FROM import_action WHERE status='active'", default=0)))


def answer_default(c) -> bool:
    """Wie ein Mensch: Karte öffnen, vorausgewählte (sonst erste) Antwort speichern. ``False``: nichts mehr offen."""
    html = c.get(f"{BASE}?new=1").text
    if 'name="kid"' not in html:
        return False
    fields = dict(re.findall(r'<input type="hidden" name="([a-z_]+(?::[a-z_]+)?)" value="([^"]*)"', html))
    ids = re.findall(r'name="a" value="([^"]+)"', html)
    checked = re.findall(r'name="a" value="([^"]+)" checked', html)
    with_inputs = set(re.findall(r'name="n_([^"]+)" value="1"', html))  # Antworten mit Eingaben → Bestätigungsseite
    aid = next((a for a in checked + ids if a not in OPENING and a not in with_inputs), None)
    if aid is None:
        return False
    form = {k: v.replace("&#34;", '"') for k, v in fields.items() if k in ("typ", "ref", "kid", "before")
            or k.startswith("x_") or k in (f"t_{aid}", f"n_{aid}", f"l_{aid}")}
    r = c.post(f"{BASE}/antwort", data={**form, "csrf_token": c.token, "a": aid}, follow_redirects=False)
    return r.status_code == 303 and "err=" not in r.headers.get("location", "")


def make_changes(c, rounds: int = 6) -> None:
    for _ in range(rounds):
        if not answer_default(c):
            break
    ctx = c.app.state.ctx
    ctx.db.set_state(G.DECLINED_KEY, [99])
    ctx.db.set_state(G.INTEGRITY_ACK_KEY, {"x": "y"})
    ctx.db.set_state(RC.MARKS_KEY, {"T1": {"at": "2025-01-01T00:00:00Z", "reason": "test"}})


def test_reset_round_trip_restores_the_previous_state(client):
    ctx = client.app.state.ctx
    before, n_before = snapshot(ctx), len(G.tasks(ctx))
    make_changes(client)
    dec, act = active(ctx)
    assert dec >= 1 and act >= 1, (dec, act)
    assert snapshot(ctx) != before or dec + act >= 2
    p = RS.plan(ctx, ["import"])
    assert p.units and p.available["antworten"] >= 1 and p.available["import"] >= 1
    assert [u.sort_key for u in p.units] == sorted((u.sort_key for u in p.units), reverse=True)  # neueste zuerst
    assert {"vormerkungen", "kenntnis", "vorrang"} <= set(p.states)
    # falsche Prüfsumme: nichts passiert
    out = RS.run(ctx, ["import"], "falsch")
    assert not out["ok"] and "Vorschau" in out["errors"][0] and active(ctx) == (dec, act)
    ctx.settings.set("autopilot.enabled", True)
    out = RS.run(ctx, ["import"], p.token)
    assert out["ok"], out
    assert out["done"] == out["total"] == len(p.units) and out["autopilot_was_on"]
    assert active(ctx) == (0, 0) and snapshot(ctx) == before
    assert len(G.tasks(ctx)) == n_before  # der Assistent fängt von vorn an
    assert RC.marks(ctx.db) == {} and not ctx.db.get_state(G.DECLINED_KEY) and not ctx.db.get_state(
        G.INTEGRITY_ACK_KEY)
    assert ctx.settings.get("autopilot.enabled") is False
    b = next(x for x in list_backups(ctx) if x["name"] == out["backup"])
    assert "Von vorn beginnen" in b["label"]
    assert RS.last(ctx.db)["ok"] and not RS.plan(ctx, ["import"]).total


def test_default_scope_keeps_decisions_on_new_transactions(client):
    ctx = client.app.state.ctx
    make_changes(client)
    _dec, act = active(ctx)
    p = RS.plan(ctx, [])
    assert p.scopes == ["antworten"] and all(u.scope == "antworten" for u in p.units)
    assert RS.run(ctx, [], p.token)["ok"]
    assert active(ctx) == (0, act)  # Entscheidungen zu neuen Vorgängen bleiben


def test_reset_stops_at_the_first_failure_and_reports_it(client, monkeypatch):
    ctx = client.app.state.ctx
    make_changes(client)
    p = RS.plan(ctx, ["import"])
    assert len(p.units) >= 2
    real, calls = RS._undo_unit, []

    def flaky(ctx_, u):
        calls.append(u)
        return ["absichtlich fehlgeschlagen"] if len(calls) == 2 else real(ctx_, u)

    monkeypatch.setattr(RS, "_undo_unit", flaky)
    out = RS.run(ctx, ["import"], p.token)
    assert not out["ok"] and out["done"] == 1 and len(calls) == 2
    assert out["failed"]["errors"] == ["absichtlich fehlgeschlagen"] and "absichtlich" in out["errors"][0]
    assert RC.marks(ctx.db)  # Zustände bleiben, wenn angehalten wurde
    page = client.get(f"{BASE}/protokoll").text
    assert "Angehalten" in page and "1 von" in page


def test_web_flow_needs_both_confirmations(client):
    ctx = client.app.state.ctx
    make_changes(client)
    page = client.get(f"{BASE}/neustart?s=import").text
    p = RS.plan(ctx, ["import"])
    assert f"<b>{p.total}</b>" in page and RS.CONFIRM_WORD in page and p.token in page
    form = {"csrf_token": client.token, "token": p.token, "s": ["antworten", "import"]}
    r = client.post(f"{BASE}/neustart", data={**form, "ok": "1", "word": "zurücksetzen"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and active(ctx) != (0, 0)
    r = client.post(f"{BASE}/neustart", data={**form, "word": RS.CONFIRM_WORD}, follow_redirects=False)
    assert "err=" in r.headers["location"] and active(ctx) != (0, 0)  # Häkchen fehlt
    r = client.post(f"{BASE}/neustart", data={**form, "ok": "1", "word": RS.CONFIRM_WORD, "token": "alt"},
                    follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert "Vorschau" in q["err"][0] and active(ctx) != (0, 0)
    r = client.post(f"{BASE}/neustart", data={**form, "ok": "1", "word": RS.CONFIRM_WORD}, follow_redirects=False)
    assert r.headers["location"] == f"{BASE}/protokoll#neustart" and active(ctx) == (0, 0)
    log_page = client.get(f"{BASE}/protokoll").text
    assert "Zuletzt am" in log_page and "Sicherung von vorher" in log_page
    assert "Von vorn beginnen" in log_page and "Sicherung wiederherstellen" in log_page
