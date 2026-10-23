# ruff: noqa: F811  (Fixture ``master`` aus test_bitpanda wird importiert und als Argument verwendet)
"""DKB-Wertpapierdokumente per ZIP (Phase 1): Klassifikation nach Inhalt, sicheres Entpacken, Ablage unter selbst
gebautem Pfad, Dubletten, Abbruch/Fortsetzen, Ergebnisseite. Alle Dokumente sind anonymisierte Nachbauten."""

from __future__ import annotations

import io
import logging
import os
import re
import stat
import threading
import time
import zipfile
from pathlib import Path

import pytest

from app.config import Config
from app.datasources import dkb as D
from app.datasources import dkb_run as R
from app.datasources import dkb_zip as Z
from app.documentimport.dkb import KINDS, classify, clean_lines, filename_hint
from app.web.security import body_limit
from tests import dkbdocs as X
from tests.test_bitpanda import master  # noqa: F401
from tests.test_dkb import ctx, rows, setup_app, wait


def make_zip(entries: dict[str, bytes], *, compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def upload(c, sid: int, data: bytes, name: str = "Wertpapierdokumente.zip"):
    return c.post(f"/settings/datasources/{sid}/dkb/zip", files={"file": (name, data, "application/zip")},
                  data={"csrf_token": c.token}, follow_redirects=False)


def run_id(r) -> int:
    return int(re.search(r"/dkb/zip/(\d+)$", r.headers["location"]).group(1))


def finish(c, sid: int, rid: int) -> dict:
    wait(sid, 60)
    run = R.view(ctx(c), sid, rid)
    assert run is not None
    return run


def doc_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.pdf"))


# ----------------------------------------------------------------------------------------------------
# Klassifikation (Inhalt entscheidet)
# ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(X.GOLDEN))
def test_classification_golden(key: str) -> None:
    lines, (kind, sub, side, day, ident) = X.GOLDEN[key]
    c = classify(lines)
    assert c is not None, key
    assert (c.kind, c.sub, c.side, c.doc_date, c.ident) == (kind, sub, side, day, ident), key
    assert c.kind in KINDS and c.label
    if c.kind not in ("kosteninfo_jahr",):
        assert c.depot == X.DEPOT and c.depot_short == "2345"


def test_classification_covers_every_kind() -> None:
    assert {v[1][0] for v in X.GOLDEN.values()} == set(KINDS)


def test_classification_identifiers_and_no_personal_data() -> None:
    c = classify(X.abrechnung("Kauf"))
    assert c is not None
    assert (c.isin, c.wkn, c.order_no) == ("US88023B1035", "A40EDP", "1234567800")
    shown = " ".join([c.subject(), c.label, c.ident, *c.notes])
    for private in ("Mustermann", "Beispielweg", "11112222", X.DEPOT):
        assert private not in shown


def test_unknown_title_is_not_guessed() -> None:
    assert classify(X.kontoauszug()) is None
    assert classify(["Willkommen bei der DKB", "Ihre Karte ist unterwegs"]) is None
    assert classify([]) is None
    # unbekannte Art der Wertpapierabrechnung: weder Kauf noch Verkauf → nicht erkannt
    lines = X.abrechnung("Kauf")
    lines[lines.index("Wertpapier Abrechnung Kauf")] = "Wertpapier Abrechnung Zeichnung"
    c = classify(lines)
    assert c is None or c.kind not in ("abrechnung_kauf", "abrechnung_verkauf")


def test_capital_action_with_unknown_subtype_is_flagged() -> None:
    c = classify(X.kapital(["Irgendeine neue Maßnahme"]))
    assert c is not None and c.kind == "kapitalmassnahme" and c.sub == ""
    assert any("Unterart" in n for n in c.notes)


def test_noise_lines_do_not_disturb() -> None:
    lines = X.abrechnung("Verkauf")
    noisy = [x for pair in zip(lines, ["R", "O9", "22"] * len(lines), strict=False) for x in pair]
    assert clean_lines(["R", "O9", "22", "00.", "Kurswert  1,00 EUR"]) == ["Kurswert  1,00 EUR"]
    c = classify(noisy)
    assert c is not None and c.kind == "abrechnung_verkauf"


def test_filename_never_decides_and_mismatch_is_noted() -> None:
    # „Depot_bertrag“ im Namen, Inhalt = Kaufabrechnung → Inhalt gilt, Hinweis
    c = classify(X.abrechnung("Kauf"), "Archiv_dwpDepotTransferStatement_2025-01-07_Depot_bertrag_vom_07.01.2025.pdf")
    assert c is not None and c.kind == "abrechnung_kauf"
    assert any("Dateiname" in n for n in c.notes) and any("Datum laut Dateiname" in n for n in c.notes)
    # ersatzlose Ausbuchung trotz Namen „Depot_bertrag“ – kein Widerspruch
    d = classify(X.depotuebertrag(), "Archiv_dwpDepotTransferStatement_2025-01-07_Depot_bertrag_vom_07.01.2025.pdf")
    assert d is not None and d.kind == "depotuebertrag" and d.sub == "ersatzlose_ausbuchung" and not d.notes
    assert filename_hint("Archiv_Wertpapierdokumente_2025-02-17_Kapitalma_nahme_Umbuchung_WKN_A3CQRG_x.pdf") \
        == ("kapitalmassnahme", "2025-02-17")


def test_duplicate_suffix_in_filename_is_irrelevant() -> None:
    a = classify(X.ertrag(), "Archiv_Wertpapierdokumente_2025-03-18_Ertragsabrechnung_WKN_A14Y6F_vom_18.03.2025.pdf")
    b = classify(X.ertrag(), "Archiv_Wertpapierdokumente_2025-03-18_Ertragsabrechnung_WKN_A14Y6F_vom_18.03.2025(1).pdf")
    assert a == b


# ----------------------------------------------------------------------------------------------------
# Archiv: Grenzen und Sicherheit
# ----------------------------------------------------------------------------------------------------

def zip_path(tmp_path: Path, data: bytes) -> Path:
    p = tmp_path / "t.zip"
    p.write_bytes(data)
    return p


def test_zip_slip_and_absolute_paths_are_harmless(tmp_path: Path) -> None:
    pdf = X.pdf_of(X.abrechnung("Kauf"))
    data = make_zip({"../../evil.pdf": pdf, "/abs/pfad/x.pdf": pdf + b" ", "ok/ordner/a.pdf": pdf + b"  "})
    with Z.open_zip(zip_path(tmp_path, data)) as zf:
        mem, skipped = Z.members(zf)
        assert [m.name for m in mem] == ["evil.pdf", "x.pdf", "a.pdf"] and not skipped
    root = tmp_path / "root"
    c = classify(X.abrechnung("Kauf"))
    assert c is not None
    placed = Z.place(root, c, pdf)
    assert placed.rel == "Wertpapierdokumente/Depot-2345/2024-08-06_abrechnung_kauf_1234567800.pdf" and placed.created
    assert (root / placed.rel).read_bytes() == pdf
    assert not (tmp_path / "evil.pdf").exists() and not Path("/abs/pfad/x.pdf").exists()
    # gleicher Inhalt: nicht überschrieben, nicht neu angelegt
    assert not Z.place(root, c, pdf).created
    # anderer Inhalt unter gleichem Namen: Zusatz _2, Original unverändert
    other = Z.place(root, c, pdf + b"\n%anders")
    assert other.rel.endswith("_1234567800_2.pdf") and other.created
    assert (root / placed.rel).read_bytes() == pdf
    assert len(doc_files(root)) == 2


def test_path_is_built_from_content_ascii_only() -> None:
    c = classify(X.kapital(["Zusammenlegung von Titeln (Reverse Split)"]))
    assert c is not None
    rel = Z.relative_path(c)
    assert rel == "Wertpapierdokumente/Depot-2345/2025-02-17_kapitalmassnahme_61432975.pdf"
    assert rel.isascii() and ".." not in rel
    unknown = classify(X.kosteninfo_jahr())
    assert unknown is not None
    assert Z.relative_path(unknown) == "Wertpapierdokumente/Depot-2345/ohne-Datum_kosteninfo_jahr.pdf"


def test_zip_rejections(tmp_path: Path, monkeypatch) -> None:
    pdf = X.pdf_of(X.abrechnung("Kauf"))
    with pytest.raises(Z.ZipRejected, match="kein ZIP"):
        Z.open_zip(zip_path(tmp_path, b"%PDF-1.4 nicht ein zip" + b"x" * 100))
    with pytest.raises(Z.ZipRejected, match="beschädigt"):
        Z.open_zip(zip_path(tmp_path, make_zip({"a.pdf": pdf})[:-30]))
    # Bombe: extrem hohes Kompressionsverhältnis
    with Z.open_zip(zip_path(tmp_path, make_zip({"b.pdf": b"%PDF-" + b"\0" * 5_000_000}))) as zf, \
            pytest.raises(Z.ZipRejected, match="Kompressionsverhältnis"):
        Z.members(zf)
    # verschlüsselt (Flag gesetzt)
    raw = bytearray(make_zip({"geheim.pdf": b"%PDF-1.4 x"}))
    raw[raw.index(b"PK\x03\x04") + 6] |= 0x1  # Verschlüsselungs-Flag im lokalen Kopf …
    raw[raw.index(b"PK\x01\x02") + 8] |= 0x1  # … und im Verzeichniseintrag
    with Z.open_zip(zip_path(tmp_path, bytes(raw))) as zf, pytest.raises(Z.ZipRejected, match="passwortgeschützt"):
        Z.members(zf)
    # zu viele Einträge
    monkeypatch.setattr(Z, "MAX_ENTRIES", 3)
    with Z.open_zip(zip_path(tmp_path, make_zip({f"{i}.pdf": pdf + bytes([i]) for i in range(5)}))) as zf, \
            pytest.raises(Z.ZipRejected, match="Zu viele"):
        Z.members(zf)


def test_members_skip_nested_symlink_and_non_pdf(tmp_path: Path) -> None:
    pdf = X.pdf_of(X.abrechnung("Kauf"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.pdf", pdf)
        z.writestr("inner.zip", make_zip({"b.pdf": pdf}))
        z.writestr("notiz.txt", b"hallo")
        z.writestr("__MACOSX/._a.pdf", b"x")
        z.writestr(".versteckt.pdf", pdf)
        link = zipfile.ZipInfo("link.pdf")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, "a.pdf")
        z.writestr("Ordner/", b"")
    with Z.open_zip(zip_path(tmp_path, buf.getvalue())) as zf:
        mem, skipped = Z.members(zf)
        assert [m.name for m in mem] == ["a.pdf"]
        assert {s.name: s.reason.split(" ")[0] for s in skipped} == {"inner.zip": "verschachteltes",
                                                                    "notiz.txt": "keine", "link.pdf": "Verknüpfung"}
        with pytest.raises(Z.ZipRejected, match="kein PDF"):
            zf2 = zipfile.ZipFile(io.BytesIO(make_zip({"fake.pdf": b"MZ\x90 keine pdf"})))
            Z.read_member(zf2, Z.Member(0, "fake.pdf", 10, 10))


def test_long_names_keep_extension() -> None:
    long = "Legacy_Wertpapierdokumente_2021-11-17_Kosteninformation_zu_Wertpapier_" + "X" * 300 + "_zu_Depot_1.pdf"
    n = Z.display_name(long)
    assert n.endswith(".pdf") and len(n) <= 240
    assert Z.display_name("a/b\\c/../d.pdf") == "d.pdf"


def test_upload_limit_for_dkb_zip_path_only() -> None:
    assert body_limit({"path": "/settings/datasources/7/dkb/zip", "root_path": ""}) == 101 * 1024 * 1024
    assert body_limit({"path": "/settings/datasources/7/dkb/settings", "root_path": ""}) == 1_000_000
    assert body_limit({"path": "/settings/datasources/x/dkb/zip", "root_path": ""}) == 1_000_000


# ----------------------------------------------------------------------------------------------------
# Lauf: Ablage, Index, Bericht, Dubletten, Reihenfolge
# ----------------------------------------------------------------------------------------------------

def sample_entries() -> dict[str, bytes]:
    """Absichtlich unsortiert, in Unterordnern, mit Dublette unter anderem Namen und mit Fremddateien."""
    pdfs = {k: X.pdf_of(v[0]) for k, v in X.GOLDEN.items()}
    return {
        "Wertpapierdokumente/z_" + "ende.pdf": pdfs["verkauf"],
        "Wertpapierdokumente/Kosten/b.pdf": pdfs["kosten_legacy"],
        "a_kauf.pdf": pdfs["kauf"],
        "Wertpapierdokumente/Kapital/c.pdf": pdfs["reverse_split"],
        "Kontoauszuege/Kontoauszug_2025-01.pdf": X.pdf_of(X.kontoauszug()),
        "Doppelt/a_kauf(1).pdf": pdfs["kauf"],
        "Wertpapierdokumente/e.pdf": pdfs["dividende"],
        "Wertpapierdokumente/readme.txt": b"nicht ablegen",
        "Wertpapierdokumente/f.pdf": pdfs["depotauszug"],
        "g.pdf": b"%PDF-1.4\nkaputt",
    }


def test_zip_upload_end_to_end_and_idempotent(config: Config, master, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    c, sid = setup_app(config)
    try:
        before = rows(c, "SELECT COUNT(*) AS n FROM bank_doc")[0]["n"]
        r = upload(c, sid, make_zip(sample_entries()))
        assert r.status_code == 303 and r.headers["location"].startswith(f"/settings/datasources/{sid}/dkb/zip/")
        rid = run_id(r)
        run = finish(c, sid, rid)
        s = run["summary"]
        assert run["status"] == "partial", run["message"]  # g.pdf nicht lesbar
        assert s["stored"] == 6 and sum(s["by_kind"].values()) == 6
        assert s["by_kind"] == {"abrechnung_kauf": 1, "abrechnung_verkauf": 1, "kapitalmassnahme": 1, "ertrag": 1,
                                "depotauszug": 1} | {"kosteninfo_order": 1}
        assert s["by_sub"] == {"kapitalmassnahme:reverse_split": 1, "ertrag:dividende": 1}
        assert [x["name"] for x in s["unrecognized"]] == ["Kontoauszug_2025-01.pdf"]
        assert [x["name"] for x in s["duplicate_in_zip"]] == ["a_kauf(1).pdf"]
        assert [x["name"] for x in s["unreadable"]] == ["g.pdf"]
        assert [x["name"] for x in s["skipped"]] == ["readme.txt"]
        assert s["depots"] == {X.DEPOT: 6}
        # Ablage nur unter selbst gebautem Pfad, Index vollständig, nichts gebucht
        root = D.root_dir(ctx(c), sid, "DKB")
        assert doc_files(root) == sorted([
            "Wertpapierdokumente/Depot-2345/2021-11-17_kosteninfo_order_A2QA4J-090045.pdf",
            "Wertpapierdokumente/Depot-2345/2024-07-01_depotauszug_2345-20240701.pdf",
            "Wertpapierdokumente/Depot-2345/2024-08-06_abrechnung_kauf_1234567800.pdf",
            "Wertpapierdokumente/Depot-2345/2024-10-14_abrechnung_verkauf_6543210900.pdf",
            "Wertpapierdokumente/Depot-2345/2025-02-17_kapitalmassnahme_61432975.pdf",
            "Wertpapierdokumente/Depot-2345/2025-03-18_ertrag_57764418230.pdf"])
        docs = rows(c, "SELECT * FROM bank_doc ORDER BY id")
        assert len(docs) == before + 6
        # nur die Abrechnungen sind ausgewertet
        assert sorted(d["status"] for d in docs) == ["classified"] * 4 + ["evaluated"] * 2
        assert all(d["category"] == "Wertpapierdokumente" and d["sha256"] and d["size"] for d in docs)
        tr = s["trades"]  # Asset der Beispiel-Abrechnungen ist unbekannt → Vorschläge im Prüf-Stapel, nichts zugeordnet
        assert (len(tr["matched"]), len(tr["new"]), len(tr["unclear"])) == (0, 2, 0)
        assert rows(c, "SELECT COUNT(*) AS n FROM document")[0]["n"] == 2
        assert rows(c, "SELECT COUNT(*) AS n FROM journal_tx")[0]["n"] == 0  # nichts gebucht
        # Zwischendatei nach erfolgreichem Lauf gelöscht
        assert not list((Path(ctx(c).config.data_dir) / "tmp").glob("dkbzip-*.zip"))
        # Seite und CSV-Bericht
        page = c.get(r.headers["location"])
        assert page.status_code == 200 and "Abrechnungen" in page.text and "Nicht erkannt" in page.text
        assert "Kontoauszug_2025-01.pdf" in page.text and "Fertig in" in page.text
        csv = c.get(r.headers["location"] + "/report.csv")
        assert csv.status_code == 200 and "Kontoauszug_2025-01.pdf" in csv.text
        assert "text/csv" in csv.headers["content-type"]
        # zweites Hochladen desselben Archivs: nichts Neues
        r2 = upload(c, sid, make_zip(sample_entries()))
        run2 = finish(c, sid, run_id(r2))
        s2 = run2["summary"]
        assert s2["stored"] == 0 and len(s2["duplicate_known"]) == 6 and len(s2["duplicate_in_zip"]) == 1
        assert len(doc_files(root)) == 6
        assert rows(c, "SELECT COUNT(*) AS n FROM bank_doc")[0]["n"] == before + 6
        # Datenschutz: weder Dokumentzeilen noch Personendaten in Logs
        log = "\n".join(rec.getMessage() for rec in caplog.records)
        for private in ("Mustermann", "Beispielweg", "11112222", "Ausmachender Betrag"):
            assert private not in log
    finally:
        c.__exit__(None, None, None)


def test_result_independent_of_order_in_zip(config: Config, master, tmp_path: Path) -> None:
    entries = {k: v for k, v in sample_entries().items()
               if k.endswith(".pdf") and "Kontoauszug" not in k and k != "g.pdf"}
    outs = []
    for order in (list(entries), list(reversed(list(entries)))):
        n = len(outs)
        cfg = Config(**{**config.__dict__, "data_dir": tmp_path / str(n), "import_dir": tmp_path / f"i{n}"})
        cfg.data_dir.mkdir(parents=True)
        cfg.import_dir.mkdir(parents=True)
        c, sid = setup_app(cfg)
        try:
            run = finish(c, sid, run_id(upload(c, sid, make_zip({k: entries[k] for k in order}))))
            outs.append((run["summary"]["by_kind"], doc_files(D.root_dir(ctx(c), sid, "DKB"))))
        finally:
            c.__exit__(None, None, None)
    assert outs[0] == outs[1]


def test_second_run_while_running_is_refused_and_other_source_untouched(config: Config, master, monkeypatch) -> None:
    c, sid = setup_app(config)
    gate = threading.Event()
    real = R.extract_isolated

    def slow(data, **kw):
        gate.wait(10)
        return real(data, **kw)

    monkeypatch.setattr(R, "extract_isolated", slow)
    try:
        r = upload(c, sid, make_zip({"a.pdf": X.pdf_of(X.abrechnung("Kauf"))}))
        assert r.status_code == 303
        r2 = upload(c, sid, make_zip({"b.pdf": X.pdf_of(X.abrechnung("Verkauf"))}))
        assert r2.status_code == 303 and "error=" in r2.headers["location"] and "l%C3%A4uft" in r2.headers["location"]
        page = c.get(r.headers["location"])  # Seite darf während des Laufs geöffnet werden
        assert page.status_code == 200 and "Abbrechen" in page.text
        gate.set()
        assert finish(c, sid, run_id(r))["status"] == "ok"
    finally:
        gate.set()
        c.__exit__(None, None, None)


def test_rejected_archive_gives_message_and_no_run(config: Config, master) -> None:
    c, sid = setup_app(config)
    try:
        r = upload(c, sid, b"%PDF-1.4 das ist kein archiv" + b"x" * 200, "x.zip")
        assert r.status_code == 303 and "error=" in r.headers["location"]
        assert rows(c, "SELECT COUNT(*) AS n FROM bank_run")[0]["n"] == 0
        assert not list((Path(ctx(c).config.data_dir) / "tmp").glob("dkbzip-*.zip"))
        xhr = c.post(f"/settings/datasources/{sid}/dkb/zip", files={"file": ("x.zip", b"kein zip" * 20)},
                     data={"csrf_token": c.token}, headers={"X-Requested-With": "XMLHttpRequest"})
        assert xhr.status_code == 400 and "kein ZIP" in xhr.json()["error"]
        ok = c.post(f"/settings/datasources/{sid}/dkb/zip", data={"csrf_token": c.token}, follow_redirects=False)
        assert ok.status_code == 303 and "ausw" in ok.headers["location"]
    finally:
        c.__exit__(None, None, None)


def test_upload_size_cap(config: Config, master, monkeypatch) -> None:
    c, sid = setup_app(config)
    monkeypatch.setattr(R, "UPLOAD_MAX", 1000)
    try:
        r = upload(c, sid, make_zip({"a.pdf": X.pdf_of(X.abrechnung("Kauf"))}))
        assert "error=" in r.headers["location"] and "zu+gro" in r.headers["location"]
        assert not list((Path(ctx(c).config.data_dir) / "tmp").glob("dkbzip-*.zip"))
    finally:
        c.__exit__(None, None, None)


# ----------------------------------------------------------------------------------------------------
# Abbruch, Fortsetzen, Neustart
# ----------------------------------------------------------------------------------------------------

def test_cancel_then_resume_processes_only_the_rest(config: Config, master, monkeypatch) -> None:
    c, sid = setup_app(config)
    pdfs = [X.pdf_of(X.GOLDEN[k][0]) for k in ("kauf", "verkauf", "dividende", "reverse_split", "depotauszug")]
    calls: list[int] = []
    real = R.extract_isolated

    def stop_after_two(data, **kw):
        calls.append(1)
        if len(calls) == 3:
            D._RUNNING[sid].cancel.set()  # Abbruch während des dritten Dokuments
        return real(data, **kw)

    monkeypatch.setattr(R, "extract_isolated", stop_after_two)
    try:
        r = upload(c, sid, make_zip({f"{i}.pdf": p for i, p in enumerate(pdfs)}))
        rid = run_id(r)
        run = finish(c, sid, rid)
        assert run["status"] == "cancelled" and run["resumable"], run
        stored = run["summary"]["stored"]
        assert 1 <= stored < 5
        assert list((Path(ctx(c).config.data_dir) / "tmp").glob("dkbzip-*.zip"))  # Zwischendatei bleibt
        page = c.get(r.headers["location"])
        assert "Fortsetzen" in page.text and "Verwerfen" in page.text
        # Fortsetzen
        monkeypatch.setattr(R, "extract_isolated", real)
        res = c.post(f"{r.headers['location']}/resume", data={"csrf_token": c.token}, follow_redirects=False)
        assert res.status_code == 303
        rid2 = run_id(res)
        assert rid2 != rid
        run2 = finish(c, sid, rid2)
        assert run2["status"] == "ok", run2["message"]
        assert run2["summary"]["stored"] == 5 and sum(run2["summary"]["by_kind"].values()) == 5
        assert not run2["summary"]["duplicate_known"]  # eigene frühere Ablage zählt nicht als Dublette
        assert rows(c, "SELECT COUNT(*) AS n FROM bank_doc")[0]["n"] == 5
        assert not list((Path(ctx(c).config.data_dir) / "tmp").glob("dkbzip-*.zip"))
        assert not R.view(ctx(c), sid, rid)["resumable"]
    finally:
        c.__exit__(None, None, None)


def test_discard_removes_temp_file_and_resume_without_it_explains(config: Config, master, monkeypatch) -> None:
    c, sid = setup_app(config)
    real = R.extract_isolated

    def cancel_first(data, **kw):
        D._RUNNING[sid].cancel.set()
        return real(data, **kw)

    monkeypatch.setattr(R, "extract_isolated", cancel_first)
    try:
        r = upload(c, sid, make_zip({"a.pdf": X.pdf_of(X.abrechnung("Kauf")), "b.pdf": X.pdf_of(X.ertrag())}))
        rid = run_id(r)
        assert finish(c, sid, rid)["status"] == "cancelled"
        d = c.post(f"{r.headers['location']}/discard", data={"csrf_token": c.token}, follow_redirects=False)
        assert d.status_code == 303 and "msg=" in d.headers["location"]
        assert not list((Path(ctx(c).config.data_dir) / "tmp").glob("dkbzip-*.zip"))
        res = c.post(f"{r.headers['location']}/resume", data={"csrf_token": c.token}, follow_redirects=False)
        assert "error=" in res.headers["location"] and "erneut+hochladen" in res.headers["location"]
    finally:
        c.__exit__(None, None, None)


def test_interrupted_run_after_restart_is_reported(config: Config, master) -> None:
    c, sid = setup_app(config)
    try:
        zp = Path(ctx(c).config.data_dir) / "tmp" / "dkbzip-1-x.zip"
        zp.parent.mkdir(parents=True, exist_ok=True)
        zp.write_bytes(make_zip({"a.pdf": X.pdf_of(X.abrechnung("Kauf"))}))
        summary = R._summary_new("x.zip", zp, [])
        import json
        rid = int(ctx(c).db.x("INSERT INTO bank_run(source_id, status, message, started_at, summary_json) VALUES "
                              "(?, 'classifying', 'läuft', ?, ?)", (sid, D._now(), json.dumps(summary))).lastrowid)
        run = R.view(ctx(c), sid, rid)
        assert run["status"] == "error" and "Unterbrochen" in run["message"] and run["resumable"] and not run["active"]
        assert c.get(f"/settings/datasources/{sid}/dkb/zip/{rid}").status_code == 200
        assert c.get(f"/settings/datasources/{sid}/dkb/zip/9999").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_dkb_page_has_upload_and_lists_runs(config: Config, master) -> None:
    c, sid = setup_app(config)
    try:
        page = c.get(f"/settings/datasources/{sid}/dkb")
        assert "ZIP-Import: Wertpapierdokumente" in page.text and "dkb_zip.js" in page.text
        upload(c, sid, make_zip({"a.pdf": X.pdf_of(X.depotauszug())}))
        wait(sid, 60)
        page = c.get(f"/settings/datasources/{sid}/dkb")
        assert "Letzte ZIP-Läufe" in page.text and "Wertpapierdokumente.zip" in page.text
        assert "1 erkannt (noch nicht ausgewertet)" in page.text
        assert os.path.exists("app/static/js/dkb_zip.js")
    finally:
        c.__exit__(None, None, None)


def test_unknown_source_and_non_dkb_source_are_404(config: Config, master) -> None:
    c, _sid = setup_app(config)
    try:
        assert upload(c, 999, make_zip({"a.pdf": b"%PDF-"})).status_code == 404
        assert c.get("/settings/datasources/999/dkb/zip/1").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_progress_job_visible_globally_while_running(config: Config, master, monkeypatch) -> None:
    from app.progress import active

    c, sid = setup_app(config)
    gate = threading.Event()
    real = R.extract_isolated

    def slow(data, **kw):
        gate.wait(10)
        return real(data, **kw)

    monkeypatch.setattr(R, "extract_isolated", slow)
    try:
        r = upload(c, sid, make_zip({"a.pdf": X.pdf_of(X.abrechnung("Kauf"))}))
        end = time.monotonic() + 10
        items = []
        while time.monotonic() < end:
            items = [i for i in active(ctx(c)) if i["id"].startswith("job:dkb_zip")]
            if items:
                break
            time.sleep(0.05)
        assert items and items[0]["href"] == r.headers["location"]
        assert "eta_text" in items[0] and items[0]["unit"] == "Dokumente"
        gate.set()
        finish(c, sid, run_id(r))
        assert not [i for i in active(ctx(c)) if i["id"].startswith("job:dkb_zip")]
    finally:
        gate.set()
        c.__exit__(None, None, None)
