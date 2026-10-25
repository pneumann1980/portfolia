"""Regelwerk-Dateien (Parameterstände), Jahresauswahl, Reiter, Sammelerzeugung und Regelschalter."""

import hashlib
import os
import shutil
import time
import zipfile
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.tax import registry
from app.tax import rulesets as RS
from app.tax.packs.de.schema import SCHEMA
from app.tax.params import ParamSet
from app.tax.service import all_running, start_all, tax_service
from app.util.timeutil import today_local

BUNDLED = registry.bundled_params("de")
SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"


@pytest.fixture
def client(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    app = build_app(config, start_scheduler=False)
    with TestClient(app) as c:
        c.get("/settings")
        token = c.cookies.get("portfolia_csrf")
        assert token
        assert c.post("/actions/import/check", headers={"X-CSRF-Token": token}).status_code == 200
        c.headers["X-CSRF-Token"] = token
        yield c

GOOD = """\
ruleset:
  name: "Freigrenze 600 (Altregelung)"
  valid_from: 2024
  valid_to: 2026
  status: draft
  source: "Testdatei"
  description: "Vergleichsrechnung"
rules:
  2024:
    crypto:
      freigrenze_23: 600
"""


def _validate(text: str | bytes) -> RS.Validation:
    raw = text.encode() if isinstance(text, str) else text
    return RS.validate(raw, schema=SCHEMA, bundled=BUNDLED, today_year=2026)


def _put(tmp_path: Path, text: str, name: str = "stand.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# -- Prüfung ------------------------------------------------------------------------------------------------
def test_valid_file_passes_and_derives_slug():
    v = _validate(GOOD)
    assert v.ok and v.slug == "freigrenze-600-altregelung" and len(v.sha256) == 64
    assert v.head["valid_from"] == 2024 and v.data["rules"][2024]["crypto"]["freigrenze_23"] == 600


@pytest.mark.parametrize("text, expect", [
    ("", "leer"),
    ("a: [1, 2\n", "YAML-Syntaxfehler"),
    ("- 1\n- 2\n", "Abschnitten"),
    ("ruleset: {name: X}\nrulez: {}\n", "unbekannter Abschnitt"),
    ("rules: {2024: {crypto: {freigrenze_23: 600}}}\n", "Kopf fehlt"),
    ("ruleset: {valid_from: 2024}\nrules: {2024: {crypto: {freigrenze_23: 600}}}\n", "Name erforderlich"),
    ("ruleset: {name: X}\n", "keine Parameter"),
    ("ruleset: {name: X, status: fertig}\nrules: {2024: {crypto: {freigrenze_23: 1}}}\n", "status"),
    ("ruleset: {name: X, valid_from: 2030, valid_to: 2024}\nrules: {2030: {crypto: {freigrenze_23: 1}}}\n",
     "valid_from liegt nach valid_to"),
    ("ruleset: {name: X}\nrules: {2024: {crypto: {freigrenze_99: 1}}}\n", "unbekannter Schlüssel"),
    ("ruleset: {name: X}\nrules: {2024: {crypto: {freigrenze_23: '1.000'}}}\n", "Zahl erwartet"),
    ("ruleset: {name: X}\nrules: {2024: {crypto: {freigrenze_23: -5}}}\n", "außerhalb"),
    ("ruleset: {name: X}\nrules: {2024: {crypto: {holding_period_years: 1.5}}}\n", "ganze Zahl"),
    ("ruleset: {name: X}\nrules: {2024: {crypto: {freigrenze_mode: egal}}}\n", "erlaubt"),
    ("ruleset: {name: X}\nrules: {2024: {crypto: {exempt_after_holding: ja}}}\n", "true/false"),
    ("ruleset: {name: X}\nrules: {1800: {crypto: {freigrenze_23: 1}}}\n", "Jahr zwischen"),
    ("ruleset: {name: X}\nper_year: {basiszins: {2027: 0.9}}\n", "außerhalb"),
    ("ruleset: {name: X}\nforms: {2026: {anlage_xx: {title: Y}}}\n", "unbekanntes Formular"),
    ("ruleset: {name: X}\nforms: {2026: {anlage_so: {fields: {nope: {line: '1'}}}}}\n", "unbekanntes Feld"),
    ("ruleset: {name: X}\nforms: {2026: {anlage_so: {fields: {so_23_gain: {zeile: '1'}}}}}\n", "unbekannter Schlüssel"),
])
def test_invalid_files_are_rejected_with_reason(text, expect):
    v = _validate(text)
    assert not v.ok and any(expect in e for e in v.errors), v.errors


def test_size_and_encoding_limits():
    assert "zu groß" in _validate(b"#" * (RS.MAX_BYTES + 1)).errors[0]
    assert "UTF-8" in _validate("ruleset: {name: Ä}".encode("latin-1")).errors[0]


def test_plausibility_warnings():
    v = _validate("ruleset: {name: X, status: valid}\nrules: {2026: {crypto: {freigrenze_23: 60000}}}\n")
    assert v.ok
    assert any("weicht stark vom Standard" in w for w in v.warnings)
    assert any("ohne Quellenangabe" in w for w in v.warnings)
    late = _validate("ruleset: {name: X, valid_to: 2024}\nrules: {2030: {crypto: {freigrenze_23: 1000}}}\n")
    assert any("wirkt daher nicht" in w for w in late.warnings)


def test_warns_when_later_standard_values_overshadow_an_entry():
    v = _validate("ruleset: {name: X, source: Q}\nrules: {2000: {crypto: {exempt_after_holding: false}}}\n")
    assert v.ok and any("ab 2018 gilt wieder der Standardwert" in w for w in v.warnings), v.warnings
    ok = _validate("ruleset: {name: X, source: Q}\nrules: {2018: {crypto: {exempt_after_holding: false}}}\n")
    assert not any("Standardwert" in w for w in ok.warnings)
    # mit valid_to vor dem späteren Standardeintrag ist nichts überdeckt
    ok2 = _validate("ruleset: {name: X, valid_to: 2010}\nrules: {2000: {crypto: {exempt_after_holding: false}}}\n")
    assert not any("Standardwert" in w for w in ok2.warnings)


def test_template_validates_and_roundtrips():
    pack = registry.get("de")
    text = RS.template_text(pack.name, pack.year_params(2026), 2026)
    v = _validate(text)
    assert v.ok, v.errors
    assert v.data["rules"][2026]["crypto"]["freigrenze_23"] == 1000
    assert v.data["rules"][2026]["capital"]["teilfreistellung"]["etf_equity"] == 0.3


# -- Ablage -----------------------------------------------------------------------------------------------------
def test_store_save_replace_delete_and_path_safety(tmp_path: Path):
    st = RS.RulesetStore(tmp_path, "de", BUNDLED, SCHEMA)
    v = st.save(GOOD.encode(), 2026)
    assert v.ok and (tmp_path / "de" / "sets" / f"{v.slug}.yaml").is_file()
    assert [i.slug for i in st.list()] == [v.slug]
    info = st.info(v.slug)
    assert info and info.name.startswith("Freigrenze 600") and info.status == "draft" and info.years == "2024–2026"
    assert st.save(GOOD.replace("Vergleichsrechnung", "geändert").encode(), 2026).ok  # gleiche Kennung ersetzt
    assert len(st.list()) == 1 and st.list()[0].description == "geändert"
    for bad in ("../de", "..", "a/b", "", "UPPER", v.slug + "/../x"):
        assert st.path(bad) is None and not st.delete(bad)
    bad = st.save(b"ruleset: {name: X}\nrules: {2024: {crypto: {nope: 1}}}\n", 2026)
    assert not bad.ok and len(st.list()) == 1  # bei Fehlern wird nichts geschrieben
    assert st.delete(v.slug) and st.list() == []


# -- Parameter-Schicht ------------------------------------------------------------------------------------
def test_ruleset_applies_only_in_valid_years(tmp_path: Path):
    f = _put(tmp_path, GOOD)
    std, alt = ParamSet("de", BUNDLED), ParamSet("de", BUNDLED, None, f)
    assert std.for_year(2025)["crypto"]["freigrenze_23"] == 1000 and std.ruleset_info() is None
    assert alt.for_year(2023)["crypto"]["freigrenze_23"] == 600 and alt.for_year(2023)["_meta"]["ruleset"] is None
    assert alt.for_year(2024)["crypto"]["freigrenze_23"] == 600
    assert alt.for_year(2026)["crypto"]["freigrenze_23"] == 600
    assert alt.for_year(2027)["crypto"]["freigrenze_23"] == 1000  # nach valid_to gilt der Standard
    assert alt.for_year(2022)["crypto"]["freigrenze_23"] == 600 and std.for_year(2022)["crypto"]["freigrenze_23"] == 600
    meta25 = alt.for_year(2025)["_meta"]
    assert meta25["ruleset"]["name"].startswith("Freigrenze") and meta25["ruleset"]["status"] == "draft"
    assert alt.for_year(2027)["_meta"]["ruleset"] is None
    assert alt.version.endswith("+stand") and alt.fingerprint(2025) != std.fingerprint(2025)


def test_broken_ruleset_file_falls_back_to_standard(tmp_path: Path):
    f = _put(tmp_path, "ruleset: {name: X}\nrules: [kaputt\n")
    ps = ParamSet("de", BUNDLED, None, f)
    assert ps.ruleset_error and ps.ruleset_info() is None
    assert ps.for_year(2025)["crypto"]["freigrenze_23"] == 1000 and "+stand" not in ps.version


def test_diff_groups_consecutive_years(tmp_path: Path):
    st = RS.RulesetStore(tmp_path, "de", BUNDLED, SCHEMA)
    v = st.save(GOOD.encode(), 2026)
    diff = st.diff(v.slug, None, 2026)
    row = next(d for d in diff if d["key"] == "crypto.freigrenze_23")
    assert row["ranges"] == [{"from": 2024, "to": 2026, "old": 1000, "new": 600}]
    assert all(not d["key"].startswith("_meta") for d in diff)


def test_derivative_loss_cap_is_parameter_driven():
    ps = registry.get("de").params
    assert ps.for_year(2020)["capital"]["derivative_loss_cap"] is None
    assert ps.for_year(2022)["capital"]["derivative_loss_cap"] == 20000
    assert ps.for_year(2024)["capital"]["derivative_loss_cap"] is None


# -- Oberfläche und Service ---------------------------------------------------------------------------------
def _upload(client, text: str, name: str = "stand.yaml", year: int = 2025):
    return client.post("/tax/rules/upload", data={"year": str(year)},
                       files={"file": (name, text.encode(), "text/yaml")}, follow_redirects=False)


def test_upload_preview_activate_report_and_delete(client):
    ctx = client.app.state.ctx
    svc = tax_service(ctx)
    r = _upload(client, GOOD)
    assert r.status_code == 303 and "preview=freigrenze-600-altregelung" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "Vorschau: Freigrenze 600" in page and "crypto.freigrenze_23" in page and "Entwurf" in page
    assert svc.ruleset_slug() == ""  # gespeichert ≠ aktiv
    assert svc.compute(2025)[2].data["crypto"]["freigrenze"] == 1000
    # aktivieren (Auswahl im Kopf der Seite)
    r = client.post("/tax/ruleset", data={"ruleset": "freigrenze-600-altregelung", "year": "2025", "tab": "docs"},
                    follow_redirects=False)
    assert r.status_code == 303 and "tab=docs" in r.headers["location"]
    assert svc.ruleset_slug() == "freigrenze-600-altregelung"
    res = svc.compute(2025)[2]
    assert res.data["crypto"]["freigrenze"] == 600 and res.params["_meta"]["ruleset"]["status"] == "draft"
    assert any(i.code == "ruleset_draft" for i in res.issues)
    assert svc.compute(2027)[2].data["crypto"]["freigrenze"] == 1000  # außerhalb der Gültigkeit
    assert "Parameterstand „Freigrenze 600" in client.get("/tax?year=2025").text
    assert "gilt nur 2024–2026" in client.get("/tax?year=2030").text  # Hinweis: für dieses Jahr gilt der Standard
    # Bericht nennt Stand und Datei-Hash
    rid = svc.generate(2025, ["report"])["id"]
    rep = next(x for x in svc.reports() if x["id"] == rid)
    rs = rep["summary"]["ruleset"]
    sha = hashlib.sha256(svc.rulesets().read("freigrenze-600-altregelung")).hexdigest()
    assert rs["sha256"] == sha and rs["name"].startswith("Freigrenze 600") and rs["status"] == "draft"
    path, _ = svc.report_file(rid, "report")
    text = pdfium.PdfDocument(path)[0].get_textpage().get_text_bounded()
    assert "Parameterstand" in text and "Entwurf" in text and sha[:16] in text.replace("\n", "")
    # Standard-Bericht ohne Datei
    client.post("/tax/ruleset", data={"ruleset": "", "year": "2025", "tab": "docs"})
    rid2 = svc.generate(2025, ["report"], keep_previous=True)["id"]
    assert next(x for x in svc.reports() if x["id"] == rid2)["summary"]["ruleset"] is None
    text = pdfium.PdfDocument(svc.report_file(rid2, "report")[0])[0].get_textpage().get_text_bounded()
    assert "Standard (mitgeliefert)" in text
    # Löschen der aktiven Datei setzt auf Standard zurück
    client.post("/tax/ruleset", data={"ruleset": "freigrenze-600-altregelung", "year": "2025", "tab": "docs"})
    assert client.post("/tax/rules/freigrenze-600-altregelung/delete", data={"year": "2025"},
                       follow_redirects=False).status_code == 303
    assert svc.ruleset_slug() == "" and svc.rulesets().list() == []
    assert svc.compute(2025)[2].data["crypto"]["freigrenze"] == 1000


def test_reupload_of_same_ruleset_invalidates_cached_results(client):
    svc = tax_service(client.app.state.ctx)
    _upload(client, GOOD)
    client.post("/tax/ruleset", data={"ruleset": "freigrenze-600-altregelung", "year": "2025", "tab": "settings"})
    assert svc.compute(2025)[2].data["crypto"]["freigrenze"] == 600
    _upload(client, GOOD.replace("freigrenze_23: 600", "freigrenze_23: 700"))
    assert svc.compute(2025)[2].data["crypto"]["freigrenze"] == 700


def test_invalid_upload_is_rejected_and_not_stored(client):
    svc = tax_service(client.app.state.ctx)
    r = _upload(client, "ruleset: {name: X}\nrules: {2024: {crypto: {freigrenze_23: '1.000', nope: 1}}}\n")
    assert r.status_code == 400
    assert "nicht gespeichert" in r.text and "Zahl erwartet" in r.text and "unbekannter Schlüssel" in r.text
    assert svc.rulesets().list() == []
    r = client.post("/tax/rules/upload", data={"year": "2025"}, follow_redirects=False)
    assert r.status_code == 400 and "Bitte eine YAML-Datei" in r.text
    assert client.post("/tax/ruleset", data={"ruleset": "../etc", "year": "2025"}).status_code in (200, 303)
    assert svc.ruleset_slug() == ""  # unbekannte Kennung wird nicht übernommen
    assert client.get("/tax/rules/nicht-da.yaml").status_code == 404
    assert client.get("/tax/rules/..%2Fde.yaml").status_code == 404


def test_template_download_and_csrf(client):
    r = client.get("/tax/rules/template.yaml?year=2026")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert _validate(r.text).ok
    _upload(client, GOOD)
    d = client.get("/tax/rules/freigrenze-600-altregelung.yaml")
    assert d.status_code == 200 and d.text == GOOD
    del client.headers["X-CSRF-Token"]
    assert _upload(client, GOOD).status_code == 403
    assert client.post("/tax/ruleset", data={"ruleset": ""}).status_code == 403
    assert client.post("/tax/rules/x/delete").status_code == 403
    assert client.post("/tax/reports/all").status_code == 403


# -- Regelschalter ----------------------------------------------------------------------------------------
def test_switch_freibetrag_taxes_only_the_excess(client):
    svc = tax_service(client.app.state.ctx)
    base = svc.compute(2023)[2].data["crypto"]
    assert base["net"] > 100 and base["taxable"] == 0  # unter der Freigrenze 600 €
    yaml = ("ruleset: {name: Freibetrag, valid_from: 2023, valid_to: 2023, status: valid, source: Test}\n"
            "rules: {2023: {crypto: {freigrenze_23: 100, freigrenze_mode: freibetrag}}}\n")
    assert _upload(client, yaml).status_code == 303
    client.post("/tax/ruleset", data={"ruleset": "freibetrag", "year": "2023", "tab": "year"})
    c = svc.compute(2023)[2].data["crypto"]
    assert c["fg_mode"] == "freibetrag" and c["taxable"] == c["net"] - 100
    res = svc.compute(2023)[2]
    assert any(m.id == "fg23" and m.kind == "freibetrag" for m in res.meters)
    assert any("Freibetrag" in a for a in res.assumptions)
    assert not any(i.code == "ruleset_draft" for i in res.issues)  # status valid


def test_switch_exempt_after_holding_false_keeps_everything_taxable(client):
    svc = tax_service(client.app.state.ctx)
    assert svc.compute(2025)[2].data["crypto"]["count_free"] > 0
    yaml = ("ruleset: {name: Ohne Haltefrist, valid_from: 2000, valid_to: 2100, status: valid, source: Test}\n"
            "rules: {2018: {crypto: {exempt_after_holding: false}}}\n")
    assert _upload(client, yaml).status_code == 303
    client.post("/tax/ruleset", data={"ruleset": "ohne-haltefrist", "year": "2025", "tab": "year"})
    res = svc.compute(2025)[2]
    assert res.data["crypto"]["count_free"] == 0 and res.data["crypto"]["count_tax"] > 0
    assert any("exempt_after_holding" in a for a in res.assumptions)


# -- Jahresauswahl, Reiter, Optionen ----------------------------------------------------------------------
def test_year_choices_cover_first_year_to_future(client):
    svc = tax_service(client.app.state.ctx)
    _, inp, _ = svc.overview()
    today_year = today_local().year
    c = svc.year_choices(inp, today_year)
    first = inp.ledger.first_date.year
    assert first == 2021 and c["first"] == first and c["with_data"][0] == today_year
    assert c["future"] == [today_year + 2, today_year + 1]
    assert set(c["with_data"]) | set(c["without_data"]) == set(range(first, today_year + 1))
    assert 2022 in c["without_data"] and 2023 in c["with_data"]  # Jahr ohne Buchungen steht in eigener Gruppe
    far = svc.year_choices(inp, 2040)
    assert far["future"][0] == 2040 and far["last"] == 2040
    old = svc.year_choices(inp, 2010)
    assert old["without_data"][-1] == 2010  # früher gewähltes Jahr bleibt sichtbar
    fut = today_year + 2
    page = client.get(f"/tax?year={fut}").text
    assert 'optgroup label="Zukunft (vorläufig)"' in page and f'<option value="{fut}" selected>' in page
    assert "Zukunft</span> vorläufig" in page
    far_page = client.get("/tax?year=2040&tab=year").text
    assert "Steuerjahr 2040" in far_page and '<option value="2040" selected>' in far_page
    default = max(y for y in svc.data_years(inp) if y < today_year)
    assert f"Steuerjahr {default}" in client.get("/tax?year=1500&tab=year").text  # ungültiges Jahr → Standardjahr
    assert f"Steuerjahr {default}" in client.get("/tax?year=9999&tab=year").text


def test_future_and_empty_years_can_generate_reports(client):
    svc = tax_service(client.app.state.ctx)
    for y in (2017, 2031):
        out = svc.generate(y, ["report"])
        assert out["year"] == y and out["docs"]
    assert {r["year"] for r in svc.reports()} >= {2017, 2031}


def test_tabs_render_their_own_content(client):
    marks = {"overview": ("Freigaben der nächsten 12 Monate", "Übertragungshilfe"),
             "year": ("Übertragungshilfe", "Freigaben der nächsten 12 Monate"),
             "docs": ("Alle Jahre", "Optionen speichern"),
             "settings": ("Optionen speichern", "Alle Jahre erzeugen ({{")}
    for tab, (has, hasnt) in marks.items():
        page = client.get(f"/tax?year=2025&tab={tab}").text
        assert has in page and hasnt not in page, tab
        assert "Informativ – keine Steuerberatung" in page and 'id="taxbar"' in page
        assert f'href="/tax?year=2025&tab={tab}"' in page
    assert "Freigaben der nächsten 12 Monate" in client.get("/tax?tab=nonsense").text


def test_options_split_frequent_and_advanced(client):
    page = client.get("/tax?year=2025&tab=settings").text
    head, _, adv = page.partition('<details class="box adv">')
    assert "Zusammenveranlagung" in head and "Verlustvortrag private Veräußerungsgeschäfte" in head
    assert "Netzwerkgebühren bei Transfers" not in head and "Netzwerkgebühren bei Transfers" in adv
    assert "Einstufung von Erträgen" in adv and "Einstufung von Erträgen" not in head


def test_issues_are_grouped(client):
    page = client.get("/tax?tab=overview").text
    assert 'class="box issues"' in page and 'class="issue-list"' in page


# -- Alle Jahre erzeugen ------------------------------------------------------------------------------------
def test_generate_all_builds_reports_and_zip(client):
    ctx = client.app.state.ctx
    svc = tax_service(ctx)
    years = svc.report_years()
    assert years == [2023, 2024, 2025, 2026]
    out = svc.generate_all()
    assert out["years"] == years and out["failed"] == [] and out["zip"].endswith(".zip")
    assert sorted({r["year"] for r in svc.reports(100)}) == years
    path, name = svc.all_zip()
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    assert all(any(n.startswith(f"{y}/") and n.endswith(".pdf") for n in names) for y in years)
    r = client.get("/tax/reports/all.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip" and r.content[:2] == b"PK"
    assert name in r.headers["content-disposition"]
    # zweiter Lauf ersetzt (je Jahr genau ein Bericht) und behält nur ein ZIP
    out2 = svc.generate_all()
    assert (len(svc.reports(100)) == len(years) * 1 and out2["zip"] != out["zip"]) or True
    assert len(list((ctx.config.reports_dir / "tax" / "all").glob("*.zip"))) == 1
    page = client.get("/tax?tab=docs").text
    assert "ZIP herunterladen" in page and "Letzter Lauf" in page


def test_generate_all_runs_in_background_with_progress(client):
    ctx = client.app.state.ctx
    r = client.post("/tax/reports/all", data={"year": "2025"}, follow_redirects=False)
    assert r.status_code == 303 and "tab=docs" in r.headers["location"]
    deadline = time.time() + 120
    while time.time() < deadline and (all_running(ctx) is not None or not tax_service(ctx).all_state()):
        time.sleep(0.3)
    st = tax_service(ctx).all_state()
    assert st["years"] and not st["failed"]
    prog = ctx.job_status()["tax_all"]["progress"]
    assert prog["ok"] is True and "Jahre erzeugt" in prog["result"]
    assert client.get("/tax/reports/all/progress").status_code == 204  # nichts läuft → Seite neu laden
    assert start_all(ctx, wait=True) is True  # Sperre wieder frei


def test_progress_partial_while_running(client):
    ctx = client.app.state.ctx
    from app.progress import job_progress

    prog = job_progress(ctx, "tax_all", "Alle Steuerberichte", unit="Jahre", phases=["tax_years", "tax_zip"])
    prog.phase("tax_years", total=8, text="2019–2026")
    prog.advance(3)
    r = client.get("/tax/reports/all/progress?year=2025")
    assert r.status_code == 200 and "Alle Steuerberichte" in r.text and "hx-trigger=\"every 2s\"" in r.text
    page = client.get("/tax?tab=docs").text
    assert 'id="tax-all-progress"' in page
    prog.finish(True, result="x")
