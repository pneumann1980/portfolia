"""Gehaltene Positionen und Asset-Namen für die News-Seiten – ohne Portfolio-/Bewertungsaufbau im Seitenaufruf (0.120).

Die News-Seite braucht das Portfolio nur für die Auswahl „Asset“ und für Namen an den Karten. Nach jeder Datenänderung
(Import, Sparplan-Abgleich, Kursquelle) baut ``ctx.valuation()`` Ledger und Bewertung neu auf – gemessen 0,7 s auf einem
Bestand mit 6000 Buchungen, auf schwächerer Hardware mehrere Sekunden. Dieser Zwischenspeicher wird von den News-Jobs
(Abruf, Neuzuordnung, Analyse) bei jedem ``NewsService.held()`` aufgefrischt; Seiten lesen ihn nur.

Grenze: Nach einem Import kann die Auswahl bis zur nächsten Neuzuordnung (läuft direkt nach dem Import) bzw. bis
zum nächsten Abruf (≤ 30 Minuten) den alten Positionsstand zeigen. Fehlt der Speicher (erster Aufruf nach dem
Start), wird er einmal im Seitenaufruf berechnet.
"""

from __future__ import annotations

import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Any

MAX_AGE_S = 6 * 3600  # Sicherheitsnetz, falls kein News-Job läuft (Abruf aus): dann einmal neu im Seitenaufruf

_lock = threading.Lock()
_cache: weakref.WeakKeyDictionary[Any, Held] = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class Held:
    options: list[tuple[str, str]] = field(default_factory=list)  # (asset_id, Name) gehaltener Nicht-Fiat-Positionen
    names: dict[str, str] = field(default_factory=dict)  # asset_id → Name für alle Assets des Portfolios
    at: float = 0.0

    def name(self, asset_id: str) -> str:
        return self.names.get(asset_id, asset_id)


def remember(ctx: Any, val: Any, pf: Any) -> Held:
    """Aus einer ohnehin berechneten Bewertung übernehmen (News-Jobs)."""
    options = [(p.asset_id, p.asset.name) for p in (val.positions if val else []) if not p.asset.is_fiat]
    names = {aid: info.name for aid, info in (pf.assets.items() if pf is not None else [])}
    h = Held(options, names, time.monotonic())
    with _lock:
        _cache[ctx] = h
    return h


def held(ctx: Any, max_age_s: float = MAX_AGE_S) -> Held:
    """Letzter bekannter Stand; nur ohne (oder mit sehr altem) Stand wird im Aufruf berechnet."""
    with _lock:
        h = _cache.get(ctx)
    if h is not None and time.monotonic() - h.at <= max_age_s:
        return h
    return remember(ctx, ctx.valuation(), ctx.portfolio())


def forget(ctx: Any) -> None:
    with _lock:
        _cache.pop(ctx, None)
