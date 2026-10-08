"""Kursqualität der Historie: Herkunft des Kurses je Asset und Tag, zusammengefasst zu Abschnitten.

Arten (je gehaltenem Tag)
* ``market`` – Schlusskurs des Hauptanbieters der Kursreihe (CoinGecko, Yahoo) bzw. dessen letzter Kurs über
  handelsfreie Tage (Wochenende/Feiertag: höchstens ``MAX_CARRY`` Tage),
* ``alt`` – Schlusskurs eines alternativen Anbieters (z. B. Yahoo vor dem CoinGecko-Fenster), im Überlappungszeitraum
  gegen den Hauptanbieter geprüft,
* ``interp`` – letzter Marktkurs länger als ``MAX_CARRY`` Tage fortgeschrieben (Lücke in der Reihe, z. B. nach einer
  Token-Migration oder Einstellung des Handels),
* ``tx`` / ``manual`` – Ersatzkurs aus Transaktionen bzw. manuellem Kurs (Schätzung),
* ``first`` – erster Marktkurs rückwirkend für die Zeit davor (Schätzung, wenn kein Ersatzkurs existiert),
* ``none`` – kein Kurs (Position mit 0 € angesetzt).

Die Abschnitte (:class:`Segment`) werden mit jeder vollständigen Neuberechnung in ``price_gap`` gespeichert und unter
Datenqualität bzw. in der Diagnose angezeigt; je Tag steht die Art zusätzlich in ``snapshot_asset_daily.price_kind``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np

MARKET, ALT, INTERP, TX, MANUAL, FIRST, NONE = 1, 2, 3, 4, 5, 6, 7
CODES = {MARKET: "market", ALT: "alt", INTERP: "interp", TX: "tx", MANUAL: "manual", FIRST: "first", NONE: "none"}
KIND_LABEL = {
    "market": "Marktkurs",
    "alt": "alternativer Kursanbieter",
    "interp": "letzter Kurs fortgeschrieben (interpoliert)",
    "tx": "Transaktionskurs als Schätzung",
    "manual": "manueller Kurs als Schätzung",
    "first": "erster Marktkurs rückwirkend (Schätzung)",
    "none": "kein Kurs verfügbar (0 €)",
}
ESTIMATE_KINDS = ("tx", "manual", "first")
GAP_KINDS = ("interp", "none")
MAX_CARRY = {"crypto": 3, "security": 5}  # Tage, die ein Schlusskurs als Marktkurs gilt (handelsfreie Tage)


@dataclass(frozen=True)
class Segment:
    asset_id: str
    kind: str
    start: date
    end: date
    days: int  # gehaltene Tage im Abschnitt
    source: str | None = None

    @property
    def method(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    @property
    def estimated(self) -> bool:
        return self.kind in ESTIMATE_KINDS


@dataclass
class AssetQuality:
    """Zusammenfassung je Asset für die Anzeige."""

    asset_id: str
    segments: list[Segment] = field(default_factory=list)
    days: dict[str, int] = field(default_factory=dict)  # Art → gehaltene Tage
    failed: str | None = None  # Historie konnte nicht geladen werden (Fehlertext)
    alt_note: str | None = None  # Ergebnis der Suche nach einem Ersatzanbieter

    @property
    def estimated_days(self) -> int:
        return sum(self.days.get(k, 0) for k in ESTIMATE_KINDS)

    @property
    def gap_days(self) -> int:
        return sum(self.days.get(k, 0) for k in GAP_KINDS)

    @property
    def alt_days(self) -> int:
        return self.days.get("alt", 0)

    @property
    def state(self) -> str:
        """failed | estimated | gaps | complete (schwerster Zustand zuerst)."""
        if self.failed:
            return "failed"
        if self.estimated_days:
            return "estimated"
        if self.gap_days:
            return "gaps"
        return "complete"

    @property
    def label(self) -> str:
        if self.failed:
            return "✕ Historie konnte nicht geladen werden"
        if self.estimated_days:
            return "⚠ Historische Kursdaten teilweise geschätzt"
        if self.gap_days:
            return f"⚠ {self.gap_days} {'Tag' if self.gap_days == 1 else 'Tage'} ohne Marktdaten"
        return "✓ Marktdaten vollständig"

    @property
    def badge(self) -> str:
        return {"failed": "crit", "estimated": "warn", "gaps": "warn"}.get(self.state, "good")


def segments(asset_id: str, codes: np.ndarray, held: np.ndarray, start: date,
             sources: list[str | None] | None = None) -> list[Segment]:
    """Zusammenhängende Abschnitte gleicher Art (ohne Marktkurs) über die gehaltenen Tage.

    Tage ohne Bestand unterbrechen einen Abschnitt nicht, zählen aber nicht mit."""
    out: list[Segment] = []
    cur: list[Any] | None = None  # [code, first_idx, last_idx, held_days, source]
    for i in np.nonzero(held)[0]:
        c = int(codes[i])
        src = sources[i] if sources is not None else None
        if c == MARKET:
            if cur is not None:
                out.append(_seg(asset_id, cur, start))
                cur = None
            continue
        if cur is not None and cur[0] == c and cur[4] == src:
            cur[2] = int(i)
            cur[3] += 1
        else:
            if cur is not None:
                out.append(_seg(asset_id, cur, start))
            cur = [c, int(i), int(i), 1, src]
    if cur is not None:
        out.append(_seg(asset_id, cur, start))
    return out


def _seg(asset_id: str, cur: list[Any], start: date) -> Segment:
    return Segment(asset_id, CODES[cur[0]], start + timedelta(days=cur[1]), start + timedelta(days=cur[2]), cur[3],
                   cur[4])


def summarize(asset_id: str, segs: list[Segment], failed: str | None = None,
              alt_note: str | None = None) -> AssetQuality:
    q = AssetQuality(asset_id, segs, failed=failed, alt_note=alt_note)
    for s in segs:
        q.days[s.kind] = q.days.get(s.kind, 0) + s.days
    return q


# -- Zustand einer Bewertung über einen Zeitraum ------------------------------------------------------------------
STATE_LABEL = {"complete": "✓ Marktdaten vollständig", "estimated": "⚠ teilweise geschätzt",
               "incomplete": "✕ unvollständig – Positionen ohne Kurs"}


def period_state(codes: np.ndarray, held: np.ndarray, start_i: int, end_i: int) -> dict[str, Any]:
    """Zustand der Bewertung im Zeitraum [start_i, end_i] aus der Kursherkunft je Asset und Tag (``price_kind``).

    ``complete``: an allen gehaltenen Tagen Marktkurse (Haupt- oder geprüfter Ersatzanbieter); ``estimated``: an
    einzelnen Tagen fortgeschriebene, Transaktions-, manuelle oder rückwirkende Kurse; ``incomplete``: Tage ohne
    Kurs (Position mit 0 € angesetzt, in Renditen neutral)."""
    if codes is None or not len(codes):
        return {"state": "complete", "label": STATE_LABEL["complete"], "estimated_days": 0, "missing_days": 0}
    c = codes[:, start_i:end_i + 1]
    h = held[:, start_i:end_i + 1]
    missing = int(np.sum(h & (c == NONE)))
    est = int(np.sum(h & np.isin(c, (INTERP, TX, MANUAL, FIRST))))
    state = "incomplete" if missing else "estimated" if est else "complete"
    return {"state": state, "label": STATE_LABEL[state], "estimated_days": est, "missing_days": missing}
