"""Discovery 0.110: KI nur per Knopf (ein Aufruf), Portfolia-Score (Daten/qualitativ), Steckbrief-Hinweise
(Sondereffekte, Übernahme, umgestellte Vorjahre), Vorschau-Karten mit Disruption und Vorwärtstest – synthetische
Daten, keine echten KI- oder Netzaufrufe."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from app.discovery import alerts as AL
from app.discovery import backup as BK
from app.discovery import engine as EN
from app.discovery import forward as FW
from app.discovery import fundamentals as F
from app.discovery import jobs as J
from app.discovery import nash as NASH
from app.discovery import service as SV
from app.discovery import universe as U
from app.discovery import valuation as V
from app.discovery.sec import SecClient
from app.util.timeutil import today_local
from tests.test_discovery import _add, annual, fact, fake_parts, grower, make_client

CID = "/discovery/c/"


@pytest.fixture
def app_client(config, monkeypatch):
    yield from make_client(config, monkeypatch)


def _cid(resp) -> int:
    return int(resp.headers["location"].split("/c/")[1].split("?")[0])


def _ai_on(ctx, monkeypatch) -> None:
    ctx.settings.set("llm.enabled", True)
    ctx.settings.set("discovery.ai_calls_per_day", 50)
    monkeypatch.setattr("app.news.ai.AiGateway.key_present", lambda self, provider=None: True)


# ------------------------------------------------------------------------------------- Daten: Umstellung, Sondereffekte

def _restated() -> dict[str, Any]:
    """Umsatz 2024 im Bericht 2025 umgestellt (verkaufter Bereich herausgerechnet): 300 → 180 Mio."""
    data = annual({"Revenues": [200e6, 300e6, 270e6], "OperatingIncomeLoss": [10e6, 20e6, 30e6],
                   "NetIncomeLoss": [5e6, 10e6, 15e6]}, [2023, 2024, 2025])
    rows = data["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    rows.append(fact(180e6, "2024-12-31", "2026-02-15", start="2024-01-01", accn="0000-2025"))  # gleiche Einreichung
    return data


def test_growth_uses_values_from_the_same_filing():
    fu = F.parse(_restated())
    assert fu.get("revenue", "2024-12-31").v == 180e6  # jüngster Wert für die Tabelle
    g = fu.get("revenue_growth", "2025-12-31")
    assert g.v == pytest.approx(270 / 180 - 1)  # 2025 ggü. umgestelltem 2024 (beide aus dem 10-K 2025)
    assert "derselben Einreichung" in g.formula
    assert fu.comparable("revenue", ["2023-12-31", "2024-12-31"]) == [200e6, 180e6]  # keine gemeinsame Einreichung
    ev = U._fy_eval(fu)
    assert "umsatzrueckgang" not in ev["flags"]  # kein Schein-Rückgang mehr (Fall AppLovin 2023)


def _special(ni: float, loss: float) -> dict[str, Any]:
    data = grower()
    gaap = data["facts"]["us-gaap"]
    gaap["NetIncomeLoss"]["units"]["USD"][-1]["val"] = ni
    gaap["GainsLossesOnExtinguishmentOfDebt"] = {"units": {"USD": [
        fact(loss, "2025-12-31", "2026-02-15", start="2025-01-01", accn="0000-2025")]}}
    return data


def test_special_items_adjusted_pe_and_alert():
    fu = F.parse(_special(ni=-6.9e9, loss=-7.76e9))
    items = F.special_items(fu)
    assert [i["key"] for i in items] == ["debt_extinguishment"] and not items[0]["in_operating"]
    adj = F.adjusted_net_income(fu)
    assert adj.v == pytest.approx(-6.9e9 + 7.76e9) and adj.origin == "derived" and "Steuereffekt" in adj.note
    m = V.market_metrics(fu, {"price": 500.0, "ccy": "USD", "market_cap": 50e9, "fx": 1.0})
    assert m["pe"] is None and m["pe_adj"] == pytest.approx(50e9 / 0.86e9)  # KGV nur bereinigt sinnvoll
    alerts = AL.build(fundamentals=fu.to_dict(), market=m, signals=[], data_coverage=1.0, sub=None)
    a = next(x for x in alerts if x["kind"] == "sondereffekt")
    assert "Schuldentilgung" in a["text"] and "KGV bereinigt" in a["text"] and a["source"] == "sec:0000-2025"
    small = F.parse(_special(ni=200e6, loss=-1e6))  # unwesentlich: kein Sondereffekt
    assert F.special_items(small) == [] and F.adjusted_net_income(small).v is None


def test_takeover_alert_from_sec_forms_and_news():
    day = today_local().isoformat()
    old = (today_local() - timedelta(days=400)).isoformat()
    sub = {"filings": {"recent": {"form": ["10-Q", "425", "SC 14D9", "DEFM14A", "8-K"],
                                  "filingDate": [day, day, day, old, day],
                                  "accessionNumber": ["q1", "a425", "a14d9", "aold", "a8k"]}}}
    assert AL.latest_periodic(sub)["accn"] == "q1"
    tk = AL.takeover_filings(sub)
    assert {t["form"] for t in tk} == {"425", "SC 14D9"}  # alte Einreichung (400 Tage) zählt nicht
    news = [{"kind": "nachricht", "title": "Marvell übernimmt Lumentum", "published_at": day, "url": "https://x"},
            {"kind": "nachricht", "title": "Quartalszahlen", "published_at": day}]
    al = AL.build(fundamentals={"years": ["2025-12-31"], "currency": "USD"}, market={"price": 1.0}, signals=news,
                  data_coverage=1.0, sub=sub)
    kinds = [a["kind"] for a in al]
    assert kinds[:2] == ["uebernahme", "uebernahme_news"] and al[0]["level"] == "crit"
    only_news = AL.build(fundamentals={"years": ["2025-12-31"]}, market={"price": 1.0}, signals=news,
                         data_coverage=0.5, sub=None)
    assert only_news[0]["level"] == "crit" and "ungeprüft" in only_news[0]["title"]
    assert any(a["title"] == "Datenlücke" for a in only_news)


def test_screening_priority_scale_and_migration(db):
    assert U.screen_priority(14) == 100 and U.screen_priority(20) == 100 and U.screen_priority(7) == 50
    db.x("INSERT INTO disc_candidate(path, name, status, priority, created_at) VALUES ('wendepunkt', 'X', 'neu', 140, "
         "'2026-01-01')")
    db.x("PRAGMA user_version=42")
    db.migrate(target=43)  # nur Migration 43 erneut (spätere Migrationen sind bereits angewendet)
    assert db.scalar("SELECT priority FROM disc_candidate WHERE name='X'") == 100  # nie mehr „140 / 100“


# ------------------------------------------------------------------------------------- KI nur per Knopf

def test_one_call_for_all_parts_and_no_ai_in_automatic_paths(app_client, monkeypatch):
    c = app_client
    ctx = c.app.state.ctx
    _ai_on(ctx, monkeypatch)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr("app.news.ai.AiGateway.generate", fake_parts(seen))
    cid = _cid(_add(c, "ALPH"))
    for mode in ("auto", "daten"):
        assert SV.analyse_company(ctx, cid, mode=mode)["ai_call"] == ""
    jid, _ = J_create_auto(ctx)
    EN.J.run(ctx, jid)
    EN.run_scheduled(ctx, "beobachtet")
    assert seen == []  # Zeitplan und Datenaktualisierung rufen die KI nie auf
    r = c.post(f"{CID}{cid}/analyse", data={"csrf_token": c.token, "mode": "ki"}, follow_redirects=False)
    assert "abgeschlossen" in r.headers["location"]
    assert [p for p, _ in seen] == ["analyse"]  # genau ein Aufruf
    assert ctx.db.scalar("SELECT calls FROM disc_ai_usage") == 1
    an = SV.analysis(ctx.db, cid)
    assert an["ai_state"] == "vollstaendig" and an["result"]["thesis"]["text"]
    sp = NASH.split(an["result"]["scores"]["nash125"])
    assert sp["data"]["max"] == 65 and sp["quali"]["max"] == 60 and not sp["quali_open"] and sp["quali"]["n_open"] == 0
    fw = ctx.db.q1("SELECT * FROM disc_forward WHERE company_id=? AND kind='analyse'", (cid,))
    assert fw is not None and fw["price"] == 40.0 and fw["score_open"] == 0  # Vorwärtstest-Eintrag
    page = c.get(f"{CID}{cid}").text
    for needle in ("KI-Analyse aktualisieren · 1 Aufruf", "Mit KI analysiert", "KI, belegt"):
        assert needle in page, needle


def J_create_auto(ctx):
    from app.discovery import jobs as J

    return J.create(ctx, "analyse", trigger="automatisch", company_id=0, params={"auto": True})


def test_stale_ai_report_after_new_sec_filing(app_client, monkeypatch):
    c = app_client
    ctx = c.app.state.ctx
    _ai_on(ctx, monkeypatch)
    monkeypatch.setattr("app.news.ai.AiGateway.generate", fake_parts([]))
    cid = _cid(_add(c, "ALPH"))
    SV.analyse_company(ctx, cid, mode="ki")
    assert "KI-Bericht veraltet:" not in c.get(f"{CID}{cid}").text
    future = (today_local() + timedelta(days=2)).isoformat()
    monkeypatch.setattr(SecClient, "submissions", lambda s, cik, fresh=False: {
        "filings": {"recent": {"form": ["10-Q"], "filingDate": [future], "accessionNumber": ["neu"]}}})
    r = SV.analyse_company(ctx, cid, mode="daten")  # neuer Quartalsbericht erkannt – ohne KI
    assert r["ai_state"] == "veraltet" and r["ai_call"] == ""
    d = SV.detail_context(ctx, cid)
    assert d["stage"] == "veraltet", (d["latest_filed"], {k: v["at"] for k, v in d["parts"].items()})
    page = c.get(f"{CID}{cid}").text
    assert "KI-Bericht veraltet:" in page and "KI-Analyse aktualisieren · 1 Aufruf" in page
    radar = c.get("/discovery?cat=veraltet").text
    assert "Alpha Robotics" in radar


def test_button_without_budget_queues_visible_waiting_job(app_client, monkeypatch):
    """0.111: Kontingent erschöpft → der Knopf bleibt aktiv, der Job wartet sichtbar (Grund, Zeitpunkt, abbrechbar)."""
    c = app_client
    ctx = c.app.state.ctx
    _ai_on(ctx, monkeypatch)
    ctx.settings.set("discovery.ai_calls_per_day", 0)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr("app.news.ai.AiGateway.generate", fake_parts(seen))
    cid = _cid(_add(c, "ALPH"))
    c.post(f"{CID}{cid}/analyse", data={"csrf_token": c.token, "mode": "daten"})
    page = c.get(f"{CID}{cid}").text
    assert "Startet nicht sofort:" in page and "Tagesgrenze" in page
    r = c.post(f"{CID}{cid}/analyse", data={"csrf_token": c.token, "mode": "ki"}, follow_redirects=False)
    assert "wartet" in r.headers["location"]
    assert seen == []
    jb = J.active(ctx.db, cid)[0]
    assert jb["status"] == "wartet" and "Tagesgrenze" in jb["wait_reason"]
    page = c.get(f"{CID}{cid}").text
    assert f"Job #{jb['id']} wartet bis" in page and "abbrechen" in page
    radar = c.get("/discovery").text
    assert "Warteschlange (1)" in radar
    assert "disabled" not in radar.split("Mit KI analysieren · 1 Aufruf")[0][-400:]  # Knopf bleibt aktiv
    r = c.post(f"/discovery/jobs/{jb['id']}/abbrechen", data={"csrf_token": c.token, "back": f"{CID}{cid}"},
               follow_redirects=False)
    assert "abgebrochen" in r.headers["location"] and J.get(ctx.db, jb["id"])["status"] == "abgebrochen"
    ctx.settings.set("llm.enabled", False)  # „KI aus“ sperrt weiterhin sofort (Warten hilft nicht)
    r = c.post(f"{CID}{cid}/analyse", data={"csrf_token": c.token, "mode": "ki"}, follow_redirects=False)
    assert "nicht+gestartet" in r.headers["location"] or "nicht%20gestartet" in r.headers["location"]
    assert "KI gesperrt:" in c.get(f"{CID}{cid}").text


# ------------------------------------------------------------------------------------- Steckbrief und Karten

def test_steckbrief_without_ai_shows_open_qualitative_part_and_rule_based_pros(app_client):
    c = app_client
    ctx = c.app.state.ctx
    cid = _cid(_add(c, "ALPH"))
    c.post(f"{CID}{cid}/analyse", data={"csrf_token": c.token, "mode": "daten"})
    d = SV.detail_context(ctx, cid)
    assert d["stage"] == "daten" and d["nash"]["quali_open"] and d["nash"]["data"]["n_done"] >= 5
    assert d["summary"]["pro_src"] == "regel" and d["summary"]["valuation"].startswith("Der Kurs")
    assert len(d["summary"]["pro"]) <= 3 and len(d["summary"]["contra"]) <= 3
    assert {n["key"] for n in d["numbers"]} == {"revenue_growth", "op_margin", "net_cash"}
    card = next(x for x in SV.radar(ctx)["cards"] if x["id"] == cid)
    assert card["stage"] == "daten" and card["nash"]["quali_open"] and card["why"]["label"] == "manuell aufgenommen"
    for s in SV.SORTS:
        assert c.get(f"/discovery?sort={s}").status_code == 200
    for k in SV.CATEGORIES:
        assert c.get(f"/discovery?cat={k}").status_code == 200


def test_forward_test_evaluation_and_backup(app_client, monkeypatch):
    c = app_client
    ctx = c.app.state.ctx
    cid = _cid(_add(c, "ALPH"))
    c.post(f"{CID}{cid}/analyse", data={"csrf_token": c.token, "mode": "daten"})
    old = (today_local() - timedelta(days=400)).isoformat()
    ctx.db.x("INSERT INTO disc_forward(company_id, kind, ref, day, series, price, ccy, bench_price, score, "
             "score_open, rating, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
             (cid, "vorschau", "job:1", old, "yahoo:ALPH", 20.0, "USD", 500.0, 40.0, 1, "hoch", old))
    prices = {"yahoo:ALPH": 30.0, FW.BENCH_SERIES: 550.0}
    monkeypatch.setattr(FW, "_snap", lambda ctx_, series: (prices.get(series), "USD"))
    ev = FW.evaluate(ctx)
    g = next(x for x in ev["groups"] if x["kind"] == "vorschau")
    h12 = g["horizons"][0]
    assert h12["n_eval"] == 1 and h12["median_excess"] == pytest.approx(0.5 - 0.1) and h12["beat"] == 1.0
    assert g["horizons"][1]["n_eval"] == 0  # 24 Monate noch nicht erreicht
    assert g["by_rating"][0] == {"rating": "hoch", "n": 1, "median_excess": pytest.approx(0.4)}
    assert "Vorwärtstest" in c.get("/discovery").text
    data = json.loads(json.dumps(BK.collect(ctx.db), default=str))
    assert len(data["forward"]) == 1
    with ctx.db.transaction() as tx:
        assert BK.restore(tx, data) == 0  # wiederholbar ohne Dubletten


def test_preview_disruption_validation():
    texts = {"U1-S": "Alpha replaces manual welding with autonomous robot cells in car plants."}
    hi = EN.validate_disruption_preview({"verdraengt": "manuelles Schweißen", "womit": "Roboterzellen",
                                         "reifegrad": "skaliert", "einschaetzung": "hoch",
                                         "beleg": {"id": "U1-S", "zitat": "replaces manual welding with autonomous "
                                                                          "robot cells"}}, texts)
    assert hi["einschaetzung"] == "hoch" and hi["beleg"]["geprueft"] and not hi["hinweis"]
    capped = EN.validate_disruption_preview({"verdraengt": "x", "einschaetzung": "hoch", "reifegrad": "Mond"}, texts)
    assert capped["einschaetzung"] == "mittel" and capped["reifegrad"] == "" and capped["hinweis"]
    assert EN.validate_disruption_preview({"einschaetzung": "hoch"}, texts) is None  # ohne Inhalt keine Vorschau
    assert EN.validate_disruption_preview("Unsinn", texts) is None


def test_automatic_refresh_new_filing_first_then_missing_then_old(app_client, monkeypatch):
    c = app_client
    ctx = c.app.state.ctx
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr("app.news.ai.AiGateway.generate", fake_parts(seen))
    a, b = _cid(_add(c, "ALPH")), _cid(_add(c, "BETA"))
    SV.analyse_company(ctx, a, mode="daten")
    SV.analyse_company(ctx, b, mode="daten")
    g = _cid(_add(c, "GOOGL"))  # noch ohne Analyse
    co_a = SV.company(ctx.db, a)
    today = today_local().isoformat()
    ctx.db.x("INSERT INTO disc_universe(cik, ticker, name, filed_seen, added_at, updated_at) VALUES (?,?,?,?,?,?)",
             (co_a["cik"], "ALPH", "Alpha", (today_local() + timedelta(days=1)).isoformat(), today, today))
    ctx.db.x("UPDATE disc_analysis SET created_at='2020-01-01T00:00:00+00:00' WHERE company_id=?", (b,))
    ctx.settings.set("discovery.refresh_days", 30)
    plan = EN.refresh_plan(ctx)
    assert [x["id"] for x in plan["bericht"]] == [a] and [x["id"] for x in plan["neu"]] == [g]
    assert [x["id"] for x in plan["alt"]] == [b]
    ctx.settings.set("discovery.auto_analyse_per_run", 2)
    jid, _ = J_create_auto(ctx)
    EN.J.run(ctx, jid)
    jb = EN.J.get(ctx.db, jid)
    res = jb["result"]
    assert jb["status"] == "teilweise", jb  # GOOGL hat in der Testumgebung keine SEC-Daten – Lauf geht weiter
    assert [x["grund"] for x in res["analysiert"]] == ["bericht", "neu"] and res["offen"] == 1
    assert res["analysiert"][0]["status"] in ("ok", "unchanged") and res["analysiert"][1]["status"] == "fehler"
    assert res["neuer_bericht"] == 1 and seen == []  # nie KI
    assert len(SV.analyses(ctx.db, a)) >= 2  # neuer SEC-Bericht → neue Version
    page = c.get("/discovery").text
    assert "Alle Vorschläge automatisch aktualisieren" in page and 'value="30" selected' in page
    form = {"csrf_token": c.token, "refresh_days": "7", "auto_analyse_per_run": "25"}
    r = c.post("/discovery/einstellungen", data=form, follow_redirects=False)
    assert r.status_code == 303 and ctx.settings.get("discovery.refresh_days") == 7
    assert ctx.settings.get("discovery.auto_analyse_per_run") == 25
