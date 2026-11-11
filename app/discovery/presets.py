"""Vereinfachte Discovery-Einstellungen (0.113): drei Entscheidungen statt vieler Einzelwerte.

* **Automatische Suche** (``aus`` | ``taeglich`` | ``woechentlich``) bildet auf die bestehenden Zeitpläne
  ``discovery.auto.<schritt>`` (Stunden: 0 aus, 24 täglich, 168 wöchentlich) und den Hauptschalter
  ``discovery.enabled`` ab. „Aus“ schaltet nur ``discovery.enabled`` ab – die einzelnen Zeitpläne bleiben gespeichert.
* **Rechercheumfang** (``sparsam`` | ``ausgewogen`` | ``intensiv``) setzt die vorhandenen Grenzen
  :data:`SCOPE_KEYS`. „Ausgewogen“ entspricht genau den bisherigen Standardwerten.
* **KI für Discovery** ist der bestehende Schalter ``discovery.ai``.

Weichen die gespeicherten Werte von jedem Profil ab, gilt die Einstellung als **benutzerdefiniert** (``custom``) –
nichts wird stillschweigend überschrieben. Die Profile ändern keine Algorithmen, nur Mengen und Takte.
"""

from __future__ import annotations

from typing import Any

# Zeitpläne: KI-Vorschläge (2 KI-Aufrufe je Lauf) bleiben in beiden Profilen wöchentlich – „täglich“ betrifft die
# kostenlosen Schritte (Nachrichten, SEC-Screening, Datenaktualisierung). „Täglich“ = bisherige Standardwerte.
SCHEDULE_PRESETS: dict[str, dict[str, int]] = {
    "taeglich": {"news": 24, "screening": 24, "analysen": 24, "beobachtet": 168, "ki": 168},
    "woechentlich": {"news": 168, "screening": 168, "analysen": 168, "beobachtet": 168, "ki": 168},
}
SCHEDULE_LABELS = {"aus": "Aus – nur manuell starten",
                   "taeglich": "Täglich – Nachrichten, Screening und Daten täglich, KI-Vorschläge wöchentlich",
                   "woechentlich": "Wöchentlich – alle Schritte einmal pro Woche",
                   "custom": "Benutzerdefiniert – einzelne Zeitpläne unter „Erweitert“"}

SCOPE_KEYS = ("screen_batch", "ki_companies", "max_new_per_run", "auto_analyse_per_run", "ai_calls_per_day",
              "ai_calls_per_month")
SCOPE_PRESETS: dict[str, dict[str, int]] = {
    "sparsam": {"screen_batch": 10, "ki_companies": 3, "max_new_per_run": 5, "auto_analyse_per_run": 10,
                "ai_calls_per_day": 6, "ai_calls_per_month": 60},
    "ausgewogen": {"screen_batch": 20, "ki_companies": 6, "max_new_per_run": 10, "auto_analyse_per_run": 25,
                   "ai_calls_per_day": 12, "ai_calls_per_month": 150},
    "intensiv": {"screen_batch": 50, "ki_companies": 10, "max_new_per_run": 20, "auto_analyse_per_run": 50,
                 "ai_calls_per_day": 24, "ai_calls_per_month": 300},
}
SCOPE_LABELS = {"sparsam": "Sparsam – wenige Kandidaten, geringer KI-Verbrauch",
                "ausgewogen": "Ausgewogen – gezielte Recherche und vertiefte Auswahl (empfohlen)",
                "intensiv": "Intensiv – breiteres Screening und mehr KI-Analysen",
                "custom": "Benutzerdefiniert – einzelne Grenzen unter „Erweitert“"}


def _get(ctx: Any, key: str) -> Any:
    from app.discovery import ai as AI
    from app.discovery import service as SV

    full = f"discovery.{key}"
    if full in SV.SETTINGS:
        return SV.setting(ctx, full)
    return ctx.settings.get(full, AI.DEFAULTS.get(full))


def model_cap(ctx: Any) -> int | None:
    """Höchstzahl Discovery-KI-Aufrufe je Tag, die die Modellgrenzen erlauben (kleinstes Tageslimit der Modelle für
    KI-Discovery und Unternehmensanalyse; ``None`` = keine lokale Tagesgrenze, z. B. Claude)."""
    from app.news.ai import AiGateway

    gw = AiGateway(ctx)
    if gw.provider_name() != "gemini":
        return None
    caps = [gw.limits(gw.model_for(t))["rpd"] for t in ("discovery", "company")]
    caps = [c for c in caps if c]
    return min(caps) if caps else None


def scope_values(ctx: Any, name: str) -> dict[str, int]:
    """Werte eines Umfangsprofils – die KI-Grenzen gekappt auf die Modellgrenzen (nie mehr als global erlaubt)."""
    vals = dict(SCOPE_PRESETS[name])
    cap = model_cap(ctx)
    if cap is not None:
        vals["ai_calls_per_day"] = min(vals["ai_calls_per_day"], cap)
        vals["ai_calls_per_month"] = min(vals["ai_calls_per_month"], cap * 31)
    return vals


def current_scope(ctx: Any) -> str:
    cur = {k: int(_get(ctx, k) or 0) for k in SCOPE_KEYS}
    for name in SCOPE_PRESETS:
        if cur == scope_values(ctx, name):
            return name
    return "custom"


def current_schedule(ctx: Any) -> str:
    if not _get(ctx, "enabled"):
        return "aus"
    cur = {step: int(_get(ctx, f"auto.{step}") or 0) for step in SCHEDULE_PRESETS["taeglich"]}
    for name, vals in SCHEDULE_PRESETS.items():
        if cur == vals:
            return name
    return "custom"


def apply_scope(ctx: Any, name: str) -> dict[str, int]:
    vals = scope_values(ctx, name)
    for k, v in vals.items():
        ctx.settings.set(f"discovery.{k}", v)
    return vals


def apply_schedule(ctx: Any, name: str) -> None:
    if name == "aus":
        ctx.settings.set("discovery.enabled", False)  # Zeitpläne bleiben gespeichert
        return
    ctx.settings.set("discovery.enabled", True)
    for step, hours in SCHEDULE_PRESETS[name].items():
        ctx.settings.set(f"discovery.auto.{step}", hours)


def view(ctx: Any) -> dict[str, Any]:
    """Für die Oberfläche: gewählte Profile, Beschriftungen und die Kappung durch die Modellgrenzen."""
    return {"schedule": current_schedule(ctx), "scope": current_scope(ctx), "schedule_labels": SCHEDULE_LABELS,
            "scope_labels": SCOPE_LABELS, "scope_presets": {n: scope_values(ctx, n) for n in SCOPE_PRESETS},
            "schedule_presets": SCHEDULE_PRESETS, "cap": model_cap(ctx), "ai": bool(_get(ctx, "ai"))}
