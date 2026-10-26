"""Restzeit-Schätzung für den Rückstau der News-Pipeline (Textabruf, KI-Analyse).

Reine Rechenfunktionen ohne Datenbankzugriff. Grundlage ist der Durchsatz, den die Pipeline nachweislich erreicht
(letzte 3 Stunden) – fehlt er, die Nennleistung je Lauf. Neu eintreffende Meldungen werden abgezogen. Es ist eine
Schätzung: Netz, Quellen-Sperren und Kontingente können sie verschieben.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app.util.timeutil import local_tz

MIN_OBSERVED = 12  # so viele erledigte Einheiten braucht ein gemessener Durchsatz, sonst gilt die Nennleistung


def fmt_minutes(minutes: int) -> str:
    """„ca. 45 Min.“, „ca. 2 Std. 30 Min.“, „ca. 1 Tag 3 Std.“"""
    if minutes < 60:
        return f"ca. {max(1, minutes)} Min."
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"ca. {hours} Std." + (f" {mins} Min." if mins else "")
    days, hours = divmod(hours, 24)
    return f"ca. {days} Tag{'e' if days != 1 else ''}" + (f" {hours} Std." if hours else "")


def _hhmm(dt: datetime) -> str:
    return f"{dt.astimezone(local_tz()):%d.%m. %H:%M}"


def _eta(waiting: int, per_hour: float, inflow_per_hour: float, interval_min: int) -> dict[str, Any]:
    """Restzeit in ganzen Lauf-Intervallen; ``None`` = wächst schneller, als abgearbeitet wird."""
    if waiting <= 0:
        return {"minutes": 0, "text": "kein Rückstau", "growing": False}
    net = per_hour - inflow_per_hour
    if net <= 0.5:
        return {"minutes": None, "growing": True,
                "text": f"wächst derzeit: Abarbeitung ca. {per_hour:.0f}/Std., neu ca. {inflow_per_hour:.0f}/Std."}
    runs = math.ceil(waiting / (net * interval_min / 60))
    minutes = runs * interval_min
    return {"minutes": minutes, "text": fmt_minutes(minutes), "growing": False}


def enrich_eta(waiting: int, *, per_run: int, interval_min: int = 15, observed_done_3h: int = 0,
               inflow_per_hour: float = 0.0) -> dict[str, Any]:
    nominal = per_run * 60 / interval_min
    rate = observed_done_3h / 3 if observed_done_3h >= MIN_OBSERVED else nominal
    out = _eta(waiting, min(rate, nominal), inflow_per_hour, interval_min)
    out["basis"] = "gemessen" if observed_done_3h >= MIN_OBSERVED else "Nennleistung"
    return out


def analyze_eta(waiting: int, *, per_run: int, interval_min: int = 10, observed_done_3h: int = 0,
                inflow_per_hour: float = 0.0, requests_left: int | None = None, batch: int = 6,
                paused_until: datetime | None = None, reset_at: datetime | None = None,
                enabled: bool = True, tokens_left: int | None = None,
                tokens_per_item: float | None = None) -> dict[str, Any]:
    """Restzeit der KI-Analyse. ``requests_left`` = Anfragen bis zum Tageslimit, ``reset_at`` = Beginn des nächsten
    Tages (Gemini: Mitternacht Pacific), ``paused_until`` = Pause des Anbieters.

    ``risk``: ``none`` | ``growing`` (Zufluss übersteigt die belegte Leistung) | ``quota`` (Tageskontingent reicht nicht
    bis zum Zurücksetzen) | ``disabled``.

    Der gemessene Durchsatz zählt nur als Kapazität, wenn die Pipeline ausgelastet war (mehr wartend als ein Lauf
    schafft). Bei kleinem Rückstau misst er lediglich das Angebot – dann gilt die Nennleistung."""
    if waiting <= 0:
        return {"minutes": 0, "text": "kein Rückstau", "growing": False, "basis": "", "risk": "none"}
    if not enabled:
        return {"minutes": None, "text": "KI ausgeschaltet – keine Analyse", "growing": False, "basis": "",
                "risk": "disabled"}
    nominal = per_run * 60 / interval_min
    busy = waiting > per_run
    measured = busy and observed_done_3h >= MIN_OBSERVED
    rate = min(observed_done_3h / 3, nominal) if measured else nominal
    out = _eta(waiting, rate, inflow_per_hour, interval_min)
    out["basis"] = "gemessen" if measured else "Nennleistung"
    out["risk"] = "growing" if out["growing"] else "none"
    notes = []
    if paused_until is not None:
        notes.append(f"Anbieter pausiert bis {_hhmm(paused_until)}, danach")
    # Meldungen, die das Tageskontingent noch zulässt: Anfragen (× Stapel) bzw. Tokens (÷ Verbrauch je Meldung) –
    # das Knappere zählt; ``None`` = ohne Tagesgrenze (oder Verbrauch unbekannt)
    fit_req = max(0, requests_left) * batch if requests_left is not None else None
    fit_tok = int(max(0, tokens_left) / tokens_per_item) if tokens_left is not None and tokens_per_item else None
    cands = [(f, who) for f, who in ((fit_req, ""), (fit_tok, " (Token-Budget)")) if f is not None]
    fits, limiter = min(cands, key=lambda c: c[0]) if cands else (None, "")
    if fits is not None and reset_at is not None:
        if waiting > fits:
            rest = waiting - fits
            after = _eta(rest, rate, inflow_per_hour, interval_min)
            tail = after["text"] if after["minutes"] is not None else "ggf. weiter wachsend"
            notes.append(f"Tageskontingent reicht für ca. {fits} Meldungen{limiter}, Rest ab {_hhmm(reset_at)}: {tail}")
            out["minutes"] = None
            out["text"] = "; ".join(notes)
            out["risk"] = "quota"
            return out
        hours = max(0.0, (reset_at - datetime.now(reset_at.tzinfo)).total_seconds() / 3600)
        expected = waiting + inflow_per_hour * hours
        if inflow_per_hour > 0 and expected > fits:
            out["risk"] = "quota"
            out["quota_note"] = (f"Tageskontingent reicht voraussichtlich nicht bis {_hhmm(reset_at)}: erwartet ca. "
                                 f"{expected:.0f} Meldungen, Kontingent für ca. {fits}{limiter}")
    if notes:
        out["text"] = f"{notes[0]} {out['text']}"
    return out


__all__ = ["analyze_eta", "enrich_eta", "fmt_minutes"]
