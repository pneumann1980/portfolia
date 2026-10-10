"""Web-Ebene: Seiten, JSON-APIs, CSRF, Security-Header, Basic-Auth, Einstellungen."""

import base64
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.web.security import hash_password, verify_password

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"


def _put_sample(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))


def _csrf(client: TestClient) -> str:
    r = client.get("/settings")
    assert r.status_code == 200
    return client.cookies.get("portfolia_csrf")


@pytest.fixture
def client(config):
    _put_sample(config)
    app = build_app(config, start_scheduler=False)
    with TestClient(app) as c:
        yield c


def test_empty_state_then_import_with_csrf(client):
    r = client.get("/")
    assert r.status_code == 200 and "Noch keine Buchungen" in r.text and "Erste Buchung erfassen" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"
    # ohne CSRF-Token abgelehnt
    assert client.post("/actions/import/check").status_code == 403
    token = _csrf(client)
    r = client.post("/actions/import/check", headers={"X-CSRF-Token": token, "HX-Request": "true"})
    assert r.status_code == 200 and "Import erfolgreich" in r.text
    # Cross-Site abgelehnt, auch mit Token
    r = client.post("/actions/import/check", headers={"X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_pages_and_apis_after_import(client):
    token = _csrf(client)
    client.post("/actions/import/check", headers={"X-CSRF-Token": token})
    from app.jobs import tasks

    ctx = client.app.state.ctx
    tasks.refresh_prices(ctx, force=True)
    tasks.backfill(ctx)
    for url in ["/", "/positions", "/positions?view=treemap&sort=gain&dir=asc", "/performance",
                "/performance?scope=segment:Krypto&period=YTD", "/quality", "/settings", "/asset/BTC",
                "/panel/asset/WKN%3A918422", "/asset/MUSTER%23mt1"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert client.get("/asset/UNKNOWN").status_code == 404
    alloc = client.get("/api/allocation").json()
    assert alloc["total"] > 0 and {n["name"] for n in alloc["data"]} >= {"Aktien", "Krypto"}
    hist = client.get("/api/history?range=MAX").json()
    assert len(hist["dates"]) > 100 and len(hist["value"]) == len(hist["dates"])
    chart = client.get("/api/asset/WKN%3A918422/chart?range=MAX&kind=candle").json()
    assert chart["markers"] and chart["candles"]
    # Split-bereinigte Marker: Kauf 2022 zu 218,18 €/Aktie → nach 10:1-Split 21,82 €
    buy = next(m for m in chart["markers"] if m["tx"] == "DEMO-00002")
    assert buy["price"] == pytest.approx(21.818, rel=1e-3) and buy["qty"] == pytest.approx(200)
    perf = client.get("/api/performance/series?period=MAX").json()
    assert perf["dates"] and len(perf["portfolio"]) == len(perf["dates"])
    assert client.get("/api/performance/contrib?period=1J").json()["items"]
    assert client.get("/api/status").status_code == 200


def test_settings_form_with_csrf_field(client):
    token = _csrf(client)
    r = client.post("/settings/save", data={"section": "general", "other_threshold_pct": "2,5",
                                            "default_range": "YTD", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    s = client.app.state.ctx.settings
    assert s.get("allocation.other_threshold_pct") == 2.5 and s.get("ui.default_range") == "YTD"


def test_password_hashing():
    h = hash_password("geheim")
    assert h.startswith("pbkdf2_sha256$") and verify_password("geheim", h) and not verify_password("x", h)
    import bcrypt

    bh = bcrypt.hashpw(b"geheim", bcrypt.gensalt(4)).decode().replace("$2b$", "$2y$")
    assert verify_password("geheim", bh)


def test_basic_auth(config):
    cfg = replace(config, auth_mode="basic", auth_user="anna", auth_password_hash=hash_password("s3cret"))
    app = build_app(cfg, start_scheduler=False)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        r = c.get("/")
        assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
        good = "Basic " + base64.b64encode(b"anna:s3cret").decode()
        assert c.get("/", headers={"Authorization": good}).status_code == 200
        bad = "Basic " + base64.b64encode(b"anna:falsch").decode()
        codes = [c.get("/", headers={"Authorization": bad}).status_code for _ in range(11)]
        assert codes[0] == 401 and codes[-1] == 429


def test_installable_app_assets_public_under_basic_auth(config):
    """Installation als App: Manifest, Icons und Service Worker auch ohne Zugangsdaten; Daten weiterhin geschützt."""
    import json as _json

    cfg = replace(config, auth_mode="basic", auth_user="anna", auth_password_hash=hash_password("s3cret"))
    app = build_app(cfg, start_scheduler=False)
    with TestClient(app) as c:
        m = c.get("/static/manifest.webmanifest")
        assert m.status_code == 200
        man = _json.loads(m.text)
        assert man["id"] == "/" and man["start_url"] == "/" and man["display"] == "standalone"
        sizes = {i["sizes"] for i in man["icons"]}
        assert {"192x192", "512x512"} <= sizes
        for icon in man["icons"]:
            assert c.get(icon["src"]).status_code == 200
        assert c.get("/static/img/apple-touch-icon.png").status_code == 200
        sw = c.get("/sw.js")
        assert sw.status_code == 200 and sw.headers["content-type"].startswith("text/javascript")
        assert sw.headers["service-worker-allowed"] == "/" and "addEventListener(\"fetch\"" in sw.text
        assert "caches." not in sw.text  # nichts wird zwischengespeichert
        for private in ("/", "/static/js/app.js", "/static/css/app.css", "/journal"):
            assert c.get(private).status_code in (401, 404), private


def test_dashboard_hide_total_toggle_and_mobile_layout_guards(client):
    """Augen-Symbol (Datenschutz-Modus): alle Beträge des Dashboards sind maskierbar (Zustand je Gerät, vor dem ersten
    Zeichnen aus theme.js); Scroll-Container sind Bezugsrahmen der sticky Tabellenköpfe (sonst wird das Layout-Viewport
    in Chrome mobil breiter als der Bildschirm), das Detail-Sheet endet mobil über der unteren Leiste."""
    client.post("/actions/import/check", headers={"X-CSRF-Token": _csrf(client)})
    page = client.get("/").text
    assert 'data-hide-total aria-pressed="false"' in page and 'aria-label="Beträge verbergen"' in page
    hero = page.split('class="kpi hero"', 1)[1].split('<div class="kpi">', 1)[0]
    assert hero.count('class="sens-real"') == 2 and 'class="sens-mask"' in hero and "#i-eye-off" in page
    # Datenschutz-Modus: alle Beträge des Dashboards (KPIs, Top-Bewegungen) maskierbar, Diagramme ebenfalls
    kpis = page.split('class="kpis"', 1)[1].split("</section>", 1)[0]
    assert "data-privacy-scope" in page and kpis.count('class="sens-real"') >= 9
    charts = client.get("/static/js/charts.js").text
    assert "function masked()" in charts and '"portfolia:privacy"' in charts
    assert '"portfolia:privacy"' in client.get("/static/js/app.js").text
    theme = client.get("/static/js/theme.js").text
    assert 'localStorage.getItem("portfolia-hide-total")' in theme and 'classList.add("hide-total")' in theme
    css = client.get("/static/css/app.css").text
    assert ".table-wrap { position: relative; overflow-x: auto;" in css
    assert "html.hide-total .eye-toggle .eye-on, html.hide-total .sens-real { display: none; }" in css
    assert ".panel { bottom: calc(56px + env(safe-area-inset-bottom)); }" in css
