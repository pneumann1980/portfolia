"""Quellen-Hosts beim Artikeltext-Abruf: pausiert oder „nur Teaser“ – und was das für die Analyse heißt.

* **Pausiert** (``news.enrich.host.<host>``): nach wiederholten Fehlern für ``HOST_PAUSE`` ausgesetzt (``enrich``).
* **Nur Teaser:** Ein Host, der dauerhaft keinen brauchbaren Volltext liefert (Sperre/403, Bezahlschranke, kein Inhalt,
  nur der Anreißer). Er wird nicht mehr abgerufen – außer einem Probeabruf je Woche –, und die Analyse wartet für seine
  Meldungen nicht auf einen Text, der nicht kommt, sondern nimmt sofort den Teaser.

Erkennung (nur aus gespeicherten Abrufergebnissen, ohne eigene Liste): Von den letzten ``SAMPLE`` Ergebnissen je Host
(14 Tage) zählen Volltext-Erfolge positiv, Sperren/leere Seiten/„nur Anreißer“ negativ. Vorübergehende Fehler
(Zeitüberschreitung, Netz, DNS, 5xx …) und Teiltexte zählen nicht – ein Netzausfall darf keinen Host abstempeln.
„Nur Teaser“, wenn mindestens ``MIN_SAMPLES`` Ergebnisse vorliegen und höchstens ``MAX_SUCCESS`` Volltext sind.
Eine Woche nach dem letzten Abruf ist ein Probeabruf fällig; gelingt er (und damit wieder ≥ 2 Erfolge in den letzten
``SAMPLE``), ist der Host wieder normal.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from app.context import AppContext
from app.util.timeutil import iso, parse_iso

SAMPLE = 10
MIN_SAMPLES = 8
MAX_SUCCESS = 1
LOOKBACK_DAYS = 14
PROBE_EVERY = timedelta(days=7)
CACHE_S = 20.0  # die Abfrage läuft sonst bei jedem Lauf und bei jeder Aktualisierung der Statusseite (alle 3 s)
# Google News: die Meldungs-URL gehört immer news.google.com, der Verlag steht erst in final_url
AGGREGATOR_HOSTS = {"news.google.com"}
# Ergebnisse, die nichts über den Host sagen (vorübergehend); entspricht ``enrich.RETRYABLE``
TRANSIENT = {"timeout", "network", "rate_limited", "http_error", "dns", "gnews_unresolved", "internal"}


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def paused_hosts(ctx: AppContext) -> dict[str, datetime]:
    """Hosts, die nach wiederholten Fehlern/Sperren pausiert sind (Host -> pausiert bis)."""
    now = datetime.now(UTC)
    out: dict[str, datetime] = {}
    for r in ctx.db.q("SELECT key FROM app_state WHERE key LIKE 'news.enrich.host.%'"):
        st = ctx.db.get_state(r["key"]) or {}
        until = parse_iso(st.get("until")) if isinstance(st, dict) else None
        if until is not None and until > now:
            out[r["key"].removeprefix("news.enrich.host.")] = until
    return out


def _verdict(status: str, reason: str | None) -> int | None:
    """+1 Volltext, -1 kein brauchbarer Volltext, ``None`` = sagt nichts über den Host."""
    if status == "success":
        return 1
    if (reason or "") in TRANSIENT:
        return None
    if status in ("blocked", "failed"):  # Sperre, leere Seite, Inhaltstyp, zu groß … – der Host liefert keinen Text
        return -1
    if status == "partial" and reason in ("paywall", "teaser_only"):
        return -1
    return None  # Teiltext „short“ u. Ä.


_cache: dict[str, tuple[float, dict[str, datetime | None]]] = {}
_cache_lock = threading.Lock()


def invalidate() -> None:
    """Zwischenspeicher verwerfen (nach neuen Abrufergebnissen)."""
    with _cache_lock:
        _cache.clear()


def teaser_hosts(ctx: AppContext, *, max_age: float = 0.0) -> dict[str, datetime | None]:
    """Hosts ohne brauchbaren Volltext (Host -> Zeitpunkt des letzten Abrufs).

    ``max_age`` > 0 erlaubt ein Ergebnis aus dem Zwischenspeicher (höchstens so viele Sekunden alt).
    """
    key = str(ctx.db.path)
    now = time.monotonic()
    if max_age > 0:
        with _cache_lock:
            hit = _cache.get(key)
        if hit is not None and now - hit[0] <= max_age:
            return dict(hit[1])
    out = _teaser_hosts(ctx)
    with _cache_lock:
        _cache[key] = (now, out)
    return dict(out)


def _teaser_hosts(ctx: AppContext) -> dict[str, datetime | None]:
    since = iso(datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS))
    rows = ctx.db.q(
        """SELECT COALESCE(NULLIF(c.final_url, ''), n.url) AS url, c.status, c.reason, c.fetched_at
           FROM news_content c JOIN news_item n ON n.id=c.item_id
           WHERE c.status IN ('success','partial','blocked','failed') AND c.fetched_at >= ?
           ORDER BY c.fetched_at DESC LIMIT 6000""", (since,))
    votes: dict[str, list[int]] = {}
    last: dict[str, str] = {}
    for r in rows:
        host = host_of(r["url"])
        if not host or host in AGGREGATOR_HOSTS:  # nicht aufgelöste Sammelseite sagt nichts über einen Verlag
            continue
        last.setdefault(host, r["fetched_at"])  # absteigend sortiert: der erste ist der jüngste
        v = _verdict(r["status"], r["reason"])
        if v is not None and len(votes.setdefault(host, [])) < SAMPLE:
            votes[host].append(v)
    out: dict[str, datetime | None] = {}
    for host, vs in votes.items():
        if len(vs) >= MIN_SAMPLES and sum(1 for v in vs if v > 0) <= MAX_SUCCESS:
            out[host] = parse_iso(last.get(host))
    return out


def probe_due(last_attempt: datetime | None) -> bool:
    """Ein Teaser-Host wird höchstens einmal je Woche neu geprüft."""
    return last_attempt is None or datetime.now(UTC) - last_attempt >= PROBE_EVERY


def text_unlikely(host: str, paused: dict[str, datetime], teaser: dict[str, datetime | None]) -> bool:
    """Kommt für Meldungen dieses Hosts in absehbarer Zeit kein Volltext? (Pause oder „nur Teaser“)."""
    return host in paused or host in teaser


__all__ = ["host_of", "invalidate", "paused_hosts", "probe_due", "teaser_hosts", "text_unlikely"]
