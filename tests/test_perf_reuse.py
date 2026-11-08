"""Leistung ohne Verhaltensänderung: eine Diagnose je Seitenaufruf (Arbeitsplan, Assistent) und eine Kennzahlberechnung
je tatsächlichem Zeitraum auf der Performance-Seite. Ergebnisse werden streng mit der unabhängigen Berechnung verglichen
(ohne gemeinsamen Bericht bzw. je Zeitraum neu berechnet – so wie vor der Optimierung)."""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.analytics import periods as P
from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis import engine as E
from app.diagnosis import guide as G
from app.diagnosis import workplan as WP
from app.diagnosis.engine import report_for
from app.main import build_app
from tests.test_diagnosis import PF_HEADER, TOKA_ASSETS, TOKA_QTY, client_with_import, popkat_like_rows, upload

_METRICS = P.metrics  # Original, vor jedem Zählen
SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"
STAMP = "2026-01-01T00:00:00Z"


class Count:
    """Zählt Aufrufe einer Modulfunktion (Original bleibt wirksam)."""

    def __init__(self, mp: pytest.MonkeyPatch, mod: Any, name: str) -> None:
        self.n = 0
        self.calls: list[tuple[Any, ...]] = []
        orig = getattr(mod, name)

        def wrapped(*a: Any, **k: Any) -> Any:
            self.n += 1
            self.calls.append(a)
            return orig(*a, **k)

        mp.setattr(mod, name, wrapped)


# ------------------------------------------------------------------------------------------- Diagnose je Anfrage

@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def _add_case(ctx: Any, key: str, findings: list[str]) -> None:
    import json

    ctx.db.x("INSERT INTO clarify_case(case_key, kind, status, title, accounts_json, assets_json, finding_ids_json, "
             "tx_ids_json, dims_json, state_json, evidence_fp, data_version, note, opened_count, created_at, "
             "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (key, "group", "wartet_info", "Testfall", "[]", "[]", json.dumps(findings), "[]", "{}",
              json.dumps({"findings": [{"id": f} for f in findings]}), "fp", 1, "", 1, STAMP, STAMP))


@pytest.fixture
def mixed(cfg):
    """Alle Aufgabenarten: Befund-Fragen, Prüfstapel (sicher gesammelt), Einzelzeilen (auch unvollständig) und ein
    Klärungsfall mit eigener Seite."""
    c = client_with_import(cfg, popkat_like_rows(), TOKA_ASSETS, valuation="2025-06-30")
    data = PF_HEADER + f"N1,2025-03-10T12:00:00Z,deposit,,,,,Wallet K,TOKA,{TOKA_QTY},,,,0,nachgetragen\n" \
                       "N2,2025-03-12T12:00:00Z,deposit,,,,,Wallet K,TOKA,1.5,,,,0,anderer Vorgang\n" \
                       "N3,2025-03-13T12:00:00Z,deposit,,,,,Wallet K,TOKA,,,,,0,ohne Menge\n"
    upload(c, "manuell.csv", data)
    ctx = c.app.state.ctx
    ids = [f.id for f in report_for(ctx).findings if f.parent is None][:2]
    _add_case(ctx, "kg-perf", ids)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _norm(x: Any) -> str:
    """Vergleichsform: Einmal-Bestätigungstoken des Prüfstapels (je Anzeige zufällig, ``batch``) ausgenommen."""
    return re.sub(r'(\\?"token\\?": \\?")[0-9a-f]{24}', r"\1…", repr(x))


def _old_step(ctx: Any, skip: list[str]) -> G.Step:
    """Wie vor der Optimierung: ohne Seitenbereich erzeugt jede Verwendung ihren eigenen Bericht."""
    return G._step(ctx, skip, hints=False, focus="")


def test_each_task_kind_one_diagnosis_per_assistant_page_and_identical_card(mixed, monkeypatch):
    ctx = mixed.app.state.ctx
    ts = G.tasks(ctx)
    kinds = {t.kind for t in ts}
    assert {"frage", "stapel", "zeile", "seite"} <= kinds, kinds
    d = Count(monkeypatch, E, "diagnose")
    for kind in sorted(kinds):
        t = next(x for x in ts if x.kind == kind)
        skip = [x.kid for x in ts if x.kid != t.kid]
        d.n = 0
        r = mixed.get(f"/quality/assistent?new=1&skip={','.join(skip)}")
        assert r.status_code == 200 and f'name="kid" value="{t.kid}"' in r.text, kind
        assert d.n == 1, (kind, d.n)
        d.n = 0
        new = G.step(ctx, skip)
        assert d.n == 1, (kind, d.n)
        assert _norm(new) == _norm(_old_step(ctx, skip)), kind  # Karte, Antworten, Wirkung, Zählung unverändert
    # keine offene Aufgabe: Restübersicht und Abschluss aus derselben Diagnose
    d.n = 0
    r = mixed.get(f"/quality/assistent?new=1&skip={','.join(x.kid for x in ts)}")
    assert r.status_code == 200 and 'name="kid"' not in r.text and d.n == 1
    with G.page_scope(ctx):
        scoped = G.rest_overview(ctx)
    assert scoped == G.rest_overview(ctx, report_for(ctx))


def test_quality_plan_one_diagnosis_and_same_plan_and_count(mixed, monkeypatch):
    import app.diagnosis.dq_web as W

    ctx = mixed.app.state.ctx
    seen: dict[str, Any] = {}
    orig = W.render
    monkeypatch.setattr(W, "render", lambda req, tpl, **kw: (seen.update(kw), orig(req, tpl, **kw))[1])
    d = Count(monkeypatch, E, "diagnose")
    r = mixed.get("/quality/plan")
    assert r.status_code == 200 and d.n == 1
    assert repr(seen["plan"]) == repr(WP.build(ctx))  # unabhängig, mit eigenem Bericht
    n, themes, minutes = G.overview(ctx)
    assert (seen["assist_n"], seen["themes"], seen["assist_min"]) == (n, themes, minutes)
    assert n == len(G.tasks(ctx))


def test_similar_and_case_detail_unchanged_with_one_report(mixed, monkeypatch):
    ctx = mixed.app.state.ctx
    rep = report_for(ctx)
    fr = next(t for t in G.tasks(ctx) if t.kind == "frage")
    sig = G.signature(rep.by_id(fr.finding_id))
    old = G._similar(ctx, rep, sig, key="", limit=G.BATCH_LIMIT)
    d = Count(monkeypatch, E, "diagnose")
    assert G.similar(ctx, sig, "mark:dismiss") == old and d.n == 1
    case = next(t for t in G.tasks(ctx) if t.kind == "seite")
    d.n = 0
    with G.page_scope(ctx):
        a = G.detail(ctx, next(t for t in G.tasks(ctx) if t.kid == case.kid))
    assert d.n == 1
    b = G.detail(ctx, next(t for t in G.tasks(ctx) if t.kid == case.kid))
    assert repr(a) == repr(b)


def test_scope_report_follows_data_changes_and_is_not_shared(mixed, monkeypatch):
    ctx = mixed.app.state.ctx
    d = Count(monkeypatch, E, "diagnose")
    with G.page_scope(ctx):
        r1 = G.scoped_report(ctx)
        assert G.scoped_report(ctx) is r1 and d.n == 1  # gleicher Datenstand: wiederverwendet
        f = next(x for x in r1.findings if x.parent is None)
        v = G._report_version(ctx.db)
        assert A.mark(ctx, f.id, "dismiss", "test").ok  # Entscheidung (kein Buchungsdatenstand)
        assert G._report_version(ctx.db) != v
        d.n = 0
        r2 = G.scoped_report(ctx)
        assert r2 is not r1 and d.n == 1  # Datenstand geändert: neuer Bericht, keine veraltete Wiederverwendung
        assert f.id not in {t.finding_id for t in G.tasks(ctx)}  # entschieden: keine Aufgabe mehr
        with G.page_scope(ctx):  # verschachtelt: derselbe Bereich
            assert G.scoped_report(ctx) is r2
    d.n = 0
    G.scoped_report(ctx)
    G.scoped_report(ctx)
    assert d.n == 2  # außerhalb eines Seitenaufrufs kein Zwischenspeicher
    # anderer Thread sieht den Bereich nicht (gleichzeitige Anfragen teilen keinen Bericht)
    got: list[Any] = []
    with G.page_scope(ctx):
        mine = G.scoped_report(ctx)
        th = threading.Thread(target=lambda: got.append(G.scoped_report(ctx)))
        th.start()
        th.join()
        assert got[0] is not mine and G.scoped_report(ctx) is mine


def test_deferred_answer_is_reflected_on_the_next_page(mixed, monkeypatch):
    """Eine Antwort (Schreiben) liegt vor dem Seitenbereich der nächsten Seite: die Aufgabe ist dort weg."""
    ctx = mixed.app.state.ctx
    t = next(x for x in G.tasks(ctx) if x.kind == "frage")
    r = mixed.post("/quality/assistent/antwort", data={"csrf_token": mixed.token, "f": t.finding_id, "kid": t.kid,
                                                         "a": "mark:defer", "typ": "frage", "before": "5"},
                   follow_redirects=False)
    assert r.status_code == 303
    d = Count(monkeypatch, E, "diagnose")
    page = mixed.get(r.headers["location"])
    assert page.status_code == 200 and d.n == 1
    assert t.kid not in {x.kid for x in G.tasks(ctx)}
    assert G.rest_overview(ctx)["findings_deferred"] >= 1


# ------------------------------------------------------------------------------------------- Performance-Kennzahlen

@pytest.fixture(scope="module")
def perf_client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("perf")
    imp = tmp / "import"
    imp.mkdir()
    dst = imp / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    cfg = Config(data_dir=tmp / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False, startup_jobs=False,
                 log_format="text", secrets=Secrets())
    from app.jobs import tasks

    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        c.get("/settings")
        c.post("/actions/import/check", headers={"X-CSRF-Token": c.cookies.get("portfolia_csrf")})
        ctx = c.app.state.ctx
        tasks.refresh_prices(ctx, force=True)
        tasks.backfill(ctx)
        yield c


def _capture(mp: pytest.MonkeyPatch) -> dict[str, Any]:
    import app.web.routes.pages as PG

    seen: dict[str, Any] = {}
    orig = PG.render
    mp.setattr(PG, "render", lambda req, tpl, **kw: (seen.update(kw), orig(req, tpl, **kw))[1])
    return seen


def _same(x: Any, y: Any) -> bool:
    return repr(x) == repr(y)  # streng: auch Typ, None, Vorzeichen und alle Nachkommastellen


@pytest.mark.parametrize("query", [
    "", "?period=1J", "?period=1j", "?period=MAX", "?period=max", "?period=YTD", "?period=1M", "?period=3J",
    "?period=5J", "?period=unbekannt", "?scope=segment:Krypto&period=3M", "?scope=segment:Aktien&period=MAX",
    "?scope=asset:BTC&period=6M", "?start=2023-01-01&end=2024-06-30", "?start=2023-01-01", "?end=2024-06-30",
    "?start=1990-01-01", "?start=kein-datum&period=1J",
])
def test_performance_metrics_once_per_period_and_identical(perf_client, monkeypatch, query):
    import app.web.routes.pages as PG

    ctx = perf_client.app.state.ctx
    seen = _capture(monkeypatch)
    m = Count(monkeypatch, P, "metrics")
    irr = Count(monkeypatch, P, "irr_period_detail")
    r = perf_client.get("/performance" + query)
    assert r.status_code == 200 and seen["rows"]
    hist, s = m.calls[0][0], m.calls[0][1]
    assert all(c[0] is hist and c[1] is s for c in m.calls)
    keys = [P.bounds(hist, k) for k in P.PERIODS]
    q = dict(x.split("=", 1) for x in query.lstrip("?").split("&") if x)
    sd, ed = PG.parse_d(q.get("start")), PG.parse_d(q.get("end"))
    sel_key = P.bounds(hist, start=sd, end=ed) if sd or ed else P.bounds(hist, q.get("period", "1J"))
    assert m.n == len(set(keys) | {sel_key}) == irr.n  # jeder tatsächliche Zeitraum genau einmal
    assert m.n == len(m.calls) and len({c[2:] for c in m.calls}) == m.n
    # Referenz: jede Zeile und die Auswahl unabhängig neu berechnet (wie vor der Optimierung)
    ref_rows = [{"key": k, "label": P.PERIOD_LABELS[k], **_METRICS(hist, s, *b)}
                for k, b in zip(P.PERIODS, keys, strict=True)]
    assert _same(seen["rows"], ref_rows)
    ref_sel = _METRICS(hist, s, *sel_key)
    ref_sel["quality"] = P.valuation_state(hist, sel_key[0], sel_key[1], PG.scope_assets(ctx, q.get("scope",
                                                                                                    "total")))
    assert _same(seen["sel"], ref_sel)
    assert all("quality" not in row for row in seen["rows"])  # die Ergänzung der Auswahl wirkt nicht auf die Tabelle


def test_performance_no_reuse_across_requests_and_after_data_change(perf_client, monkeypatch):
    """Wiederverwendung nur innerhalb einer Anfrage: jede Anfrage rechnet alle Zeiträume neu, nach einer Datenänderung
    aus dem neuen Datenstand."""
    ctx = perf_client.app.state.ctx
    seen = _capture(monkeypatch)
    m = Count(monkeypatch, P, "metrics")
    perf_client.get("/performance?period=MAX")
    first, n1, hist1 = seen["rows"], m.n, m.calls[0][0]
    perf_client.get("/performance?period=MAX")
    assert m.n == 2 * n1
    from app.journal.service import journal_service

    assert journal_service(ctx).delete_import("DEMO-00002")  # Kauf 2022 löschen (umkehrbar)
    try:
        m.calls.clear()
        perf_client.get("/performance?period=MAX")
        hist, s = m.calls[0][0], m.calls[0][1]
        assert hist is not hist1 and not _same(seen["rows"], first)
        ref = [{"key": k, "label": P.PERIOD_LABELS[k], **_METRICS(hist, s, *P.bounds(hist, k))} for k in P.PERIODS]
        assert _same(seen["rows"], ref)
    finally:
        assert journal_service(ctx).restore_import("DEMO-00002")


def test_performance_with_missing_prices_identical(perf_client, monkeypatch):
    """Fehlende Kurse (Bewertungslücken, geschätzte Tage): Kennzahlen weiterhin je Zeitraum identisch mit der
    unabhängigen Berechnung."""
    ctx = perf_client.app.state.ctx
    series = ctx.db.scalar("SELECT series FROM price_daily GROUP BY series ORDER BY COUNT(*) DESC LIMIT 1")
    ctx.db.x("CREATE TEMP TABLE keep_prices AS SELECT * FROM price_daily WHERE series=? AND date BETWEEN "
             "'2023-02-01' AND '2023-09-30'", (series,))
    ctx.db.x("DELETE FROM price_daily WHERE series=? AND date BETWEEN '2023-02-01' AND '2023-09-30'", (series,))
    ctx.invalidate_data()
    try:
        for query in ("?period=MAX", "?start=2023-01-01&end=2024-06-30", "?period=3J"):
            seen = _capture(monkeypatch)
            m = Count(monkeypatch, P, "metrics")
            assert perf_client.get("/performance" + query).status_code == 200
            hist, s = m.calls[0][0], m.calls[0][1]
            ref = [{"key": k, "label": P.PERIOD_LABELS[k], **_METRICS(hist, s, *P.bounds(hist, k))}
                   for k in P.PERIODS]
            assert _same(seen["rows"], ref), query
            assert m.n == len({c[2:] for c in m.calls})
            monkeypatch.undo()
    finally:
        ctx.db.x("INSERT OR REPLACE INTO price_daily SELECT * FROM keep_prices")
        ctx.db.x("DROP TABLE keep_prices")
        ctx.invalidate_data()


def test_custom_range_equal_to_a_standard_period_is_computed_once(perf_client, monkeypatch):
    """Eigene Grenzen, die einem Standardzeitraum entsprechen (gleicher Start- und Endindex), werden nicht doppelt
    berechnet – Auswahl und Tabellenzeile sind identisch."""
    ctx = perf_client.app.state.ctx
    hist = ctx.history_with_live()
    start = P.base_date("1J", hist.dates[-1])
    seen = _capture(monkeypatch)
    m = Count(monkeypatch, P, "metrics")
    assert perf_client.get(f"/performance?start={start.isoformat()}").status_code == 200
    h = m.calls[0][0]
    assert P.bounds(h, start=start) == P.bounds(h, "1J")
    assert m.n == len({P.bounds(h, k) for k in P.PERIODS})
    row = next(r for r in seen["rows"] if r["key"] == "1J")
    sel = {k: v for k, v in seen["sel"].items() if k != "quality"}
    assert _same(sel, {k: v for k, v in row.items() if k not in ("key", "label")})
