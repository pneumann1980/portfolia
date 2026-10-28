"""Betrieb: eigener Pool für News-Jobs (M4), keine blockierende DB-Arbeit in async-Handlern (M5), Verbindungen (M6)."""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import anyio.to_thread

from app import main as app_main
from app.jobs.scheduler import Scheduler
from app.main import build_app

NEWS_JOBS = ("news_fetch", "news_rematch", "youtube_discovery", "llm_digest", "news_enrich", "news_analyze",
             "news_group")
DB_METHODS = {"x", "q", "q1", "scalar", "xmany", "transaction"}


def _ctx(config):
    ctx = build_app(config, start_scheduler=False).state.ctx
    ctx.startup()
    return ctx


def test_news_jobs_run_in_their_own_pool(config):
    s = Scheduler(_ctx(config))
    s.setup_default_jobs()
    assert s.pool_sizes == {"default": 3, "news": 2, "interactive": 2, "wave": 1}
    for name in NEWS_JOBS:
        assert s.sched.get_job(name) is not None or name == "news_rematch"
        assert s._executor[name] == "news", name
    for name in ("import_poll", "prices_crypto", "prices_securities", "fx_ecb"):
        assert s._executor[name] == "default", name
    assert s._executor["news_report"] == "interactive" and s._executor["news_wave"] == "wave"


def test_blocked_news_jobs_do_not_delay_core_jobs(config):
    """Beide News-Threads belegt → ein Kern-Job startet trotzdem sofort (vorher: gleicher Pool, Läufe verfielen)."""
    ctx = _ctx(config)
    s = Scheduler(ctx)
    gate, started, core_done = threading.Event(), threading.Barrier(3, timeout=10), threading.Event()

    def blocker(_ctx):
        started.wait()
        gate.wait(15)
        return {}

    s.register("t_news_a", blocker, None, executor="news")
    s.register("t_news_b", blocker, None, executor="news")
    s.register("t_core", lambda _c: core_done.set() or {}, None)  # Pool „default“
    s.sched.start()
    try:
        s.trigger("t_news_a", 0.05)
        s.trigger("t_news_b", 0.05)
        started.wait()  # beide News-Threads sind belegt
        s.trigger("t_core", 0.05)
        assert core_done.wait(5), "Kern-Job wurde durch blockierte News-Jobs verzögert"
    finally:
        gate.set()
        s.sched.shutdown(wait=True)


def _blocking_async_handlers(root: Path) -> list[tuple[str, str, int]]:
    """``async def`` mit direktem DB-Zugriff im Rumpf (verschachtelte ``def`` laufen im Thread-Pool, zählen nicht)."""
    bad: list[tuple[str, str, int]] = []

    def visit(node: ast.AST, fn: ast.AsyncFunctionDef | None, path: str) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.AsyncFunctionDef):
                visit(ch, ch, path)
            elif isinstance(ch, (ast.FunctionDef, ast.Lambda)):
                visit(ch, None, path)  # eigener (synchroner) Rahmen
            else:
                if (fn is not None and isinstance(ch, ast.Call) and isinstance(ch.func, ast.Attribute)
                        and ch.func.attr in DB_METHODS and ast.unparse(ch.func.value).endswith(("db", "settings"))):
                    bad.append((path, fn.name, ch.lineno))
                visit(ch, fn, path)

    for p in sorted((root / "app").rglob("*.py")):
        visit(ast.parse(p.read_text(encoding="utf-8")), None, str(p.relative_to(root)))
    return bad


def test_no_async_handler_touches_the_database_directly():
    """Ein Schreibzugriff im Ereignisschleifen-Thread blockiert bei gehaltener Sperre die ganze App (bis 30 s)."""
    root = Path(__file__).resolve().parent.parent
    assert not _blocking_async_handlers(root)


def test_wrapped_handlers_still_work(config):
    from tests.test_journal import _client

    with _client(config) as c:
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        ctx = c.app.state.ctx
        r = c.post("/settings/save", data={"section": "general", "other_threshold_pct": "2.5", "default_range": "3M"},
                   follow_redirects=False)
        assert r.status_code == 303 and ctx.settings.get("allocation.other_threshold_pct") == 2.5
        r = c.post("/settings/save", data={"section": "export", "auto": "1", "keep": "7"}, follow_redirects=False)
        assert r.status_code == 303 and ctx.settings.get("export.keep") == 7  # Zusatzmodul-Abschnitt (jetzt synchron)
        assert c.post("/news/read-all", data={"tab": "articles", "days": "7"}).status_code == 204


def test_sqlite_cache_is_limited_and_threadpool_is_capped(config):
    from app import db as dbmod
    from tests.test_journal import _client

    with _client(config) as c:
        ctx = c.app.state.ctx
        assert ctx.db.conn.execute("PRAGMA cache_size").fetchone()[0] == -dbmod.CACHE_KB == -4000
        tokens = c.portal.call(lambda: anyio.to_thread.current_default_thread_limiter().total_tokens)
        assert tokens == app_main.THREADPOOL_TOKENS == 24


def test_health_state_counts_connections_across_threads(config):
    from app import health

    ctx = _ctx(config)
    ctx.db.q("SELECT 1")
    base = ctx.db.conn_count
    done = threading.Event()

    def other():
        ctx.db.q("SELECT 1")
        done.wait(5)
        ctx.db.close_thread_conn()

    t = threading.Thread(target=other)
    t.start()
    time.sleep(0.3)
    assert health.state(ctx)["db_conns"] == base + 1
    done.set()
    t.join()
    assert ctx.db.conn_count == base
