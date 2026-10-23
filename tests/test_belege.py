# ruff: noqa: F811  (Fixture ``master`` aus test_bitpanda wird importiert und als Argument verwendet)
"""Belege-Archiv (``/belege``): Liste mit Filtern, PDF ansehen/herunterladen, ZIP der Auswahl. Alle Daten erfunden."""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pytest

from app.config import Config
from tests import dkbdocs as X
from tests.test_bitpanda import master, post  # noqa: F401
from tests.test_dkb import ctx, rows
from tests.test_dkb_costinfo import _sid, _start, pdf, run_zip


@pytest.fixture
def app(config: Config, master):
    c = _start(config)
    sid = _sid(c)
    settle = X.abrechnung("Kauf", order="123456/78.00", day="06.08.2024", qty="10", price="32,82 USD", fx="1,0887",
                          kurswert="301,46", close="05.08.2024 15:30:00")
    run = run_zip(c, sid, {"abr.pdf": pdf(settle), "depot.pdf": pdf(X.depotauszug_voll(with_fee=False)),
                           "ki.pdf": pdf(X.kosteninfo_voll("Kauf"))})
    assert run["status"] == "ok", run["message"]
    yield c, sid
    c.__exit__(None, None, None)


def test_list_shows_all_archived_documents_with_filters(app) -> None:
    c, _ = app
    page = c.get("/belege")
    assert page.status_code == 200
    assert "Archiv der Bankdokumente" in page.text and "Abrechnung Kauf" in page.text
    assert "Depotauszug" in page.text and "Kosteninformation zur Order" in page.text
    assert "Auswahl als ZIP herunterladen (3)" in page.text
    only = c.get("/belege?art=depotauszug").text
    assert "Auswahl als ZIP herunterladen (1)" in only and only.count('data-label="Art"') == 1
    assert "Keine Belege" in c.get("/belege?jahr=1999").text
    assert "Auswahl als ZIP herunterladen (1)" in c.get("/belege?q=US88023B1035&art=abrechnung_kauf").text
    # Filter nach Konto und Verknüpfung: Abrechnung und Kosteninformation gehören zur vorhandenen Buchung K-1
    assert "Auswahl als ZIP herunterladen (1)" in c.get("/belege?konto=DKB&verknuepft=nein").text
    linked = c.get("/belege?verknuepft=ja").text
    assert "Auswahl als ZIP herunterladen (2)" in linked and "1 Buchung" in linked
    # ungültige Jahre werden ignoriert, nicht abgelehnt
    assert "Auswahl als ZIP herunterladen (3)" in c.get("/belege?jahr=99999").text


def test_file_is_served_inline_and_as_download_without_original_name(app) -> None:
    c, _ = app
    bid = rows(c, "SELECT id FROM bank_doc WHERE doctype='depotauszug'")[0]["id"]
    r = c.get(f"/belege/{bid}/datei")
    assert r.status_code == 200 and r.content.startswith(b"%PDF-") and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline") and r.headers["cache-control"] == "private, no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    d = c.get(f"/belege/{bid}/datei?dl=1")
    disp = d.headers["content-disposition"]
    assert disp.startswith("attachment") and "Depotauszug" in disp
    assert ".pdf" in disp and "depot.pdf" not in disp
    assert c.get("/belege/99999/datei").status_code == 404


def test_missing_file_and_path_escape_give_404_and_manifest_status(app) -> None:
    c, _ = app
    ids = {r["doctype"]: r["id"] for r in rows(c, "SELECT id, doctype FROM bank_doc")}
    path = Path(ctx(c).config.documents_dir)
    f = next(p for p in path.rglob("*.pdf") if "depotauszug" in p.name)
    f.unlink()
    assert c.get(f"/belege/{ids['depotauszug']}/datei").status_code == 404
    ctx(c).db.x("UPDATE bank_doc SET rel_path=? WHERE id=?", ("../../etc/passwd", ids["kosteninfo_order"]))
    assert c.get(f"/belege/{ids['kosteninfo_order']}/datei").status_code == 404
    z = zipfile.ZipFile(io.BytesIO(c.get("/belege/export").content))
    man = list(csv.reader(io.StringIO(z.read("belege/manifest.csv").decode("utf-8-sig")), delimiter=";"))
    status = {r[2]: r[-1] for r in man[1:]}
    assert status["Depotauszug"] == "original_fehlt" and status["Kosteninformation zur Order"] == "original_fehlt"
    assert status["Abrechnung Kauf"] == "ok"


def test_zip_contains_selection_with_folders_and_manifest(app) -> None:
    c, _ = app
    r = c.get("/belege/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "private, no-store"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert any("/01_Abrechnungen/2024-08-06_Abrechnung Kauf".replace(" ", "_") in n for n in names), names
    assert any("/04_Depotauszuege/" in n for n in names) and any("/05_Orderbelege/" in n for n in names)
    assert not any("abr.pdf" in n or "depot.pdf" in n for n in names)  # nie der Originalname
    man = list(csv.reader(io.StringIO(z.read("belege/manifest.csv").decode("utf-8-sig")), delimiter=";"))
    assert man[0][0] == "datei_im_zip" and len(man) == 4
    assert {r[6] for r in man[1:]} == {"", "K-1"}  # Buchungen je Beleg
    assert {r[-1] for r in man[1:]} == {"ok"}
    # Auswahl wirkt auf den Download
    only = zipfile.ZipFile(io.BytesIO(c.get("/belege/export?art=depotauszug").content))
    assert sum(n.endswith(".pdf") for n in only.namelist()) == 1
    # leere Auswahl: verständliche Meldung statt Datei
    r = c.get("/belege/export?jahr=1999", follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
