"""Steuerdaten je Jahr: Parser (JSON/CSV), Jahreserkennung, Ordnerprüfung (idempotent), genau eine aktive Datei je
Jahr, Ersetzen nur nach Bestätigung mit Verlauf, Zuordnung (externe ID → Buchungs-ID → Asset/Datum/Menge) und keine
neuen Buchungen. Synthetische Daten aus der Beispiel-ZIP – keine persönlichen Exporte.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.taxdata.parsers import TaxCsvParser, TaxJsonParser
from app.taxdata.service import TaxImportService, analyze, tax_import_service

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"

DOC_2025 = {"taxYear": 2025, "records": [
    {"transactionId": "DEMO-00073", "asset": "SOL", "quantity": 10, "acquisitionDate": "2024-02-01",
     "disposalDate": "2025-03-01", "acquisitionCost": "900", "disposalValue": "1300", "taxable": True},
    {"asset": "SOL", "quantity": "10", "disposalDate": "2025-03-01", "disposalValue": "1300,00",
     "acquisitionCost": "900", "externalId": "ext-unbekannt"},
    {"asset": "BTC", "quantity": "0.5", "disposalDate": "2025-06-30", "gainLoss": "-12.5"},
]}


def _old(p: Path) -> None:
    t = time.time() - 3600
    os.utime(p, (t, t))


def _write(p: Path, data: str | bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    _old(p)
    return p


@pytest.fixture
def client(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    _old(dst)
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.token = c.get("/settings") and c.cookies.get("portfolia_csrf")
        assert c.post("/actions/import/check", headers={"X-CSRF-Token": c.token}).status_code == 200
        yield c


def post(c, url, data=None, **kw):
    return c.post(url, data={**(data or {}), "csrf_token": c.token}, follow_redirects=False, **kw)


def svc(c) -> TaxImportService:
    return tax_import_service(c.app.state.ctx)


def n_tx(c) -> int:
    return c.app.state.ctx.db.scalar("SELECT COUNT(*) FROM journal_tx")


# -- Parser und Jahreserkennung -----------------------------------------------------------------------------

def test_json_parser_header_year_and_derived_values():
    res = TaxJsonParser().parse(json.dumps(DOC_2025).encode())
    assert not res.errors and res.year == 2025 and len(res.records) == 3
    r = res.records[0]
    assert r.quantity == Decimal("10") and r.taxable is True
    assert r.gain_loss == Decimal("400") and r.holding_period_days == 394  # abgeleitet, nicht geraten
    assert res.records[1].disposal_value == Decimal("1300.00")


def test_csv_parser_german_headers_and_decimal_comma():
    data = ("Steuerjahr;Asset;Menge;Kaufdatum;Verkaufsdatum;Anschaffungskosten;Erlös;Gewinn;Steuerpflichtig\n"
            "2025;ETH;1,5;01.02.2024;03.03.2025;2.000,50;3.100,00;1.099,50;ja\n"
            "2025;ADA;100;;15.04.2025;;;;nein\n").encode()
    res = TaxCsvParser().parse(data)
    assert not res.errors and len(res.records) == 2
    a, b = res.records
    assert a.quantity == Decimal("1.5") and a.acquisition_cost == Decimal("2000.50")
    assert a.gain_loss == Decimal("1099.50")
    assert a.disposal_date.isoformat() == "2025-03-03" and a.taxable is True and b.taxable is False


def test_unreadable_values_become_warnings_not_guesses():
    rows = [{"asset": "BTC", "quantity": "viel", "disposalDate": "2025-01-02"}]
    res = TaxJsonParser().parse(json.dumps(rows).encode())
    assert res.records[0].quantity is None and any("quantity" in w for w in res.warnings)


def test_year_detection_content_filename_and_ambiguity():
    assert analyze(json.dumps(DOC_2025).encode(), "x.json").year == 2025
    rows = [{"asset": "BTC", "disposalDate": "2024-05-01"}]
    a = analyze(json.dumps(rows).encode(), "steuer-2024.json")
    assert a.year == 2024 and a.year_source == "Datensätze"
    a = analyze(json.dumps([{"asset": "BTC", "quantity": 1}]).encode(), "bericht_2023.json")
    assert a.year == 2023 and a.year_source == "Dateiname"
    mixed = [{"asset": "BTC", "disposalDate": "2024-12-30"}, {"asset": "ETH", "disposalDate": "2025-01-02"}]
    a = analyze(json.dumps(mixed).encode(), "export.json")
    assert a.year is None and a.year_candidates == [2024, 2025]  # mehrdeutig → Nutzer wählt
    bad = analyze(b"{kaputt", "x.json")
    assert not bad.ok and bad.errors


# -- Ordner: Erkennung, Idempotenz, eine aktive Datei je Jahr ------------------------------------------------

def test_folder_scan_activates_new_year_and_is_idempotent(client):
    s = svc(client)
    before = n_tx(client)
    _write(s.dir / "steuer-2025.json", json.dumps(DOC_2025))
    res = s.scan()
    assert res["new"] == [{"year": 2025, "file": "steuer-2025.json"}]
    act = s.active(2025)
    assert act is not None and act["records"] == 3 and act["origin"] == "folder"
    html = client.get("/").text
    assert "Neue Steuerdatei erkannt: Steuerjahr 2025" in html
    client.get(f"/tax/data/file/{act['id']}")  # angesehen → Hinweis verschwindet
    assert "Neue Steuerdatei erkannt" not in client.get("/").text
    again = s.scan()
    assert again["unchanged"] == 1 and not again["new"] and not again["pending"]
    _write(s.dir / "kopie.json", json.dumps(DOC_2025))  # gleicher Inhalt, anderer Name
    s.scan()
    assert client.app.state.ctx.db.scalar("SELECT COUNT(*) FROM tax_file") == 1
    assert n_tx(client) == before  # Steuerdateien erzeugen nie Buchungen


def test_changed_file_waits_for_confirmation_and_keeps_history(client):
    s = svc(client)
    _write(s.dir / "steuer-2025.json", json.dumps(DOC_2025))
    s.scan()
    first = s.active(2025)
    doc = json.loads(json.dumps(DOC_2025))
    doc["records"].append({"asset": "ETH", "quantity": "1", "disposalDate": "2025-08-01", "gainLoss": "50"})
    _write(s.dir / "steuer-2025-korrigiert.json", json.dumps(doc))
    res = s.scan()
    assert res["pending"] and not res["new"]
    assert s.active(2025)["id"] == first["id"]  # nicht still ersetzt
    pid = res["pending"][0]["id"]
    assert "bitte prüfen und bestätigen" in client.get("/").text
    page = client.get(f"/tax/data/file/{pid}").text
    assert "Für 2025 existieren bereits Steuerdaten." in page and "Aktualisieren / Ersetzen" in page
    assert "hinzugekommen 1" in page
    assert s.activate(pid) == {"needs_confirm": True, "existing": s.active(2025)}
    r = post(client, f"/tax/data/file/{pid}/activate")  # ohne Bestätigung → zurück zur Rückfrage
    assert r.headers["location"].endswith("confirm=replace") and s.active(2025)["id"] == first["id"]
    r = post(client, f"/tax/data/file/{pid}/activate", {"replace": "1"})
    assert r.status_code == 303
    assert s.active(2025)["id"] == pid
    old = s.file(first["id"])
    assert old["status"] == "replaced" and old["replaced_by"] == pid
    assert len(s.records(first["id"])) == 3  # frühere Fassung bleibt vollständig erhalten
    with pytest.raises(sqlite3.IntegrityError):  # Datenbank erzwingt eine aktive Datei je Jahr
        client.app.state.ctx.db.x("UPDATE tax_file SET status='active' WHERE id=?", (first["id"],))


def test_rejected_file_is_reported_and_not_reread(client):
    s = svc(client)
    _write(s.dir / "kaputt.json", "{nicht json")
    res = s.scan()
    assert res["rejected"] and res["rejected"][0]["file"] == "kaputt.json"
    assert s.scan()["unchanged"] == 1
    assert not s.files()[0]["tax_year"] and s.files()[0]["status"] == "rejected"


# -- Zuordnung ----------------------------------------------------------------------------------------------

def test_matching_priorities_and_no_new_transactions(client):
    s = svc(client)
    before = n_tx(client)
    fid, _a = s.register(json.dumps(DOC_2025).encode(), "s.json", "upload")
    recs = s.records(fid)
    assert recs[0]["match_status"] == "matched" and recs[0]["match_method"] == "transaction_id"
    assert recs[0]["match_tx"] == "DEMO-00073"
    assert recs[1]["match_status"] == "matched" and recs[1]["match_method"] == "heuristic"  # Asset+Datum+Menge+Betrag
    assert recs[2]["match_status"] == "unmatched"
    f = s.file(fid)
    assert (f["matched"], f["unmatched"], f["conflicts"]) == (2, 1, 0)
    assert n_tx(client) == before


def test_external_id_has_priority_and_ambiguity_is_conflict():
    t1 = SimpleNamespace(tx_id="A", from_asset="SOL", from_qty=Decimal(1), value_eur=Decimal(10), type="sell")
    t2 = SimpleNamespace(tx_id="B", from_asset="SOL", from_qty=Decimal(1), value_eur=Decimal(10), type="sell")
    by_id = {"A": t1, "B": t2}
    row = {"external_id": "X-1", "transaction_id": "B", "asset": "SOL", "disposal_date": "2025-01-01",
           "quantity": "1", "disposal_value": "10"}
    m = TaxImportService._match_one(row, by_id, {"x-1": [t1]}, {("SOL", "2025-01-01"): [t1, t2]}, {})
    assert m[:3] == ("matched", "A", "external_id")
    row2 = {**row, "external_id": None, "transaction_id": None}
    m = TaxImportService._match_one(row2, by_id, {}, {("SOL", "2025-01-01"): [t1, t2]}, {})
    assert m[0] == "conflict"
    row3 = {**row2, "quantity": "2"}
    assert TaxImportService._match_one(row3, by_id, {}, {("SOL", "2025-01-01"): [t1, t2]}, {})[0] == "unmatched"


# -- Weboberfläche: Upload mit Vorschau, Abbrechen, Entfernen nach Bestätigung, Export ----------------------

def test_upload_preview_activate_export_and_remove(client):
    before = n_tx(client)
    r = client.post("/tax/data/upload", data={"csrf_token": client.token},
                    files={"file": ("steuer.json", json.dumps(DOC_2025).encode(), "application/json")},
                    follow_redirects=False)
    assert r.status_code == 303
    fid = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/tax/data/file/{fid}").text
    assert "3 Datensätze erkannt" in page and "Übernehmen" in page
    assert svc(client).active(2025) is None  # Upload wird nie ohne Bestätigung aktiv
    stored = Path(svc(client).file(fid)["path"])
    assert stored.exists() and stored.parent.name == "uploads"
    assert post(client, f"/tax/data/file/{fid}/activate").status_code == 303
    assert svc(client).active(2025)["id"] == fid
    ov = client.get("/tax/data").text
    assert "Steuerdateien prüfen" in ov and "steuer.json" in ov
    csv_body = client.get(f"/tax/data/file/{fid}/export.csv").content.decode("utf-8-sig")
    assert csv_body.splitlines()[0].startswith("line;tax_year;") and "DEMO-00073" in csv_body
    r = post(client, f"/tax/data/file/{fid}/remove")  # ohne Bestätigung nur Rückfrage
    assert r.headers["location"].endswith("confirm=remove") and svc(client).active(2025) is not None
    assert "wirklich entfernen" in client.get(f"/tax/data/file/{fid}?confirm=remove").text
    post(client, f"/tax/data/file/{fid}/remove", {"confirm": "1"})
    assert svc(client).active(2025) is None and svc(client).file(fid)["status"] == "removed"
    assert len(svc(client).records(fid)) == 3
    assert n_tx(client) == before


def test_ambiguous_year_requires_choice_and_discard(client):
    mixed = [{"asset": "BTC", "disposalDate": "2024-12-30"}, {"asset": "ETH", "disposalDate": "2025-01-02"}]
    r = client.post("/tax/data/upload", data={"csrf_token": client.token},
                    files={"file": ("export.csv", b"", "text/csv")}, follow_redirects=False)
    assert r.status_code == 400  # leere Datei
    fid, _a = svc(client).register(json.dumps(mixed).encode(), "export.json", "upload")
    page = client.get(f"/tax/data/file/{fid}").text
    assert "Jahr festlegen" in page and "2024, 2025" in page
    assert svc(client).activate(fid)["errors"]
    assert post(client, f"/tax/data/file/{fid}/year", {"year": "1999"}).status_code == 400
    post(client, f"/tax/data/file/{fid}/year", {"year": "2024"})
    assert svc(client).file(fid)["tax_year"] == 2024
    post(client, f"/tax/data/file/{fid}/discard")
    assert svc(client).file(fid)["status"] == "removed" and not svc(client).pending()
