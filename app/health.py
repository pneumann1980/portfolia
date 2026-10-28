"""Betriebszustand für das Protokoll: Startbanner, stündliche Zustandszeile, kritische Hinweise für die Oberfläche.

Keine Geheimnisse: Anbieter erscheinen nur als ja/nein, Pfade nur als Verzeichnis. Die Messwerte (Threads, offene
Datenbankverbindungen, Speicher) dienen auch dazu, Annahmen aus ``docs/PROJEKTANALYSE.md`` (M4/M6) zu belegen.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import sqlite3
import threading
import time
from typing import Any

from app import REVISION, __version__
from app.context import AppContext

log = logging.getLogger("app")
health_log = logging.getLogger("app.health")

LOW_DISK_MB = 100
WRITE_ERROR_WINDOW_S = 600.0
_CRITICAL_TTL_S = 30.0
_critical_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_critical_lock = threading.Lock()


def rss_mb() -> int | None:
    """Resident Set Size aus ``/proc/self/status`` (nur Linux; sonst ``None``) – keine zusätzliche Abhängigkeit."""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def db_size_mb(ctx: AppContext) -> int:
    try:
        return round(os.path.getsize(ctx.config.db_path) / 1_048_576)
    except OSError:
        return 0


def schema_version(ctx: AppContext) -> int | None:
    try:
        return int(ctx.db.scalar("PRAGMA user_version", default=0))
    except sqlite3.Error:
        return None


def _providers(ctx: AppContext) -> str:
    names = {"COINGECKO_API_KEY": "coingecko", "FINNHUB_API_KEY": "finnhub", "YOUTUBE_API_KEY": "youtube",
             "GEMINI_API_KEY": "gemini", "ANTHROPIC_API_KEY": "anthropic", "CRYPTOPANIC_API_KEY": "cryptopanic"}
    return " ".join(f"{names[k]}={'ja' if v else 'nein'}" for k, v in ctx.config.secrets.status().items() if k in names)


def log_startup(ctx: AppContext) -> None:
    """Startbanner: was läuft, womit, und was ist abgeschaltet (z. B. Anmeldung). Vor dem Fehlerprotokoll der
    Datenbank aufrufen, damit die Warnung zur fehlenden Anmeldung nicht bei jedem Neustart auf der Datenqualitäts-Seite
    landet."""
    cfg = ctx.config
    log.info("Portfolia %s (Build %s) | Python %s | SQLite %s | TZ %s", __version__, REVISION[:7] or "lokal",
             platform.python_version(), sqlite3.sqlite_version, cfg.tz)
    log.info("Daten %s (DB %d MB, Schema %s) | Port %s | Demo=%s | Log: Format=%s Level=%s Zugriffe=%s", cfg.data_dir,
             db_size_mb(ctx), schema_version(ctx), cfg.port, cfg.demo_mode, cfg.log_format, cfg.log_level,
             cfg.log_access)
    log.info("Anbieter: %s", _providers(ctx))
    if cfg.auth_mode == "none":
        log.warning("AUTH_MODE=none – jede Person, die den Port erreicht, sieht und ändert alle Daten. "
                    "Nur im vertrauenswürdigen Heimnetz betreiben oder AUTH_MODE=basic setzen.")


def log_modules(report: dict[str, list[str]]) -> None:
    failed = report.get("failed", [])
    log.info("Module: %d geladen%s", len(report.get("loaded", [])),
             f", {len(failed)} FEHLGESCHLAGEN: {', '.join(failed)}" if failed else "")
    if failed:
        log.error("Nicht geladene Module: %s – deren Seiten und Funktionen fehlen", ", ".join(failed))


def log_scheduler(ctx: AppContext) -> None:
    sched = ctx.scheduler
    if sched is None:
        log.info("Scheduler: aus (Hintergrundjobs laufen nicht)")
        return
    jobs = sched.sched.get_jobs()
    log.info("Scheduler: an, %d Jobs, Threads je Pool: %s", len(jobs),
             ", ".join(f"{n}={w}" for n, w in sched.pool_sizes.items()))


def state(ctx: AppContext) -> dict[str, Any]:
    """Kennzahlen für die Zustandszeile (und Tests)."""
    sched = ctx.scheduler
    running = 0
    missed = 0
    if sched is not None:
        running = int(ctx.db.scalar("SELECT COUNT(*) FROM job_status WHERE running=1", default=0))
        missed = sum(sched.missed.values())
    return {"threads": threading.active_count(), "db_conns": ctx.db.conn_count, "rss_mb": rss_mb(),
            "jobs_running": running, "missed": missed, "db_mb": db_size_mb(ctx)}


def log_state(ctx: AppContext) -> None:
    s = state(ctx)
    health_log.info("Betriebszustand: Threads %s | DB-Verbindungen %s | Speicher %s | Jobs laufend %s | "
                    "verpasste Läufe seit Start %s | DB %s MB", s["threads"], s["db_conns"],
                    f"{s['rss_mb']} MB" if s["rss_mb"] is not None else "?", s["jobs_running"], s["missed"], s["db_mb"])


# -- kritische Hinweise für die Oberfläche -----------------------------------------------------------------------
def critical_alerts(ctx: AppContext) -> list[dict[str, str]]:
    """Nur Zustände, in denen **Benutzeraktionen nicht ausgeführt werden können** (keine Konfigurationshinweise).

    Kurz zwischengespeichert, weil jede Seite die Hinweise abfragt.
    """
    key = str(ctx.config.db_path)
    now = time.monotonic()
    with _critical_lock:
        hit = _critical_cache.get(key)
    if hit is not None and now - hit[0] < _CRITICAL_TTL_S:
        return [dict(a) for a in hit[1]]
    out = _critical(ctx)
    with _critical_lock:
        _critical_cache[key] = (now, out)
    return [dict(a) for a in out]


def _critical(ctx: AppContext) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    cfg = ctx.config
    try:
        if not os.access(cfg.data_dir, os.W_OK) or (cfg.db_path.exists() and not os.access(cfg.db_path, os.W_OK)):
            out.append({"level": "crit", "text": "Das Datenverzeichnis ist nicht beschreibbar – Änderungen (Import, "
                        "Buchungen, Einstellungen) können nicht gespeichert werden. Rechte des Verzeichnisses "
                        f"{cfg.data_dir} prüfen.", "href": "/quality"})
        else:
            free_mb = shutil.disk_usage(cfg.data_dir).free // 1_048_576
            if free_mb < LOW_DISK_MB:
                out.append({"level": "crit", "text": f"Nur noch {free_mb} MB freier Speicher im Datenverzeichnis – "
                            "Speichern und Importe können fehlschlagen.", "href": "/quality"})
    except OSError as e:
        log.debug("Speicherprüfung nicht möglich: %s", e)
    err = ctx.db.last_write_error
    if err is not None and time.monotonic() - err[0] < WRITE_ERROR_WINDOW_S:
        out.append({"level": "crit", "text": f"Schreibzugriff auf die Datenbank ist fehlgeschlagen ({err[1]}) – "
                    "Änderungen werden womöglich nicht gespeichert.", "href": "/quality"})
    if cfg.scheduler_enabled and ctx.scheduler is not None and not ctx.scheduler.sched.running:
        out.append({"level": "crit", "text": "Der Hintergrunddienst läuft nicht – Kurse, Importe und News werden nicht "
                    "mehr aktualisiert. Container neu starten.", "href": "/quality"})
    return out


def reset_cache() -> None:
    with _critical_lock:
        _critical_cache.clear()
