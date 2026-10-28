"""Logging für die Docker-Konsole: Anfrage-ID, Fehlerzeile, Zugriffsprotokoll, Jobs, Banner, Flutschutz."""

from __future__ import annotations

import dataclasses
import json
import logging
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import health
from app.config import Config, Secrets
from app.jobs import tasks
from app.logging_setup import (
    FloodFilter,
    JsonFormatter,
    SecretRedactor,
    TextFormatter,
    parse_levels,
    recent_lines,
    request_id,
)
from app.main import build_app
from app.util import http as uhttp
from app.web import accesslog


def _client(config: Config, **kw) -> TestClient:
    app = build_app(config, start_scheduler=False)

    def boom() -> None:
        raise ValueError("kaputt-marker")

    app.add_api_route("/__boom", boom)
    app.add_api_route("/__slow", lambda: {"ok": 1})
    return TestClient(app, raise_server_exceptions=False, **kw)


def _lines(marker: str) -> list[str]:
    """Zeilen mit ``marker`` – ohne die des Testclients selbst (Logger ``httpx2``)."""
    return [ln for ln in recent_lines() if marker in ln and " httpx2:" not in ln]


def test_defaults_are_text_format_and_slow_access_log():
    cfg = Config()
    assert cfg.log_format == "text" and cfg.log_access == "slow" and cfg.log_levels == ""


def test_unhandled_error_is_one_line_with_method_path_rid_and_place(config):
    with _client(config) as c:
        r = c.get("/__boom?key=GEHEIM123&tab=x")
        assert r.status_code == 500
        rid = r.headers.get("X-Request-ID") or ""
        assert "Fehler-ID" in r.text
        rid = rid or r.text.split("<code>")[1].split("</code>")[0]
        lines = _lines("kaputt-marker")
        assert len(lines) == 1
        line = lines[0]
        assert "ERROR" in line and "GET /__boom?[key,tab]" in line and "ValueError: kaputt-marker" in line
        assert "GEHEIM123" not in line and rid in r.text
        assert "Traceback" not in line  # bei INFO nur die kompakte Zeile
        assert "tests/test_logging.py" in line or "test_logging.py" in line  # Fundstelle im eigenen Code


def test_api_errors_are_json_with_request_id(config):
    with _client(config) as c:
        c.app.add_api_route("/api/__boom", lambda: 1 / 0)
        r = c.get("/api/__boom")
        assert r.status_code == 500 and r.json()["error"] == "internal error" and r.json()["rid"]


def test_request_id_header_and_default_slow_mode_is_silent_for_fast_requests(config):
    with _client(config) as c:
        r = c.get("/healthz?marker=fastmarker")
        assert len(r.headers["X-Request-ID"]) == 6
        assert not _lines("fastmarker")  # schnelle Anfrage: keine Zeile


def test_slow_and_failed_requests_are_logged_without_query_values(config, monkeypatch):
    monkeypatch.setattr(accesslog, "SLOW_S", 0.0)
    with _client(config) as c:
        c.get("/__slow?token=NIEMALS&days=7")
    slow = _lines("GET /__slow")
    assert slow and "WARNING" in slow[-1] and "?[token,days]" in slow[-1] and "NIEMALS" not in "".join(_lines(""))


def test_client_errors_are_info_not_alarms_and_stay_out_of_the_error_log(config):
    with _client(config) as c:
        c.get("/gibt-es-nicht-xyz")
        assert c.post("/settings/irgendwas", data={}).status_code == 403  # ohne CSRF-Token
        rows = c.app.state.ctx.db.scalar("SELECT COUNT(*) FROM event_log WHERE message LIKE '%403%'", default=0)
    for marker in ("GET /gibt-es-nicht-xyz", "POST /settings/irgendwas"):
        lines = _lines(marker)
        assert lines and "INFO" in lines[-1]
    assert rows == 0


def test_access_log_all_mode_skips_polling_endpoints(config):
    with _client(dataclasses.replace(config, log_access="all")) as c:
        c.get("/__slow?allmarker=1")
        c.get("/healthz?allmarker=1")
        c.get("/news/pipeline/live?allmarker=1")
    assert _lines("GET /__slow?[allmarker]")
    assert not _lines("/healthz?[allmarker") and not _lines("/pipeline/live?[allmarker")


def test_access_log_off(config, monkeypatch):
    monkeypatch.setattr(accesslog, "SLOW_S", 0.0)
    with _client(dataclasses.replace(config, log_access="off")) as c:
        c.get("/__slow?offmarker=1")
    assert not _lines("offmarker")


def test_safe_target_keeps_keys_only_and_limits_length():
    assert accesslog.safe_target("/a", "x=1&y=2&x=3") == "/a?[x,y]"
    assert accesslog.safe_target("/a", "") == "/a"
    assert len(accesslog.safe_target("/" + "p" * 500, "")) <= 200


def test_run_job_logs_duration_and_full_traceback_on_error(config, monkeypatch):
    ctx = build_app(config, start_scheduler=False).state.ctx
    ctx.startup()
    monkeypatch.setattr(tasks, "SLOW_JOB_S", 0.0)
    res = {"neu": 3, "liste": [1], "text": "x"}
    assert tasks.run_job(ctx, "t_ok", lambda: res) == res
    ok = _lines("Job t_ok ok")
    assert ok and "neu=3" in ok[-1] and "liste" not in ok[-1] and "job=t_ok" in ok[-1]

    def bad() -> None:
        raise RuntimeError("job-kaputt-marker")

    assert tasks.run_job(ctx, "t_bad", bad) is None
    err = "\n".join(recent_lines())
    assert "Job t_bad fehlgeschlagen" in err and "RuntimeError: job-kaputt-marker" in err and "Traceback" in err
    assert ctx.db.q1("SELECT last_ok, last_error FROM job_status WHERE job='t_bad'")["last_ok"] == 0


def test_fast_job_stays_off_the_info_log(config):
    ctx = build_app(config, start_scheduler=False).state.ctx
    ctx.startup()
    tasks.run_job(ctx, "t_fast_marker", lambda: {"n": 1})
    assert not _lines("Job t_fast_marker ok")  # < 5 s: nur DEBUG


def test_context_vars_reach_log_records():
    rec_lines: list[str] = []

    class H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rec_lines.append(TextFormatter(SecretRedactor()).format(record))

    from app.logging_setup import ContextFilter

    h = H()
    h.addFilter(ContextFilter())
    lg = logging.getLogger("app.test_ctx")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    token = request_id.set("abc123")
    try:
        lg.info("hallo", extra={"status": 200})
    finally:
        request_id.reset(token)
        lg.removeHandler(h)
    assert "hallo" in rec_lines[0] and "rid=abc123" in rec_lines[0] and "status=200" in rec_lines[0]


def test_text_formatter_extras_and_exception_json_unchanged():
    red = SecretRedactor(["GEHEIM-ABC"])
    rec = logging.LogRecord("app.x", logging.WARNING, "f.py", 1, "Test GEHEIM-ABC %s", ("v",), None)
    rec.job = "news fetch"
    line = TextFormatter(red).format(rec)
    assert "WARNING" in line and "app.x: Test *** v" in line and 'job="news fetch"' in line
    j = json.loads(JsonFormatter(red).format(rec))
    assert j["msg"] == "Test *** v" and j["job"] == "news fetch" and j["level"] == "WARNING"


def test_flood_filter_limits_identical_warnings_and_reports_suppressed():
    f = FloodFilter(limit=3, window_s=0.05)

    def rec(i: int) -> logging.LogRecord:
        return logging.LogRecord("app.n", logging.WARNING, "f.py", 1, "Netzwerkfehler bei %s", (f"host{i}",), None)

    passed = [r for r in (rec(i) for i in range(100)) if f.filter(r)]
    assert len(passed) == 3
    time.sleep(0.06)
    nxt = rec(999)
    assert f.filter(nxt) and nxt.suppressed == 97
    info = logging.LogRecord("app.n", logging.INFO, "f.py", 1, "info", (), None)
    assert all(f.filter(info) for _ in range(50))  # nur WARNING und höher


def test_parse_levels():
    assert parse_levels("app.news=DEBUG, app.db=warning ,kaputt, x=nope") == {"app.news": logging.DEBUG,
                                                                                "app.db": logging.WARNING}
    assert parse_levels("") == {}


def test_uvicorn_asgi_traceback_is_dropped_but_other_messages_stay(config):
    build_app(config, start_scheduler=False)
    flt = logging.getLogger("uvicorn.error").filters
    assert flt
    asgi = logging.LogRecord("uvicorn.error", logging.ERROR, "f.py", 1, "Exception in ASGI application\n", (), None)
    other = logging.LogRecord("uvicorn.error", logging.ERROR, "f.py", 1, "anderes", (), None)
    assert not all(f.filter(asgi) for f in flt) and all(f.filter(other) for f in flt)


def test_startup_banner_has_version_providers_and_no_secret(config):
    cfg = dataclasses.replace(config, auth_mode="none", secrets=Secrets(coingecko_api_key="CG-GEHEIM-12345"))
    with _client(cfg):
        pass
    text = "\n".join(recent_lines())
    assert "Portfolia 0." in text and "SQLite" in text and "coingecko=ja" in text and "finnhub=nein" in text
    assert "AUTH_MODE=none" in text and "CG-GEHEIM-12345" not in text and "Module:" in text


def test_health_state_and_line(config):
    ctx = build_app(config, start_scheduler=False).state.ctx
    ctx.startup()
    ctx.db.q("SELECT 1")
    s = health.state(ctx)
    assert s["threads"] >= 1 and s["db_conns"] >= 1 and s["db_mb"] >= 0 and s["missed"] == 0
    health.log_state(ctx)
    assert _lines("Betriebszustand: Threads")


def test_db_connections_are_counted_and_slow_statements_logged(config, monkeypatch):
    from app import db as dbmod

    ctx = build_app(config, start_scheduler=False).state.ctx
    ctx.startup()
    ctx.db.q("SELECT 1")
    n = ctx.db.conn_count
    ctx.db.close_thread_conn()
    assert ctx.db.conn_count == n - 1
    monkeypatch.setattr(dbmod, "SLOW_SQL_S", 0.0)
    ctx.db.q("SELECT 'parameter-marker' = ?", ("GEHEIMER-WERT",))
    line = _lines("Langsame Datenbankanweisung")[-1]
    assert "SELECT" in line and "GEHEIMER-WERT" not in line


def test_critical_alerts_only_for_blocked_user_actions(config, monkeypatch):
    ctx = build_app(config, start_scheduler=False).state.ctx
    ctx.startup()
    health.reset_cache()
    assert health.critical_alerts(ctx) == []  # Normalfall: kein Hinweis (auch nicht wegen AUTH_MODE=none)
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: SimpleNamespace(free=10 * 1024 * 1024))
    health.reset_cache()
    low = health.critical_alerts(ctx)
    assert low and low[0]["level"] == "crit" and "10 MB" in low[0]["text"]
    monkeypatch.undo()
    health.reset_cache()
    ctx.db.last_write_error = (time.monotonic(), "attempt to write a readonly database")
    err = health.critical_alerts(ctx)
    assert err and "Schreibzugriff" in err[0]["text"]
    ctx.db.last_write_error = (time.monotonic() - 3600, "alt")
    health.reset_cache()
    assert health.critical_alerts(ctx) == []  # nach 10 Minuten Ruhe verschwindet der Hinweis


def test_critical_alert_is_rendered_on_pages(config):
    with _client(config) as c:
        ctx = c.app.state.ctx
        ctx.db.last_write_error = (time.monotonic(), "database or disk is full")
        health.reset_cache()
        r = c.get("/settings")
        assert r.status_code == 200 and "Schreibzugriff auf die Datenbank ist fehlgeschlagen" in r.text
    health.reset_cache()


def test_write_failures_are_recorded_on_the_database(db):
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        db.x("INSERT INTO gibt_es_nicht VALUES (1)")
    assert db.last_write_error is None  # normaler SQL-Fehler ist kein Speicherproblem
    db._write_failed(sqlite3.OperationalError("attempt to write a readonly database"))
    assert db.last_write_error is not None


def test_log_download_returns_recent_lines(config):
    with _client(config) as c:
        r = c.get("/quality/log.txt")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
        assert "attachment" in r.headers["content-disposition"] and "Portfolia 0." in r.text


def test_scheduler_reports_missed_and_skipped_runs(config):
    from datetime import UTC, datetime, timedelta

    from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED

    from app.jobs.scheduler import Scheduler

    ctx = build_app(config, start_scheduler=False).state.ctx
    ctx.startup()
    s = Scheduler(ctx)
    s._on_missed(SimpleNamespace(job_id="news_enrich", code=EVENT_JOB_MISSED,
                                 scheduled_run_time=datetime.now(UTC) - timedelta(minutes=12)))
    s._on_missed(SimpleNamespace(job_id="news_enrich-once-123.4", code=EVENT_JOB_MAX_INSTANCES))
    assert s.missed == {"news_enrich": 2}
    text = "\n".join(recent_lines())
    assert "Lauf von news_enrich verpasst" in text and "Lauf von news_enrich übersprungen" in text
    assert s.pool_sizes["default"] == 3


def test_outbound_calls_are_logged_without_query_and_slow_or_server_errors_warn():
    seen: list[tuple[int, str]] = []

    class H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append((record.levelno, record.getMessage()))

    h = H()
    lg = logging.getLogger("app.http")
    old = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if req.url.path == "/down" else 200, json={})

    try:
        with httpx.Client(transport=httpx.MockTransport(handler), event_hooks=uhttp.CALL_HOOKS) as cl:
            cl.get("https://api.example.com/v1/preis?api_key=GEHEIM&x=1")
            cl.get("https://api.example.com/down")
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    assert seen[0][0] == logging.DEBUG and "api.example.com/v1/preis -> 200" in seen[0][1]
    assert "GEHEIM" not in seen[0][1]
    assert seen[1][0] == logging.WARNING and "-> 503" in seen[1][1]


def test_module_loader_reports_absent_and_broken_modules(tmp_path, monkeypatch):
    from app import main

    (tmp_path / "fakemod_ok.py").write_text("X = 1\n")
    (tmp_path / "fakemod_dep.py").write_text("import abhaengigkeit_gibt_es_nicht_zz\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(main, "MODULES", ("fakemod_ok", "fakemod_dep", "fakemod_fehlt_komplett"))
    rep = main._load_modules()
    assert rep == {"loaded": ["fakemod_ok"], "failed": ["fakemod_dep"], "absent": ["fakemod_fehlt_komplett"]}
    assert "Modul fakemod_dep nicht geladen" in "\n".join(recent_lines())
    sys.modules.pop("fakemod_ok", None)
