"""Abnahme: Großimport (≈5.700 Tx / ≈170 Assets) in < 30 s, Abgleich exakt, Seiten und Steuerberechnung zügig."""

import json
import os
import time

from fastapi.testclient import TestClient

from app.importer.loader import check_import_dir
from app.main import build_app
from tests.synthetic import write_zip


def test_large_import_and_response_times(config):
    path, ntx, nassets = write_zip(config.import_dir / "gross.zip")
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert ntx >= 5500 and nassets >= 165
    app = build_app(config, start_scheduler=False)
    with TestClient(app) as c:
        ctx = app.state.ctx
        t0 = time.perf_counter()
        out = check_import_dir(ctx.db, config.import_dir, ctx.engine_options("global"), trigger="test")
        dt = time.perf_counter() - t0
        assert out.status == "imported", out.message
        assert dt < 30, f"Import dauerte {dt:.1f} s"
        chk = json.loads(ctx.db.q1("SELECT check_json FROM imports WHERE id=?", (out.import_id,))["check_json"])
        assert chk["checked"] > 300 and chk["ok"] == chk["checked"] and not chk.get("mismatches")
        ctx.invalidate_data()
        assert ctx.ledger().tx_count == ntx
        c.get("/")  # Aufwärmen (Ledger, Bewertung)
        for path in ("/", "/positions", "/panel/asset/BTC"):
            t0 = time.perf_counter()
            r = c.get(path)
            assert r.status_code == 200
            limit = 0.5 if path.startswith("/panel") else 1.5
            assert time.perf_counter() - t0 < limit * 3, path  # Puffer für langsame CI-Runner
        t0 = time.perf_counter()
        assert c.get("/tax").status_code == 200
        assert time.perf_counter() - t0 < 15
