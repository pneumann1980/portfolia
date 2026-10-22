# ruff: noqa: F811  (Fixture ``master`` aus test_bitpanda wird importiert und als Argument verwendet)
"""DKB-Abruf in Portfolia: Plugin-Installer, Abruf per Knopfdruck, Ablage, Index, Auswertung der Depot-Abrechnungen.

Der Fetcher (GPL) wird durch ein kleines Ersatzprogramm mit derselben Zeilenschnittstelle ersetzt; die Installation
läuft echt (venv + pip) mit einem lokal erzeugten Wheel – ohne Netzwerk. Die Bank wird nie kontaktiert."""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import textwrap
import time
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.datasources import dkb as D
from app.datasources import plugins as P
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests.docfixtures import text_pdf
from tests.helpers import ASSETS, tx
from tests.test_bitpanda import ctx, master, post  # noqa: F401

PASSWORD = "Sehr-Geheim-4711"
SAP = {"asset_id": "SAP", "name": "SAP SE", "asset_class": "security", "quote_source": "yahoo", "quote_id": "SAP.DE",
       "isin": "DE0007164600", "wkn": "716460"}
ORDER = ["Deutsche Kreditbank AG", "Wertpapierabrechnung Kauf", "Depot  1234567890",
         "Handelstag  02.01.2025  Handelszeit  09:15:03", "ISIN  DE0007164600  Stück  10", "SAP SE Inhaber-Aktien",
         "Kurs  123,45 EUR  Kurswert  1.234,50 EUR", "Provision  4,90 EUR", "Ausmachender Betrag  1.239,40 EUR",
         "Auftragsnummer  A-778899"]
STATEMENT = ["Deutsche Kreditbank AG", "Kontoauszug 01/2025", "IBAN DE00 1234 5678 9012 3456 78", "Saldo 1.000,00 EUR"]

FETCHER = textwrap.dedent('''
    import base64, json, os, sys, time
    args = sys.argv[1:]
    BAKED = __BAKED__
    sc = BAKED if BAKED is not None else json.load(open(args[0]))
    if "--version" in args:
        print(json.dumps({"version": "0.0.1", "libs": {"dkb_robo": "0.31.2"}}))
        sys.exit(0)
    out = args[args.index("--out") + 1]
    known = set(json.load(open(args[args.index("--known") + 1])))
    creds = json.loads(sys.stdin.readline())
    if sc.get("seen"):
        json.dump({"user": creds["user"], "pw_in_argv": any(creds["password"] in a for a in sys.argv),
                   "pw_in_env": any(creds["password"] in v for v in os.environ.values()),
                   "env_keys": sorted(k for k in os.environ if k.startswith("PORTFOLIA")), "cwd": os.getcwd(),
                   "argv": args}, open(sc["seen"], "w"))
    def emit(event, **kw):
        print(json.dumps({"event": event, **kw}), flush=True)
    emit("hello", fetcher="0.0.1", dkb_robo="0.31.2")
    emit("login")
    emit("awaiting_2fa", timeout_s=50)
    if sc.get("error"):
        emit("error", kind=sc["error"], message="Fehlertext " + creds["password"])
        sys.exit(2)
    emit("logged_in", accounts=2)
    todo = [d for d in sc["docs"] if d["id"] not in known]
    emit("listed", total=len(sc["docs"]), new=len(todo))
    if sc.get("sleep"):
        time.sleep(sc["sleep"])
    for d in todo:
        rel = f"{d['category']}/{d['account']}/{d['date']}_{d['name']}"
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(base64.b64decode(d["data"]))
        emit("document", id=d["id"], category=d["category"], doctype=d["doctype"], account=d["account"],
             date=d["date"], subject=d["name"], rel=rel, size=0, checksum_ok=True, status="downloaded")
    for ev in sc.get("raw_events", []):
        print(json.dumps(ev), flush=True)
    emit("done", downloaded=len(todo), existing=0, failed=0, cancelled=False)
''')


def doc(id: str, category: str, doctype: str, account: str, date: str, name: str, data: bytes) -> dict:
    return {"id": id, "category": category, "doctype": doctype, "account": account, "date": date, "name": name,
            "data": base64.b64encode(data).decode()}


def default_docs() -> list[dict]:
    return [doc("o1", "Wertpapierdokumente", "dwpOrderStatement", "1234567890", "2025-01-02", "Abrechnung_Kauf.pdf",
                text_pdf(ORDER)),
            doc("k1", "Kontoauszüge", "bankAccountStatement", "DE00123456789012345678", "2025-01-31", "Kontoauszug.pdf",
                text_pdf(STATEMENT)),
            doc("x1", "Wertpapierdokumente", "exAnteCostInformation", "1234567890", "2025-01-02", "Kosten.pdf",
                text_pdf(["Kosteninformation vorab"]))]


def make_wheel(path: Path, baked: dict | None, version: str = "0.0.1") -> Path:
    src = FETCHER.replace("__BAKED__", repr(baked))
    name = f"portfolia_dkb_fetch-{version}"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("portfolia_dkb_fetch/__init__.py", f'__version__ = "{version}"\n')
        z.writestr("portfolia_dkb_fetch/__main__.py", src)
        z.writestr(f"{name}.dist-info/METADATA",
                   f"Metadata-Version: 2.1\nName: portfolia-dkb-fetch\nVersion: {version}\n")
        z.writestr(f"{name}.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
                                              "Tag: py3-none-any\n")
        z.writestr(f"{name}.dist-info/RECORD", "")
    return path


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    """Ersatz-Fetcher als Skript; ``sc`` steuert, was er meldet."""
    script = tmp_path / "fetcher.py"
    script.write_text(FETCHER.replace("__BAKED__", "None"))
    sc: dict = {"docs": default_docs(), "seen": str(tmp_path / "seen.json")}
    path = tmp_path / "scenario.json"

    def write() -> None:
        path.write_text(json.dumps(sc))

    write()
    monkeypatch.setattr(D, "fetcher_command", lambda c: [sys.executable, str(script), str(path)])
    monkeypatch.setattr(D, "ready", lambda c: True)
    sc["_write"] = write  # type: ignore[assignment]
    return sc


def commit(sc: dict) -> None:
    sc.pop("_write")()  # type: ignore[operator]
    sc["_write"] = lambda: None  # pragma: no cover


def setup_app(config: Config):
    """Import mit SAP (für die Abrechnung) und ein DKB-Konto; liefert (Client, sid)."""
    build_zip(config.import_dir / "imp.zip", transactions=[
        tx("D1", "2024-12-01T10:00:00Z", "deposit", to=("DKB", "EUR", "5000"), value="5000")],
        assets=[*ASSETS, SAP], generated_at="2025-03-01T00:00:00Z", valuation_date="2024-12-31")
    old = time.time() - 3600
    os.utime(config.import_dir / "imp.zip", (old, old))
    c = TestClient(build_app(config, start_scheduler=False))
    c.__enter__()
    c.get("/settings")
    c.token = c.cookies.get("portfolia_csrf")
    assert tasks.import_check(ctx(c), "test").status == "imported"
    r = post(c, "/settings/datasources", kind="exchange", provider="dkb", name="DKB", account="DKB",
             sync_interval_min="0")
    assert r.status_code == 303, r.text[:500]
    sid = int(r.headers["location"].split("/settings/datasources/")[1].split("?")[0].split("#")[0])
    return c, sid


def wait(sid: int, seconds: float = 30) -> None:
    end = time.monotonic() + seconds
    while D.active(sid) and time.monotonic() < end:
        time.sleep(0.05)
    assert not D.active(sid)


def rows(c, sql: str, *a):
    return [dict(r) for r in ctx(c).db.q(sql, a)]


def db_bytes(c) -> bytes:
    p = Path(ctx(c).db.path)
    wal = Path(str(p) + "-wal")
    return p.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


# ----------------------------------------------------------------------------------------------------
# Plugin-Installer (echte venv + pip, lokales Wheel)
# ----------------------------------------------------------------------------------------------------

def test_plugin_install_update_failure_keeps_old_and_remove(config: Config, tmp_path, monkeypatch) -> None:
    whl = make_wheel(tmp_path / "w1" / "portfolia_dkb_fetch-0.0.1-py3-none-any.whl", {"docs": []}) \
        if (tmp_path / "w1").mkdir() is None else None
    monkeypatch.setenv(P.PLUGINS["dkb"].spec_env, str(whl))
    assert not P.installed(config, "dkb") and P.status(config, "dkb")["installed"] is False
    res = P.install(config, "dkb")
    assert res["ok"], res
    st = P.status(config, "dkb")
    assert st["installed"] and st["version"] == "0.0.1" and st["libs"] == {"dkb_robo": "0.31.2"}
    assert (Path(config.plugins_dir) / "dkb" / "venv" / "bin" / "python").exists()
    assert not (Path(config.plugins_dir) / "dkb" / "venv.new").exists()
    out = os.popen(" ".join([*P.command(config, "dkb"), "--version"])).read()
    assert json.loads(out)["version"] == "0.0.1"
    # fehlgeschlagene Aktualisierung (Quelle gibt es nicht) lässt die bestehende Installation unberührt
    monkeypatch.setenv(P.PLUGINS["dkb"].spec_env, str(tmp_path / "gibt-es-nicht.whl"))
    bad = P.install(config, "dkb")
    assert not bad["ok"] and "fehlgeschlagen" in bad["message"]
    assert P.installed(config, "dkb") and P.status(config, "dkb")["version"] == "0.0.1"
    assert not (Path(config.plugins_dir) / "dkb" / "venv.new").exists()
    # Entfernen löscht den Ordner rückstandsfrei
    assert P.remove(config, "dkb") and not (Path(config.plugins_dir) / "dkb").exists()
    assert not P.installed(config, "dkb")


def test_plugin_env_is_cleaned_and_default_spec_points_to_contrib(config: Config, monkeypatch) -> None:
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", "geheim")
    monkeypatch.setenv("PORTFOLIA_DS_BITPANDA", "geheim")
    monkeypatch.setenv("COINGECKO_API_KEY", "geheim")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    env = P.clean_env()
    assert "geheim" not in "".join(env.values()) and env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["PYTHONNOUSERSITE"] == "1"
    monkeypatch.delenv(P.PLUGINS["dkb"].spec_env, raising=False)
    spec = P.PLUGINS["dkb"].spec()
    assert "subdirectory=contrib/portfolia-dkb-fetch" in spec and "archive/refs/tags/v" in spec  # ohne Image-Commit
    monkeypatch.setattr(P, "REVISION", "0123456789abcdef0123456789abcdef01234567")
    assert "/archive/0123456789abcdef0123456789abcdef01234567.tar.gz#" in P.PLUGINS["dkb"].spec()
    assert P.PLUGINS["dkb"].pins == ("dkb-robo==0.31.2",)


# ----------------------------------------------------------------------------------------------------
# Abruf per Knopfdruck
# ----------------------------------------------------------------------------------------------------

def test_run_downloads_indexes_evaluates_and_never_stores_the_password(config: Config, master, scenario,
                                                                       caplog) -> None:
    caplog.set_level(logging.DEBUG)
    commit(scenario)
    c, sid = setup_app(config)
    try:
        res = D.start(ctx(c), sid, "kunde1", PASSWORD)
        assert res["errors"] == []
        wait(sid)
        run = D.view_run(ctx(c).db, sid)
        assert run["status"] == "ok", run
        assert "3 neue Dokumente" in run["message"]
        root = Path(config.documents_dir) / "DKB"
        assert (root / "Wertpapierdokumente" / "1234567890" / "2025-01-02_Abrechnung_Kauf.pdf").is_file()
        assert (root / "Kontoauszüge" / "DE00123456789012345678" / "2025-01-31_Kontoauszug.pdf").is_file()
        docs = {r["doc_id"]: r for r in rows(c, "SELECT * FROM bank_doc WHERE source_id=?", sid)}
        assert set(docs) == {"o1", "k1", "x1"}
        assert docs["o1"]["status"] == "evaluated" and docs["k1"]["status"] == "archived"
        assert docs["x1"]["status"] == "archived" and len(docs["o1"]["sha256"]) == 64 and docs["o1"]["size"] > 100
        # Depot-Abrechnung → Vorschlag im Prüf-Stapel, gebucht wird nichts
        csv_rows = rows(c, "SELECT status, rec_json FROM csv_row")
        assert len(csv_rows) == 1 and csv_rows[0]["status"] in ("new", "duplicate", "unclear", "invalid")
        rec = json.loads(csv_rows[0]["rec_json"])
        assert rec["row"]["to_asset"] == "SAP" and Decimal(rec["row"]["value_eur"]) == Decimal("1234.50")
        assert ctx(c).db.scalar("SELECT COUNT(*) FROM journal_tx", default=0) == 0
        assert run["summary"]["evaluated"]["evaluated"] == 1 and run["summary"]["evaluated"]["transactions"] == 1
        # Zugangsdaten: nur Benutzername in der Datenbank, nie das Passwort – weder in der Datenbank noch im Protokoll
        assert PASSWORD.encode() not in db_bytes(c) and PASSWORD not in caplog.text
        assert D.settings(ctx(c).db, sid)["username"] == "kunde1"
        seen = json.loads(Path(json.loads(Path(scenario["seen"]).read_text()) and scenario["seen"]).read_text())
        assert seen["user"] == "kunde1" and not seen["pw_in_argv"] and not seen["pw_in_env"]  # nur per stdin
        assert seen["env_keys"] == [] and Path(seen["cwd"]).resolve() == root.resolve()
        assert "--known" in seen["argv"] and "--out" in seen["argv"]
        # zweiter Lauf: nichts wird erneut geladen, nichts doppelt ausgewertet
        D.start(ctx(c), sid, "kunde1", PASSWORD)
        wait(sid)
        run2 = D.view_run(ctx(c).db, sid)
        assert run2["status"] == "ok" and "0 neue Dokumente" in run2["message"]
        assert len(rows(c, "SELECT 1 FROM bank_doc WHERE source_id=?", sid)) == 3
        assert len(rows(c, "SELECT 1 FROM csv_row")) == 1
        assert not list((ctx(c).config.data_dir / "tmp").glob("dkb-known-*"))
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("kind", ["login", "2fa_timeout", "2fa_canceled", "network", "changed"])
def test_errors_are_explained_and_nothing_is_stored(config: Config, master, scenario, kind) -> None:
    scenario["error"] = kind
    commit(scenario)
    c, sid = setup_app(config)
    try:
        D.start(ctx(c), sid, "kunde1", PASSWORD)
        wait(sid)
        run = D.view_run(ctx(c).db, sid)
        assert run["status"] == "error" and D.KIND_TEXT[kind][:30] in run["message"]
        assert PASSWORD not in run["message"] and not rows(c, "SELECT 1 FROM bank_doc")
    finally:
        c.__exit__(None, None, None)


def test_cancel_stops_the_process_and_keeps_loaded_documents(config: Config, master, scenario) -> None:
    scenario["sleep"] = 60
    commit(scenario)
    c, sid = setup_app(config)
    try:
        D.start(ctx(c), sid, "kunde1", PASSWORD)
        end = time.monotonic() + 15
        while time.monotonic() < end and (D.view_run(ctx(c).db, sid) or {}).get("status") != "fetching":
            time.sleep(0.05)
        assert D.view_run(ctx(c).db, sid)["status"] == "fetching" and D.cancel(sid)
        wait(sid, 20)
        run = D.view_run(ctx(c).db, sid)
        assert run["status"] == "cancelled" and "Abgebrochen" in run["message"]
        assert D.cancel(sid) is False  # es läuft nichts mehr
    finally:
        c.__exit__(None, None, None)


def test_paths_from_the_fetcher_are_validated(config: Config, master, scenario) -> None:
    scenario["docs"] = []
    scenario["raw_events"] = [
        {"event": "document", "id": "evil1", "rel": "../../evil.pdf", "category": "Kontoauszüge", "doctype": "x"},
        {"event": "document", "id": "evil2", "rel": "/etc/passwd", "category": "Kontoauszüge", "doctype": "x"},
        {"event": "document", "id": "evil3", "rel": "gibt/es/nicht.pdf", "category": "Kontoauszüge", "doctype": "x"},
    ]
    commit(scenario)
    c, sid = setup_app(config)
    try:
        D.start(ctx(c), sid, "kunde1", PASSWORD)
        wait(sid)
        run = D.view_run(ctx(c).db, sid)
        assert not rows(c, "SELECT 1 FROM bank_doc") and run["summary"]["failed"] == 3
        assert run["status"] == "partial" or run["status"] == "ok"
    finally:
        c.__exit__(None, None, None)


def test_start_is_refused_without_setup_credentials_or_for_other_sources(config: Config, master, scenario,
                                                                          monkeypatch) -> None:
    commit(scenario)
    c, sid = setup_app(config)
    try:
        assert "Benutzername und Passwort" in D.start(ctx(c), sid, "", "")["errors"][0]
        monkeypatch.setattr(D, "ready", lambda c: False)
        assert "noch nicht eingerichtet" in D.start(ctx(c), sid, "u", "p")["errors"][0]
        monkeypatch.setattr(D, "ready", lambda c: True)
        other = post(c, "/settings/datasources", kind="exchange", provider="bitpanda", name="BP", account="Bitpanda",
                     sync_interval_min="0")
        osid = int(other.headers["location"].split("/settings/datasources/")[1].split("?")[0].split("#")[0])
        assert "kein DKB-Abruf" in D.start(ctx(c), osid, "u", "p")["errors"][0]
        assert c.get(f"/settings/datasources/{osid}/dkb").status_code == 404
        D._RUNNING[sid] = D.Handle()
        try:
            assert "läuft bereits" in D.start(ctx(c), sid, "u", "p")["errors"][0]
        finally:
            D._RUNNING.pop(sid)
    finally:
        c.__exit__(None, None, None)


def test_evaluate_pending_without_login_and_interrupted_run_display(config: Config, master, scenario) -> None:
    scenario["docs"] = default_docs()
    commit(scenario)
    c, sid = setup_app(config)
    try:
        # ohne automatische Auswertung: Depot-Abrechnung bleibt „auszuwerten“, Auswertung separat ohne Anmeldung
        D.save_settings(ctx(c).db, sid, username="u", folder="", mfa_device=0, evaluate=False)
        D.start(ctx(c), sid, "u", PASSWORD)
        wait(sid)
        assert D.documents(ctx(c).db, sid)["pending"] == 1 and not rows(c, "SELECT 1 FROM csv_row")
        assert D.start_evaluate(ctx(c), sid)["errors"] == []
        wait(sid)
        assert D.documents(ctx(c).db, sid)["pending"] == 0 and len(rows(c, "SELECT 1 FROM csv_row")) == 1
        assert D.view_run(ctx(c).db, sid)["message"].startswith("1 Dokumente ausgewertet")
        # Neustart während eines Laufs: Anzeige „unterbrochen“, kein hängender Zustand
        ctx(c).db.x("INSERT INTO bank_run(source_id, status, message, started_at) VALUES (?, 'fetching', 'x', 'y')",
                    (sid,))
        v = D.view_run(ctx(c).db, sid)
        assert v["status"] == "error" and "Neustart" in v["message"] and not v["active"]
    finally:
        c.__exit__(None, None, None)


# ----------------------------------------------------------------------------------------------------
# Oberfläche
# ----------------------------------------------------------------------------------------------------

def test_button_appears_for_new_and_existing_dkb_accounts_and_page_flow(config: Config, master, scenario) -> None:
    commit(scenario)
    c, sid = setup_app(config)
    try:
        form = c.get(f"/settings/datasources/{sid}").text
        assert "DKB-Abruf einrichten" in form and f"/settings/datasources/{sid}/dkb" in form
        # bestehendes Konto mit anderem Anbieter („Andere Börse“), Name beginnt mit DKB
        r = post(c, "/settings/datasources", kind="exchange", provider="other_exchange", name="DKB Depot",
                 account="DKB Wertpapierdepot", sync_interval_min="0")
        sid2 = int(r.headers["location"].split("/settings/datasources/")[1].split("?")[0].split("#")[0])
        assert "DKB-Abruf einrichten" in c.get(f"/settings/datasources/{sid2}").text
        # kein Knopf bei fremdem Konto
        r = post(c, "/settings/datasources", kind="exchange", provider="bitpanda", name="BP", account="Bitpanda",
                 sync_interval_min="0")
        sid3 = int(r.headers["location"].split("/settings/datasources/")[1].split("?")[0].split("#")[0])
        assert "DKB-Abruf einrichten" not in c.get(f"/settings/datasources/{sid3}").text
        page = c.get(f"/settings/datasources/{sid}/dkb")
        assert page.status_code == 200 and "Passwort wird nicht gespeichert" in page.text
        assert "Struktur: Kategorie / Konto bzw. Depot" in page.text and "Unterordner" in page.text
        # Einstellungen
        bad = post(c, f"/settings/datasources/{sid}/dkb/settings", username="u", folder="../x", mfa_device="0")
        assert (bad.status_code == 303 and "Ordnername" in bad.headers["location"].replace("+", " ")) \
            or "error=" in bad.headers["location"]
        ok = post(c, f"/settings/datasources/{sid}/dkb/settings", username="meinname", folder="Bank DKB",
                  mfa_device="1", evaluate="1")
        assert ok.status_code == 303
        assert D.settings(ctx(c).db, sid) == {"username": "meinname", "folder": "Bank DKB", "mfa_device": 1,
                                               "evaluate": True}
        assert 'value="meinname"' in c.get(f"/settings/datasources/{sid}/dkb").text
        # Abruf per Knopfdruck über die Seite; Passwort nie in der Antwort
        r = post(c, f"/settings/datasources/{sid}/dkb/run", username="meinname", password=PASSWORD, mfa_device="1")
        assert r.status_code == 303 and PASSWORD not in r.headers["location"]
        wait(sid)
        page = c.get(f"/settings/datasources/{sid}/dkb").text
        assert "Letzter Abruf" in page and "Wertpapierdokumente" in page and "Kontoauszüge" in page
        assert PASSWORD not in page and (Path(config.documents_dir) / "Bank DKB").is_dir()
        # ohne Eingabe: Fehlermeldung statt Lauf
        r = post(c, f"/settings/datasources/{sid}/dkb/run", username="meinname", password="")
        assert "Passwort" in r.headers["location"].replace("%20", " ").replace("+", " ")
        # Einrichtung und Entfernen über die Seite (Installer ersetzt)
        called = []
        import app.datasources.dkb_web as W

        W.P.start_install = lambda cfg, pid: called.append(pid) or True  # type: ignore[assignment]
        assert post(c, f"/settings/datasources/{sid}/dkb/setup").status_code == 303 and called == ["dkb"]
    finally:
        c.__exit__(None, None, None)


def test_folder_state_reports_missing_mount(config: Config, master, scenario, tmp_path) -> None:
    commit(scenario)
    c, sid = setup_app(config)
    try:
        st = D.folder_state(ctx(c), sid, "DKB")
        assert st["writable"] and not st["mounted"] and st["in_data"]
        assert st["path"].endswith("/bank-documents/DKB")
        page = c.get(f"/settings/datasources/{sid}/dkb").text
        assert "nicht auf einer eigenen Freigabe" in page
    finally:
        c.__exit__(None, None, None)


def test_explain_names_status_and_technical_message() -> None:
    wrong = D.explain("login", "DKBRoboError: Login failed: 1st factor authentication failed. RC: 401")
    assert "vor der Freigabe" in wrong and "HTTP-Status 401" in wrong and "Anmeldename" in wrong
    assert "Technische Meldung: DKBRoboError: Login failed" in wrong and "Bot-Schutz" not in wrong
    # 503/400/502 beim ersten Schritt: Bot-Schutz der DKB (dkb-robo Issue #92), nicht Benutzername/Passwort
    for rc in (400, 502, 503):
        blocked = D.explain("login", f"Login failed: 1st factor authentication failed. RC: {rc}")
        assert "Bot-Schutz" in blocked and "kein Fehler bei Benutzername" in blocked and f"HTTP-Status {rc}" in blocked
        assert "Anmeldename" not in blocked
    assert "Bot-Schutz" in D.explain("blocked", "")
    assert "HTTP-Status" not in D.explain("network", "ConnectionError: timed out")
    assert D.explain("other", "").endswith(D.KIND_TEXT["other"])


def test_error_run_keeps_diagnosis_for_the_page(config: Config, master, scenario) -> None:
    scenario["error"] = "login"
    commit(scenario)
    c, sid = setup_app(config)
    try:
        D.start(ctx(c), sid, "kunde1", PASSWORD)
        wait(sid)
        run = D.view_run(ctx(c).db, sid)
        assert run["status"] == "error" and run["summary"]["diagnose"]["kind"] == "login"
        assert "Fehlertext" in run["message"] and PASSWORD not in run["message"] + json.dumps(run["summary"])
        page = c.get(f"/settings/datasources/{sid}/dkb").text
        assert "Technische Einzelheiten" in page and "Fehlertext" in page and PASSWORD not in page
    finally:
        c.__exit__(None, None, None)
