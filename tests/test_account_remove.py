"""Konto entfernen: Buchungen ausblenden (Überlagerung nach Buchungskennung), Vorschau, Bestätigung, Rückgängig
(synthetische Daten, temporäre DB)."""

from __future__ import annotations

import json
import os
import re
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import fullexport
from app.assetchange import account_remove as AR
from app.config import Config, Secrets
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import COLS, make_ctx

D = Decimal
KEEP, DROP = "DKB", "Testkonto"
ROWS = [
    tx("F0", "2024-01-02T08:00:00Z", "deposit", to=(KEEP, "EUR", "10000"), value=10000),
    tx("B1", "2024-01-10T10:00:00Z", "buy", frm=(KEEP, "EUR", "2000"), to=(KEEP, "BTC", "0.1"), value=2000),
    tx("T0", "2024-02-02T08:00:00Z", "deposit", to=(DROP, "EUR", "1000"), value=1000),
    tx("T1", "2024-02-10T10:00:00Z", "buy", frm=(DROP, "EUR", "1000"), to=(DROP, "ETH", "1"), value=1000),
    tx("T2", "2024-09-10T10:00:00Z", "sell", frm=(DROP, "ETH", "1"), to=(DROP, "EUR", "1500"), value=1500),
]


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def test_preview_changes_nothing_and_reports_effects(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS)
    before = ctx.db.scalar("SELECT COUNT(*) FROM tx_override", default=0)
    p = AR.preview(ctx, DROP)
    assert p.ok and p.n_txs == 3 and p.fp and p.first and p.last
    assert {b.asset for b in p.balances} == {"EUR"} and p.balances[0].qty == D("1500")  # Bestand fehlt danach
    assert any("Bestände" in w for w in p.warnings)
    assert p.years and any(y.changed for y in p.years)  # Veräußerung 2024 entfällt
    assert ctx.db.scalar("SELECT COUNT(*) FROM account_remove", default=0) == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM tx_override", default=0) == before
    assert not AR.preview(ctx, "gibtsnicht").ok and not AR.preview(ctx, "").ok


def test_apply_hides_bookings_is_reversible_and_new_bookings_are_unaffected(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS)
    p = AR.preview(ctx, DROP)
    assert AR.apply(ctx, DROP, "falsch")[0] is None  # Prüfsumme muss stimmen
    rid, errors = AR.apply(ctx, DROP, p.fp, "Testkonto")
    assert rid and not errors
    ctx.invalidate_data()
    assert DROP not in ctx.portfolio().all_accounts() and KEEP in ctx.portfolio().all_accounts()
    assert ctx.ledger().balances.get((DROP, "EUR")) is None
    # Rohdaten bleiben: der Import enthält die Buchungen weiterhin
    assert {"T0", "T1", "T2"} <= {t.tx_id for t in ctx.base_portfolio().txs}
    # idempotent: erneutes Entfernen findet nichts mehr
    assert AR.apply(ctx, DROP, p.fp)[0] is None
    # später eintreffende Buchung desselben Kontonamens wird nicht ausgeblendet
    from app.journal.service import journal_service

    res = journal_service(ctx).save({"kind": "expert", "time": "10:00", "type": "deposit", "tag": "",
                                     "date": "2025-01-05", "to_account": DROP, "to_asset": "EUR", "to_qty": "5",
                                     "value_eur": "5"})
    assert not res.errors, res.errors
    ctx.invalidate_data()
    assert ctx.ledger().balances[(DROP, "EUR")] == D("5")
    # Rückgängig stellt die ursprünglichen Buchungen wieder her
    assert AR.revert(ctx, rid) == []
    ctx.invalidate_data()
    assert ctx.ledger().balances[(DROP, "EUR")] == D("1505")
    assert AR.history(ctx.db)[0]["status"] == "reverted"


def test_transfers_to_other_accounts_block_removal(cfg):
    rows = [*ROWS, tx("X1", "2024-03-01T10:00:00Z", "transfer", frm=(DROP, "EUR", "100"), to=(KEEP, "EUR", "100"),
                      value=100)]
    ctx = make_ctx(cfg, rows, ASSETS)
    p = AR.preview(ctx, DROP)
    assert not p.ok and p.cross and "Transfers" in p.errors[0]
    assert AR.apply(ctx, DROP, p.fp)[0] is None
    assert ctx.ledger().balances[(KEEP, "EUR")] == D("8100")  # nichts verändert


def test_preview_lists_sources_and_account_without_bookings(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS)
    ctx.db.x("INSERT INTO data_source(kind, provider, name, account, status, created_at, updated_at) VALUES "
             "('exchange','kraken','Kraken API',?, 'manual','2025-01-01T00:00:00Z','2025-01-01T00:00:00Z')", (DROP,))
    p = AR.preview(ctx, DROP)
    assert p.ok and [s[1] for s in p.sources] == ["Kraken API"] and any("Datenquelle" in w for w in p.warnings)
    ctx.db.x("INSERT INTO data_source(kind, provider, name, account, status, created_at, updated_at) VALUES "
             "('exchange','kraken','Leer','Leeres Konto', 'manual','2025-01-01T00:00:00Z','2025-01-01T00:00:00Z')")
    q = AR.preview(ctx, "Leeres Konto")
    assert not q.ok and q.sources and "keine Buchungen" in q.errors[0]  # nur die Datenquelle lässt sich entfernen


def test_export_keeps_history_as_restored(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS)
    rid, _ = AR.apply(ctx, DROP, AR.preview(ctx, DROP).fp)
    files = fullexport.collect(ctx, set())
    st = json.loads(files[fullexport.STATE].decode())
    assert [(r["account"], r["status"], r["tx_ids_json"]) for r in st["account_removals"]] == [(DROP, "restored", "[]")]
    assert rid


def test_web_flow(cfg):
    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=ROWS, assets=ASSETS, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        page = c.get("/accounts/merge")
        assert page.status_code == 200 and 'id="entfernen"' in page.text and "Konto entfernen" in page.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        ds = c.get("/settings/datasources")
        assert "Konto entfernen …" in ds.text
        prev = c.post("/accounts/remove/preview", data={"csrf_token": token, "account": DROP})
        assert prev.status_code == 200 and "Bestände, die im Portfolio fehlen werden" in prev.text
        fp = re.search(r'name="fp" value="([^"]+)"', prev.text).group(1)
        no = c.post("/accounts/remove/apply", data={"csrf_token": token, "account": DROP, "fp": fp},
                    follow_redirects=True)
        assert "Bitte die Vorschau bestätigen" in no.text
        ok = c.post("/accounts/remove/apply", data={"csrf_token": token, "account": DROP, "fp": fp, "confirm": "1"},
                    follow_redirects=True)
        assert "ausgeblendet" in ok.text and DROP not in ctx.portfolio().all_accounts()
        assert "Entfernte Konten" in ok.text
        rid = AR.history(ctx.db)[0]["id"]
        back = c.post(f"/accounts/remove/{rid}/revert", data={"csrf_token": token}, follow_redirects=True)
        assert "wieder eingeblendet" in back.text and DROP in ctx.portfolio().all_accounts()
        bad = c.post("/accounts/remove/preview", data={"csrf_token": token, "account": "nix"})
        assert bad.status_code == 400
