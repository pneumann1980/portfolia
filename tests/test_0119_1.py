"""0.119.1 – Jedes klassisch abgeschickte POST-Formular trägt das CSRF-Feld (Fehler: Karte „KI je Bereich“)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.news import aiplan


class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {"attrs": a, "fields": {}}
            self.forms.append(self._cur)
        elif self._cur is not None and tag in ("input", "select", "textarea") and a.get("name"):
            if tag == "input" and a.get("type") in ("checkbox", "radio") and "checked" not in a:
                return
            self._cur["fields"].setdefault(a["name"], a.get("value") or "")

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def _post_forms(html: str) -> list[dict]:
    p = _Forms()
    p.feed(html)
    return [f for f in p.forms if (f["attrs"].get("method") or "").lower() == "post"
            and not any(k.startswith("hx-") for k in f["attrs"])]


def _routes(routes, prefix=""):
    """Alle Routen samt eingebundener Router (FastAPI hält sie verpackt)."""
    for r in routes:
        if hasattr(r, "original_router"):
            yield from _routes(r.original_router.routes, prefix + (getattr(r.include_context, "prefix", "") or ""))
        elif getattr(r, "methods", None):
            yield prefix + r.path, r.methods


@pytest.fixture
def client(config):
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def test_every_plain_post_form_has_csrf_field(client):
    paths = sorted({p for p, m in _routes(client.app.routes) if "GET" in m and "{" not in p
                    and not re.search(r"\.(js|css|json|png|ico|webmanifest|zip|csv|txt)$|^/(static|api|img|health)|/progress|/status$|/live$", p)})
    checked, missing = 0, []
    for path in paths:
        r = client.get(path)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            continue
        for f in _post_forms(r.text):
            checked += 1
            if f["fields"].get("csrf_token") != client.token:
                missing.append(f"{path} → {f['attrs'].get('action')}")
    assert checked > 20
    assert not missing, missing


def test_ai_areas_form_saves_as_rendered(client):
    ctx = client.app.state.ctx
    form = next(f for f in _post_forms(client.get("/settings/ki").text) if f["fields"].get("section") == "news_ai")
    data = dict(form["fields"], ai_articles="manual", ai_top_per_day="70")
    r = client.post(form["attrs"]["action"], data=data, follow_redirects=False)
    assert r.status_code == 303 and "saved=news_ai" in r.headers["location"]
    assert (aiplan.article_mode(ctx), aiplan.top_per_day(ctx)) == ("manual", 70)
