"""Abrufrate der Kursquellen: Hochrechnung der Aufrufe je Monat und Empfehlung.

CoinGecko zählt jeden Aufruf gegen ein Monatskontingent (Demo-Schlüssel: 10.000). Verbraucher in Portfolia:

* **Kurse:** je Aktualisierung ein ``/simple/price``-Aufruf für alle gehaltenen Coins (mehrere erst, wenn die
  IDs nicht mehr in eine URL passen, siehe :meth:`CoinGeckoProvider.id_chunks`).
* **Historie:** Gehaltene Coins erhalten ihren Tagesschluss aus dem letzten Kurs (``write_eod_closes``) und brauchen
  keinen laufenden Historien-Abruf. ``/market_chart`` fällt etwa jeden zweiten Tag (siehe ``PriceService._needs``)
  nur für nicht mehr gehaltene Coins und CoinGecko-Benchmarks an; der erste Abruf je neuem Coin zählt zur Reserve.
* **Reserve:** Neustarts (erzwungene Aktualisierung), „Kurse aktualisieren“, CSV-„Kurse laden“ und die
  Kursquellen-Suche – pauschal ``RESERVE_PCT`` des Limits.

Empfohlen wird das kürzeste Intervall ab ``RECOMMENDED_FLOOR_MIN``, bei dem die Hochrechnung unter der
Drosselschwelle bleibt; für den Drosselbetrieb das kürzeste Intervall, mit dem das Rest-Kontingent ab der Schwelle
einen ganzen Monat reicht. Yahoo Finance hat kein veröffentlichtes Kontingent (inoffizielle Schnittstelle, Sperren
je IP bei zu vielen Anfragen) – dort gilt eine feste Empfehlung, abgerufen wird nur in den Handelsfenstern.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DAYS_PER_MONTH = 30.44
MINUTES_PER_MONTH = DAYS_PER_MONTH * 24 * 60
HISTORY_EVERY_DAYS = 2
RESERVE_PCT = 5.0
RECOMMENDED_FLOOR_MIN = 5

CRYPTO_PRESETS = (2, 5, 10, 15, 20, 30, 60, 120, 240)
THROTTLED_PRESETS = (10, 15, 20, 30, 60, 120, 240, 360, 720)
SECURITY_PRESETS = (5, 10, 15, 20, 30, 60, 120)
SECURITY_RECOMMENDED = 15
# Abruffenster je Werktag in Minuten (siehe market_hours.WINDOWS): Europa 07:30–22:45, Devisen ganztägig
EU_WINDOW_MIN = 915
FX_WINDOW_MIN = 1440
WEEKDAYS_PER_MONTH = DAYS_PER_MONTH * 5 / 7
YAHOO_SYMBOLS_PER_CALL = 40


@dataclass(frozen=True)
class CgEstimate:
    interval_min: int
    quotes: int
    history: int
    reserve: int
    total: int
    limit: int
    pct: float
    level: str  # ok (unter der Drosselschwelle) | tight (über der Schwelle, unter dem Limit) | over


def snap(value: object, presets: tuple[int, ...], default: int) -> int:
    """Nächstliegende Vorgabe (ungültige Eingaben → ``default``)."""
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return default
    return min(presets, key=lambda p: (abs(p - v), p))


def cg_estimate(interval_min: int, calls_per_refresh: int, history_series: int, limit: int,
                throttle_pct: float) -> CgEstimate:
    interval = max(1, int(interval_min))
    quotes = math.ceil(MINUTES_PER_MONTH / interval * max(0, calls_per_refresh))
    history = math.ceil(max(0, history_series) * DAYS_PER_MONTH / HISTORY_EVERY_DAYS)
    lim = max(1, int(limit))
    reserve = math.ceil(lim * RESERVE_PCT / 100)
    total = quotes + history + reserve
    level = "ok" if total <= lim * throttle_pct / 100 else ("tight" if total <= lim else "over")
    return CgEstimate(interval, quotes, history, reserve, total, lim, total / lim * 100, level)


def recommend_crypto(calls_per_refresh: int, history_series: int, limit: int, throttle_pct: float) -> int:
    if calls_per_refresh <= 0:
        return 10  # keine Krypto-Kurse abzurufen – Standard beibehalten
    for m in CRYPTO_PRESETS:
        if m >= RECOMMENDED_FLOOR_MIN and cg_estimate(m, calls_per_refresh, history_series, limit,
                                                      throttle_pct).level == "ok":
            return m
    return CRYPTO_PRESETS[-1]


def throttled_days(interval_min: int, calls_per_refresh: int, history_series: int, limit: int,
                   throttle_pct: float) -> float | None:
    """Tage, die das Rest-Kontingent ab der Drosselschwelle im Drosselbetrieb reicht (None = unbegrenzt)."""
    per_day = (1440 / max(1, interval_min)) * max(0, calls_per_refresh) + max(0, history_series) / HISTORY_EVERY_DAYS
    if per_day <= 0:
        return None
    return max(0.0, limit * (100 - throttle_pct) / 100) / per_day


def recommend_throttled(normal_min: int, calls_per_refresh: int, history_series: int, limit: int,
                        throttle_pct: float) -> int:
    """Mindestens doppelt so lang wie normal (sonst drosselt nichts) und nicht unter 30 Min.; bevorzugt so lang,
    dass das Rest-Kontingent ab der Drosselschwelle einen ganzen Monat reicht."""
    candidates = [m for m in THROTTLED_PRESETS if m >= max(2 * normal_min, 30)] or [THROTTLED_PRESETS[-1]]
    rest = limit * (100 - throttle_pct) / 100
    if history_series and rest / (history_series / HISTORY_EVERY_DAYS) < DAYS_PER_MONTH:
        # schon die Historie allein reicht keinen Monat – längere Kurs-Intervalle bringen kaum etwas
        return next((m for m in candidates if m >= 60), candidates[-1])
    for m in candidates:
        days = throttled_days(m, calls_per_refresh, history_series, limit, throttle_pct)
        if days is None or days >= DAYS_PER_MONTH:
            return m
    return candidates[-1]


def yahoo_calls_per_month(interval_min: int, symbols: int, fx: bool) -> int:
    """Grobe Obergrenze: Abrufe in den Handelsfenstern (Devisen ganztägig an Werktagen)."""
    if symbols <= 0 and not fx:
        return 0
    window = FX_WINDOW_MIN if fx else EU_WINDOW_MIN
    per_refresh = max(1, math.ceil((symbols + (1 if fx else 0)) / YAHOO_SYMBOLS_PER_CALL))
    return math.ceil(window / max(1, interval_min) * per_refresh * WEEKDAYS_PER_MONTH)


def stale_hint(stale_minutes: int, crypto_min: int, throttled_min: int) -> str | None:
    """Warnung, wenn Kurse zwischen zwei regulären Abrufen schon als veraltet gelten würden."""
    need = 2 * max(crypto_min, throttled_min)
    if stale_minutes < need:
        return (f"Krypto-Kurse gelten nach {stale_minutes} Min. als veraltet, werden aber (gedrosselt) nur alle "
                f"{max(crypto_min, throttled_min)} Min. abgerufen – Grenze auf mindestens {need} Min. setzen.")
    return None


def every(minutes: int) -> str:
    if minutes == 60:
        return "stündlich"
    if minutes > 60 and minutes % 60 == 0:
        return f"alle {minutes // 60} Std."
    return f"alle {minutes} Min."


def plan_view(settings: object, inputs: dict[str, object], used: dict[str, object], plan: str,
              has_key: bool) -> dict[str, object]:
    """Alles, was die Einstellungsseite für Auswahl, Hochrechnung und Empfehlung braucht."""
    get = settings.get  # type: ignore[attr-defined]
    limit = int(get("prices.coingecko_monthly_limit", 10000) or 10000)
    thr = float(get("prices.coingecko_throttle_pct", 80) or 80)
    calls = int(inputs.get("cg_calls", 0) or 0)  # type: ignore[call-overload]
    hist = int(inputs.get("cg_history", 0) or 0)  # type: ignore[call-overload]
    crypto_now = snap(get("prices.crypto_interval_min", 10), CRYPTO_PRESETS, 10)
    throttled_now = max(crypto_now, snap(get("prices.crypto_throttled_interval_min", 30), THROTTLED_PRESETS, 30))
    stock_now = snap(get("prices.stock_interval_min", 15), SECURITY_PRESETS, 15)
    ysym = int(inputs.get("yahoo_symbols", 0) or 0)  # type: ignore[call-overload]
    fx = bool(inputs.get("fx"))
    crypto = [(m, every(m), cg_estimate(m, max(calls, 1), hist, limit, thr)) for m in CRYPTO_PRESETS]
    return {
        "inputs": inputs, "limit": limit, "throttle_pct": thr, "plan": plan, "has_key": has_key, "used": used,
        "crypto": crypto, "crypto_now": crypto_now,
        "rec_crypto": recommend_crypto(max(calls, 1), hist, limit, thr),
        "estimate": cg_estimate(crypto_now, max(calls, 1), hist, limit, thr),
        "throttled": [(m, every(m), throttled_days(m, max(calls, 1), hist, limit, thr))
                      for m in THROTTLED_PRESETS if m >= crypto_now],
        "throttled_now": throttled_now,
        "rec_throttled": recommend_throttled(crypto_now, max(calls, 1), hist, limit, thr),
        "stock": [(m, every(m), yahoo_calls_per_month(m, ysym, fx)) for m in SECURITY_PRESETS],
        "stock_now": stock_now, "rec_stock": SECURITY_RECOMMENDED,
        "stock_calls": yahoo_calls_per_month(stock_now, ysym, fx),
        "stale_hint": stale_hint(int(get("prices.stale_crypto_minutes", 60) or 60), crypto_now, throttled_now),
        "every": every,
    }
