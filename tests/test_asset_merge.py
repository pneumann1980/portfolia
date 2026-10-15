"""Assets zusammenführen – nur synthetische Daten, temporäre Datenbanken.

Abgedeckt: reine Abbildung (keine Mutation, Stammdaten, Referenzen), FIFO findet die Lots der Zwillingskennung,
Kandidaten (nur Vorschläge, Umstellung ausgenommen), Vorschau → Bestätigung mit Prüfsumme → Protokoll → Rückgängig,
Idempotenz, Fehlerfälle, Auflösung von Symbolen/Referenzen, Gesamtexport.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
import re
from dataclasses import replace
from decimal import Decimal

from fastapi.testclient import TestClient

from app.assetchange import merge as M
from app.ledger.engine import run_ledger
from app.main import build_app
from tests.helpers import ASSETS, portfolio, tx
from tests.test_diagnosis import asset, cfg, fingerprint, make_ctx  # noqa: F401

D = Decimal
TWIN = "PEAQ#29184678"
PEAQ = [asset("PEAQ", "peaq", "coingecko", "peaq-2"), asset(TWIN, "peaq (Koinly)", "coingecko", "peaq-2")]


def rows():
    return [tx("B1", "2023-01-10T10:00:00Z", "buy", frm=("Börse", "EUR", "100"), to=("Börse", TWIN, "100"),
               value="100"),
            tx("B2", "2023-03-01T10:00:00Z", "buy", frm=("Börse", "EUR", "20"), to=("Börse", "PEAQ", "10"),
               value="20"),
            tx("S1", "2023-09-01T10:00:00Z", "sell", frm=("Börse", "PEAQ", "50"), to=("Börse", "EUR", "90"),
               value="90")]


def test_map_portfolio_is_pure_and_complete():
    pf = portfolio(rows(), [*ASSETS, *PEAQ])
    pf.manual_prices = {TWIN: [(pf.txs[0].date, 1.0)], "PEAQ": [(pf.txs[1].date, 2.0)]}
    pf.holdings_check = [{"account": "Börse", "asset_id": TWIN, "qty": "100"}]
    out = M.map_portfolio(pf, {TWIN: "PEAQ"})
    assert out is not pf and TWIN not in out.assets and TWIN in pf.assets  # Original unverändert
    assert {t.to_asset for t in out.txs if t.tx_id == "B1"} == {"PEAQ"}
    # Symbol der Zwillingskennung bleibt auflösbar
    assert TWIN in out.assets["PEAQ"].aliases and out.assets["PEAQ"].extra["merged"] == [TWIN]
    assert [d for d, _ in out.manual_prices["PEAQ"]] == sorted(d for d, _ in out.manual_prices["PEAQ"])
    assert out.holdings_check[0]["asset_id"] == "PEAQ"
    assert M.map_portfolio(pf, {}) is pf
    assert M.map_portfolio(pf, {TWIN: "GIBT_ES_NICHT"}) is pf  # Ziel fehlt: nicht anwenden


def test_fifo_finds_the_lots_of_the_twin_id():
    pf = portfolio(rows(), [*ASSETS, *PEAQ])
    before = run_ledger(pf)
    assert any(p.missing_basis for d in before.disposals for p in d.parts)  # 50 verkauft, nur 10 PEAQ gekauft
    after = run_ledger(M.map_portfolio(pf, {TWIN: "PEAQ"}))
    assert not any(p.missing_basis for d in after.disposals for p in d.parts)
    assert after.balances[("Börse", "PEAQ")] == D(60) and ("Börse", TWIN) not in after.balances


def test_candidates_are_suggestions_and_exclude_migrations(cfg):
    ctx = make_ctx(cfg, rows(), [*ASSETS, *PEAQ])
    c = M.candidates(ctx)
    assert [(x.alias, x.target, x.strength) for x in c] == [(TWIN, "PEAQ", "stark")]  # Kursquelle + Basis-Kennung
    assert any("Kursquelle" in r for r in c[0].reasons) and c[0].n_alias == 1 and c[0].n_target == 2
    ctx.db.x("INSERT INTO asset_change(kind, status, origin, old_asset, new_asset, effective_date, created_at) VALUES "
             "('migration','applied','user',?,?,'2023-06-01','2023-06-01T00:00:00Z')", (TWIN, "PEAQ"))
    assert M.candidates(ctx) == []  # gebuchte Umstellung: bewusst getrennte Bestände
    assert M.active_map(ctx.db) == {}  # Vorschlag ändert nichts


def post(c, url, **data):
    return c.post(url, data={"csrf_token": c.token, **data}, follow_redirects=False)


def client_for(cfg):
    c = TestClient(build_app(cfg, start_scheduler=False))
    c.__enter__()
    c.get("/settings")
    c.token = c.cookies.get("portfolia_csrf")
    return c


def test_preview_confirm_apply_revert_flow(cfg):
    make_ctx(cfg, rows(), [*ASSETS, *PEAQ])
    c = client_for(cfg)
    try:
        ctx = c.app.state.ctx
        base = fingerprint(ctx.db)
        assert TWIN in c.get("/assets/merge").text and "Vorschläge (1)" in c.get("/assets/merge").text
        r = post(c, "/assets/merge/preview", alias=TWIN, target="PEAQ")
        assert r.status_code == 200 and "Wirkung auf FIFO und Steuer" in r.text and "ändert sich" in r.text
        fp = re.search(r'name="fp" value="([0-9a-f]+)"', r.text).group(1)
        assert fingerprint(ctx.db) == base  # Vorschau ändert nichts
        # ohne Bestätigung / mit falscher Prüfsumme wird nichts übernommen
        assert "err=" in post(c, "/assets/merge/apply", alias=TWIN, target="PEAQ", fp=fp).headers["location"]
        assert "err=" in post(c, "/assets/merge/apply", alias=TWIN, target="PEAQ", fp="falsch",
                              confirm="1").headers["location"]
        assert M.active_map(ctx.db) == {}
        r = post(c, "/assets/merge/apply", alias=TWIN, target="PEAQ", fp=fp, confirm="1", note="Test")
        assert "msg=" in r.headers["location"] and M.active_map(ctx.db) == {TWIN: "PEAQ"}
        pf, led = ctx.portfolio(), ctx.ledger()
        assert TWIN not in pf.assets and led.balances[("Börse", "PEAQ")] == D(60)
        assert not any(p.missing_basis for d in led.disposals for p in d.parts)
        # Idempotenz: dieselbe Zuordnung entsteht nicht zweimal
        post(c, "/assets/merge/apply", alias=TWIN, target="PEAQ", fp=fp, confirm="1")
        assert ctx.db.scalar("SELECT COUNT(*) FROM asset_merge WHERE status='active'") == 1
        # Rohdaten und Buchungen unverändert (nur die Zuordnung ist neu)
        assert ctx.db.scalar("SELECT COUNT(*) FROM tx WHERE to_asset=? OR from_asset=?", (TWIN, TWIN)) == 1
        assert "aktiv" in c.get("/assets/merge").text
        mid = ctx.db.scalar("SELECT id FROM asset_merge")
        post(c, f"/assets/merge/{mid}/revert")
        assert M.active_map(ctx.db) == {} and TWIN in ctx.portfolio().assets
        assert any(p.missing_basis for d in ctx.ledger().disposals for p in d.parts)
        post(c, f"/assets/merge/{mid}/revert")  # idempotent
        assert ctx.db.scalar("SELECT status FROM asset_merge WHERE id=?", (mid,)) == "reverted"
    finally:
        c.__exit__(None, None, None)


def test_errors_and_warnings(cfg):
    assets = [*ASSETS, *PEAQ, {**asset("PEA2", "Wertpapier A"), "asset_class": "security", "isin": "DE0000000001"},
              {**asset("PEA3", "Wertpapier B"), "asset_class": "security", "isin": "DE0000000002"}]
    ctx = make_ctx(cfg, rows(), assets)
    assert "Zwei verschiedene" in " ".join(M.preview(ctx, "PEAQ", "PEAQ").errors)
    assert "Währungen" in " ".join(M.preview(ctx, "EUR", "USD").errors)
    assert "Klassen" in " ".join(M.preview(ctx, "PEAQ", "WKN:A0B1C2").errors)
    assert "ISIN" in " ".join(M.preview(ctx, "PEA2", "PEA3").errors)
    assert M.preview(ctx, "NIX", "PEAQ").errors and not M.preview(ctx, "PEAQ", "NIX").ok
    assert M.apply(ctx, "PEAQ", "PEAQ", "x")[0] is None
    ok = M.preview(ctx, TWIN, "PEAQ")
    assert ok.ok and ok.fp and [y.year for y in ok.years] == [2023] and ok.years[0].changed
    assert any("Steuerlich betroffene Jahre" in w for w in ok.warnings)
    # Tausch der Kennungen gegeneinander wird gewarnt (würde ein Asset gegen sich selbst tauschen)
    other = replace(cfg, data_dir=cfg.data_dir.parent / "d2", import_dir=cfg.import_dir.parent / "i2")
    other.import_dir.mkdir(parents=True, exist_ok=True)
    swap = [*rows(), tx("X1", "2023-10-01T10:00:00Z", "trade", frm=("Börse", TWIN, "5"), to=("Börse", "PEAQ", "5"),
                        value="9")]
    ctx2 = make_ctx(other, swap, [*ASSETS, *PEAQ])
    assert any("gegen sich selbst" in w for w in M.preview(ctx2, TWIN, "PEAQ").warnings)


def test_symbols_references_and_known_assets_use_the_main_asset(cfg):
    ctx = make_ctx(cfg, rows(), [*ASSETS, *PEAQ])
    ctx.db.x("INSERT INTO csv_symbol(symbol, asset_id, origin, updated_at) VALUES ('PEAQ', ?, NULL, 'x')", (TWIN,))
    ctx.db.x("INSERT INTO reference_balance(account, asset_id, qty, as_of, source, status, created_at) VALUES "
             "('Börse', ?, '60', '2023-12-31', 'statement', 'active', 'x')", (TWIN,))
    from app.csvimport.service import csv_service
    from app.diagnosis.collect import references
    from app.journal.service import journal_service

    assert csv_service(ctx).saved_symbols()["PEAQ"] == TWIN
    p = M.preview(ctx, TWIN, "PEAQ")
    mid, errs = M.apply(ctx, TWIN, "PEAQ", p.fp)
    assert mid and not errs
    assert csv_service(ctx).saved_symbols()["PEAQ"] == "PEAQ"
    assert [r.asset_id for r in references(ctx.db)] == ["PEAQ"]
    assert TWIN not in journal_service(ctx).known_assets() and "PEAQ" in journal_service(ctx).known_assets()
    assert M.revert(ctx, mid) == [] and TWIN in journal_service(ctx).known_assets()


def test_merges_are_part_of_the_full_export(cfg):
    from app import fullexport

    ctx = make_ctx(cfg, rows(), [*ASSETS, *PEAQ])
    mid, _ = M.apply(ctx, TWIN, "PEAQ", M.preview(ctx, TWIN, "PEAQ").fp)
    extras = fullexport.collect(ctx, {t.tx_id for t in ctx.recorded_portfolio().txs})
    st = json.loads(extras[fullexport.STATE].decode("utf-8"))
    assert mid and [m["alias_asset"] for m in st["asset_merges"]] == [TWIN]
