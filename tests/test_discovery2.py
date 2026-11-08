"""Discovery & Intelligence 2.0: Tom Nash 125, KI-Steuerung (Status, Jobs, Nachholen), Discovery Engine (Universum,
Screening, KI-Discovery), Oberfläche und Export/Restore – synthetische Daten, keine echten KI- oder Netzaufrufe."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from app.discovery import ai as AI
from app.discovery import backup as BK
from app.discovery import engine as EN
from app.discovery import fundamentals as F
from app.discovery import jobs as J
from app.discovery import nash as NASH
from app.discovery import scoring as S
from app.discovery import service as SV
from app.discovery import universe as U
from app.discovery import valuation as V
from app.discovery.sec import SecClient, SecError
from app.news.ai import AiResult
from tests.test_discovery import _add, annual, burner, grower, make_client, nash_answer

CID = "/discovery/c/"


@pytest.fixture
def app_client(config, monkeypatch):
    yield from make_client(config, monkeypatch)


@pytest.fixture(autouse=True)
def _no_market_history(monkeypatch):
    monkeypatch.setattr(SV, "_adj_history", lambda ctx, sym: [])


def _cid(resp) -> int:
    return int(resp.headers["location"].split("/c/")[1].split("?")[0])


def _ai_on(ctx, monkeypatch) -> None:
    ctx.settings.set("llm.enabled", True)
    ctx.settings.set("discovery.ai_calls_per_day", 50)
    monkeypatch.setattr("app.news.ai.AiGateway.key_present", lambda self, provider=None: True)


# ------------------------------------------------------------------------------------------------ Tom Nash 125

MARKET = {"short_pct": 0.03, "inst_pct": 0.65, "tr_5y": 1.5, "spx_tr_5y": 0.9, "tr_period": "2021-11-05–2026-11-05"}


def test_nash125_has_13_criteria_and_125_points():
    fu = F.parse(grower())
    sc = NASH.nash125(fu, None, MARKET)
    assert len(sc.criteria) == 13 and sc.max == 125
    maxes = {c.label: c.max for c in sc.criteria}
    assert maxes["Recession Resistance"] == 5 and all(v == 10 for k, v in maxes.items() if k != "Recession Resistance")
    assert [c.label for c in sc.criteria] == ["Cash vs. Debt", "Revenue Growth", "Operating Margin", "Short Interest",
                                              "Institutional Ownership", "Scalability", "5-Year Return vs. S&P 500",
                                              "Competitive Moat", "CEO Quality", "Recession Resistance",
                                              "Secular Trend", "Company Culture", "Talent"]
    d = sc.to_dict()
    assert sc.total == S.recompute(d)  # reproduzierbar aus den gespeicherten Teilpunkten
    pts = {c.key: c.points for c in sc.criteria}
    assert pts["nash_cash"] == 10 and pts["nash_umsatz"] == 10  # 200 Mio. Kasse ≥ 2 × 50 Mio. Schulden; +30 %
    assert pts["nash_marge"] == 5  # genau 25 %: „10–25 %“ → 5
    assert pts["nash_short"] == 10 and pts["nash_institutionell"] == 10 and pts["nash_rendite"] == 10
    assert all(c.points is None for c in sc.criteria if c.kind == "ai")  # ohne KI: unbewertet, nicht erfunden


@pytest.mark.parametrize(("v", "low", "high", "inverse", "want"), [
    (0.099, 0.10, 0.20, False, 0.0), (0.10, 0.10, 0.20, False, 5.0), (0.20, 0.10, 0.20, False, 5.0),
    (0.2001, 0.10, 0.20, False, 10.0), (0.0999, 0.10, 0.25, False, 0.0), (0.25, 0.10, 0.25, False, 5.0),
    (0.251, 0.10, 0.25, False, 10.0), (0.049, 0.05, 0.10, True, 10.0), (0.05, 0.05, 0.10, True, 5.0),
    (0.10, 0.05, 0.10, True, 0.0), (None, 0.05, 0.10, True, None)])
def test_nash_three_level_thresholds(v, low, high, inverse, want):
    assert NASH._three(v, low, high, inverse=inverse) == want


def test_nash_cash_institutional_scalability_and_relative_return_rules():
    fu = F.parse(burner())  # 95 Mio. Kasse, keine Schulden
    assert NASH._cash_debt(fu).points == 10
    data = burner()
    data["facts"]["us-gaap"]["LongTermDebt"] = {"units": {"USD": [
        {"val": v, "end": f"{y}-12-31", "filed": f"{y + 1}-02-15", "form": "10-K", "accn": f"0000-{y}", "fy": y,
         "fp": "FY"} for y, v in ((2023, 60e6), (2024, 60e6), (2025, 60e6))]}}
    assert NASH._cash_debt(F.parse(data)).points == 5  # 95 > 60, aber < 2 × 60
    data["facts"]["us-gaap"]["LongTermDebt"]["units"]["USD"][-1]["val"] = 120e6
    assert NASH._cash_debt(F.parse(data)).points == 0  # mehr Schulden als Liquidität
    for inst, want in ((0.29, 0), (0.30, 5), (0.60, 5), (0.61, 10), (None, None)):
        assert NASH._institutional({"inst_pct": inst}).points == want
    for diff, want in ((0.21, 10), (0.20, 5), (0.10, 5), (0.05, 0), (-0.3, 0)):
        c = NASH._relative_return({"tr_5y": 1.0 + diff, "spx_tr_5y": 1.0})
        assert c.points == want, diff
    miss = NASH._relative_return({"tr_missing": "Kurshistorie beginnt erst 2023-01-02 – weniger als 5 Jahre"})
    assert miss.points is None and "weniger als 5 Jahre" in miss.missing[0]
    sc = NASH._scalability(F.parse(burner()))  # Umsatz +75 %, Kosten (Bruttogewinn − op. Ergebnis) +26 %
    assert sc.points == 10 and sc.parts[0]["prov"] == "annahme"
    assert NASH._cash_debt(fu).parts[0]["prov"] == "belegt" and NASH._short({}).parts[0]["prov"] == "auftrag"


def test_nash_qualitative_requires_evidence_and_caps():
    good = AI.validate_nash(nash_answer(), [{"id": "S1", "text": "Alpha sells robots. Revenue was 742 million dollars "
                                                                "in 2025."}])["kriterien"]
    c = NASH._ai("rezession", good)
    assert c.points == 5 and c.max == 5  # Stufe 2 von 2 → Höchstpunktzahl 5
    assert NASH._ai("moat", good).points == 10
    unverified = {"moat": {**good["moat"], "belege": [{"id": "S1", "zitat": "steht nicht in der Quelle", "geprueft":
                                                       False}]}}
    capped = NASH._ai("moat", unverified)
    assert capped.points == 5 and "gekappt" in capped.reason
    low_conf = {"ceo": {**good["ceo"], "konfidenz": 0.3}}
    assert NASH._ai("ceo", low_conf).points == 5
    none = {"trend": {**good["trend"], "belege": []}}
    t = NASH._ai("trend", none)
    assert t.points is None and "kein Beleg" in t.missing[0]  # ohne Beleg: unbewertet statt geraten
    assert NASH._ai("talent", None).points is None


def test_nash_band_only_when_determinable():
    def score(points: list[float | None], maxes: list[float]) -> dict[str, Any]:
        return {"criteria": [{"points": p, "max": m} for p, m in zip(points, maxes, strict=True)]}

    full = [10.0] * 12 + [5.0]
    assert NASH.band(score(full, [10] * 12 + [5]))["key"] == "exceptional"
    b = NASH.band(score([10.0] * 7 + [None] * 6, [10] * 12 + [5]))  # 70 sicher, bis zu 125 möglich
    assert b["key"] == "offen" and not b["sure"] and b["lo"] == 70 and b["hi"] == 125
    sure_low = NASH.band(score([0.0] * 7 + [None] * 3 + [0.0] * 3, [10] * 12 + [5]))  # max. 25 erreichbar
    assert sure_low["key"] == "niedrig" and sure_low["sure"]
    w = NASH.band(score([10.0] * 8 + [0.0] * 5, [10] * 12 + [5]))
    assert w["key"] == "watchlist" and w["sure"]
    assert NASH.band(score([10.0] * 10 + [None] * 3, [10] * 12 + [5]))["key"] == "exceptional"  # 100 sicher


def test_total_return_same_period_and_short_history():
    stock = [("2021-11-01", 10.0), ("2021-11-05", 11.0), ("2024-01-02", 20.0), ("2026-11-05", 33.0)]
    bench = [("2021-11-05", 100.0), ("2024-01-02", 130.0), ("2026-11-05", 190.0), ("2026-11-06", 191.0)]
    r = V.total_return(stock, bench)
    assert r["period"] == "2021-11-05–2026-11-05" and r["tr"] == pytest.approx(2.0) and r["bench"] == pytest.approx(0.9)
    young = V.total_return([("2023-05-01", 5.0), ("2026-11-05", 9.0)], bench)
    assert "weniger als 5 Jahre" in young["missing"]


def test_legacy_method_kept_and_not_recalculated():
    legacy = {"scores": {"nash": {"key": "nash", "label": "Fundamental (Tom-Nash-inspiriert)", "total": 77.0,
                                  "max": 125, "criteria": []}}, "ai_meta": {"provider": None}}
    res = SV.normalize(legacy)
    v1 = res["scores"]["fundamental_v1"]
    assert v1["total"] == 77.0 and v1["label"] == "Portfolia Fundamental v1" and "nash" not in res["scores"]
    assert v1["legacy_label"] == "Fundamental (Tom-Nash-inspiriert)" and res["ai_state"] == "fehlt"
    assert NASH.METHOD == "Tom Nash 125 – Public Reconstruction v1" and "Original" not in NASH.METHOD


def test_migration_copies_legacy_score_column(db):
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(disc_analysis)")}
    assert {"nash125", "fundamental_v1", "ai_state", "parts_json", "job_id"} <= cols


# ------------------------------------------------------------------------------------------------ KI-Prüfung

def test_lenient_json_and_quote_check():
    raw = '```json\n{"a": "Zeile eins\nZeile zwei", "b": [1, 2,],}\n```'
    assert AI.parse_lenient(raw) == {"a": "Zeile eins\nZeile zwei", "b": [1, 2]}
    with pytest.raises(ValueError):
        AI.parse_lenient("kein json")
    src = "Alpha sells robots to   automotive plants. Revenue was 742 million dollars."
    assert AI.quote_found("sells robots to automotive plants", src)
    assert AI.quote_found("„Alpha sells robots to automotive plants.“", src)
    assert not AI.quote_found("sells drones to farmers worldwide", src)
    assert not AI.quote_found("Alpha", src)  # zu kurz – kein Beleg


def test_ai_unavailable_reasons(app_client, monkeypatch):
    ctx = app_client.app.state.ctx
    assert AI.unavailable_reason(ctx)[0] == "aus"  # KI global aus
    ctx.settings.set("llm.enabled", True)
    assert "Schlüssel" in AI.unavailable_reason(ctx)[1]
    monkeypatch.setattr("app.news.ai.AiGateway.key_present", lambda self, provider=None: True)
    assert AI.unavailable_reason(ctx) == ("", "")
    ctx.settings.set("discovery.ai", False)
    assert "discovery.ai" in AI.unavailable_reason(ctx)[1]
    ctx.settings.set("discovery.ai", True)
    ctx.settings.set("discovery.ai_calls_per_day", 0)
    assert AI.unavailable_reason(ctx)[0] == "kontingent"
    ctx.settings.set("discovery.ai_calls_per_day", 10)
    ctx.settings.set("discovery.ai_calls_per_month", 0)
    assert "Monatsgrenze" in AI.unavailable_reason(ctx)[1]
    ctx.settings.set("discovery.ai_calls_per_month", 100)
    from datetime import UTC, datetime, timedelta

    from app.news.ai import AiGateway

    AiGateway(ctx)._pause("claude", datetime.now(UTC) + timedelta(hours=1), "Test")
    assert AI.unavailable_reason(ctx)[0] == "pausiert"


def test_ai_status_reports_provider_model_budget_without_key(app_client, monkeypatch):
    ctx = app_client.app.state.ctx
    _ai_on(ctx, monkeypatch)
    ctx.settings.set("llm.provider", "gemini")
    ctx.settings.set("llm.gemini_model", "gemini-2.5-flash-lite")
    st = EN.ai_status(ctx)
    assert st["provider"] == "gemini" and st["model"] == "gemini-2.5-flash-lite" and st["usable"]
    assert st["day"]["limit"] == 50 and st["day"]["used"] == 0 and "nicht verlässlich" in st["cost"]
    assert {p["step"] for p in st["plan"]} == set(EN.SCHEDULE)
    page = app_client.get("/discovery").text
    for needle in ("KI-Researchstatus", "gemini-2.5-flash-lite", "KI-Discovery starten", "Fundamentalscreening starten",
                   "Vollständigen Discovery-Lauf starten", "Suchfortschritt", "Research-Jobs", "Nash 100+",
                   "Fundamentale Wendepunkte", "Mögliche Marktfehlbewertungen", "Disruptive Geschäftsmodelle"):
        assert needle in page, needle
    assert "Schlüssel" in page and "sk-" not in page


# ------------------------------------------------------------------------------------------------ Nachholen (Phase D)

def test_missing_ai_parts_can_be_completed_later(app_client, monkeypatch):
    c = app_client
    ctx = c.app.state.ctx
    cid = _cid(_add(c, "ALPH"))
    r1 = SV.analyse_company(ctx, cid, mode="ergaenzen")  # KI aus
    assert r1["status"] == "ok" and r1["ai_state"] == "aus"
    assert {p["status"] for p in SV.parts_state(ctx.db, cid).values()} == {"aus"}
    seen: list[tuple[str, str]] = []
    from tests.test_discovery import fake_parts

    _ai_on(ctx, monkeypatch)
    monkeypatch.setattr("app.news.ai.AiGateway.generate", fake_parts(seen))
    # gleiche Daten wie vorher – trotzdem neue Version, weil KI-Teile nachgeholt wurden (früher: „unverändert“)
    r = c.post(f"{CID}{cid}/analyse", data={"csrf_token": c.token, "mode": "ergaenzen"}, follow_redirects=False)
    assert "abgeschlossen" in r.headers["location"]
    an = SV.analysis(ctx.db, cid)
    assert an["ai_state"] == "vollstaendig" and len(seen) == 3 and len(SV.analyses(ctx.db, cid)) == 2
    again = SV.analyse_company(ctx, cid, mode="ergaenzen")
    assert again["status"] == "unchanged" and len(seen) == 3  # gültige Teile werden nicht erneut erzeugt


def test_partial_ai_failure_reruns_only_the_failed_part(app_client, monkeypatch):
    from tests.test_discovery import fake_parts

    c = app_client
    ctx = c.app.state.ctx
    _ai_on(ctx, monkeypatch)
    cid = _cid(_add(c, "ALPH"))
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr("app.news.ai.AiGateway.generate", fake_parts(seen, nash="{kaputt"))
    r = SV.analyse_company(ctx, cid, mode="ki")
    assert r["ai_state"] == "teilweise" and "kein gültiges JSON" in r["ai_error"]
    st = SV.parts_state(ctx.db, cid)
    assert st["nash"]["status"] == "ungueltig" and st["bericht"]["status"] == "ok"
    call = ctx.db.q1("SELECT * FROM disc_ai_call WHERE step='nash' ORDER BY id DESC LIMIT 1")
    assert call["status"] == "ungueltig" and call["error_class"] == "json" and call["output_tokens"] == 800
    seen.clear()
    monkeypatch.setattr("app.news.ai.AiGateway.generate", fake_parts(seen))
    r2 = SV.analyse_company(ctx, cid, mode="ergaenzen")
    assert [p for p, _ in seen] == ["nash"] and r2["ai_state"] == "vollstaendig"
    res = SV.analysis(ctx.db, cid)["result"]
    assert res["scores"]["nash125"]["coverage"] > 0.9 and res["nash_band"]["lo"] == res["scores"]["nash125"]["total"]


def test_ai_parts_reuse_and_stale_detection(app_client, monkeypatch):
    from tests.test_discovery import fake_parts

    ctx = app_client.app.state.ctx
    _ai_on(ctx, monkeypatch)
    cid = _cid(_add(app_client, "ALPH"))
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr("app.news.ai.AiGateway.generate", fake_parts(seen))
    SV.analyse_company(ctx, cid, mode="ki")
    fps = {p: AI.part_fingerprint(p, SV.company(ctx.db, cid), [{"id": "S9", "text": "neu"}]) for p in AI.PARTS}
    assert SV.plan_parts(ctx.db, cid, "ergaenzen", fps) == []  # alles gültig
    assert SV.plan_parts(ctx.db, cid, "voll", fps) == list(AI.PARTS)  # neue Daten → veraltet → neu
    assert SV.plan_parts(ctx.db, cid, "daten", fps) == []
    assert SV.overall_ai_state(SV.parts_state(ctx.db, cid, fps)) == "veraltet"


# ------------------------------------------------------------------------------------------------ Jobs

def test_jobs_no_duplicates_run_once_and_recover(app_client):
    ctx = app_client.app.state.ctx
    j1, new1 = J.create(ctx, "screening")
    j2, new2 = J.create(ctx, "screening")
    assert new1 and not new2 and j1 == j2  # Doppelklick startet keinen zweiten Job
    ids: list[tuple[int, bool]] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        ids.append(J.create(ctx, "news"))

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len({i for i, _ in ids}) == 1 and sum(1 for _, n in ids if n) == 1  # gleichzeitige Anfragen
    calls: list[int] = []
    J.HANDLERS["_test"] = lambda ctx_, job: (calls.append(job.id), ("abgeschlossen", {"ok": 1}))[1]
    J.KINDS["_test"] = "Test"
    try:
        jid, _ = J.create(ctx, "_test")
        J.run(ctx, jid)
        J.run(ctx, jid)  # zweiter Aufruf führt nicht erneut aus
        assert calls == [jid] and J.get(ctx.db, jid)["status"] == "abgeschlossen"
        J.HANDLERS["_test"] = lambda ctx_, job: 1 / 0
        bad, _ = J.create(ctx, "_test")
        J.run(ctx, bad)
        b = J.get(ctx.db, bad)
        assert b["status"] == "fehlgeschlagen" and b["error_class"] == "ZeroDivisionError" and b["error"]
    finally:
        J.HANDLERS.pop("_test")
        J.KINDS.pop("_test")
    ctx.db.x("UPDATE disc_job SET worker='anderer-prozess', status='laeuft' WHERE id=?", (j1,))
    assert J.recover(ctx) >= 1
    rec = J.get(ctx.db, j1)
    assert rec["status"] == "unterbrochen" and "neu gestartet" in rec["error"]
    j3, new3 = J.create(ctx, "screening")
    assert new3 and j3 != j1  # nach Unterbrechung wieder startbar
    ctx.db.x("UPDATE disc_job SET status='laeuft', heartbeat_at='2020-01-01T00:00:00+00:00' WHERE id=?", (j3,))
    assert J.expire_stale(ctx) == 1 and J.get(ctx.db, j3)["status"] == "unterbrochen"


def test_job_routes_status_and_polling(app_client):
    c = app_client
    ctx = c.app.state.ctx
    r = c.post("/discovery/start/news", data={"csrf_token": c.token}, follow_redirects=False)
    assert r.status_code == 303 and "job=" in r.headers["location"]
    jid = int(r.headers["location"].split("job=")[1])
    jb = J.get(ctx.db, jid)
    assert jb["trigger"] == "manuell" and jb["status"] in ("abgeschlossen", "teilweise")  # ohne Scheduler: synchron
    part = c.get(f"/discovery/jobs/{jid}").text
    assert f"Job #{jid}" in part and "hx-trigger" not in part  # beendet: keine Abfrage mehr
    run, _ = J.create(ctx, "screening")
    ctx.db.x("UPDATE disc_job SET status='laeuft', step='Bestätigung 3/20' WHERE id=?", (run,))
    live = c.get(f"/discovery/jobs/{run}").text
    assert 'hx-trigger="every 2s"' in live and "Bestätigung 3/20" in live
    st = c.get("/discovery/status").text
    assert 'hx-trigger="every 4s"' in st and "Bestätigung 3/20" in st
    dup = c.post("/discovery/start/screening", data={"csrf_token": c.token}, follow_redirects=False)
    assert "l%C3%A4uft+bereits" in dup.headers["location"]
    log_page = c.get(f"/discovery/protokoll?job={jid}").text
    assert "Ausführungsprotokoll" in log_page and f"Job #{jid}" in log_page
    assert c.post("/discovery/start/kaputt", data={"csrf_token": c.token}).status_code == 404


def test_company_buttons_start_jobs_and_partial(app_client, monkeypatch):
    c = app_client
    ctx = c.app.state.ctx
    cid = _cid(_add(c, "ALPH"))
    r = c.post(f"{CID}{cid}/analyse", data={"csrf_token": c.token, "mode": "ki"}, follow_redirects=False)
    assert "job=" in r.headers["location"] and "err=" in r.headers["location"]  # KI aus: Grund sichtbar
    jb = J.recent(ctx.db, 1)[0]
    assert jb["kind"] == "analyse" and jb["status"] == "ki_fehlt" and "ausgeschaltet" in jb["error"]
    assert c.post(f"{CID}{cid}/analyse", data={"csrf_token": c.token, "mode": "auto"}).status_code == 400
    part = c.get(f"{CID}{cid}/status").text
    assert "KI-Researchstatus dieser Firma" in part and "KI nicht konfiguriert" in part
    run, _ = J.create(ctx, "analyse", company_id=cid)
    ctx.db.x("UPDATE disc_job SET status='laeuft', step='KI-Teil 1/3' WHERE id=?", (run,))
    busy = c.post(f"{CID}{cid}/analyse", data={"csrf_token": c.token, "mode": "daten"}, follow_redirects=False)
    assert "bereits" in busy.headers["location"]
    assert 'hx-trigger="every 3s"' in c.get(f"{CID}{cid}/status").text


def test_scheduled_steps_are_jobs_and_configurable(app_client):
    from app.discovery.web import schedule
    from app.jobs.scheduler import Scheduler

    ctx = app_client.app.state.ctx
    s = Scheduler(ctx)
    s.setup_default_jobs()
    assert s.sched.get_job("disc_auto_screening") is not None and s.sched.get_job("disc_auto_ki") is not None
    ctx.settings.set("discovery.auto.ki", 0)
    schedule(s)
    assert s.sched.get_job("disc_auto_ki") is None and s.sched.get_job("disc_auto_news") is not None
    assert s._executor["disc_auto_screening"] == "discovery"
    out = EN.run_scheduled(ctx, "news")
    jb = J.recent(ctx.db, 1)[0]
    assert jb["kind"] == "news" and jb["trigger"] == "automatisch" and out["new"]
    EN.run_scheduled(ctx, "analysen")
    assert J.recent(ctx.db, 1)[0]["params"] == {"auto": True}


# ------------------------------------------------------------------------------------------------ Engine

def decliner() -> dict[str, Any]:
    ys = [2023, 2024, 2025]
    rev = [300e6, 240e6, 180e6]
    return annual({"Revenues": rev, "GrossProfit": [r * 0.4 for r in rev], "OperatingIncomeLoss": [-5e6, 1e6, 8e6],
                   "NetIncomeLoss": [-5e6, 0.0, 5e6], "NetCashProvidedByUsedInOperatingActivities": [10e6, 5e6, 2e6],
                   "PaymentsToAcquirePropertyPlantAndEquipment": [3e6, 3e6, 3e6],
                   "CashAndCashEquivalentsAtCarryingValue": [20e6, 15e6, 10e6], "LongTermDebt": [200e6] * 3,
                   "StockholdersEquity": [100e6] * 3}, ys, shares=[40e6, 44e6, 50e6])


FACTS = {1: grower(), 2: burner(), 4: grower(), 5: burner(), 6: decliner()}


def _frames(facts: dict[int, dict[str, Any]]) -> Any:
    def frame(self, tag, period, unit="USD", tax="us-gaap"):
        year = period[2:6]
        out = []
        for cik, f in facts.items():
            for row in ((f["facts"]["us-gaap"].get(tag) or {}).get("units") or {}).get(unit) or []:
                if row["end"].startswith(year):
                    out.append({"cik": cik, "val": row["val"], "end": row["end"]})
        return out
    return frame


@pytest.fixture
def engine_client(app_client, monkeypatch):
    from tests.test_discovery import FakeSec

    FakeSec(FACTS, sic={5: "6770"}).install(monkeypatch)
    monkeypatch.setattr(SecClient, "frame", _frames(FACTS))
    monkeypatch.setattr(SecClient, "daily_index", lambda self, day: [("10-Q", 2, day.replace("-", "")),
                                                                     ("8-K", 1, day.replace("-", ""))])
    monkeypatch.setattr(U, "latest_year", lambda today=None: 2025)
    return app_client


def test_size_bucket_handles_negative_and_missing_revenue():
    assert U.size_bucket(-5e6) == "mikro" and U.size_bucket(None) is None and U.size_bucket(2e10) == "gross"


def test_universe_screening_inflection_resume_and_negative_signals(engine_client):
    ctx = engine_client.app.state.ctx
    sec = SecClient(ctx)
    sync = U.sync(ctx, sec)
    assert sync["aktiv"] == 5  # Meta Materials (nur OTC) gehört nicht ins Universum
    assert ctx.db.scalar("SELECT COUNT(*) FROM disc_universe WHERE cik=3") == 0
    idx = U.scan_filings(ctx, sec, days=3)
    assert idx["neue_einreichungen"] >= 1 and ctx.db.q1("SELECT filed_seen FROM disc_universe WHERE cik=2")[0]
    pre = U.prescreen(ctx, sec)
    assert pre["vorauswahl"] == 2 and pre["bewertet"] == 5  # Beta und Gamma (gleiche Zahlen)
    flags = json.loads(ctx.db.q1("SELECT pre_json FROM disc_universe WHERE cik=2")["pre_json"])["flags"]
    assert set(flags) >= {"marge", "hebel"}
    neg = json.loads(ctx.db.q1("SELECT pre_json FROM disc_universe WHERE cik=6")["pre_json"])
    assert "umsatzrueckgang" in neg["flags"] and ctx.db.q1("SELECT state FROM disc_universe WHERE cik=6")[0] == \
        "unauffaellig"  # negative Signale führen nie zur Vorauswahl
    out = U.screening(ctx, None, batch=1)  # Stapel 1: fortsetzbar
    assert out["bestaetigung"]["geprueft"] == 1
    out2 = U.screening(ctx, None, batch=5)
    assert out2["bestaetigung"]["geprueft"] == 1  # nur der noch offene Emittent
    states = {r["cik"]: r["state"] for r in ctx.db.q("SELECT cik, state FROM disc_universe WHERE cik IN (2, 5)")}
    assert states == {2: "bestaetigt", 5: "verworfen"}  # Gamma: SPAC abgelehnt
    beta = SV.by_ticker(ctx.db, "BETA")
    assert beta["origin"] == "screening"
    sig = ctx.db.q1("SELECT * FROM disc_signal WHERE company_id=? AND kind='wendepunkt'", (beta["id"],))
    assert sig is not None and "Geschäftsjahre" in sig["detail"]
    cands = {r["status"] for r in ctx.db.q("SELECT status FROM disc_candidate WHERE path='wendepunkt'")}
    assert "neu" in cands
    assert U.screening(ctx, None, batch=5)["bestaetigung"]["geprueft"] == 0  # nichts doppelt
    prog = EN.screening_progress(ctx)
    assert prog["total"] == 5 and prog["checked"] == 2 and prog["pending"] == 0


def test_mispricing_distinguishes_chance_from_value_trap(engine_client, monkeypatch):
    ctx = engine_client.app.state.ctx
    sec = SecClient(ctx)
    rows_down = [("2025-11-05", 100.0), ("2026-11-05", 75.0)]
    rows_up = [("2025-11-05", 100.0), ("2026-11-05", 112.0)]
    monkeypatch.setattr(SV, "_adj_history", lambda c, sym: rows_up if sym in SV.BENCHMARKS else rows_down)
    from app.discovery.identity import Issuer

    good, _ = SV.upsert_issuer(ctx, Issuer(1, "Alpha Robotics Inc.", ["ALPH"], ["Nasdaq"]), "screening")
    trap, _ = SV.upsert_issuer(ctx, Issuer(6, "Alphabet Inc.", ["GOOGL"], ["Nasdaq"]), "screening")
    assert U.mispricing(ctx, sec, good, None)["status"] == "moeglich"
    t = U.mispricing(ctx, sec, trap, None)
    assert t["status"] == "value_trap" and any("Umsatz" in x for x in t["traps"])
    assert ctx.db.q1("SELECT direction FROM disc_signal WHERE company_id=? AND kind='fehlbewertung'", (trap,))[0] == \
        "negativ"
    monkeypatch.setattr(SV, "_adj_history", lambda c, sym: rows_up)
    assert U.mispricing(ctx, sec, good, None)["status"] == "kein_rueckstand"  # Kursrückgang allein ist kein Signal


def _discovery_ai(seen: list[tuple[str, str]], themes: dict[str, Any]) -> Any:
    def gen(self, system, user, schema, max_tokens, json_mode=False):
        if system == EN.THEMES_SYSTEM:
            seen.append(("themen", user))
            return AiResult(json.dumps(themes), 900, 700, "gemini", "gemini-test")
        if system == EN.THESES_SYSTEM:
            seen.append(("thesen", user))
            units = json.loads(user)["unternehmen"]
            return AiResult(json.dumps({"unternehmen": [{
                "key": u["key"], "besonderheit": "Roboter für Fabriken", "geld": "Verkauf und Wartung",
                "nachfrage": "wachsende Aufträge", "langfristig": "Automatisierung", "vorteile": "Software",
                "widerlegung": "Preisdruck durch Wettbewerber", "stufen": {"chance": 2, "besonderheit": 2,
                                                                          "nachfrage": 1},
                "belege": [{"id": f"{u['key']}-S", "zitat": "Alpha sells robots. Revenue was 742 million dollars"}],
                "konfidenz": 0.7} for u in units]}), 900, 700, "gemini", "gemini-test")
        from tests.test_discovery import fake_parts

        return fake_parts(seen)(self, system, user, schema, max_tokens, json_mode)  # vertiefte Analyse (3 Teile)
    return gen


THEMES = {"themen": [{"thema": "Fabrikautomatisierung",
                      "fakten": [{"text": "Beta verbessert Margen", "belege": ["B1"]}],
                      "hypothese": "Automatisierung könnte zunehmen", "gegenargument": "Zyklische Nachfrage",
                      "wertschoepfung": [
                          {"stufe": "Roboter", "rolle": "direkt", "unternehmen": [
                              {"name": "Alpha Robotics", "ticker": "ALPH", "begruendung": "Robotik"},
                              {"name": "Alpha Robotics Inc.", "ticker": "ALPH", "begruendung": "doppelt"},
                              {"name": "Alpha Robotics", "ticker": "BETA", "begruendung": "falscher Ticker"},
                              {"name": "OpenAI", "ticker": "OPAI", "begruendung": "privat"},
                              {"name": "Meta Materials", "ticker": "MTA", "begruendung": "nur OTC"}]}]},
                     {"thema": "Ohne Beleg", "fakten": [{"text": "x", "belege": ["B99"]}], "wertschoepfung": []}]}


def test_ki_discovery_finds_companies_outside_news_and_verifies_identity(engine_client, monkeypatch):
    c = engine_client
    ctx = c.app.state.ctx
    _ai_on(ctx, monkeypatch)
    sec = SecClient(ctx)
    U.sync(ctx, sec)
    U.prescreen(ctx, sec)
    assert ctx.db.scalar("SELECT COUNT(*) FROM news_item") == 0  # keine Meldungen: Discovery unabhängig von News
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr("app.news.ai.AiGateway.generate", _discovery_ai(seen, THEMES))
    r = c.post("/discovery/start/ki_discovery", data={"csrf_token": c.token}, follow_redirects=False)
    jid = int(r.headers["location"].split("job=")[1])
    jb = J.get(ctx.db, jid)
    assert jb["status"] == "abgeschlossen", jb
    res = jb["result"]
    assert [t["thema"] for t in res["themen"]] == ["Fabrikautomatisierung"]  # Thema ohne gültigen Beleg verworfen
    assert res["neu"] == 1 and res["kandidaten"][0]["ticker"] == "ALPH"
    reasons = " | ".join(v["grund"] for v in res["verworfen"])
    assert "gehört zu" in reasons and "OpenAI" in reasons and "OTC" in reasons
    alpha = SV.by_ticker(ctx.db, "ALPH")
    assert alpha["origin"] == "ki" and SV.analysis(ctx.db, alpha["id"]) is not None  # fundamental validiert
    cand = ctx.db.q1("SELECT * FROM disc_candidate WHERE company_id=? AND path='ki'", (alpha["id"],))
    assert cand["status"] == "neu" and cand["priority"] > 50 and cand["theme"] == "Fabrikautomatisierung"
    sig = ctx.db.q1("SELECT * FROM disc_signal WHERE company_id=? AND kind='ki_discovery'", (alpha["id"],))
    assert "Preisdruck" in sig["detail"]  # Widerlegung gehört zur Discovery-These
    assert [s for s, _ in seen] == ["themen", "thesen", "bericht", "disruption", "nash"]  # + 1 vertiefte Analyse
    assert res["analysiert"][0]["ki"] == "vollstaendig"
    payload = json.loads(seen[0][1])
    assert set(payload) == {"ausgangsbelege"} and all(e["id"].startswith(("B", "E")) for e in
                                                       payload["ausgangsbelege"])
    calls = ctx.db.q("SELECT step, status, job_id FROM disc_ai_call WHERE job_id=?", (jid,))
    assert {r_["step"] for r_ in calls} == {"themen", "thesen", "bericht", "disruption", "nash"}
    assert all(r_["status"] == "ok" for r_ in calls)  # jeder Aufruf dem Job zugeordnet
    # erneuter Lauf: keine künstlichen Neuentdeckungen
    r2 = c.post("/discovery/start/ki_discovery", data={"csrf_token": c.token}, follow_redirects=False)
    res2 = J.get(ctx.db, int(r2.headers["location"].split("job=")[1]))["result"]
    assert res2["neu"] == 0 and res2["bekannt"] == 1
    radar = c.get("/discovery?cat=neu").text
    assert "Alpha Robotics" in radar and "KI-Discovery" in radar


def test_ki_discovery_without_ai_or_budget_is_explained(engine_client, monkeypatch):
    ctx = engine_client.app.state.ctx
    jid, _ = J.create(ctx, "ki_discovery")
    J.run(ctx, jid)
    jb = J.get(ctx.db, jid)
    assert jb["status"] == "ki_fehlt" and "ausgeschaltet" in jb["error"]
    _ai_on(ctx, monkeypatch)
    ctx.settings.set("discovery.ai_calls_per_day", 1)
    jid2, _ = J.create(ctx, "ki_discovery")
    J.run(ctx, jid2)
    assert J.get(ctx.db, jid2)["status"] == "kontingent"


def test_full_run_continues_after_partial_failures(engine_client, monkeypatch):
    ctx = engine_client.app.state.ctx

    def broken(self, tag, period, unit="USD", tax="us-gaap"):
        raise SecError("SEC down")

    monkeypatch.setattr(SecClient, "frame", broken)
    jid, _ = J.create(ctx, "voll")
    J.run(ctx, jid)
    jb = J.get(ctx.db, jid)
    assert set(jb["result"]) >= {"news", "screening", "ki_discovery"}
    assert jb["result"]["ki_discovery"]["status"] == "ki_fehlt" and jb["status"] in ("teilweise", "fehlgeschlagen")


# ------------------------------------------------------------------------------------------------ Bewertung

def test_classic_peg_and_revenue_analogue_are_separate():
    fu = F.parse(grower())
    m = V.market_metrics(fu, {"price": 50.0, "ccy": "USD", "market_cap": 5e9, "fx": 1.0})
    assert m["eps_growth"] == pytest.approx(1.3 / 1.01 - 1, rel=1e-3)  # Ergebnis/Aktie: +30 % Gewinn, +1 % Aktien
    assert m["peg"] == pytest.approx(m["pe"] / (m["eps_growth"] * 100))
    assert m["pe_rev_growth"] == pytest.approx(m["pe"] / 30.0)
    v = V.valuation_score(fu, m, V.scenarios(fu, m), 1.0)
    crit = next(c for c in v.criteria if c.key == "v_wachstum")
    assert crit.label == "Wachstum relativ zur Bewertung (PEG)" and "Ergebnisses je Aktie" in crit.definition


def test_valuation_profiles_by_company_type():
    grow = V.scenarios(F.parse(grower()), V.market_metrics(F.parse(grower()), {"price": 50.0, "ccy": "USD",
                                                                                "market_cap": 5e9, "fx": 1.0}))
    burn = V.scenarios(F.parse(burner()), V.market_metrics(F.parse(burner()), {"price": 5.0, "ccy": "USD",
                                                                                "market_cap": 3e8, "fx": 1.0}))
    assert grow["profile"] == "wachstum" and grow["discount"] == 0.09
    assert burn["profile"] == "frueh" and burn["discount"] == 0.11
    assert V.PROFILES["reif"]["multiples"]["basis"] < V.PROFILES["wachstum"]["multiples"]["basis"]


# ------------------------------------------------------------------------------------------------ Export/Restore

def test_discovery_export_restore_roundtrip(app_client, db):
    ctx = app_client.app.state.ctx
    cid = _cid(_add(app_client, "ALPH"))
    SV.analyse_company(ctx, cid, mode="ergaenzen")
    jid, _ = J.create(ctx, "screening")
    SV.record_candidate(ctx.db, jid, "wendepunkt", name="Alpha", ticker="ALPH", cik=1, company_id=cid,
                        status="neu", reason="Test")
    data = json.loads(json.dumps(BK.collect(ctx.db), default=str))
    with db.transaction() as c:
        n = BK.restore(c, data)
    assert n > 0
    assert db.scalar("SELECT COUNT(*) FROM disc_company") == 1
    assert db.scalar("SELECT COUNT(*) FROM disc_analysis") == 1 and db.scalar("SELECT COUNT(*) FROM disc_ai_part") == 3
    assert db.q1("SELECT status FROM disc_job")[0] == "unterbrochen"  # aktiver Job des alten Systems
    assert db.scalar("SELECT COUNT(*) FROM disc_candidate") == 1
    with db.transaction() as c:
        assert BK.restore(c, data) == 0  # wiederholbar ohne Dubletten
    from app import fullexport

    st = json.loads(fullexport.collect(ctx, set())[fullexport.STATE])
    assert st["discovery"]["companies"][0]["ticker"] == "ALPH"
    assert not fullexport.validate({}, {"discovery": st["discovery"]})
    assert fullexport.validate({}, {"discovery": "kaputt"})


def test_privacy_holdings_never_in_ai_payloads(engine_client, monkeypatch):
    """Portfolio-Informationen bleiben lokal – auch bei KI-Discovery und Teilanalysen."""
    from app.discovery import fit as FIT
    from tests.test_discovery import fake_parts

    ctx = engine_client.app.state.ctx
    monkeypatch.setattr(FIT, "holdings", lambda ctx_: [{"name": "Geheimdepot AG", "ticker": "SECRET", "value": 1e6,
                                                        "weight": 0.5}])
    _ai_on(ctx, monkeypatch)
    sec = SecClient(ctx)
    U.sync(ctx, sec)
    U.prescreen(ctx, sec)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr("app.news.ai.AiGateway.generate", _discovery_ai(seen, THEMES))
    jid, _ = J.create(ctx, "ki_discovery")
    J.run(ctx, jid)
    seen2: list[tuple[str, str]] = []
    monkeypatch.setattr("app.news.ai.AiGateway.generate", fake_parts(seen2))
    SV.analyse_company(ctx, SV.by_ticker(ctx.db, "ALPH")["id"], mode="ki")
    for _step, user in seen + seen2:
        for secret in ("Geheimdepot", "SECRET", "1000000", "weight", "Depot"):
            assert secret not in user
