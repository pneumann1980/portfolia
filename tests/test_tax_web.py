"""Steuerseite, Optionen, PDF-Erzeugung und Downloads (inkl. Pfadschutz)."""

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.tax.service import tax_service

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"


@pytest.fixture
def client(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    app = build_app(config, start_scheduler=False)
    with TestClient(app) as c:
        r = c.get("/settings")
        token = c.cookies.get("portfolia_csrf")
        assert r.status_code == 200 and token
        assert c.post("/actions/import/check", headers={"X-CSRF-Token": token}).status_code == 200
        c.headers["X-CSRF-Token"] = token
        yield c


def test_tax_page_and_year_views(client):
    r = client.get("/tax")
    assert r.status_code == 200
    assert "Steuerjahr 2025" in r.text and "Informativ – keine Steuerberatung" in r.text
    assert "Freigaben der nächsten 12 Monate" in r.text and "Übertragungshilfe" in r.text
    r = client.get("/tax?year=2024")
    assert "Steuerjahr 2024" in r.text and "Freigrenze erreicht" in r.text  # § 22 Nr. 3: 328,10 € ≥ 256 €
    data = client.get("/api/tax/releases").json()
    assert data["months"] and all({"month", "value", "gain", "count"} <= set(m) for m in data["months"])


def test_options_mapping_profile_roundtrip(client):
    r = client.post("/tax/options", data={"year": "2025", "scope": "global", "crypto_fee": "ignore",
                                          "marginal_rate": "42", "loss_cf_23": "1.234,50",
                                          "income_map__airdrop": "none", "joint": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=options" in r.headers["location"]
    ctx = client.app.state.ctx
    svc = tax_service(ctx)
    pack = svc.pack()
    o25, o24 = svc.options(pack, 2025), svc.options(pack, 2024)
    assert o25["scope"] == "global" and o24["scope"] == "global"  # globale Option
    assert o25["loss_cf_23"] == "1234.5" and o24["loss_cf_23"] == 0  # jahresbezogen
    assert o25["marginal_rate"] == "42" and o25["joint"] is True
    assert o25["income_map"]["airdrop"] == "none" and o25["income_map"]["staking"] == "22_3"
    r = client.post("/tax/options", data={"year": "2025", "marginal_rate": "250", "scope": "evil"},
                    follow_redirects=False)
    assert r.status_code == 303
    o25 = svc.options(pack, 2025)
    assert o25["marginal_rate"] is None and o25["scope"] == "global"  # ungültige Werte verworfen
    r = client.post("/tax/mapping", data={"year": "2025", "acc__Depot A": "foreign", "type__WKN:A0RPWH": "etf_mixed"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert ctx.settings.get("tax.account_withholding")["Depot A"] == "foreign"
    assert ctx.settings.get("tax.asset_types")["WKN:A0RPWH"] == "etf_mixed"
    r = client.post("/tax/profile", data={"name": "Erika Muster", "tax_id": "12 345 678 901<x>"},
                    follow_redirects=False)
    assert r.status_code == 303 and ctx.settings.get("tax.profile")["tax_id"] == "12 345 678 901"
    # ohne CSRF-Token abgelehnt
    del client.headers["X-CSRF-Token"]
    assert client.post("/tax/options", data={"year": "2025"}).status_code == 403


def test_generate_download_and_delete_reports(client):
    r = client.post("/tax/report", data={"year": "2025", "doc": ["report", "anlage_so", "anlage_kap"]},
                    follow_redirects=False)
    assert r.status_code == 303 and "saved=report" in r.headers["location"]
    ctx = client.app.state.ctx
    svc = tax_service(ctx)
    rep = svc.reports()[0]
    assert rep["year"] == 2025 and rep["rulepack"].startswith("de@")
    docs = {d["id"]: d for d in rep["summary"]["docs"]}
    assert set(docs) == {"report", "anlage_so", "anlage_kap"}
    r = client.get(f"/tax/report/{rep['id']}/report.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and b"%%EOF" in r.content[-64:]
    assert r.content.count(b"/Type /Page\n") + r.content.count(b"/Type /Page ") >= 4
    assert "Steuerreport_2025.pdf" in r.headers["content-disposition"]
    assert client.get(f"/tax/report/{rep['id']}/anlage_so.pdf?inline=1").headers["content-disposition"].startswith(
        "inline")
    assert client.get(f"/tax/report/{rep['id']}/unknown.pdf").status_code == 404
    assert client.get("/tax/report/9999/report.pdf").status_code == 404
    # manipulierte Pfadangabe in der DB wird nicht ausgeliefert
    summary = rep["summary"]
    summary["docs"][0]["file"] = "../../../app.sqlite"
    ctx.db.x("UPDATE tax_report SET summary_json=? WHERE id=?", (json.dumps(summary), rep["id"]))
    assert client.get(f"/tax/report/{rep['id']}/{summary['docs'][0]['id']}.pdf").status_code == 404
    folder = ctx.config.reports_dir / rep["file_path"]
    assert folder.is_dir()
    assert client.post(f"/tax/report/{rep['id']}/delete", follow_redirects=False).status_code == 303
    assert not folder.exists() and svc.reports() == []


def test_report_for_year_without_import_data(client):
    r = client.post("/tax/report", data={"year": "2019", "doc": "report"}, follow_redirects=False)
    assert r.status_code == 303  # leeres Jahr → Bericht mit Hinweis „keine Eintragungen“
    rep = tax_service(client.app.state.ctx).reports()[0]
    assert rep["year"] == 2019


def test_asset_detail_uses_tax_pack_lots(client):
    r = client.get("/asset/ETH")
    assert r.status_code == 200 and "Zuordnung laut Steuer-Regelwerk" in r.text
    assert "Deutschland (EStG / InvStG)" in r.text
