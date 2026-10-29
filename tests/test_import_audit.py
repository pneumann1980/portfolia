"""Prüfunterlagen des Kurators (Ordner ``kuration/``): archiviert und angezeigt statt 14 Einzelwarnungen."""

from __future__ import annotations

import os
import shutil
import time
import zipfile

from fastapi.testclient import TestClient

from app.importer import contract as C
from app.importer.validate import validate_zip
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests.test_import import ASSETS, HOLD, ROWS


def zip_with(tmp_path, files: dict[str, bytes]):
    p = build_zip(tmp_path / "kuratiert.zip", transactions=ROWS, assets=ASSETS, holdings_check=HOLD)
    with zipfile.ZipFile(p, "a") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return p


AUDIT = {f"kuration/datei{i}.csv": f"spalte_a,spalte_b\nzeile{i},1\nzweite,2\n".encode() for i in range(14)}


def test_curator_folder_is_one_info_not_fourteen_warnings(tmp_path):
    rep, parsed = validate_zip(zip_with(tmp_path, AUDIT))
    assert rep.ok and parsed is not None
    assert not [m for m in rep.warnings if m.code == "zip_extra"]
    infos = [m for m in rep.messages if m.severity == "info" and m.code == "zip_audit"]
    assert len(infos) == 1 and "14 Prüfunterlagen" in infos[0].message
    assert set(parsed.audit) == set(AUDIT) and parsed.audit["kuration/datei3.csv"].startswith(b"spalte_a")


def test_other_subfolders_are_bundled_into_one_warning(tmp_path):
    rep, parsed = validate_zip(zip_with(tmp_path, {f"fremd/x{i}.txt": b"x" for i in range(5)}))
    assert rep.ok and parsed is not None and not parsed.audit
    extra = [m for m in rep.warnings if m.code == "zip_extra"]
    assert len(extra) == 1 and "5 Dateien in Unterordnern ignoriert" in extra[0].message and "(+2)" in extra[0].message


def test_audit_limits_skip_oversized_and_surplus_files(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_AUDIT_MEMBER_BYTES", 100)
    monkeypatch.setattr(C, "MAX_AUDIT_FILES", 3)
    files = {"kuration/klein1.csv": b"a\n", "kuration/gross.csv": b"x" * 500, "kuration/klein2.csv": b"b\n",
             "kuration/klein3.csv": b"c\n", "kuration/klein4.csv": b"d\n"}
    rep, parsed = validate_zip(zip_with(tmp_path, files))
    assert parsed is not None and set(parsed.audit) == {"kuration/klein1.csv", "kuration/klein2.csv",
                                                        "kuration/klein3.csv"}
    msg = next(m.message for m in rep.messages if m.code == "zip_audit")
    assert "2 zu große bzw. überzählige Dateien ausgelassen" in msg


def test_audit_files_are_stored_shown_and_kept_out_of_portfolia_extras(config):
    cfg = config
    dst = cfg.import_dir / "kuratiert.zip"
    p = zip_with(dst.parent, {**AUDIT, "kuration/binaer.bin": bytes([0, 1, 2, 3])})
    shutil.move(p, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        iid = ctx.active_import_id()
        names = [r["name"] for r in ctx.db.q("SELECT name FROM import_extra WHERE import_id=? ORDER BY name", (iid,))]
        assert len(names) == 15 and all(n.startswith("audit/kuration/") for n in names)
        from app.fullexport import extras_for

        assert extras_for(ctx.db, iid) == {}  # Prüfunterlagen sind keine Portfolia-Zusatzdaten
        page = c.get("/quality/technik").text
        assert "Prüfunterlagen des Kurators (15)" in page and "datei3.csv" in page and "zeile3,1" in page
        assert "Binärdatei" in page
        # Importmeldungen: ein Hinweis, keine Warnung
        rep = ctx.db.q1("SELECT report_json FROM imports WHERE id=?", (iid,))["report_json"]
        assert "zip_audit" in rep and "Unerwartete Datei" not in rep
