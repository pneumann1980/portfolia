"""Konfliktklärung im Gesamtsystem: Oberfläche, gezielte Recherche, Export/Restore, Skalierung (synthetische Daten)."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Config, Secrets
from app.diagnosis import clarify as K
from app.diagnosis import references as R
from app.diagnosis.engine import report_for
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.journal.service import journal_service
from app.main import build_app
from tests.helpers import tx
from tests.test_clarify import ASSETS_K, FUND, balance, sand_rows
from tests.test_diagnosis import COLS, make_ctx
from tests.test_diagnosis_audit import koinly
from tests.test_full_export import MASTER, SAMPLE, _client, _other, _place, _prepare

D = Decimal


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


@pytest.fixture(autouse=True)
def _reset_flag():
    K._FLAG["dirty"] = False
    yield
    K._FLAG["dirty"] = False


# ----------------------------------------------------------------------------------------------------
# Oberfläche
# ----------------------------------------------------------------------------------------------------

def test_web_flow_question_confirm_apply_and_undo(cfg):
    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=sand_rows(), assets=ASSETS_K, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        page = c.get("/quality/klaerung")
        assert page.status_code == 200 and "Konfliktklärung" in page.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        c.headers["X-CSRF-Token"] = token
        r = c.post("/quality/klaerung/lauf", data={"csrf_token": token}, follow_redirects=True)
        assert r.status_code == 200 and "wartet auf Bestandsnachweis" in r.text
        key = re.search(r'href="/quality/klaerung/(kg-[0-9a-f]+)"', r.text).group(1)
        case = c.get(f"/quality/klaerung/{key}")
        assert case.status_code == 200 and "SAND-Bestand" in case.text and "Uhrzeit" in case.text
        assert "Bestand/Menge" in case.text and "Gebühren" in case.text and "rechnerisch exakt erklärt" in case.text
        # ohne CSRF-Token wird abgelehnt
        bad = TestClient(c.app).post(f"/quality/klaerung/{key}/bestand", data={"qty": "52,319"},
                                     follow_redirects=False)
        assert bad.status_code in (400, 403)
        form = {"csrf_token": token, "account": "Bybit", "asset": "SAND", "qty": "52,319", "as_of": "2025-06-01",
                "time": "12:00:00", "tz": "UTC", "source": "api", "apply": "1"}
        r = c.post(f"/quality/klaerung/{key}/bestand", data=form, follow_redirects=True)
        assert r.status_code == 200 and "Übernommen und nachgeprüft" in r.text
        assert balance(ctx, "Bybit", "SAND") == D("52.319")
        done = c.get(f"/quality/klaerung/{key}")
        assert "Verlauf" in done.text and "Gemeinsam rückgängig" in done.text
        run = K.runs_of(ctx.db, key)[0]
        r = c.post(f"/quality/klaerung/rueckgaengig/{run['token']}", data={"csrf_token": token},
                   follow_redirects=True)
        assert "zurückgenommen" in r.text and balance(ctx, "Bybit", "SAND") == D("104.638")
        assert c.get("/quality/klaerung/kg-gibtsnicht").status_code == 404
        assert c.get("/quality/diagnose").status_code == 200  # Verweis auf die Klärung ist Teil der Diagnose


# ----------------------------------------------------------------------------------------------------
# Gezieltes Nachladen (bestehende Recherche-Infrastruktur, 0.28.3-Schutzregeln)
# ----------------------------------------------------------------------------------------------------

class _DS:
    def __init__(self, sid: int, name: str, account: str, *, supported: bool = True, enabled: bool = True) -> None:
        self.id, self.name, self.account, self.provider = sid, name, account, "bybit"
        self.provider_label, self.supported, self.enabled = "Bybit", supported, enabled


def test_targeted_research_respects_protections(cfg, monkeypatch):
    ctx = make_ctx(cfg, sand_rows(), ASSETS_K)
    items = [_DS(1, "ok", "Bybit"), _DS(2, "aus", "Bybit", enabled=False), _DS(3, "busy", "Bybit"),
             _DS(4, "manuell", "Bybit", supported=False), _DS(5, "andere", "Anderes Konto")]
    case = K.analyse(ctx)[0]
    monkeypatch.setattr(K, "_ds_list", lambda c: items)
    monkeypatch.setattr(K, "_ds_busy", lambda sid: sid == 3)
    calls: list[tuple] = []

    def fake(c, sid, **kw):
        calls.append((sid, kw))
        return {"message": "2 Ereignis(se) gelesen, 0 neu abgelegt"}

    monkeypatch.setattr(K, "RESEARCH_RUN", fake)
    notes = K.request_evidence(ctx, case)
    assert calls == [(1, {"account": "Bybit", "asset": "SAND"})]  # begrenzt auf Konto und Asset; nur diese Quelle
    text = "\n".join(notes)
    assert "Blockade: Bybit (aus) ist deaktiviert" in text and "Abruf läuft bereits" in text
    assert "kein automatischer Abruf" in text and "andere" not in text
    # bereits recherchiert (abgeschlossener Lauf) → kein zweiter Abruf; Abruflimit je Durchgang
    ctx.db.x("INSERT INTO diag_research(source_id, account, asset, status, started_at, finished_at) VALUES "
             "(1, 'Bybit', 'SAND', 'done', '2025-06-01T00:00:00+00:00', '2025-06-01T00:01:00+00:00')")
    calls.clear()
    notes = K.request_evidence(ctx, case)
    assert calls == [] and any("bereits recherchiert" in n for n in notes)
    notes = K.request_evidence(ctx, case, [0])
    assert calls == []


def test_run_documents_research_and_blockade_in_the_case(cfg, monkeypatch):
    ctx = make_ctx(cfg, sand_rows(), ASSETS_K)
    monkeypatch.setattr(K, "_ds_list", lambda c: [_DS(2, "aus", "Bybit", enabled=False)])
    monkeypatch.setattr(K, "_ds_busy", lambda sid: False)
    out = K.run(ctx, "manual", research=True)
    assert out["researched"]
    st = K.stored_cases(ctx.db)[0]
    assert any(n.startswith("Blockade") for n in st["state"]["research"])
    assert any(m.startswith("Blockade") for m in st["state"]["decision"]["missing"])  # konkrete Blockade, kein „später“


# ----------------------------------------------------------------------------------------------------
# 16 · Export/Restore
# ----------------------------------------------------------------------------------------------------

@pytest.fixture
def source(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    shutil.copy(SAMPLE, config.import_dir / "beispiel.zip")
    os.utime(config.import_dir / "beispiel.zip", (time.time() - 7200, time.time() - 7200))
    with _client(config) as c:
        ctx = c.app.state.ctx
        tasks.import_check(ctx, "test")
        _prepare(c)
        js = journal_service(ctx)
        assert not js.save_asset({"asset_id": "SAND", "name": "The Sandbox", "asset_class": "crypto",
                                  "quote_source": "none", "quote_id": ""}).errors
        base = {"kind": "expert", "time": "10:00", "note": "synthetisch"}
        for data in ({"type": "deposit", "tag": "", "date": "2024-04-30", "to_account": "Bybit",
                      "to_asset": "SAND", "to_qty": "500", "value_eur": "100"},
                     {"type": "deposit", "tag": "", "date": "2024-04-30", "to_account": "Bybit",
                      "to_asset": "SAND", "to_qty": "500", "value_eur": "100", "time": "10:01"}):
            res = js.save({**base, **data})
            assert not res.errors, res.errors
        ctx.invalidate_data()
        yield c, ctx, config


def test_16_clarification_state_survives_real_restore(source, tmp_path, monkeypatch):
    c, ctx, config = source
    # Nachweise mit Umfang/Unterkonto/gebundener Menge und ein Klärungsdurchgang
    assert R.add(ctx, "Bybit", "SAND", "500", "2025-06-01", "api", "", "12:00", "UTC", subaccount="Spot",
                 scope="spot", qty_locked="50", case_key="kg-test").ok
    assert R.add(ctx, "Bybit", "SAND", "5,5", "2025-06-01", "api", "", "12:00", "UTC", scope="derivatives",
                 qty_margin="2", qty_pnl="-1").ok
    ctx.invalidate_data()
    out = K.run(ctx, "manual", research=False)
    cases = K.stored_cases(ctx.db)
    assert cases, out
    key = cases[0]["key"]
    assert K.defer(ctx, key, "geprüft, später").ok
    K.log_run(ctx, K.analyse(ctx)[0], "manual", "wartet", effect={"x": 1})
    before = {
        "cases": [(x["key"], x["status"], x["title"], json.dumps(x["dims"], sort_keys=True)) for x in
                  K.stored_cases(ctx.db)],
        "runs": [(r["trigger"], r["outcome"]) for r in K.runs_of(ctx.db, key)],
        "refs": sorted((r["scope"], r["subaccount"], r["qty"], r["qty_locked"], r["qty_margin"], r["qty_pnl"])
                       for r in ctx.db.q("SELECT * FROM reference_balance WHERE status='active'")),
        "ledger": sorted((k, str(v)) for k, v in report_for(ctx).snapshot.ledger.balances.items()
                         if k[1] == "SAND"),
    }
    body = c.get("/journal/export.zip").content
    cfg2 = _other(config, tmp_path, "b")
    _place(cfg2, body, "portfolia-export.zip")
    monkeypatch.setattr(K, "run", lambda *a, **k: None)  # reiner Restore-Zustand, ohne anschließenden Durchgang
    with _client(cfg2) as c2:
        ctx2 = c2.app.state.ctx
        assert tasks.import_check(ctx2, "test").status == "imported"
        after = {
            "cases": [(x["key"], x["status"], x["title"], json.dumps(x["dims"], sort_keys=True)) for x in
                      K.stored_cases(ctx2.db)],
            "runs": [(r["trigger"], r["outcome"]) for r in K.runs_of(ctx2.db, key)],
            "refs": sorted((r["scope"], r["subaccount"], r["qty"], r["qty_locked"], r["qty_margin"], r["qty_pnl"])
                           for r in ctx2.db.q("SELECT * FROM reference_balance WHERE status='active'")),
            "ledger": sorted((k, str(v)) for k, v in report_for(ctx2).snapshot.ledger.balances.items()
                             if k[1] == "SAND"),
        }
        assert after == before
        assert ctx2.db.scalar("SELECT status FROM clarify_case WHERE case_key=?", (key,)) == "zurueck"
        monkeypatch.undo()
        # ein späterer Durchgang in der neuen Instanz setzt die Rückstellung bei gleicher Evidenz fort
        K.run(ctx2, "manual", research=False)
        assert ctx2.db.scalar("SELECT status FROM clarify_case WHERE case_key=?", (key,)) == "zurueck"


# ----------------------------------------------------------------------------------------------------
# Skalierung
# ----------------------------------------------------------------------------------------------------

def test_scale_many_cases_bounded_search_budget_and_rotation(cfg):
    n = 24
    rows = [FUND]
    assets = [*ASSETS_K]
    from tests.test_diagnosis import asset as mk_asset

    for i in range(n):
        aid = f"TK{i:02d}"
        assets.append(mk_asset(aid, f"Token {i}", "coingecko", f"token-{i}"))
        acc = f"Konto {i:02d}"
        rows += [koinly(tx(f"K{i}", f"2025-03-{1 + i % 27:02d}T10:00:00Z", "deposit", to=(acc, aid, "100"), value=10)),
                 tx(f"A{i}", f"2025-03-{1 + i % 27:02d}T10:00:30Z", "deposit", to=(acc, aid, "100"), value=10)]
        rows[-1]["source"], rows[-1]["source_ref"] = "portfolia:sync:x", f"x:{i}#0"
    # Ballast: viele unauffällige Vorgänge (Skalierung der Ledger-Rechnung)
    for j in range(600):
        rows.append(tx(f"B{j}", f"2025-01-{1 + j % 27:02d}T{j % 24:02d}:00:00Z", "deposit",
                       to=("Ballast", "USDT", "1"), value=1))
    ctx = make_ctx(cfg, rows, assets)
    t0 = time.perf_counter()
    cases = K.analyse(ctx, budget=10)
    elapsed = time.perf_counter() - t0
    done = [x for x in cases if not x.deferred]
    deferred = [x for x in cases if x.deferred]
    assert len(cases) >= n and deferred, "Budget muss deterministisch begrenzen"
    assert sum(max(1, len(x.hyps)) for x in done) <= 10 + max(len(x.hyps) for x in done)
    assert all("Folgelauf" in x.decision.reason for x in deferred)
    assert elapsed < 120
    # Wiederholen liefert dieselbe Auswahl; persistierte Rotation: zuerst die noch nicht untersuchten Gruppen
    again = K.analyse(ctx, budget=10)
    assert [x.key for x in again if not x.deferred] == [x.key for x in done]
    K.persist(ctx, cases)
    third = K.analyse(ctx, budget=10)
    assert {x.key for x in third if not x.deferred} & {x.key for x in deferred}
    # Hypothesenraum je Gruppe begrenzt: Grenze 3 → „Prüfumfang überschritten“, keine Auswahl
    limited = K.analyse(ctx, limit=1, budget=1000)
    assert all(x.decision.kind == "umfang" and x.status == "umfang" for x in limited if len(x.cands) > 0)
    assert all("Prüfumfang überschritten" in x.decision.reason for x in limited if x.status == "umfang")
