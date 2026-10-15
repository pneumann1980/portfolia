"""Konten zusammenführen (synthetische Daten, temporäre DB): Zuordnung ohne Buchung, Vorschau, Undo, Randfälle."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.assetchange import account_merge as AM
from app.config import Config, Secrets
from app.diagnosis.engine import report_for
from app.main import build_app
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import make_ctx

D = Decimal
A, T = "Wertpapiere Broker unbekannt", "DKB"


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


ROWS = [
    tx("F0", "2024-01-02T08:00:00Z", "deposit", to=(T, "EUR", "10000"), value=10000),
    tx("B1", "2024-01-10T10:00:00Z", "buy", frm=(T, "EUR", "2000"), to=(T, "BTC", "0.1"), value=2000),
    # Kauf und Verkauf liegen in verschiedenen Konten (Alias = Platzhalter aus dem kuratierten Import)
    tx("F1", "2024-02-02T08:00:00Z", "deposit", to=(A, "EUR", "1000"), value=1000),
    tx("B2", "2024-02-10T10:00:00Z", "buy", frm=(A, "EUR", "1000"), to=(A, "ETH", "1"), value=1000),
    tx("S1", "2024-09-10T10:00:00Z", "sell", frm=(A, "ETH", "1"), to=(A, "EUR", "1500"), value=1500),
]


def journal_rows(ctx) -> list[tuple]:
    return [tuple(r) for r in ctx.db.q("SELECT * FROM journal_tx ORDER BY 1")]


def test_pure_mapping_renames_accounts_and_sums_soll_bestand(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS)
    pf = ctx.portfolio()
    pf2 = AM.map_portfolio(pf, {A: T})
    assert A not in pf2.all_accounts() and T in pf2.all_accounts()
    assert AM.map_portfolio(pf, {}) is pf and AM.map_portfolio(pf, {T: T}) is pf
    hc = [{"account": A, "asset_id": "BTC", "qty": D("1")}, {"account": T, "asset_id": "BTC", "qty": D("2")}]
    from dataclasses import replace
    out = AM.map_portfolio(replace(pf, holdings_check=hc), {A: T})
    assert out.holdings_check == [{"account": T, "asset_id": "BTC", "qty": D("3")}]
    assert pf.holdings_check != out.holdings_check  # Original unverändert


def test_preview_reports_balances_twins_and_changes_nothing(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS)
    rows = journal_rows(ctx)
    p = AM.preview(ctx, A, T)
    assert p.ok and p.fp and p.n_alias_txs == 3 and p.n_target_txs == 2
    eur = next(b for b in p.balances if b.asset == "EUR")
    assert eur.alias_qty + eur.target_qty == eur.after_qty
    assert journal_rows(ctx) == rows and AM.active_map(ctx.db) == {}
    assert not AM.preview(ctx, A, A).ok and not AM.preview(ctx, "gibtsnicht", T).ok
    assert not AM.preview(ctx, T, "").ok


def test_twin_bookings_are_counted_not_removed(cfg):
    rows = [*ROWS, tx("B1x", "2024-01-10T10:00:00Z", "buy", frm=(A, "EUR", "2000"), to=(A, "BTC", "0.1"),
                      value=2000)]
    ctx = make_ctx(cfg, rows, ASSETS)
    p = AM.preview(ctx, A, T)
    assert p.n_twins == 1 and any("identische Entsprechung" in w for w in p.warnings)
    mid, errors = AM.apply(ctx, A, T, p.fp)
    assert mid and not errors
    ctx.invalidate_data()
    assert ctx.ledger().balances[(T, "BTC")] == D("0.2")  # beide Käufe zählen – nichts entfernt
    assert [f for f in report_for(ctx).findings if f.kind == "duplicate"]  # Diagnose legt sie zur Entscheidung vor


def test_apply_requires_fingerprint_is_idempotent_and_reversible(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS)
    p = AM.preview(ctx, A, T)
    assert AM.apply(ctx, A, T, "falsch")[0] is None
    mid, errors = AM.apply(ctx, A, T, p.fp, "dasselbe Depot")
    assert mid and not errors
    assert AM.apply(ctx, A, T, p.fp)[0] == mid  # idempotent (Alias ist nicht mehr vorhanden → bestehende Zuordnung)
    assert AM.active_map(ctx.db) == {A: T}
    ctx.invalidate_data()
    pf = ctx.portfolio()
    assert A not in pf.all_accounts()
    assert ctx.ledger().balances[(T, "EUR")] == D(8000) + D(1500)  # 10000-2000 + (1000-1000+1500)
    assert AM.revert(ctx, 9999 + mid) != []
    assert AM.revert(ctx, mid) == [] and AM.revert(ctx, mid) == []  # idempotent
    ctx.invalidate_data()
    assert A in ctx.portfolio().all_accounts() and AM.active_map(ctx.db) == {}


def test_chain_and_cycle_resolution(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS)
    now = "2026-01-01T00:00:00Z"
    for i, (a, t) in enumerate((("X", "Y"), ("Y", "Z"), ("Z", "X"))):
        ctx.db.x("INSERT INTO account_merge(alias_account, target_account, status, created_at) VALUES (?,?,'active',?)",
                 (a, t, f"{now[:-1]}{i}Z"))
    assert AM.active_map(ctx.db) == {}  # Zyklus wird ignoriert
    ctx.db.x("DELETE FROM account_merge")
    for i, (a, t) in enumerate((("X", "Y"), ("Y", "Z"))):
        ctx.db.x("INSERT INTO account_merge(alias_account, target_account, status, created_at) VALUES (?,?,'active',?)",
                 (a, t, f"{now[:-1]}{i}Z"))
    assert AM.active_map(ctx.db) == {"X": "Z", "Y": "Z"}


def test_data_source_follows_merged_account(cfg):
    from app.datasources.service import datasource_service

    ctx = make_ctx(cfg, ROWS, ASSETS)
    svc = datasource_service(ctx)
    ctx.db.x("INSERT INTO data_source(kind, provider, name, account, status, created_at, updated_at) VALUES "
             "('exchange','other_exchange','Alt',?, 'manual','2024-01-01T00:00:00Z','2024-01-01T00:00:00Z')", (A,))
    assert svc.list()[0].account == A
    AM.apply(ctx, A, T, AM.preview(ctx, A, T).fp)
    assert svc.list()[0].account == T
    assert not any(u["account"] == A for u in svc.unlinked_accounts())


def test_web_flow(cfg):
    import os
    import time

    from app.importer.zipbuilder import build_zip
    from app.jobs import tasks
    from tests.test_diagnosis import COLS

    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=ROWS, assets=ASSETS, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        page = c.get("/accounts/merge")
        assert page.status_code == 200 and "Dasselbe Konto" in page.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        prev = c.post("/accounts/merge/preview", data={"csrf_token": token, "alias": A, "target": T})
        assert prev.status_code == 200 and "Bestände je Asset" in prev.text
        fp = re.search(r'name="fp" value="([^"]+)"', prev.text).group(1)
        no = c.post("/accounts/merge/apply", data={"csrf_token": token, "alias": A, "target": T, "fp": fp},
                    follow_redirects=True)
        assert "Bitte die Vorschau bestätigen" in no.text and not AM.active_map(ctx.db)
        ok = c.post("/accounts/merge/apply", data={"csrf_token": token, "alias": A, "target": T, "fp": fp,
                                                    "confirm": "1"}, follow_redirects=True)
        assert "ist jetzt" in ok.text and AM.active_map(ctx.db) == {A: T}
        assert c.get("/settings/datasources").status_code == 200
        mid = AM.history(ctx.db)[0]["id"]
        back = c.post(f"/accounts/merge/{mid}/revert", data={"csrf_token": token}, follow_redirects=True)
        assert "zurückgenommen" in back.text and AM.active_map(ctx.db) == {}
        bad = c.post("/accounts/merge/preview", data={"csrf_token": token, "alias": "nix", "target": T})
        assert bad.status_code == 400


def test_export_roundtrip_keeps_merges(cfg):
    import json

    from app import fullexport

    ctx = make_ctx(cfg, ROWS, ASSETS)
    AM.apply(ctx, A, T, AM.preview(ctx, A, T).fp)
    files = fullexport.collect(ctx, set())
    assert fullexport.summary(files)["account_merges"] == 1
    st = json.loads(files[fullexport.STATE].decode())
    with ctx.db.transaction() as c:
        fullexport._insert(c, "account_merge", st["account_merges"], "OR IGNORE")
    assert ctx.db.scalar("SELECT COUNT(*) FROM account_merge") == 1


def test_transfer_between_the_two_accounts_becomes_internal(cfg):
    rows = [ROWS[0], ROWS[1],
            tx("T1", "2024-03-01T10:00:00Z", "transfer", frm=(T, "BTC", "0.1"), to=(A, "BTC", "0.1"), value=2500)]
    ctx = make_ctx(cfg, rows, ASSETS)
    p = AM.preview(ctx, A, T)
    assert p.n_between == 1 and any("innerhalb eines Kontos" in w for w in p.warnings)
    assert AM.apply(ctx, A, T, p.fp)[0]
    ctx.invalidate_data()
    led = ctx.ledger()
    assert led.balances[(T, "BTC")] == D("0.1") and (A, "BTC") not in led.balances
    assert not [d for d in led.disposals if d.asset == "BTC"]  # keine Scheinveräußerung
