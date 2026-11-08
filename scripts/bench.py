"""Lasttest gegen die Abnahmekriterien (synthetischer Import ≈5.700 Tx / ≈170 Assets, Demo-Kurse).

Aufruf:  python scripts/bench.py [arbeitsverzeichnis]
Misst: Import (Validierung + Speichern + Abgleich), Kurs-/Historienaufbau, Antwortzeiten der Seiten,
Steuerübersicht und PDF-Erzeugung sowie den Speicherbedarf (RSS).

Mit ``--dq-perf AUSGABE.json`` (statt des vollständigen Lasttests) nach Import, Kursen und Historie nur die Seiten
„Prüfen“, Assistent und Performance: Kalt (Zwischenspeicher der App geleert) und warm (Median, 95. Perzentil aus
``--runs`` Aufrufen) sowie je Aufruf die Zahl vollständiger Diagnosen, ``P.metrics``- und IRR-Berechnungen.

Sicherheit: Das Arbeitsverzeichnis wird gelöscht und neu angelegt – nur ein neues, leeres Verzeichnis oder eines,
das dieses Skript selbst angelegt hat (Markierung ``.portfolia-bench``), ist erlaubt.
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


MARKER = ".portfolia-bench"


def _workdir(arg: str | None) -> Path:
    base = Path(arg) if arg else Path(tempfile.mkdtemp(prefix="portfolia-bench-"))
    if base.exists() and any(base.iterdir()) and not (base / MARKER).exists():
        raise SystemExit(f"{base} ist nicht leer und kein Benchmark-Verzeichnis – wird nicht gelöscht.")
    shutil.rmtree(base, ignore_errors=True)
    (base / "import").mkdir(parents=True)
    (base / MARKER).write_text("portfolia bench\n")
    return base


class _Counter:
    """Zählt Aufrufe einer Modulfunktion (ersetzt sie für die Messung, ruft das Original auf)."""

    def __init__(self, module: object, name: str) -> None:
        self.module, self.name, self.n = module, name, 0
        self.orig = getattr(module, name)

        def wrapped(*a, **k):
            self.n += 1
            return self.orig(*a, **k)

        setattr(module, name, wrapped)

    def restore(self) -> None:
        setattr(self.module, self.name, self.orig)


def dq_perf(c, ctx, out_path: Path, runs: int) -> dict:
    """Kalt-/Warmzeiten und Zählungen für die beiden Optimierungsthemen (Diagnose je Anfrage, Kennzahlen)."""
    import json
    import statistics

    from app.analytics import periods as P
    from app.diagnosis import engine as E

    counters = {"diagnosen": _Counter(E, "diagnose"), "metrics": _Counter(P, "metrics"),
                "irr": _Counter(P, "irr_period_detail")}
    endpoints = ["/quality/plan", "/quality/assistent?new=1", "/performance", "/performance?period=1J",
                 "/performance?period=MAX", "/performance?start=2023-01-01&end=2024-06-30"]
    res: dict = {}
    try:
        for ep in endpoints:
            def call(ep=ep):
                for k in counters.values():
                    k.n = 0
                t0 = time.perf_counter()
                r = c.get(ep)
                dt = time.perf_counter() - t0
                assert r.status_code == 200, (ep, r.status_code)
                return dt, {n: k.n for n, k in counters.items()}

            ctx.invalidate_data()  # kalt: Portfolio-, Ledger-, Bewertungs- und Historien-Zwischenspeicher leer
            cold, cold_n = call()
            call()  # Aufwärmen
            warm = [call() for _ in range(runs)]
            ts = sorted(w[0] for w in warm)
            res[ep] = {"cold_s": round(cold, 4), "warm_median_s": round(statistics.median(ts), 4),
                       "warm_p95_s": round(ts[min(len(ts) - 1, round(0.95 * (len(ts) - 1)))], 4),
                       "warm_min_s": round(ts[0], 4), "runs": runs, "calls_cold": cold_n, "calls_warm": warm[-1][1]}
            print(f"{ep:<52} kalt {cold:7.3f}s  warm Median {res[ep]['warm_median_s']:7.3f}s  "
                  f"p95 {res[ep]['warm_p95_s']:7.3f}s  {warm[-1][1]}")
    finally:
        for k in counters.values():
            k.restore()
    out_path.write_text(json.dumps(res, indent=1))
    return res


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dq_out = None
    runs = 7
    for i, a in enumerate(sys.argv):
        if a == "--dq-perf":
            dq_out = Path(sys.argv[i + 1])
            args = [x for x in args if x != sys.argv[i + 1]]
        if a == "--runs":
            runs = int(sys.argv[i + 1])
            args = [x for x in args if x != sys.argv[i + 1]]
    base = _workdir(args[0] if args else None)
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
        if dq_out is not None:
            dq_perf(c, ctx, dq_out, runs)
            return 0
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
