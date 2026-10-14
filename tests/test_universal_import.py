"""Universeller Börsenimport (Financial Integrity, Phase 4): CSV, XLSX, ZIP – sicher, mit Herkunft, idempotent.

Nur synthetische Dateien, im Test erzeugt. Abgedeckt: XLSX mit Datums- und Textzellen, ZIP mit mehreren Dateien,
übersprungene Einträge (verschachtelt, Makros, Pfadangriff), Kompressionsbombe, Makro-Arbeitsmappe, Binär-Excel,
Portfolia-eigene Formate, Datenquelle als Dateiquelle, wiederholter Upload derselben Datei.
"""

# ruff: noqa: F811
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from app.csvimport import files as F
from app.csvimport import queue as Q
from app.csvimport import review as RV
from app.csvimport.service import csv_service
from tests.test_csvimport import post
from tests.test_datasources import client, create_source  # noqa: F401

DATA = Path(__file__).parent / "data" / "csv"
KRAKEN = (DATA / "kraken.csv").read_bytes()


def ctx(c):
    return c.app.state.ctx


def _cell(ref: str, v: object, style: int | None = None) -> str:
    s = f' s="{style}"' if style is not None else ""
    if isinstance(v, (int, float)):
        return f'<c r="{ref}"{s}><v>{v}</v></c>'
    return f'<c r="{ref}" t="inlineStr"{s}><is><t>{v}</t></is></c>'


def make_xlsx(rows: list[list[object]], *, date_cols: tuple[int, ...] = (), macro: bool = False,
              sheets: int = 1) -> bytes:
    """Minimale, gültige XLSX-Datei (Office Open XML) – Datumsspalten mit Zahlenformat 22 (Datum + Uhrzeit)."""
    def sheet_xml() -> str:
        out = []
        for i, r in enumerate(rows, start=1):
            cells = "".join(_cell(f"{chr(65 + j)}{i}", v, 1 if (j in date_cols and i > 1) else None)
                            for j, v in enumerate(r))
            out.append(f'<row r="{i}">{cells}</row>')
        return ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                f'spreadsheetml/2006/main"><sheetData>{"".join(out)}</sheetData></worksheet>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    sheet_tags = "".join(f'<sheet name="Blatt{i}" sheetId="{i}" r:id="rId{i}"/>' for i in range(1, sheets + 1))
    rels = "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   f'relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, sheets + 1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rns}><sheets>{sheet_tags}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
                                                 f'2006/relationships">{rels}</Relationships>')
        z.writestr("xl/styles.xml", f'<styleSheet {ns}><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="22"/>'
                                    '</cellXfs></styleSheet>')
        for i in range(1, sheets + 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", sheet_xml())
        if macro:
            z.writestr("xl/vbaProject.bin", b"\x00" * 64)
    return buf.getvalue()


def kraken_rows() -> list[list[object]]:
    """Kraken-Ledger als Tabelle; Zeitpunkt als Excel-Seriennummer (2024-03-01 10:00 = 45352.416666…)."""
    return [["txid", "refid", "time", "type", "subtype", "aclass", "asset", "wallet", "amount", "fee", "balance"],
            ["L1", "R1", 45352 + 10 / 24, "deposit", "", "currency", "ZEUR", "spot / main", 1000, 0, 1000],
            ["L5", "R5", 45353 + 12 / 24, "deposit", "", "currency", "ZEUR", "spot / main", 250, 0, 1250]]


def make_zip(entries: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def upload(c, name: str, data: bytes, **form):
    return c.post("/journal/csv", data={"csrf_token": c.token, "profile": "auto", **form},
                  files={"file": (name, data, "application/octet-stream")}, follow_redirects=False)


# ----------------------------------------------------------------------------------------------------
# Datei-Ebene (ohne App)
# ----------------------------------------------------------------------------------------------------

def test_xlsx_cells_dates_and_sheets():
    up = F.unpack(make_xlsx(kraken_rows(), date_cols=(2,), sheets=2), "kraken.xlsx")
    assert [p.fmt for p in up.parts] == ["xlsx", "xlsx"]
    text = up.parts[0].data.decode()
    assert text.splitlines()[0].startswith("txid,refid,time") and "2024-03-01 10:00:00" in text
    assert up.parts[0].origin["sheet"] == "Blatt1" and up.parts[1].name.endswith("› Blatt2")


@pytest.mark.parametrize("name,data,text", [
    ("makro.xlsm", b"PK\x03\x04", "Makros"),
    ("alt.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 50, "Excel-Binärformat"),
    ("bin.dat", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 50, "Binärformat"),
])
def test_refused_office_formats(name, data, text):
    with pytest.raises(F.FileError, match=text):
        F.unpack(data, name)


def test_xlsx_with_macro_project_is_refused_even_if_named_xlsx():
    with pytest.raises(F.FileError, match="Makros"):
        F.unpack(make_xlsx(kraken_rows(), macro=True), "harmlos.xlsx")


def test_zip_members_skips_and_path_traversal_label_only():
    data = make_zip({"export/trades.csv": KRAKEN, "../../etc/evil.csv": KRAKEN, "nested.zip": make_zip({"a.csv": b"x"}),
                     "m.xlsm": b"x", "readme.pdf": b"%PDF", "sheet.xlsx": make_xlsx(kraken_rows(), date_cols=(2,))})
    up = F.unpack(data, "archiv.zip")
    assert [p.fmt for p in up.parts] == ["csv", "csv", "xlsx"]
    names = [p.name for p in up.parts]
    assert "archiv.zip › export/trades.csv" in names and "archiv.zip › etc/evil.csv" in names  # nur Bezeichnung
    joined = " | ".join(up.notes)
    assert "unzulässiger Pfad" in joined and "verschachteltes Archiv" in joined and "Makros" in joined
    assert "kein CSV/XLSX" in joined
    assert all(p.origin["archive_sha256"] == F.sha256(data) and p.origin["sha256"] for p in up.parts)


def test_zip_bomb_and_limits_are_refused(monkeypatch):
    bomb = make_zip({"big.csv": b"0" * (5 * 1024 * 1024)})  # Verhältnis weit über 100
    with pytest.raises(F.FileError, match="ungewöhnlich stark komprimiert"):
        F.unpack(bomb, "bomb.zip")
    monkeypatch.setattr(F, "MAX_MEMBERS", 3)
    with pytest.raises(F.FileError, match="zu viele Dateien"):
        F.unpack(make_zip({f"f{i}.csv": KRAKEN for i in range(4)}), "many.zip")
    with pytest.raises(F.FileError, match="kein gültiges ZIP"):
        F.unpack(b"PK\x03\x04kaputt", "x.zip")


def test_portfolia_formats_are_recognised():
    contract = make_zip({"manifest.json": b"{}", "transactions.csv": b"tx_id\n", "assets.csv": b"asset_id\n"})
    assert F.unpack(contract, "imp.zip").kind == "portfolia_contract"
    full = make_zip({"export/manifest.json": b"{}", "export/transactions.csv": b"x", "export/portfolia/state.json":
                     b"{}"})
    assert F.unpack(full, "voll.zip").kind == "portfolia_full"


# ----------------------------------------------------------------------------------------------------
# App: Upload, Herkunft, Idempotenz, Datenquelle als Dateiquelle
# ----------------------------------------------------------------------------------------------------

def test_xlsx_upload_creates_batch_with_provenance(client):
    c = client
    r = upload(c, "kraken.xlsx", make_xlsx(kraken_rows(), date_cols=(2,)))
    assert r.status_code == 303, r.text[:500]
    bid = int(re.search(r"/journal/csv/(\d+)", r.headers["location"]).group(1))
    b = csv_service(ctx(c)).batch(bid)
    origin = json.loads(b["origin_json"])
    assert b["profile"] == "kraken" and origin["format"] == "xlsx" and origin["sheet"] == "Blatt1"
    rows = csv_service(ctx(c)).rows(bid)
    assert len(rows) == 2 and rows[0].rec.ts.isoformat().startswith("2024-03-01T")
    page = c.get(f"/journal/csv/{bid}").text
    assert "Herkunft" in page and "XLSX" in page and "Blatt „Blatt1“" in page


def test_zip_upload_creates_one_batch_per_table_and_reports_skips(client):
    c = client
    data = make_zip({"a/kraken.csv": KRAKEN, "b/kraken.xlsx": make_xlsx(kraken_rows(), date_cols=(2,)),
                     "notes.txt.zip": b"PK"})
    r = upload(c, "export.zip", data)
    assert r.status_code == 200 and "2 Prüf-Stapel angelegt" in r.text and "verschachteltes Archiv" in r.text
    origins = [json.loads(b["origin_json"]) for b in ctx(c).db.q("SELECT origin_json FROM csv_batch ORDER BY id")]
    assert [o["member"] for o in origins] == ["a/kraken.csv", "b/kraken.xlsx"]


def test_portfolia_contract_zip_is_not_read_as_exchange_export(client):
    c = client
    data = make_zip({"manifest.json": b"{}", "transactions.csv": b"tx_id\n"})
    r = upload(c, "imp.zip", data)
    assert r.status_code == 400 and "Portfolia-Importdatei" in r.text
    assert not ctx(c).db.scalar("SELECT COUNT(*) FROM csv_batch")


def test_same_file_twice_is_marked_and_rows_are_recognised(client):
    c = client
    r1 = upload(c, "kraken.csv", KRAKEN)
    b1 = int(re.search(r"/journal/csv/(\d+)", r1.headers["location"]).group(1))
    r2 = upload(c, "kraken.csv", KRAKEN)
    b2 = int(re.search(r"/journal/csv/(\d+)", r2.headers["location"]).group(1))
    assert json.loads(csv_service(ctx(c)).batch(b2)["origin_json"])["same_as"] == b1
    assert f"gleiche Datei wie <a href=\"/journal/csv/{b1}\">" in c.get(f"/journal/csv/{b2}").text


def test_any_exchange_as_file_source(client):
    """Pionex (keine Schnittstelle) als Datenquelle; Datei wird der Quelle zugeordnet, zählt in deren Prüfung."""
    c = client
    r = post(c, "/settings/datasources", kind="exchange", provider="pionex", name="Pionex Hauptkonto",
             account="Pionex", sync_interval_min="0")
    assert r.status_code == 303, r.text[:500]
    sid = int(re.search(r"/settings/datasources/(\d+)", r.headers["location"]).group(1))
    assert "Exportdatei dieser Quelle hochladen" in c.get(f"/settings/datasources/{sid}").text
    r = c.post(f"/settings/datasources/{sid}/upload", data={"csrf_token": c.token},
               files={"file": ("pionex.xlsx", make_xlsx(kraken_rows(), date_cols=(2,)), "application/octet-stream")},
               follow_redirects=False)
    assert r.status_code == 303 and "/journal/csv/" in r.headers["location"], r.headers.get("location")
    bid = int(re.search(r"/journal/csv/(\d+)", r.headers["location"]).group(1))
    b = csv_service(ctx(c)).batch(bid)
    assert b["datasource_id"] == sid and b["account"] == "Pionex"
    assert RV.source_summary(ctx(c).db, sid).total == 2
    assert Q.build(ctx(c).db, "offen", flt=Q.Filter(source=sid)).total + \
        Q.build(ctx(c).db, "sicher", flt=Q.Filter(source=sid)).total == 2
