"""Einheitlicher Fortschritt (app.progress) für Synchronisierungen und Importe.

Abgedeckt: feste Phasen, monotoner Prozentwert (kein Rücksprung, kein vorzeitiges 100 %, kein Stillstand bei 0 %),
unbekannte Gesamtzahl (asymptotisch), Anzeige älterer Formate, Datenquellen-Sync mit Phasenfolge, ZIP-Import und die
globale Anzeige laufender Vorgänge.
"""

from __future__ import annotations

import json

from app.progress import PHASES, Progress, active, view


def collect() -> tuple[list[dict], Progress]:
    out: list[dict] = []
    clock = iter(range(0, 10_000, 5))  # jede Meldung „später“ – keine Drosselung im Test
    p = Progress(out.append, "Synchronisierung", source="Bitpanda", clock=lambda: next(clock))
    return out, p


def test_phases_monotonic_and_counts():
    out, p = collect()
    assert out[-1]["pct"] == 0 and out[-1]["running"]
    p.phase("prepare")
    p.phase("fetch", total=2013)
    p.update(1284)
    pay = out[-1]
    assert pay["phase_label"] == "Daten abrufen" and pay["done"] == 1284 and pay["total"] == 2013
    assert 5 < pay["pct"] < 50  # Vorbereitung (5) + Anteil am Abruf (45)
    v = view(pay)
    assert v["count"] == "1.284 / 2.013 Datensätze" and v["source"] == "Bitpanda"
    before = pay["pct"]
    p.phase("prepare")  # Rückschritt wird ignoriert
    p.update(10)
    assert out[-1]["pct"] >= before and out[-1]["phase"] == "fetch"
    p.phase("save")
    assert out[-1]["pct"] >= 90 and out[-1]["pct"] < 100
    steps = {s["key"]: s["state"] for s in out[-1]["steps"]}
    assert steps["fetch"] == "done" and steps["save"] == "active"
    p.finish(True, "fertig")
    assert out[-1]["pct"] == 100 and not out[-1]["running"] and out[-1]["phase_label"] == "Fertig"


def test_unknown_total_never_reaches_100_and_moves():
    out, p = collect()
    p.phase("fetch")
    vals = []
    for n in (0, 10, 500, 50_000):
        p.update(n)
        vals.append(out[-1]["pct"])
    assert vals == sorted(vals) and vals[1] > vals[0] and vals[-1] < 50  # Phase „Daten abrufen“ endet bei 50 %


def test_skipped_phases_are_reweighted():
    out: list[dict] = []
    tick = iter(range(0, 1000, 5))
    p = Progress(out.append, "Kurse", phases=["prices", "save"], clock=lambda: next(tick))
    p.phase("prices", total=4)
    p.update(2)
    assert round(out[-1]["pct"]) == 25  # Gewichte 10 : 10 → je Phase 50 %, davon die Hälfte
    assert [s["key"] for s in out[-1]["steps"]] == ["prices", "save"]
    assert {k for k, _l, _w in PHASES} >= {"prepare", "fetch", "process", "reconcile", "prices", "save"}


def test_view_understands_old_payloads():
    v = view({"running": True, "stage": "KAS", "done": 3, "total": 4, "text": "Transaktionen"})
    assert v["pct_int"] == 75 and v["phase_label"] == "KAS" and v["count"] == "3 / 4"
    assert view({"running": False, "ok": True, "done": 1, "total": 9})["pct"] == 100.0


def test_active_lists_running_jobs(db):
    class Ctx:
        pass

    ctx = Ctx()
    ctx.db = db
    from app.progress import job_progress

    p = job_progress(ctx, "csv_upload", "CSV-Import", unit="Zeilen", phases=["prepare", "process"])
    p.phase("process", total=200)
    items = active(ctx)
    assert len(items) == 1 and items[0]["label"] == "CSV-Import" and items[0]["phase_label"] == "Verarbeiten"
    p.finish(True)
    assert active(ctx) == []
    row = db.q1("SELECT running, progress_json FROM job_status WHERE job='csv_upload'")
    assert row["running"] == 0 and json.loads(row["progress_json"])["pct"] == 100


def test_datasource_sync_reports_phases(tmp_path, monkeypatch):
    """Ein Wallet-Sync durchläuft Vorbereitung → Abruf → Verarbeiten → Abgleichen → Speichern; jeder gespeicherte
    Zwischenstand ist monoton steigend."""
    import httpx

    from app.config import Config, Secrets
    from app.datasources import chainhttp as CH
    from app.datasources.service import DataSourceService, datasource_service
    from app.datasources.wallet import WalletConnector
    from tests.test_wallets_kaspa import KAS, A, history, ops
    from tests.wallet_fakes import MASTER, FakeKaspa, create_wallet, ctx, make_client

    fake = FakeKaspa(history(), {A: 1000 * KAS}, ops(), [], {})
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    seen: list[dict] = []
    orig = DataSourceService._set_progress

    def spy(self, sid, data, force=False):
        seen.append(dict(data))
        return orig(self, sid, data, force)

    monkeypatch.setattr(DataSourceService, "_set_progress", spy)
    imp = tmp_path / "import"
    imp.mkdir()
    cfg = Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                 startup_jobs=False, log_format="text", secrets=Secrets())
    with make_client(cfg) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        sid = create_wallet(c, "kaspa", A, name="Kaspium KAS")
        datasource_service(ctx(c)).sync(sid, "manual")
        phases = [p.get("phase") for p in seen if p.get("v") == 2]
        order = [x for i, x in enumerate(phases) if x and (i == 0 or phases[i - 1] != x)]
        assert order == ["prepare", "fetch", "process", "reconcile", "save"], order
        pcts = [p["pct"] for p in seen if p.get("v") == 2]
        assert pcts == sorted(pcts)
        final = json.loads(ctx(c).db.scalar("SELECT progress_json FROM data_source WHERE id=?", (sid,)))
        assert final["running"] is False and view(final)["pct"] == 100
        page = c.get("/progress/active").text
        assert 'id="progress-active"' in page and "every 20s" in page  # nichts läuft


def test_zip_import_reports_progress(tmp_path):
    from app.config import Config, Secrets
    from app.context import AppContext
    from app.importer.zipbuilder import build_zip
    from app.jobs import tasks
    from tests.helpers import ASSETS, tx

    imp = tmp_path / "import"
    imp.mkdir()
    cfg = Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                 startup_jobs=False, log_format="text", secrets=Secrets())
    build_zip(imp / "k.zip", transactions=[tx("d", "2025-01-02", "deposit", to=("Bank", "EUR", 100), value=100)],
              assets=ASSETS, valuation_date="2025-06-30", generated_at="2025-06-30T20:00:00Z")
    import os
    import time

    old = time.time() - 3600  # Datei gilt als fertig kopiert
    os.utime(imp / "k.zip", (old, old))
    ctx = AppContext(cfg)
    ctx.startup()
    assert tasks.import_check(ctx, "manual").status == "imported"
    p = json.loads(ctx.db.scalar("SELECT progress_json FROM job_status WHERE job='import'"))
    assert p["pct"] == 100 and p["ok"] and [s["key"] for s in p["steps"]] == ["process", "reconcile", "save"]
    tasks.import_check(ctx, "poll")  # unveränderte Datei: kein neuer Fortschritt
    assert json.loads(ctx.db.scalar("SELECT progress_json FROM job_status WHERE job='import'"))["started_at"] == \
        p["started_at"]
