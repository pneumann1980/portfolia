"""Datierte ZIP-Sicherungen im Import-Format, Import-Archiv, Einstellungen, Rundreise Export → Import."""

import io
import os
import shutil
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.importer.validate import validate_zip
from app.jobs import exports, tasks
from app.jobs.scheduler import Scheduler
from app.ledger.engine import run_ledger
from app.main import build_app

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"


def _client(config):
    return TestClient(build_app(config, start_scheduler=False))


@pytest.fixture
def sample_client(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with _client(config) as c:
        tasks.import_check(c.app.state.ctx, "test")
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def post(c, url, **data):
    return c.post(url, data={"csrf_token": c.token, **data}, follow_redirects=False)


def test_export_only_when_content_changes(sample_client):
    ctx = sample_client.app.state.ctx
    first = exports.export_now(ctx, "auto")
    assert exports.EXPORT_RE.match(first["file"])
    assert exports.export_now(ctx, "auto") == {"skipped": "unverändert"}
    # Änderung an Buchungen → neuer Stand (Zeitstempel im Namen; gleiche Sekunde wird überschrieben)
    r = post(sample_client, "/journal/new", kind="income", tag="staking", date="2026-09-22", account="Börse X",
             asset="ETH", qty="0,003", value_eur="9")
    assert r.status_code == 303
    time.sleep(1.05)
    second = exports.export_now(ctx, "auto")
    assert second["file"] != first["file"]
    names = [e["name"] for e in exports.list_exports(ctx)]
    assert names == sorted(names, reverse=True) and len(names) == 2
    rep, parsed = validate_zip(ctx.config.export_dir / second["file"])
    assert rep.ok and not rep.warnings
    assert any(t["tx_id"].startswith("PF-M-") for t in parsed.transactions)


def test_prune_and_settings(sample_client):
    ctx = sample_client.app.state.ctx
    d = ctx.config.export_dir
    for i in range(5):
        (d / f"portfolia-export-2026-01-0{i + 1}_120000.zip").write_bytes(b"x")
    (d / "fremde-datei.zip").write_bytes(b"x")
    r = post(sample_client, "/settings/save", section="export", auto="1", keep="2", import_keep="3")
    assert r.status_code == 303
    assert ctx.settings.get("export.keep") == 2 and ctx.settings.get("export.auto") is True
    names = sorted(p.name for p in d.iterdir() if p.is_file())
    assert names == ["fremde-datei.zip", "portfolia-export-2026-01-04_120000.zip",
                     "portfolia-export-2026-01-05_120000.zip"]
    page = sample_client.get("/settings").text
    assert "ZIP-Sicherungen" in page and "portfolia-export-2026-01-05_120000.zip" in page


def test_import_archived_once_with_date(sample_client):
    ctx = sample_client.app.state.ctx
    arch = exports.list_archive(ctx)
    assert len(arch) == 1 and arch[0]["name"].startswith("import-") and arch[0]["name"].endswith("-beispiel.zip")
    assert exports.archive_import(ctx, ctx.config.import_dir / "beispiel.zip")["skipped"] == "bereits archiviert"
    data = (ctx.config.export_dir / exports.ARCHIVE_DIR / arch[0]["name"]).read_bytes()
    assert data == SAMPLE.read_bytes()


def test_download_only_known_names(sample_client):
    ctx = sample_client.app.state.ctx
    name = exports.export_now(ctx, "manual")["file"]
    r = sample_client.get(f"/exports/{name}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    arch = exports.list_archive(ctx)[0]["name"]
    assert sample_client.get(f"/exports/{arch}").status_code == 200
    for bad in ("app.sqlite", "..%2Fapp.sqlite", "portfolia-export-2026-01-01_120000.zip.tmp",
                "portfolia-export-x.zip"):
        assert sample_client.get(f"/exports/{bad}").status_code == 404


def test_manual_export_action(sample_client):
    r = sample_client.post("/actions/export", headers={"X-CSRF-Token": sample_client.token})
    assert r.status_code == 200 and "Gesichert" in r.text


def test_export_carries_tax_settings_and_all_accounts(sample_client):
    ctx = sample_client.app.state.ctx
    ctx.settings.set("tax.asset_types", {"WKN:865985": "share", "WKN:A0RPWH": "etf_mixed"})
    ctx.settings.set("tax.account_withholding", {"Börse X": "foreign"})
    z = zipfile.ZipFile(io.BytesIO(sample_client.get("/journal/export.zip").content))
    assets = z.read("assets.csv").decode()
    assert "tax_type" in assets.splitlines()[0]
    assert any(line.startswith("WKN:A0RPWH,") and line.rstrip().endswith("etf_mixed") for line in assets.splitlines())
    accounts = z.read("accounts.csv").decode().splitlines()
    assert "tax_withholding" in accounts[0] and any(a.startswith("Börse X,") for a in accounts)


def test_export_roundtrip_as_curated_import(sample_client, tmp_path):
    """Gesamtexport als neuer kuratierter Import: Journal-Buchungen werden über die tx_id erkannt (keine Doppelung)."""
    c = sample_client
    ctx = c.app.state.ctx
    r = post(c, "/journal/new", kind="income", tag="staking", date="2026-09-22", account="Börse X", asset="ETH",
             qty="0,003", value_eur="9")
    assert r.status_code == 303
    # Vergleich ohne geschätzte Sparplan-Ausführungen (die exportiert werden nicht – sie sind unbestätigt)
    before = {k: v for k, v in run_ledger(ctx.recorded_portfolio(), ctx.engine_options()).balances.items() if v}
    name = exports.export_now(ctx, "manual")["file"]
    dst = ctx.config.import_dir / "neu.zip"
    shutil.copy(ctx.config.export_dir / name, dst)
    old = time.time() - 60
    os.utime(dst, (old, old))
    out = tasks.import_check(ctx, "test")
    assert out.status == "imported", out.message
    after = {k: v for k, v in run_ledger(ctx.recorded_portfolio(), ctx.engine_options()).balances.items() if v}
    assert after == before
    assert len(exports.list_archive(ctx)) == 2  # auch der neue Import wurde archiviert


def test_change_listener_debounces(sample_client):
    ctx = sample_client.app.state.ctx
    sched = Scheduler(ctx)
    sched.setup_default_jobs()
    ctx.scheduler = sched
    try:
        sched.sched.start(paused=True)
        ctx.invalidate_overlay()
        ctx.invalidate_data()
        ids = [j.id for j in sched.sched.get_jobs()]
        assert ids.count("auto_export-debounce") == 1
        assert "auto_export-debounce" not in sched.next_runs()
        ctx.settings.set("export.auto", False)
        sched.sched.remove_job("auto_export-debounce")
        ctx.invalidate_overlay()
        assert "auto_export-debounce" not in [j.id for j in sched.sched.get_jobs()]
    finally:
        sched.shutdown()
        ctx.scheduler = None
