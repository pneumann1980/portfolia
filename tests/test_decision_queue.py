"""Entscheidungsliste: Aufgaben der Reihe nach entscheiden (synthetische Daten, temporäre DB)."""

from __future__ import annotations

import os
import re
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis import queue as Q
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import COLS, make_ctx

FUND = tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Börse", "EUR", "10000"), value=10000)
ROWS = [FUND,
        tx("B1", "2025-01-10T10:00:00Z", "buy", frm=("Börse", "EUR", "2000"), to=("Börse", "ETH", "1"), value=2000),
        tx("W1", "2025-03-01T12:00:00Z", "withdrawal", frm=("Börse", "ETH", "1"), value=3000),
        tx("D1", "2025-03-01T12:20:00Z", "deposit", to=("Wallet", "ETH", "1"), value=3000),
        tx("F1", "2022-01-10T10:00:00Z", "deposit", to=("Wallet Alt", "KAS", "5000"), value="500")]


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def test_queue_holds_only_open_tasks_without_hints(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS)
    items = Q.queue(ctx)
    assert items and all(f.status != "hinweis" and not f.children for f in items)
    assert any(f.kind == "transfer" for f in items) and not any(f.kind == "inactive" for f in items)
    first = items[0]
    assert A.mark(ctx, first.id, "defer").ok  # später vorgemerkt → nicht mehr in der Liste
    assert first.id not in {f.id for f in Q.queue(ctx)}
    assert Q.position(items, "gibtsnicht") == 0 and Q.position(items, items[-1].id) == len(items) - 1


def test_queue_page_flow_decides_and_advances(cfg):
    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=ROWS, assets=ASSETS, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        n = len(Q.queue(ctx))
        assert n >= 1
        page = c.get("/quality/diagnose/liste")
        assert page.status_code == 200 and f"Fall 1 von {n}" in page.text and "Empfehlung" in page.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        cur = re.search(r'name="f" value="([^"]+)"', page.text).group(1)
        # Vorschau-Link trägt den Rückweg in die Liste
        assert "back=liste" in page.text
        r = c.post("/quality/diagnose/mark", data={"csrf_token": token, "f": cur, "action": "dismiss", "back": "liste",
                                                    "note": "geprüft"}, follow_redirects=True)
        assert r.status_code == 200 and "Entscheidungsliste" in r.text
        assert len(Q.queue(ctx)) == n - 1 and cur not in {f.id for f in Q.queue(ctx)}
        assert c.get(f"/quality/diagnose/liste?f={cur}").status_code == 200  # veralteter Verweis → erster Fall
        # Rest bis zum leeren Zustand entscheiden
        for _ in range(n):
            nxt = Q.queue(ctx)
            if not nxt:
                break
            c.post("/quality/diagnose/mark", data={"csrf_token": token, "f": nxt[0].id, "action": "defer",
                                                    "back": "liste"})
        assert "Nichts mehr zu entscheiden" in c.get("/quality/diagnose/liste").text
        # Plan-Seite mit Rückweg
        plan_page = c.get("/quality/diagnose")
        assert plan_page.status_code == 200 and "Der Reihe nach" not in plan_page.text  # ersetzt durch den Assistenten


def test_plan_page_keeps_back_to_queue(cfg):
    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=ROWS, assets=ASSETS, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        tasks.import_check(ctx, "test")
        page = c.get("/quality/diagnose/liste").text
        href = re.search(r'href="(/quality/diagnose/plan\?[^"]+back=liste)"', page)
        assert href, "kein Vorschau-Link in der Liste"
        plan = c.get(href.group(1).replace("&amp;", "&"))
        assert plan.status_code == 200 and 'name="back" value="liste"' in plan.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', plan.text).group(1)
        form = dict(re.findall(r'name="(f|o|token|p__set)" value="([^"]*)"', plan.text))
        r = c.post("/quality/diagnose/apply", data={"csrf_token": token, **form, "back": "liste"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/quality/diagnose/liste")
