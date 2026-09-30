"""Vollständiger Export und Neueinrichtung: alles, was eine neue Installation zum selben Stand braucht."""

from __future__ import annotations

import io
import json
import os
import shutil
import time
import zipfile
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.importer.validate import validate_zip
from app.jobs import tasks
from app.ledger.engine import run_ledger
from app.main import build_app
from app.prices.models import Bar

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"
MASTER = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
SECRET = "bp_test_key_0123456789abcdefABCDEF"


def _client(config: Config) -> TestClient:
    return TestClient(build_app(config, start_scheduler=False))


def _prepare(c: TestClient) -> None:
    c.get("/settings")
    c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")


def _post(c, url, **data):
    return c.post(url, data=data, follow_redirects=False)


def _place(config: Config, data: bytes, name: str, age: int = 3600) -> None:
    dst = config.import_dir / name
    dst.write_bytes(data)
    os.utime(dst, (time.time() - age, time.time() - age))


def _other(config: Config, tmp_path: Path, name: str) -> Config:
    (tmp_path / name / "import").mkdir(parents=True)
    return replace(config, data_dir=tmp_path / name / "data", import_dir=tmp_path / name / "import")


@pytest.fixture
def source(config, monkeypatch):
    """Instanz A: Import, eigene Buchungen, bearbeitete Import-Buchung, Einstellungen, Zuordnungen, Kurshistorie."""
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    shutil.copy(SAMPLE, config.import_dir / "beispiel.zip")
    os.utime(config.import_dir / "beispiel.zip", (time.time() - 7200, time.time() - 7200))
    with _client(config) as c:
        ctx = c.app.state.ctx
        tasks.import_check(ctx, "test")
        _prepare(c)
        assert _post(c, "/journal/asset", asset_id="ADA", name="Cardano", asset_class="crypto",
                     quote_source="coingecko", quote_id="cardano").status_code == 303
        assert _post(c, "/journal/new", kind="buy", date="2026-09-20", account="Börse X", asset="ADA", qty="300",
                     amount="150").status_code == 303
        first = ctx.base_portfolio().txs[0].tx_id
        assert _post(c, f"/journal/{first}/delete").status_code == 303  # Import-Buchung in der App gelöscht
        assert _post(c, "/settings/save", section="prices", crypto_interval_min="15",
                     crypto_throttled_interval_min="60", stock_interval_min="30", stale_crypto_minutes="90",
                     stale_crypto_market_hours="36", coingecko_monthly_limit="9000", coingecko_throttle_pct="75",
                     auto_map="mittel").status_code == 303
        db = ctx.db
        db.x("INSERT INTO asset_source(asset_id, quote_source, quote_id, status, origin, confidence, reason, "
             "checked_at, updated_at) VALUES ('XYZ', 'coingecko', 'xyz-token', 'rejected', 'user', 'niedrig', 'test', "
             "'2026-09-01', '2026-09-01')")
        db.x("INSERT INTO event_decision(event_key, decision, reason, decided_at) VALUES "
             "('bitpanda:abc', 'ignore', 'Test', '2026-09-01')")
        db.x("INSERT INTO csv_symbol(symbol, asset_id, updated_at) VALUES ('CARDANO', 'ADA', '2026-09-01')")
        db.x("INSERT INTO csv_account(name, account, updated_at) VALUES ('Main', 'Börse X', '2026-09-01')")
        cols = ("INSERT INTO journal_tx(tx_id, source, external_id, status, ts_utc, date_only, type, to_account, "
                "to_asset, to_qty, created_at, updated_at) VALUES ")
        db.x(cols + "('PF-C-000999', 'csv:kraken', 'kraken:L999#1', 'deleted', '2026-01-01T00:00:00Z', 0, 'deposit', "
                    "'Kraken', 'EUR', '5', '2026-01-01', '2026-01-01')")
        db.x(cols + "('PF-C-000998', 'csv:binance', '0123456789abcdef0123#1', 'active', '2026-01-02T00:00:00Z', 0, "
                    "'deposit', 'Binance', 'EUR', '7', '2026-01-02', '2026-01-02')")
        from app.datasources.service import datasource_service

        _sid, err = datasource_service(ctx).create({"kind": "exchange", "provider": "bitpanda", "name": "BP",
                                                   "account": "Bitpanda", "sync_interval_min": "60",
                                                   "api_key": SECRET})
        assert not err
        ctx.store.upsert_daily("cg:old-coin", [Bar(date=date(2020, 1, 1), close=1.5),
                                               Bar(date=date(2020, 1, 2), close=1.6)], "coingecko", "EUR")
        ctx.store.set_meta("cg:old-coin", history_from="2020-01-01", history_to="2020-01-02", history_status="ok")
        config.sources_path.write_text("# eigene Quellen\nsources: []\n", encoding="utf-8")
        (config.tax_rules_dir / "de").mkdir(parents=True, exist_ok=True)
        (config.tax_rules_dir / "de" / "override.yaml").write_text("basiszins: {2027: 0.03}\n", encoding="utf-8")
        ctx.invalidate_overlay()
        body = c.get("/journal/export.zip").content
        balances = {k: v for k, v in run_ledger(ctx.recorded_portfolio(), ctx.engine_options()).balances.items()
                    if v}
        yield {"zip": body, "balances": balances, "deleted": first, "config": config}


def test_export_contains_everything_but_secrets(source):
    body = source["zip"]
    assert SECRET.encode() not in body
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = set(z.namelist())
        assert {"portfolia/state.json", "portfolia/price_daily.csv", "portfolia/series_meta.csv",
                "portfolia/files/sources.yaml", "portfolia/files/tax_rules/de/override.yaml"} <= names
        manifest = json.loads(z.read("manifest.json"))
        assert set(manifest["extra_files"]) == {n for n in names if n.startswith("portfolia/")}
        state = json.loads(z.read("portfolia/state.json"))
        assert state["settings"]["prices.crypto_interval_min"] == 15
        assert state["datasources"][0]["provider"] == "bitpanda" and "api_key" not in state["datasources"][0]
        assert all("ciphertext" not in json.dumps(v) for v in state.values())
        tx = z.read("transactions.csv").decode()
        assert source["deleted"] not in tx  # in der App gelöschte Import-Buchung fehlt
        assets = z.read("assets.csv").decode()
        assert "ADA,Cardano,crypto" in assets and "cardano" in assets  # eigene Assets mit Kursquelle
    _rep, parsed = validate_zip_bytes(body)
    assert parsed is not None and set(parsed.extras) >= {"state.json", "price_daily.csv"}


def validate_zip_bytes(body: bytes):
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.zip"
        p.write_bytes(body)
        return validate_zip(p)


def test_fresh_install_restores_everything(source, tmp_path, monkeypatch):
    cfg = _other(source["config"], tmp_path, "b")
    _place(cfg, source["zip"], "portfolia-export.zip")
    with _client(cfg) as c:
        ctx = c.app.state.ctx
        out = tasks.import_check(ctx, "test")
        assert out.status == "imported"
        s = ctx.settings
        assert s.get("prices.crypto_interval_min") == 15 and s.get("prices.stale_crypto_market_hours") == 36
        assert s.get("prices.coingecko_monthly_limit") == 9000 and s.get("prices.auto_map") == "mittel"
        db = ctx.db
        assert db.scalar("SELECT status FROM asset_source WHERE asset_id='XYZ'") == "rejected"
        assert db.scalar("SELECT decision FROM event_decision WHERE event_key='bitpanda:abc'") == "ignore"
        assert db.scalar("SELECT asset_id FROM csv_symbol WHERE symbol='CARDANO'") == "ADA"
        assert db.scalar("SELECT status FROM journal_tx WHERE tx_id='PF-C-000999'") == "deleted"
        ds = db.q1("SELECT * FROM data_source")
        assert ds["provider"] == "bitpanda" and ds["sync_interval_min"] == 60
        assert db.scalar("SELECT COUNT(*) FROM data_source_secret") == 0  # API-Key neu eingeben
        assert db.scalar("SELECT close FROM price_daily WHERE series='cg:old-coin' AND date='2020-01-02'") == 1.6
        assert db.scalar("SELECT history_from FROM series_meta WHERE series='cg:old-coin'") == "2020-01-01"
        assert cfg.sources_path.read_text(encoding="utf-8").startswith("# eigene Quellen")
        assert (cfg.tax_rules_dir / "de" / "override.yaml").is_file()
        balances = {k: v for k, v in run_ledger(ctx.recorded_portfolio(), ctx.engine_options()).balances.items()
                    if v}
        assert balances == source["balances"]
        assert source["deleted"] not in {t.tx_id for t in ctx.portfolio().txs}
        st = ctx.db.get_state(f"restore.{out.import_id}")
        assert st["state"] == "applied"
        _prepare(c)
        page = c.get("/settings").text
        assert "Portfolia-Export" not in page or "Übernehmen" not in page  # keine offene Rückfrage mehr


def test_existing_install_asks_first(source, tmp_path):
    cfg = _other(source["config"], tmp_path, "c")
    with _client(cfg) as c:
        ctx = c.app.state.ctx
        ctx.settings.set("ui.default_range", "3J")  # keine neue Installation mehr
        _place(cfg, source["zip"], "portfolia-export.zip")
        out = tasks.import_check(ctx, "test")
        assert out.status == "imported"
        assert ctx.settings.get("prices.crypto_interval_min") == 10  # noch nicht übernommen
        _prepare(c)
        page = c.get("/").text
        assert "Portfolia-Export" in page and "/actions/restore/apply" in page
        r = _post(c, "/actions/restore/apply", import_id=str(out.import_id))
        assert r.status_code == 303
        assert ctx.settings.get("prices.crypto_interval_min") == 15
        assert ctx.settings.get("ui.default_range") == "3J"  # nicht im Export → bleibt
        assert "/actions/restore/apply" not in c.get("/").text
        # zweites Übernehmen ist folgenlos (idempotent)
        from app import fullexport

        counts = fullexport.apply(ctx, out.import_id)
        assert counts["prices"] == 0 and counts["datasources"] == 0


def test_dismiss_and_broken_checksum(source, tmp_path):
    cfg = _other(source["config"], tmp_path, "d")
    with _client(cfg) as c:
        ctx = c.app.state.ctx
        ctx.settings.set("ui.default_range", "3J")
        # manipulierte Zusatzdatei → Zusatzdaten ignoriert, Import selbst gültig
        src = zipfile.ZipFile(io.BytesIO(source["zip"]))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as dst:
            for n in src.namelist():
                data = src.read(n)
                dst.writestr(n, data + b"\n# x" if n == "portfolia/state.json" else data)
        _place(cfg, buf.getvalue(), "manipuliert.zip")
        out = tasks.import_check(ctx, "test")
        assert out.status == "imported" and any(m["code"] == "extra_checksum" for m in out.report["messages"])
        from app import fullexport

        assert fullexport.status(ctx) is None
        _place(cfg, source["zip"], "echt.zip", age=60)
        out = tasks.import_check(ctx, "test")
        _prepare(c)
        assert _post(c, "/actions/restore/dismiss", import_id=str(out.import_id)).status_code == 303
        assert fullexport.status(ctx)["state"] == "dismissed"
        assert "/actions/restore/apply" not in c.get("/settings").text


def test_restored_csv_rows_are_known(source, tmp_path):
    """Nach der Neueinrichtung sind App-Buchungen Import-Buchungen – derselbe CSV-Export wird erkannt."""
    from types import SimpleNamespace

    from app.csvimport.service import csv_service

    cfg = _other(source["config"], tmp_path, "e")
    _place(cfg, source["zip"], "portfolia-export.zip")
    with _client(cfg) as c:
        ctx = c.app.state.ctx
        tasks.import_check(ctx, "test")
        t = next(t for t in ctx.portfolio().txs if t.tx_id == "PF-C-000998")
        assert t.origin == "import" and t.source == "portfolia:csv:binance"

        def row(ext_id: str):
            rec = SimpleNamespace(ext_id=ext_id, event_key=None, aliases=[], txhash=None)
            return SimpleNamespace(idx=0, rec=rec, status="new", dup_of=[], warnings=[], row=None,
                                   dup_same_account=False)

        same = row("0123456789abcdef0123#1")  # Prüfsummen-Kennung derselben Quelle
        csv_service(ctx)._same_events([same], "csv:binance")
        assert same.status == "known" and "PF-C-000998" in same.warnings[0]
        other = row("0123456789abcdef0123#1")
        csv_service(ctx)._same_events([other], "csv:kraken")  # andere Quelle: keine Gleichsetzung per Prüfsumme
        assert other.status == "new"
