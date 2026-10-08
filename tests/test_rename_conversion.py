"""Ticker-Umbenennung aus zwei Quellen (synthetische Nachbildung eines realen Musters, keine echten Daten).

Muster: Eine Börse benennt einen Token um (alter Ticker ``OLDT`` → neuer Ticker ``NEWT``, 1 : 1). Das Steuertool hat
den Altbestand bereits unter dem neuen Symbol mit eigener Kennung geführt (``NEWT#77``) und bucht die Umstellung als
**Tausch** ``NEWT#77`` → ``NEWT``. Die Börsen-API liefert denselben Vorgang als Kapitalmaßnahme ``OLDT`` → ``NEWT``;
``OLDT`` hat in Portfolia keinen Bestand. Folgen ohne Korrektur: ``NEWT`` doppelt, ``OLDT`` negativ, Scheinverlust
und neue Haltedauer aus dem Tausch.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis.engine import report_for
from app.diagnosis.recommend import recommend
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import asset, by_kind, make_ctx

D = Decimal
Q = "2345.67891234"
ACC = "Bitpanda"
CG = "newt-network"


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def rename_assets(old_quote: tuple[str, str] = ("none", "")) -> list[dict]:
    return [*ASSETS, asset("NEWT", "NEWT", "coingecko", CG),
            asset("NEWT#77", "NEWT (Token-ID 77)", "coingecko", CG),
            asset("OLDT", "OLDT", *old_quote)]


def import_rows() -> list[dict]:
    buy = tx("B1", "2024-02-19T22:11:02Z", "buy", frm=(ACC, "EUR", "150"), to=(ACC, "NEWT#77", Q), value="150")
    trade = tx("T1", "2026-04-30T09:27:58Z", "trade", frm=(ACC, "NEWT#77", Q), to=(ACC, "NEWT", Q), value="11.40")
    for r in (buy, trade):
        r["source"] = "koinly"
    return [buy, trade]


def sync_migration(ctx, tx_id: str = "PF-S-000045", ts: str = "2026-04-30T08:00:00Z", frm: str = "OLDT",
                   qty: str = Q, uuid: str = "5e000000-0000-4000-8000-000000000001") -> None:
    stamp = "2026-10-02T06:43:45Z"
    ctx.db.x("INSERT INTO journal_tx(tx_id, source, external_id, status, ts_utc, type, tag, from_account, from_asset, "
             "from_qty, to_account, to_asset, to_qty, created_at, updated_at, event_key) VALUES "
             "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (tx_id, "sync:bitpanda", f"bitpanda:{uuid}#0", "active", ts, "corporate_action", "migration", ACC, frm,
              qty, ACC, "NEWT", qty, stamp, stamp, f"bitpanda:{uuid}"))
    ctx.invalidate_overlay()


def rename_ctx(cfg, **kw):
    ctx = make_ctx(cfg, import_rows(), rename_assets(**kw), valuation="2026-06-30")
    sync_migration(ctx)
    return ctx


def twin(rep):
    (f,) = [x for x in by_kind(rep, "duplicate") if x.data.get("type") == "conversion_twin"]
    return f


def rename(rep):
    (f,) = [x for x in by_kind(rep, "migration") if x.data.get("type") == "rename_trade"]
    return f


def test_same_rename_from_tax_tool_and_exchange_api_is_detected_as_duplicate(cfg):
    ctx = rename_ctx(cfg)
    led = ctx.ledger()
    assert led.balances[(ACC, "NEWT")] == 2 * D(Q)  # Ausgangslage: doppelt
    assert led.balances[(ACC, "OLDT")] == -D(Q)  # und negativ
    rep = report_for(ctx)
    f = twin(rep)
    assert f.status == "wahrscheinlich" and f.data["weak"] == "PF-S-000045" and f.data["strong"] == "T1"
    assert f.data["short"] and "OLDT" in f.title and "NEWT" in f.title
    assert any("negativ" in e for e in f.evidence)
    # der Befund „Bestand zeitweise negativ“ verweist auf die eigentliche Ursache statt aufs Ausblenden
    neg = next(x for x in by_kind(rep, "history") if x.key == f"issue|negative_balance|OLDT|{ACC}")
    assert "Umtausch doppelt gebucht" in neg.suspected[0]
    rec = recommend(rep, f)
    assert rec.primary.key == "cover_twin" and not rec.conditional
    plan = A.build_plan(ctx, rep, f, "cover_twin")
    assert [(op.kind, op.target, op.link) for op in plan.ops] == [("cover", "PF-S-000045", "T1")]
    did = A.apply(ctx, f.id, "cover_twin", plan.params, plan.token).decision_id
    led = ctx.ledger()
    assert led.balances[(ACC, "NEWT")] == D(Q) and not led.balances.get((ACC, "OLDT"))
    assert not [i for i in led.issues if i.code == "negative_balance"]
    assert ctx.db.q1("SELECT status FROM journal_tx WHERE tx_id='PF-S-000045'")["status"] == "active"
    assert A.undo(ctx, did).ok
    assert ctx.ledger().balances[(ACC, "NEWT")] == 2 * D(Q)


def test_rename_booked_as_trade_can_be_rebooked_as_migration(cfg):
    ctx = rename_ctx(cfg)
    rep = report_for(ctx)
    f = rename(rep)
    # gleiche Kurszuordnung und gleiches Symbol, vollständiger Übergang, Altbestand danach nicht mehr verwendet
    assert f.status == "wahrscheinlich" and f.data["old"] == "NEWT#77" and f.data["new"] == "NEWT"
    assert D(f.data["gain"]) == D("-138.60")  # Scheinverlust: Erlös 11,40 € − Einstand 150 €
    rec = recommend(rep, f)
    assert rec.primary.key == "trade_to_migration" and rec.conditional and "Steuer" in rec.primary.caution
    led = ctx.ledger()
    assert [d.tx_id for d in led.disposals if d.asset == "NEWT#77"] == ["T1"]
    plan = A.build_plan(ctx, rep, f, "trade_to_migration")
    assert [op.kind for op in plan.ops] == ["create", "hide"] and plan.ops[0].tx.tag == "migration"
    eff = A.preview(ctx, rep, plan)
    assert eff is not None
    did = A.apply(ctx, f.id, "trade_to_migration", plan.params, plan.token).decision_id
    # zuerst die doppelte API-Buchung auflösen, dann Lots prüfen
    rep = report_for(ctx)
    t = twin(rep)
    assert t.data["weak"] == "PF-S-000045"  # die neue Migration aus der Diagnose bleibt maßgeblich
    plan2 = A.build_plan(ctx, rep, t, recommend(rep, t).primary.key)
    assert not plan2.errors
    A.apply(ctx, t.id, plan2.option.key, plan2.params, plan2.token)
    led = ctx.ledger()
    assert not [d for d in led.disposals if d.asset == "NEWT#77"]  # kein realisierter Scheinverlust
    (lot,) = [x for x in led.lots if x.asset == "NEWT" and x.qty > 0]
    assert lot.qty == D(Q) and lot.cost == D("150") and lot.acq_date == date(2024, 2, 19)
    assert not led.balances.get((ACC, "OLDT")) and not led.balances.get((ACC, "NEWT#77"))
    assert A.undo(ctx, did).ok


def test_ordinary_one_to_one_trade_between_different_tokens_is_not_a_rename(cfg):
    rows = [tx("D1", "2025-01-02T10:00:00Z", "deposit", to=(ACC, "USDT", "1000"), value="920"),
            tx("S1", "2025-03-01T10:00:00Z", "trade", frm=(ACC, "USDT", "1000"), to=(ACC, "USDC", "1000"),
               value="930")]
    assets = [*ASSETS, asset("USDT", "Tether", "coingecko", "tether"), asset("USDC", "USD Coin", "coingecko",
                                                                               "usd-coin")]
    ctx = make_ctx(cfg, rows, assets)
    rep = report_for(ctx)
    assert not [x for x in rep.findings if x.data.get("type") in ("rename_trade", "conversion_twin")]


def test_partial_conversion_of_same_symbol_is_not_a_rename(cfg):
    rows = [tx("B1", "2025-01-02T10:00:00Z", "buy", frm=(ACC, "EUR", "150"), to=(ACC, "NEWT#77", "5000.123456")),
            tx("T1", "2025-03-01T10:00:00Z", "trade", frm=(ACC, "NEWT#77", Q), to=(ACC, "NEWT", Q), value="20")]
    rows[0]["value_eur"] = "150"
    ctx = make_ctx(cfg, rows, rename_assets())
    assert not [x for x in report_for(ctx).findings if x.data.get("type") == "rename_trade"]


def test_two_conversions_with_own_ids_of_the_same_source_are_legitimate(cfg):
    ctx = make_ctx(cfg, [tx("B1", "2024-02-19T22:11:02Z", "buy", frm=(ACC, "EUR", "300"),
                            to=(ACC, "OLDT", str(2 * D(Q))), value="300")], rename_assets(), valuation="2026-06-30")
    sync_migration(ctx, "PF-S-000001", "2026-04-30T08:00:00Z", uuid="5e000000-0000-4000-8000-0000000000a1")
    sync_migration(ctx, "PF-S-000002", "2026-04-30T09:00:00Z", uuid="5e000000-0000-4000-8000-0000000000a2")
    assert not [x for x in report_for(ctx).findings if x.data.get("type") == "conversion_twin"]


def test_twin_is_independent_of_insertion_order(cfg):
    a = rename_ctx(cfg)
    fa = twin(report_for(a))
    assert (fa.data["weak"], fa.data["strong"]) == ("PF-S-000045", "T1")
    # API-Buchung nach dem Tausch: die Buchung ohne Bestand des Ausgangs-Assets bleibt die zusätzliche
    b = make_ctx(cfg, import_rows(), rename_assets(("coingecko", CG)), valuation="2026-06-30")
    b.db.x("DELETE FROM journal_tx")
    sync_migration(b, ts="2026-04-30T11:00:00Z")
    fb = twin(report_for(b))
    assert (fb.data["weak"], fb.data["strong"]) == ("PF-S-000045", "T1")
    assert any("Kurszuordnung" in e for e in fb.evidence)


def test_findings_and_previews_render(cfg):
    from fastapi.testclient import TestClient

    from app.main import build_app

    ctx = rename_ctx(cfg)
    rep = report_for(ctx)
    ids = {"cover_twin": twin(rep).id, "trade_to_migration": rename(rep).id}
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        html = c.get("/quality/diagnose").text
        assert "Umtausch doppelt gebucht" in html and "Umbenennung als Tausch gebucht" in html
        for opt, fid in ids.items():
            r = c.get(f"/quality/diagnose/plan?f={fid}&o={opt}")
            assert r.status_code == 200 and "Übernehmen" in r.text, opt


def test_review_batch_never_books_the_same_rename_again(config, monkeypatch):
    """Schutzregel im Prüf-Stapel: dieselbe Umstellung aus einer weiteren Quelle (hier eine Steuertool-CSV mit dem
    alten Ticker) wird nicht als neu übernommen, sondern zur Prüfung vorgelegt."""
    import os
    import time

    from app.csvimport.service import csv_service
    from app.importer.zipbuilder import build_zip
    from app.jobs import tasks
    from tests import test_bitpanda as TB
    from tests.test_importcheck import KOINLY_HEAD, _upload_csv

    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", TB.MASTER)
    dst = config.import_dir / "imp.zip"
    build_zip(dst, transactions=import_rows(), assets=rename_assets(), generated_at="2026-06-30T00:00:00Z",
              valuation_date="2026-06-30")
    os.utime(dst, (time.time() - 3600,) * 2)
    with TB.make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        assert tasks.import_check(TB.ctx(c), "test").status == "imported"
        csv = KOINLY_HEAD + f"2026-04-30 07:10:00 UTC,exchange,,{ACC},{Q},OLDT,,{ACC},{Q},NEWT,,,,0,11.40,0,,,,\n"
        bid = _upload_csv(c, "steuertool.csv", csv, ACC)
        (row,) = csv_service(TB.ctx(c)).rows(bid)
        assert row.status == "duplicate" and row.dup_of == ["T1"] and not row.include()
        assert row.match["basis"] == "conversion_twin" and row.match["cat"] == "komplex"
        assert "Ticker-Umbenennung" in row.warnings[0]
