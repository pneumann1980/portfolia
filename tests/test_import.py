"""Importvalidierung (fehlende Spalten, doppelte IDs, falsche Prüfsumme, …) und Import-Ablauf."""

import json
import zipfile
from decimal import Decimal

import pytest

from app.db import Database
from app.importer.loader import active_import_id, check_import_dir, import_file
from app.importer.validate import validate_zip
from app.importer.zipbuilder import TX_COLUMNS, build_zip
from app.ledger.engine import EngineOptions
from tests.helpers import ASSETS, tx

ROWS = [
    tx("t1", "2023-01-10", "buy", frm=("Depot", "EUR", 1000), to=("Depot", "WKN:A0B1C2", 10), value=1000),
    tx("t2", "2023-02-10T09:30:00Z", "buy", frm=("Exchange", "EUR", 500), to=("Exchange", "BTC", "0.02"), value=500,
       fee=("EUR", "1.5", "1.5")),
    tx("t3", "2023-03-01", "deposit", tag="reward", to=("Exchange", "BTC", "0.001"), value=20),
]
HOLD = [{"asset_id": "WKN:A0B1C2", "qty": "10"}, {"asset_id": "BTC", "qty": "0.021"}]


def codes(rep):
    return {m.code for m in rep.errors}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "app.sqlite")
    d.migrate()
    return d


def test_valid_zip(tmp_path):
    p = build_zip(tmp_path / "ok.zip", transactions=ROWS, assets=ASSETS, holdings_check=HOLD)
    rep, parsed = validate_zip(p)
    assert rep.ok, rep.to_json()
    assert parsed and parsed.counts["transactions"] == 3
    t2 = parsed.transactions[1]
    assert t2["fee_eur"] == Decimal("1.5") and t2["date_only"] is False
    # Nur Datum → 12:00 Europe/Berlin (Winterzeit: 11:00 UTC)
    assert parsed.transactions[0]["ts_utc"].hour == 11


def test_missing_required_column(tmp_path):
    cols = [c for c in TX_COLUMNS if c != "value_eur"]
    p = build_zip(tmp_path / "bad.zip", transactions=ROWS, assets=ASSETS, tx_columns=cols)
    rep, parsed = validate_zip(p)
    assert parsed is None
    assert "missing_columns" in codes(rep)


def test_duplicate_tx_id(tmp_path):
    rows = [*ROWS, dict(ROWS[0])]
    p = build_zip(tmp_path / "dup.zip", transactions=rows, assets=ASSETS)
    rep, parsed = validate_zip(p)
    assert parsed is None and "tx_dup" in codes(rep)


def test_wrong_checksum(tmp_path):
    p = build_zip(tmp_path / "sum.zip", transactions=ROWS, assets=ASSETS, mutate={"bad_checksum": "transactions.csv"})
    rep, parsed = validate_zip(p)
    assert parsed is None and "checksum" in codes(rep)


def test_missing_file_and_schema_version(tmp_path):
    p = build_zip(tmp_path / "miss.zip", transactions=ROWS, assets=ASSETS, mutate={"drop_file": "issues.csv"})
    rep, _ = validate_zip(p)
    assert "file_missing" in codes(rep)
    p2 = build_zip(tmp_path / "v2.zip", transactions=ROWS, assets=ASSETS, schema_version="2.0")
    rep2, _ = validate_zip(p2)
    assert "schema_version" in codes(rep2)


def test_unknown_asset_reference_and_bad_numbers(tmp_path):
    rows = [*ROWS, tx("t9", "2023-04-01", "buy", frm=("Depot", "EUR", 10), to=("Depot", "WKN:XXXX", 1), value=10)]
    rows.append({**tx("t10", "2023-04-02", "buy", frm=("Depot", "EUR", 10), to=("Depot", "BTC", 1), value=10),
                 "to_qty": "0,5"})
    p = build_zip(tmp_path / "ref.zip", transactions=rows, assets=ASSETS)
    rep, parsed = validate_zip(p)
    assert parsed is None
    assert {"asset_ref", "number"} <= codes(rep)
    msg = next(m for m in rep.errors if m.code == "number")
    assert msg.line == 6 and msg.column == "to_qty"


def test_leg_rules(tmp_path):
    rows = [tx("w1", "2023-01-01", "withdrawal", to=("Depot", "EUR", 1), value=1),
            tx("s1", "2023-01-01", "sell", to=("Depot", "EUR", 1), value=1)]
    p = build_zip(tmp_path / "legs.zip", transactions=rows, assets=ASSETS)
    rep, _ = validate_zip(p)
    assert "leg_rule" in codes(rep)


def test_path_traversal_rejected(tmp_path):
    p = tmp_path / "evil.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("../manifest.json", "{}")
    rep, parsed = validate_zip(p)
    assert parsed is None and "zip_path" in codes(rep)


def test_not_a_zip(tmp_path):
    p = tmp_path / "x.zip"
    p.write_bytes(b"not a zip")
    rep, parsed = validate_zip(p)
    assert parsed is None and "zip_invalid" in codes(rep)


def test_import_diff_retention_and_failures(tmp_path, db):
    imp = tmp_path / "import"
    p1 = build_zip(imp / "a.zip", transactions=ROWS, assets=ASSETS, holdings_check=HOLD)
    out = import_file(db, p1, EngineOptions())
    assert out.status == "imported", out.report
    first = out.import_id
    assert active_import_id(db) == first
    assert out.check["deviations"] == []

    # Gleiche Datei → unverändert
    assert import_file(db, p1, EngineOptions()).status == "unchanged"

    # Geänderte Version: t2 geändert, t3 entfernt, t4 neu
    rows2 = [ROWS[0], {**ROWS[1], "value_eur": "510"},
             tx("t4", "2023-04-01", "buy", frm=("Depot", "EUR", 100), to=("Depot", "WKN:A0B1C2", 1), value=100)]
    p2 = build_zip(imp / "b.zip", transactions=rows2, assets=ASSETS, holdings_check=HOLD)
    out2 = import_file(db, p2, EngineOptions())
    assert out2.status == "imported"
    d = out2.diff
    assert (d["new"], d["changed"], d["removed"]) == (1, 1, 1)
    assert d["changed_list"][0]["fields"] == [{"field": "value_eur", "old": "500", "new": "510"}]
    hold = {h["asset_id"]: h for h in d["holdings"]}
    assert hold["WKN:A0B1C2"]["delta"] == "1" and hold["BTC"]["delta"] == "-0.001"
    # Soll-Ist-Abweichung als Warnung, Bestände aus dem Ledger
    devs = {x["asset_id"] for x in out2.check["deviations"]}
    assert devs == {"WKN:A0B1C2", "BTC"}

    # Fehlerhafte Datei ändert den aktiven Stand nicht
    p3 = build_zip(imp / "c.zip", transactions=rows2, assets=ASSETS, mutate={"bad_checksum": "assets.csv"})
    out3 = import_file(db, p3, EngineOptions())
    assert out3.status == "failed"
    assert active_import_id(db) == out2.import_id
    row = db.q1("SELECT status, report_json FROM imports WHERE id=?", (out3.import_id,))
    assert row["status"] == "failed" and json.loads(row["report_json"])["errors"] >= 1

    # Aufbewahrung: nur 10 Stände behalten
    for i in range(11):
        rows_i = [*rows2, tx(f"x{i}", "2023-05-01", "deposit", to=("Exchange", "EUR", i + 1), value=i + 1)]
        assert import_file(db, build_zip(imp / f"r{i}.zip", transactions=rows_i, assets=ASSETS),
                           EngineOptions()).status == "imported"
    kept = db.scalar("SELECT COUNT(DISTINCT import_id) FROM tx")
    assert kept == 10
    assert db.scalar("SELECT data_retained FROM imports WHERE id=?", (first,)) == 0


def test_check_import_dir_pending_and_newest(tmp_path, db, monkeypatch):
    imp = tmp_path / "import"
    build_zip(imp / "a.zip", transactions=ROWS, assets=ASSETS)
    out = check_import_dir(db, imp, EngineOptions())
    assert out.status == "pending"  # gerade geschrieben
    monkeypatch.setattr("app.importer.loader.MIN_FILE_AGE_S", 0)
    out = check_import_dir(db, imp, EngineOptions())
    assert out.status == "imported"
    assert check_import_dir(db, tmp_path / "missing", EngineOptions()).status == "none"
