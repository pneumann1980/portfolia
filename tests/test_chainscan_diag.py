"""Kettenbestand in der Diagnose: Empfehlung, Lösung „einzeln buchen“ (Vorschau, Übernehmen, Rückgängig), Seite."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis import chainscan as CS
from app.diagnosis.engine import report_for
from app.diagnosis.recommend import recommend
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import asset, by_kind, client_with_import

D = Decimal
ADDR = "0x1e27c175d496f315c4955fa1f31d4fd5777ed856"
ACC = "peaq Wallet"
PEAQ_ASSETS = [*ASSETS, asset("PEAQ")]
NOW = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")  # frischer Abruf
T1 = datetime(2025, 6, 10, 12, 0, 8, tzinfo=UTC)
T2 = datetime(2025, 9, 2, 3, 30, 0, tzinfo=UTC)


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def scan_result(total: str = "100", steps=None) -> CS.Result:
    steps = steps if steps is not None else [
        CS.Step(T1, D("1052.5"), D("1052.5"), {"peaq": 5_123_456}, {"peaq": "https://peaq.subscan.io/block/5123456"}),
        CS.Step(T2, D("10718.83"), D("10718.83"), {"peaq": 9_000_001})]
    return CS.Result(ok=True, complete=True, steps=steps, start=T1.replace(year=2024), end=T2, r_end=D("11771.33"),
                     chain_end=D("11871.33"), asset="PEAQ", nets=["peaq"], calls=77, checked_at=NOW,
                     ledger_total=D(total))


@pytest.fixture
def client(cfg):
    rows = [tx("D0", "2025-03-01T00:00:00Z", "deposit", to=(ACC, "PEAQ", "100"), value="10")]
    c = client_with_import(cfg, rows, PEAQ_ASSETS, valuation="2026-10-01")
    ctx = c.app.state.ctx
    ctx.db.x("INSERT INTO data_source(id, kind, provider, name, account, address, status, coverage_json, created_at, "
             "updated_at) VALUES (1,'wallet','peaq','peaq Hauptwallet',?,?, 'synced', ?, ?, ?)",
             (ACC, ADDR, json.dumps({"complete": True, "gaps": []}), NOW, NOW))
    ctx.db.x("INSERT INTO ds_balance(source_id, asset_key, qty, observed_at) VALUES (1,'PEAQ','11871.33',?)", (NOW,))
    ctx.db.set_state(CS.state_key(ACC, "PEAQ"), {"result": scan_result().to_json(), "cache": {}})
    ctx.invalidate_data()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def holdings(ctx):
    rep = report_for(ctx)
    (f,) = [x for x in by_kind(rep, "holdings") if ACC in x.title]
    return rep, f


def test_recommendation_offers_chain_steps_with_exact_blocks(client):
    ctx = client.app.state.ctx
    rep, f = holdings(ctx)
    assert f.data["status"] == "extern_diff" and D(f.data["observed"]) - D(f.data["computed"]) == D("11771.33")
    rec = recommend(rep, f)
    opt = rec.option("chain_steps")
    assert opt is not None and opt.recommended and opt.params
    assert "Block 5.123.456" in rec.text and "2-mal ohne passende Buchung" in rec.text
    assert [p.name for p in opt.params if p.name.startswith("value")] == ["value0", "value1"]
    assert any(lk.url.startswith("/quality/kette?account=") for lk in rec.links)
    assert not rec.conditional
    assert rec.option("adjust") is not None  # Notlösung bleibt


def test_booking_each_jump_with_block_time_and_undo(client):
    ctx = client.app.state.ctx
    rep, f = holdings(ctx)
    params = {"tag": [""], "value0": ["50"], "value1": [""], "p": []}
    plan = A.build_plan(ctx, rep, f, "chain_steps", {k: v for k, v in params.items() if v}, given=True)
    assert plan.errors and "kein Kurs" in plan.errors[0]  # Tageskurs fehlt → Eingabe verlangt
    params["value1"] = ["500,25"]
    plan = A.build_plan(ctx, rep, f, "chain_steps", {k: v for k, v in params.items() if v}, given=True)
    assert not plan.errors, plan.errors
    t1, t2 = (op.tx for op in plan.ops)
    assert (t1.type, t1.to_account, t1.to_qty, t1.ts) == ("deposit", ACC, D("1052.5"), T1)
    assert (t2.to_qty, t2.ts, t2.value_eur) == (D("10718.83"), T2, D("500.25"))
    assert "Block 5123456" in plan.ops[0].row["note"] and "Hash und Gegenadresse unbekannt" in plan.ops[0].row["note"]
    eff = A.preview(ctx, rep, plan)
    (pos,) = eff.positions
    assert pos.bal == (D(100), D("11871.33")) and pos.status[1] == "mit externer Quelle abgestimmt"
    did = A.apply(ctx, f.id, "chain_steps", plan.params, plan.token).decision_id
    assert [h.status for h in report_for(ctx).holdings if h.account == ACC] == ["extern_ok"]
    assert A.undo(ctx, did).ok
    assert [h.status for h in report_for(ctx).holdings if h.account == ACC] == ["extern_diff"]


def test_stale_scan_is_not_offered_and_not_booked(client):
    ctx = client.app.state.ctx
    ctx.db.set_state(CS.state_key(ACC, "PEAQ"), {"result": scan_result(total="90").to_json(), "cache": {}})
    ctx.invalidate_data()
    rep, f = holdings(ctx)
    rec = recommend(rep, f)
    assert rec.option("chain_steps") is None
    assert any("seit der Suche wurde gebucht" in lk.label for lk in rec.links)
    plan = A.build_plan(ctx, rep, f, "chain_steps", {"tag": [""], "value0": ["1"], "value1": ["1"]}, given=True)
    assert plan.errors and "nicht (mehr) zur Verfügung" in plan.errors[0]  # Option wird nicht angeboten


def test_ledger_only_steps_are_not_booked(client):
    ctx = client.app.state.ctx
    only = [CS.Step(T1, D("-3"), D(0), {"peaq": 5}, {}, ["x"], "ledger")]
    ctx.db.set_state(CS.state_key(ACC, "PEAQ"), {"result": scan_result(steps=only).to_json(), "cache": {}})
    ctx.invalidate_data()
    rep, f = holdings(ctx)
    assert recommend(rep, f).option("chain_steps") is None


def test_chain_page_lists_accounts_result_and_link_to_solution(client):
    page = client.get("/quality/kette").text
    assert "Konten mit Archivweg" in page and ACC in page
    page = client.get(f"/quality/kette?account={ACC}&asset=PEAQ").text
    assert "5123456" in page and "1.052,5" in page
    assert "Sprünge einzeln buchen" in page and "o=chain_steps" in page
    assert "Erneut prüfen" in page
    r = client.post("/quality/kette/pruefen", data={"csrf_token": client.token, "account": "Fremd", "asset": "PEAQ"},
                    follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_page_without_capable_wallet_explains_support(cfg):
    c = client_with_import(cfg, [tx("D0", "2025-03-01T00:00:00Z", "deposit", to=(ACC, "PEAQ", "1"), value="1")],
                           PEAQ_ASSETS, valuation="2026-10-01")
    try:
        page = c.get("/quality/kette").text
        assert "keinen kostenlosen Archivweg" in page or "Für keine deiner Wallet-Quellen" in page
        assert "Nachweise" in (hub := c.get("/quality/belege").text) and "/quality/kette" in hub
    finally:
        c.__exit__(None, None, None)


def fake_peaq(events: dict[int, str], tip: int = 5_000_000):
    """Archiv-Knoten: Blockzeit 12 s ab 01.01.2025, Bestand = Summe der Ereignisse bis zum Block."""
    import httpx

    t0 = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())

    def respond(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        m, p = body["method"], body["params"]
        if m == "eth_blockNumber":
            res = hex(tip)
        elif m == "eth_getBlockByNumber":
            res = {"timestamp": hex(t0 + (int(p[0], 16) - 1) * 12)}
        elif m == "eth_getBalance":
            res = hex(int(sum((D(q) for b, q in events.items() if b <= int(p[1], 16)), D(0)) * 10**18))
        else:
            res = None
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": res})
    return httpx.MockTransport(respond)


def test_refresh_finds_jumps_end_to_end_stores_cache_and_reruns_cheaply(client):
    ctx = client.app.state.ctx
    ctx.db.x("DELETE FROM app_state WHERE key LIKE 'chainscan:%'")
    events = {424_801: "100", 1_500_000: "1052.5", 3_000_000: "10718.83"}  # Block 424.801 = D0 im Ledger
    res = CS.refresh(ctx, ACC, "PEAQ", transport=fake_peaq(events), sleep=lambda s: None, max_calls=600)
    assert res.ok and res.complete, res.error
    assert [(s.blocks["peaq"], s.delta) for s in res.steps] == [(1_500_000, D("1052.5")), (3_000_000, D("10718.83"))]
    assert res.ledger_total == D(100) and res.residual == 0
    again = CS.stored(ctx.db, ACC, "PEAQ")
    assert again is not None and [s.delta for s in again.steps] == [D("1052.5"), D("10718.83")]
    cached = CS.refresh(ctx, ACC, "PEAQ", transport=fake_peaq(events), sleep=lambda s: None, max_calls=600)
    assert cached.calls < res.calls / 3  # nur Spitze und Zeit der Spitze neu; alles andere aus dem Zwischenspeicher
    ctx.invalidate_data()
    rep, f = holdings(ctx)
    assert recommend(rep, f).option("chain_steps") is not None


def test_refresh_without_archive_node_reports_it(client):
    import httpx

    ctx = client.app.state.ctx

    def pruned(req: httpx.Request) -> httpx.Response:
        m = json.loads(req.content)["method"]
        if m == "eth_blockNumber":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(4_000_000)})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32603, "message": "UnknownBlock(\"State already discarded\")"}})
    res = CS.refresh(ctx, ACC, "PEAQ", transport=httpx.MockTransport(pruned), sleep=lambda s: None)
    assert not res.ok and "kein öffentlicher Knoten" in res.error
    out = CS.job_run(ctx, "", "")
    assert out == {"ok": False, "error": "Konto oder Asset fehlt."}
