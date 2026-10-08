"""Lasttest der finanziellen Integritätsprüfung und der Sammelbearbeitung (synthetisch, temporäres Verzeichnis).

    python scripts/bench_integrity.py [scale]

Erzeugt ≥ 10.000 Buchungen und > 200 Assets (``tests.synthetic`` mit ``scale`` = 3), mehrere Konten, Transfers,
Splits, teils fehlende Kurse, dazu eindeutige (gleiche Buchung doppelt) und mehrdeutige (gleiche Menge, anderer
Zeitpunkt) Dubletten. Misst Import, Ledger, Integritätsprüfung (Diagnose, Lösungsvorschläge, Invarianten, Steuer,
Kurse), Sammelvorschau, Speicher und die Übersicht danach. Ändert keine echten Daten.
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
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def add_duplicates(rows: list[dict]) -> None:
    """40 eindeutige Dubletten (gleiche Buchung, neue Kennung, 1 s später) und 20 mehrdeutige (gleiche Menge auf
    einem anderen Konto als Zugang ohne Gegenbuchung, Stunden später)."""
    buys = [r for r in rows if r["type"] == "buy" and r["to_account"].startswith("Börse")][::37][:40]
    n = len(rows)
    for i, r in enumerate(buys):
        dup = dict(r)
        dup["tx_id"] = f"DUP-{i:04d}"
        rows.append(dup)
    transfers = [r for r in rows if r["type"] == "transfer"][::5][:20]
    for i, r in enumerate(transfers):
        rows.append({**dict.fromkeys(r, ""), "tx_id": f"AMB-{i:04d}", "datetime": r["datetime"][:11] + "23:59:00Z",
                     "type": "deposit", "to_account": r["to_account"], "to_asset": r["to_asset"],
                     "to_qty": r["to_qty"], "value_eur": "0"})
    rows.sort(key=lambda x: (x["datetime"], x["tx_id"]))
    assert len(rows) == n + len(buys) + len(transfers)


def timed(label: str, fn, results: dict):
    t = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t
    results[label] = dt
    print(f"{label:<52}{dt:8.3f}s")
    return out


def main() -> int:
    scale = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    base = Path(tempfile.mkdtemp(prefix="portfolia-bench-integrity-"))
    results: dict[str, float] = {}
    try:
        (base / "import").mkdir(parents=True)
        zpath, ntx, nassets = timed("Synthetischen Import erzeugen", lambda: write_zip(
            base / "import" / "gross.zip", scale=scale, mutate=add_duplicates), results)
        old = time.time() - 3600
        os.utime(zpath, (old, old))
        print(f"  {ntx} Buchungen, {nassets} Assets")
        cfg = Config(data_dir=base / "data", import_dir=base / "import", demo_mode=True, scheduler_enabled=False,
                     startup_jobs=False, log_format="text", log_level="WARNING", secrets=Secrets())
        app = build_app(cfg, start_scheduler=False)
        with TestClient(app) as c:
            ctx = app.state.ctx
            from app.diagnosis import integrity as I
            from app.diagnosis.collect import collect
            from app.diagnosis.engine import diagnose
            from app.importer.loader import check_import_dir
            from app.jobs import tasks

            out = timed("Import", lambda: check_import_dir(ctx.db, cfg.import_dir, ctx.engine_options("global"),
                                                           trigger="bench"), results)
            print(f"  Status: {out.status} – {out.message}")
            ctx.invalidate_data()
            timed("Ledger (global FIFO)", lambda: ctx.ledger(), results)
            timed("Kurse (Demo) + Historie", lambda: (tasks.refresh_prices(ctx, force=True), tasks.backfill(ctx)),
                  results)
            snap = timed("Schnappschuss (collect)", lambda: collect(ctx), results)
            rep = timed("Dublettenerkennung/Diagnose (diagnose)", lambda: diagnose(snap), results)
            dups = [f for f in rep.findings if f.kind == "duplicate"]
            print(f"  {len(rep.findings)} Befunde, davon {len(dups)} Dubletten")
            timed("Lösungsvorschläge (Empfehlung je Befund)", lambda: I.from_findings(rep), results)
            r = timed("Integritätsprüfung gesamt (kalt)", lambda: I.run(ctx), results)
            print(f"  ok={r.ok}, Befunde {len(r.items)}, offen {r.counts()['open']}, Abschnitte {r.timings}")
            timed("Integritätsprüfung gesamt (warm)", lambda: I.run(ctx), results)
            try:
                from app.diagnosis import bulk as B

                sel = [f.id for f in dups][:25]
                bp = timed(f"Sammelvorschau ({len(sel)} Dubletten)", lambda: B.plan(ctx, sel), results)
                print(f"  ausführbar {len(bp.ready)}, ausgeschlossen {len(bp.excluded)}, Konflikte {len(bp.conflicts)}")
            except ImportError:
                print("  (Sammelbearbeitung nicht vorhanden)")
            c.get("/settings")
            c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
            for p in ("/", "/", "/quality/integrity"):
                timed(f"GET {p}", lambda p=p: c.get(p), results)
            print(f"RSS (max) {rss_mb():.0f} MB")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
