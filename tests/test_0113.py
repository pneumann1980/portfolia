"""0.113: Vereinfachte KI- und Discovery-Einstellungen – eigene Seite „KI & Modelle“, Modellstrategie, getrennte
Speicherung (kein Bereich setzt einen anderen zurück), Discovery-Profile mit „Benutzerdefiniert“, kompakter KI-Status.

Keine echten KI- oder Netzaufrufe; synthetische Daten."""

from __future__ import annotations

import re

import httpx
import pytest

from app.discovery import jobs as J
from app.discovery import presets as PR
from app.news import ai as A
from tests.test_discovery import make_client

LITE, FLASH_CO = "gemini-3.5-flash-lite", "gemini-3.8-flash"


@pytest.fixture
def c(config, monkeypatch):
    yield from make_client(config, monkeypatch)


def snap(ctx, prefix: str) -> dict:
    return {k: v for k, v in ctx.settings.all().items() if k.startswith(prefix)}


def save(c, **data):
    return c.post("/settings/save", data={"csrf_token": c.token, **data}, follow_redirects=False)


def disc(c, **data):
    return c.post("/discovery/einstellungen", data={"csrf_token": c.token, **data}, follow_redirects=False)


# ------------------------------------------------------------------------------------------- KI & Modelle

def test_ki_page_is_its_own_tab_and_news_no_longer_holds_ai_fields(c):
    page = c.get("/settings/ki").text
    for needle in ("KI &amp; Modelle", "KI-Funktionen verwenden", "KI-Anbieter", "Modellstrategie",
                   "Kostenbewusst optimiert", "Ein Modell für alles", "Individuell", "Aktuell zugeordnete Modelle",
                   "Kontingent heute", "Erweiterte KI-Einstellungen"):
        assert needle in page, needle
    assert '<details class="box ai-advanced" id="ki-erweitert">' in page  # standardmäßig geschlossen
    assert "data-unsaved-provider hidden" in page and 'data-saved-provider="' in page  # Übersicht = gespeicherter Stand
    settings = c.get("/settings").text
    assert "News &amp; Artikeltexte" in settings and 'href="/settings/ki"' in settings
    for gone in ('name="llm_provider"', 'name="gemini_model"', 'name="gemini_rpm"', 'name="llm_budget"',
                 'name="gemini_profile"', 'name="lim_model_0"'):
        assert gone not in settings, gone
    for tab in ("/settings", "/settings/ki", "/settings/zugaenge"):
        assert 'href="/settings/ki">KI &amp; Modelle</a>' in c.get(tab).text


def test_provider_blocks_render_for_saved_provider_only(c):
    ctx = c.app.state.ctx
    ctx.settings.set("llm.provider", "claude")
    page = c.get("/settings/ki").text
    assert re.search(r'data-only-provider="gemini"\s+hidden', page)
    assert not re.search(r'data-only-provider="claude"\s+hidden', page)
    ctx.settings.set("llm.provider", "gemini")
    ctx.settings.set("llm.gemini_profile", "free_tier")
    page = c.get("/settings/ki").text
    assert re.search(r'data-only-provider="claude"\s+hidden', page)
    assert re.search(r'data-only-strategy="eigen"\s+hidden', page)  # Modell je Aufgabe nur bei „Individuell“
    ctx.settings.set("llm.gemini_profile", "eigen")
    page = c.get("/settings/ki").text
    assert not re.search(r'data-only-strategy="eigen"\s+hidden', page)
    for label in ("Nachrichtenanalyse", "Ereignisgruppierung", "Tagesbericht", "KI-Discovery", "Unternehmensanalyse",
                  "Video-Kernaussagen"):
        assert f"Modell für {label}" in page


def test_switching_provider_keeps_the_other_providers_settings(c):
    ctx = c.app.state.ctx
    save(c, section="ai", llm_provider="gemini", gemini_model="gemini-2.5-flash", gemini_rpm="7",
         gemini_profile="eigen", task_model_video="gemini-3.6-flash", has_gemini_unlimited="1", gemini_unlimited="1")
    gem = snap(ctx, "llm.gemini")
    task_models = ctx.settings.get("llm.task_models")
    # Wechsel zu Claude: Browser deaktiviert die ausgeblendeten Gemini-Felder → sie werden nicht gesendet
    r = save(c, section="ai", llm_provider="claude", llm_model="claude-opus-5", llm_budget="12345")
    assert r.status_code == 303 and r.headers["location"].startswith("/settings/ki")
    assert ctx.settings.get("llm.provider") == "claude" and ctx.settings.get("llm.daily_token_budget") == 12345
    assert snap(ctx, "llm.gemini") == gem and ctx.settings.get("llm.task_models") == task_models
    save(c, section="ai", llm_provider="gemini")  # zurück: alles wie vorher
    assert snap(ctx, "llm.gemini") == gem and ctx.settings.get("llm.daily_token_budget") == 12345


def test_strategies_and_effective_models(c):
    ctx = c.app.state.ctx
    ctx.settings.set("llm.provider", "gemini")
    save(c, section="ai", gemini_profile="eigen", gemini_model=LITE, task_model_news="",
         task_model_events="", task_model_report="", task_model_discovery="", task_model_company="gemini-2.5-flash",
         task_model_video="gemini-3.6-flash")
    gw = A.AiGateway(ctx)
    assert gw.task_models()["company"] == "gemini-2.5-flash" and gw.task_models()["news"] == LITE
    own = ctx.settings.get("llm.task_models")
    save(c, section="ai", gemini_profile="free_tier")  # Individuell → Kostenbewusst: eigene Wahl bleibt gespeichert
    assert ctx.settings.get("llm.task_models") == own
    assert A.AiGateway(ctx).task_models() == A.PROFILES["free_tier"]["models"]
    save(c, section="ai", gemini_profile="einzeln", gemini_model="gemini-2.5-flash")
    assert set(A.AiGateway(ctx).task_models().values()) == {"gemini-2.5-flash"}
    save(c, section="ai", gemini_profile="eigen")
    assert A.AiGateway(ctx).task_models()["company"] == "gemini-2.5-flash"  # Individuelle Wahl wieder wirksam
    page = c.get("/settings/ki").text
    assert 'id="ki-wirksam"' in page and "Unternehmensanalyse" in page


def test_saving_only_present_fields_never_resets_others(c):
    ctx = c.app.state.ctx
    ctx.settings.set("llm.gemini_tokens_unlimited", True)
    ctx.settings.set("llm.manual_reserve_pct", 25)
    ctx.settings.set("llm.model_limits", {FLASH_CO: {"rpm": 3, "tpm": 0, "rpd": 33, "tpd": 0}})
    before = snap(ctx, "llm.")
    save(c, section="ai", has_llm_enabled="1", llm_enabled="1")  # nur der Hauptschalter im Formular
    after = snap(ctx, "llm.")
    assert after.pop("llm.enabled") is True
    before.pop("llm.enabled", None)
    assert after == before
    save(c, section="ai", has_llm_enabled="1")  # Haken entfernt → aus
    assert ctx.settings.get("llm.enabled") is False


def test_sections_are_saved_independently(c):
    ctx = c.app.state.ctx
    save(c, section="ai", has_llm_enabled="1", llm_enabled="1", llm_provider="gemini")
    disc(c, scope_preset="intensiv", has_ai="1", ai="1")
    llm, discovery = snap(ctx, "llm."), snap(ctx, "discovery.")
    save(c, section="news", min_relevance="0.4", dashboard_count="7", report_hour="6", report_minute="5")
    assert snap(ctx, "llm.") == llm and snap(ctx, "discovery.") == discovery
    news = snap(ctx, "news.")
    save(c, section="ai", llm_provider="claude")
    assert snap(ctx, "news.") == news and snap(ctx, "discovery.") == discovery
    disc(c, scope_preset="sparsam")
    assert snap(ctx, "news.") == news and ctx.settings.get("llm.provider") == "claude"


def test_model_list_refresh_changes_no_settings(c):
    from dataclasses import replace

    from app.config import Secrets

    ctx = c.app.state.ctx
    ctx.config = replace(ctx.config, secrets=Secrets(gemini_api_key="test-gemini-key-0123456789abcdef"))
    ctx.settings.set("llm.provider", "gemini")
    ctx.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"models": [
        {"name": "models/gemini-3.9-flash", "supportedGenerationMethods": ["generateContent"]}]})))
    before = ctx.settings.all()
    # HTMX: nur der Kasten wird ersetzt; selbst mitgesendete Formularfelder werden ignoriert
    r = c.post("/settings/ai-models", data={"gemini_model": "gemini-2.5-flash", "llm_provider": "claude"},
               headers={"hx-request": "true", "X-CSRF-Token": c.token})
    assert r.status_code == 200 and 'id="ki-modellliste"' in r.text and "Modellliste abgerufen (1 Modelle)" in r.text
    assert "<html" not in r.text.lower()
    assert ctx.settings.all() == before
    page = c.get("/settings/ki").text
    assert 'hx-post="/settings/ai-models"' in page and 'hx-params="none"' in page and 'type="button"' in page


# ------------------------------------------------------------------------------------------- Discovery-Profile

def test_defaults_map_to_profiles_not_custom(c):
    ctx = c.app.state.ctx
    assert PR.current_scope(ctx) == "ausgewogen" and PR.current_schedule(ctx) == "taeglich"
    page = c.get("/discovery").text
    assert "Discovery-Einstellungen" in page and "KI für Discovery-Recherche nutzen" in page
    assert 'hidden data-choice="scope-custom"' in page and 'hidden data-choice="schedule-custom"' in page
    assert '<details class="box" id="disc-erweitert">' in page


@pytest.mark.parametrize("name", ["sparsam", "ausgewogen", "intensiv"])
def test_scope_profiles_set_their_parameters(c, name):
    ctx = c.app.state.ctx
    if name == "ausgewogen":
        disc(c, scope_preset="sparsam")
    r = disc(c, scope_preset=name)
    assert r.status_code == 303 and "gespeichert" in r.headers["location"]
    for k, v in PR.SCOPE_PRESETS[name].items():
        assert ctx.settings.get(f"discovery.{k}") == v, k
    assert PR.current_scope(ctx) == name


def test_scope_profile_never_exceeds_model_limits(c):
    ctx = c.app.state.ctx
    ctx.settings.set("llm.provider", "gemini")
    ctx.settings.set("llm.gemini_profile", "free_tier")
    ctx.settings.set("llm.model_limits", {FLASH_CO: {"rpm": 5, "tpm": 0, "rpd": 10, "tpd": 0}})
    disc(c, scope_preset="intensiv")
    assert ctx.settings.get("discovery.ai_calls_per_day") == 10  # statt 24: Modell erlaubt nur 10 je Tag
    assert ctx.settings.get("discovery.ai_calls_per_month") == 300
    assert PR.current_scope(ctx) == "intensiv"
    assert "höchstens 10 Aufrufe je Tag" in c.get("/discovery").text


def test_changed_advanced_value_wins_and_shows_custom(c):
    ctx = c.app.state.ctx
    form = {k: str(v) for k, v in PR.SCOPE_PRESETS["ausgewogen"].items()}
    # erweiterter Wert geändert, Profil-Radio unverändert → Wert bleibt, Profil „Benutzerdefiniert“
    disc(c, scope_preset="ausgewogen", **{**form, "screen_batch": "33"})
    assert ctx.settings.get("discovery.screen_batch") == 33 and PR.current_scope(ctx) == "custom"
    page = c.get("/discovery").text
    assert "Benutzerdefiniert" in page and 'hidden data-choice="scope-custom"' not in page
    # erweiterter Wert geändert UND anderes Profil gewählt → die Anpassung wird nicht überschrieben
    disc(c, scope_preset="intensiv", **{**form, "screen_batch": "34"})
    assert ctx.settings.get("discovery.screen_batch") == 34 and PR.current_scope(ctx) == "custom"
    # Profil gewählt, erweiterte Werte unverändert gesendet → Profil gilt
    cur = {k: str(ctx.settings.get(f"discovery.{k}")) for k in PR.SCOPE_KEYS}
    disc(c, scope_preset="sparsam", **cur)
    assert PR.current_scope(ctx) == "sparsam"


def test_schedule_profiles_keep_individual_schedules_when_off(c):
    ctx = c.app.state.ctx
    disc(c, schedule_preset="woechentlich")
    assert all(ctx.settings.get(f"discovery.auto.{s}") == 168 for s in PR.SCHEDULE_PRESETS["woechentlich"])
    assert PR.current_schedule(ctx) == "woechentlich"
    disc(c, schedule_preset="aus")
    assert ctx.settings.get("discovery.enabled") is False and PR.current_schedule(ctx) == "aus"
    assert all(ctx.settings.get(f"discovery.auto.{s}") == 168 for s in PR.SCHEDULE_PRESETS["woechentlich"])
    disc(c, schedule_preset="taeglich")
    assert ctx.settings.get("discovery.enabled") is True and PR.current_schedule(ctx) == "taeglich"
    disc(c, schedule_preset="taeglich", auto_news="0")  # einzelner Zeitplan geändert → Benutzerdefiniert
    assert ctx.settings.get("discovery.auto.news") == 0 and PR.current_schedule(ctx) == "custom"
    st = c.get("/discovery").text
    assert "Nächster automatischer Lauf" in st


def test_discovery_switch_and_invalid_values_change_nothing(c):
    ctx = c.app.state.ctx
    disc(c, has_ai="1")  # Haken entfernt
    assert ctx.settings.get("discovery.ai") is False
    disc(c, has_ai="1", ai="1")
    assert ctx.settings.get("discovery.ai") is True
    before = snap(ctx, "discovery.")
    r = disc(c, scope_preset="intensiv", screen_batch="40", ai_calls_per_day="x")
    assert "err=" in r.headers["location"] and snap(ctx, "discovery.") == before  # erst prüfen, dann speichern
    disc(c, themes="1")  # ohne has_themes: Schalter bleibt
    assert snap(ctx, "discovery.").get("discovery.themes") == before.get("discovery.themes")


# ------------------------------------------------------------------------------------------- KI-Status

def test_status_compact_ready_off_and_busy(c):
    ctx = c.app.state.ctx
    page = c.get("/discovery").text
    assert "KI deaktiviert" in page  # KI aus (Standard)
    for needle in ("Discovery-Modell", "Verbleibendes Kontingent", "Laufende Research-Aufgabe",
                   "Letzter erfolgreicher KI-Lauf", "Nächster automatischer Lauf", 'id="ki-details"',
                   "KI-Discovery starten", "Fundamentalscreening starten"):
        assert needle in page, needle
    ctx.settings.set("llm.enabled", True)
    import app.news.ai as AImod

    orig = AImod.AiGateway.key_present
    AImod.AiGateway.key_present = lambda self, provider=None: True
    try:
        assert "KI bereit" in c.get("/discovery").text
        jid, _ = J.create(ctx, "ki_discovery")
        ctx.db.x("UPDATE disc_job SET status='laeuft', step='Schritt 1–2: Themen' WHERE id=?", (jid,))
        page = c.get("/discovery").text
        assert "KI arbeitet gerade" in page and "Schritt 1–2: Themen" in page
        ctx.db.x("UPDATE disc_job SET status='wartet', wait_until='2999-01-01T00:00:00+00:00', "
                 "wait_reason='Tageslimit' WHERE id=?", (jid,))
        page = c.get("/discovery").text
        assert "KI arbeitet gerade" not in page and "Warteschlange (1)" in page and "wartet auf KI-Kontingent" in page
    finally:
        AImod.AiGateway.key_present = orig
