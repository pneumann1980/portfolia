"""0.116 – Review-Korrekturen: Export-Verweis, Marktkapitalisierung, Hinweisbanner, Brotkrumen, Begriffe."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import build_app
from app.web import deps, fmt
from tests.test_portfolio_ux import client  # noqa: F401  (Fixture: Beispielportfolio)


def test_market_cap_uses_billions():
    assert fmt.eur_compact(112_754_460_000).replace("\xa0", " ") == "112,75 Mrd. €"
    assert fmt.eur_compact(2_500_000).replace("\xa0", " ") == "2,50 Mio. €"
    assert fmt.eur_compact(-3_000_000_000, sign=True).replace("\xa0", " ").startswith("-3,00 Mrd.")


def test_full_export_link_only_with_transactions(config, client):  # noqa: F811
    assert 'href="/journal/export.zip"' in client.get("/settings").text


def test_full_export_link_hidden_without_transactions(config):
    with TestClient(build_app(config, start_scheduler=False)) as c:
        page = c.get("/settings").text
        assert 'href="/journal/export.zip"' not in page and "erst mit Buchungen verfügbar" in page
        assert c.get("/journal/export.zip").status_code == 404  # der Abruf selbst bleibt ehrlich


def test_warning_banner_only_on_overview_and_target_page(config, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(deps, "global_alerts", lambda ctx: [
        {"level": "warn", "text": "WARNBANNER-X", "href": "/plans#confirmed"},
        {"level": "crit", "text": "KRITISCH-Y"}])
    for path, warn in (("/", True), ("/plans", True), ("/settings", False), ("/journal", False), ("/watchlist", False)):
        page = client.get(path).text
        assert ("WARNBANNER-X" in page) is warn, path
        assert "KRITISCH-Y" in page, path  # Kritisches bleibt überall sichtbar


def test_breadcrumbs_use_one_pattern(config, client):  # noqa: F811
    for path, parent in (("/quality/diagnose", "/quality"), ("/quality/technik", "/quality"),
                         ("/discovery/protokoll", "/watchlist")):
        page = client.get(path).text
        head = page.split('<span class="title">', 1)[1].split("</span>", 1)[0]
        assert f'<a href="{parent}" class="hint">' in head and " › " in head, path
    assert "Datenqualität · " not in client.get("/quality/diagnose").text.split("<main", 1)[0]


def test_suggestion_wording(config, client):  # noqa: F811
    page = client.get("/watchlist").text
    assert "Vorschläge suchen" in page and "Kandidaten suchen" not in page
