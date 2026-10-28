"""Eingaben außerhalb des Erwartbaren dürfen nie zu einem Serverfehler (5xx) führen.

Deckt M1/M2 der Projektanalyse ab: unbekannte Auswahlwerte (``/news?tab=foo``) und Ganzzahl-Pfadparameter jenseits
des SQLite-Wertebereichs. Die Prüfung läuft über *alle* GET-Routen, damit auch künftige Routen erfasst werden.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

import pytest

from app.jobs import tasks
from tests.test_journal import _client

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"
BIG = str(10**20)
JUNK_QUERY = ["tab=foo", "sort=%00", "status=zzz", "page=-1", "page=abc", "days=abc", "year=abc", "period=ZZZ",
              "range=nope", "scope=%FF%FE", "kind=nope", "view=nope", "q=" + "A" * 3000]


@pytest.fixture
def sample_client(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with _client(config) as c:
        tasks.import_check(c.app.state.ctx, "test")
        yield c


def _walk(routes, prefix=""):
    """Alle Routen inkl. der per ``include_router`` eingebundenen (FastAPI verschachtelt sie)."""
    for r in routes:
        if type(r).__name__ == "_IncludedRouter":
            sub = getattr(r, "router", None) or getattr(r, "original_router", None)
            yield from _walk(sub.routes, prefix + (getattr(r, "prefix", "") or ""))
        elif hasattr(r, "methods") and hasattr(r, "path"):
            yield prefix + r.path, set(r.methods)


def _get_routes(app) -> list[str]:
    return sorted({p for p, m in _walk(app.routes) if "GET" in m and not p.startswith("/static")})


def test_unknown_news_tab_falls_back_to_default(sample_client):
    r = sample_client.get("/news?tab=foo")
    assert r.status_code == 200
    assert 'class="active"' in r.text  # Standardreiter ist markiert


def test_huge_integer_path_parameters_are_404(sample_client):
    """Jede Route mit ``{…:int}``-Pfadparameter: ID ≥ 2⁶³ → 404, nie 500."""
    checked = 0
    for path in _get_routes(sample_client.app):
        if "{" not in path:
            continue
        resp = sample_client.get(re.sub(r"\{[^}]+\}", BIG, path), follow_redirects=False)
        assert resp.status_code < 500, (path, resp.status_code)
        checked += 1
    assert checked >= 15


def test_no_get_route_returns_5xx_for_garbage_query(sample_client):
    bad: list[tuple[str, str, int]] = []
    for path in _get_routes(sample_client.app):
        if "{" in path:
            continue
        for q in JUNK_QUERY:
            resp = sample_client.get(f"{path}?{q}", follow_redirects=False)
            if resp.status_code >= 500:
                bad.append((path, q[:20], resp.status_code))
    assert not bad, bad
