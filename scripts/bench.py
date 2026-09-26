"""Lasttest gegen die Abnahmekriterien (synthetischer Import ≈5.700 Tx / ≈170 Assets, Demo-Kurse).

Aufruf:  python scripts/bench.py [arbeitsverzeichnis]
Misst: Import (Validierung + Speichern + Abgleich), Kurs-/Historienaufbau, Antwortzeiten der Seiten,
Steuerübersicht und PDF-Erzeugung sowie den Speicherbedarf (RSS).
"""

from __future__ import annotations

import os
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.config import Config, Secrets
from app.main import build_app
from tests.synthetic import write_zip


def rss_mb() -> float:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def timed(label: str, fn, results: dict, limit: float | None = None):
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    ok = "" if limit is None else (" OK" if dt <= limit else f" ÜBER GRENZE ({limit}s)")
    print(f"{label:<44} {dt:8.3f}s{ok}")
    results[label] = dt
    return out


def main() -> int:
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="portfolia-bench-"))
    shutil.rmtree(base, ignore_errors=True)
    (base / "import").mkdir(parents=True)
    results: dict[str, float] = {}
    zpath, ntx, nassets = timed("Synthetischen Import erzeugen", lambda: write_zip(base / "import" / "gross.zip"),
                                results)
    old = time.time() - 3600
    os.utime(zpath, (old, old))
    print(f"  {ntx} Transaktionen, {nassets} Assets, {zpath.stat().st_size // 1024} KB")
    cfg = Config(data_dir=base / "data", import_dir=base / "import", demo_mode=True, scheduler_enabled=False,
                 startup_jobs=False, log_format="text", log_level="WARNING", secrets=Secrets())
    app = build_app(cfg, start_scheduler=False)
    with TestClient(app) as c:
        ctx = app.state.ctx
        from app.importer.loader import check_import_dir
        from app.jobs import tasks

        out = timed("Import (Validierung, Speichern, Abgleich)",
                    lambda: check_import_dir(ctx.db, cfg.import_dir, ctx.engine_options("global"), trigger="bench"),
                    results, 30)
        print(f"  Status: {out.status} – {out.message}")
        check = ctx.db.q1("SELECT check_json FROM imports WHERE id=?", (out.import_id,))
        import json

        chk = json.loads(check["check_json"])
        print(f"  holdings_check: {chk.get('ok')} von {chk.get('checked')} übereinstimmend, "
              f"Abweichungen: {len(chk.get('mismatches', []))}")
        ctx.invalidate_data()
        timed("Ledger (global FIFO)", lambda: ctx.ledger(), results)
        timed("Kurse aktualisieren (Demo)", lambda: tasks.refresh_prices(ctx, force=True), results)
        timed("Historie laden + Snapshots (Demo)", lambda: tasks.backfill(ctx), results)
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        for path, limit in (("/", 1.5), ("/", 1.5), ("/positions", 1.5), ("/performance", None), ("/news", None),
                            ("/quality", None)):
            r = timed(f"GET {path}", lambda p=path: c.get(p), results, limit)
            assert r.status_code == 200, (path, r.status_code)
        val = ctx.valuation()
        top = max((p for p in val.positions if not p.asset.is_fiat), key=lambda p: p.value).asset_id
        from urllib.parse import quote

        r = timed(f"GET /panel/asset/{top} (Sunburst-Klick)", lambda: c.get(f"/panel/asset/{quote(top, safe='')}"),
                  results, 0.5)
        assert r.status_code == 200
        timed(f"GET /api/asset/{top}/chart?range=1J",
              lambda: c.get(f"/api/asset/{quote(top, safe='')}/chart?range=1J"), results, 0.5)
        timed("GET /asset/BTC", lambda: c.get("/asset/BTC"), results, 1.0)
        timed("GET /api/allocation", lambda: c.get("/api/allocation"), results, 0.5)
        timed("GET /tax (kalt: Steuer-Ledger + Übersicht)", lambda: c.get("/tax"), results)
        timed("GET /tax (warm)", lambda: c.get("/tax"), results, 1.5)
        timed("GET /tax?year=2024", lambda: c.get("/tax?year=2024"), results, 1.5)
        r = timed("POST /tax/report 2025 (3 PDFs)",
                  lambda: c.post("/tax/report", data={"year": "2025", "doc": ["report", "anlage_so", "anlage_kap"]},
                                 follow_redirects=False), results)
        assert r.status_code == 303, r.text[:300]
        from app.tax.service import tax_service

        rep = tax_service(ctx).reports()[0]
        for d in rep["summary"]["docs"]:
            print(f"  {d['title']}: {d['bytes'] // 1024} KB")
        print(f"RSS nach Lasttest: {rss_mb():.0f} MB")
    slow = [k for k, v in results.items() if k.startswith(("Import", "GET /", "GET /panel")) and v > 30]
    return 1 if slow else 0


if __name__ == "__main__":
    raise SystemExit(main())
