"""HTTP-Client mit Timeouts, Retry/Backoff (inkl. Retry-After), Rate-Limit und Drosselung je Quelle."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.db import Database
from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """Mindestabstand zwischen Anfragen (thread-sicher)."""

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval = min_interval_s
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next = now + self.min_interval


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def make_client(user_agent: str, timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


def request_with_retry(client: httpx.Client, method: str, url: str, *, retries: int = 3,
                       limiter: RateLimiter | None = None, sleep: Callable[[float], None] = time.sleep,
                       **kwargs: Any) -> httpx.Response:
    """Anfrage mit Backoff. Wirft :class:`HttpError` nach erschöpften Versuchen oder bei 4xx (≠429)."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        if limiter:
            limiter.wait()
        try:
            resp = client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt >= retries:
                break
            sleep(min(30.0, (2 ** attempt) * 0.8 + random.uniform(0, 0.5)))
            continue
        if resp.status_code in RETRY_STATUS and attempt < retries:
            ra = resp.headers.get("Retry-After")
            try:
                delay = float(ra) if ra else (2 ** attempt) * 1.0
            except ValueError:
                delay = (2 ** attempt) * 1.0
            sleep(min(60.0, delay + random.uniform(0, 0.5)))
            continue
        if resp.status_code >= 400:
            raise HttpError(f"HTTP {resp.status_code}", resp.status_code)
        return resp
    raise HttpError(f"{type(last_exc).__name__}: {last_exc}" if last_exc else "Anfrage fehlgeschlagen")


class SourceGuard:
    """Protokolliert Erfolg/Fehler je Quelle und drosselt fehlerhafte Quellen exponentiell."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def allowed(self, source_id: str) -> bool:
        row = self.db.q1("SELECT next_allowed FROM source_status WHERE source_id=?", (source_id,))
        if row is None or not row["next_allowed"]:
            return True
        nxt = parse_iso(row["next_allowed"])
        return nxt is None or datetime.now(UTC) >= nxt

    def success(self, source_id: str, kind: str, name: str | None = None, items: int | None = None,
                etag: str | None = None, last_modified: str | None = None) -> None:
        now = iso(datetime.now(UTC))
        self.db.x(
            """INSERT INTO source_status(source_id, kind, name, last_attempt, last_success, consecutive_failures,
                   next_allowed, last_error, etag, last_modified, items_last, verified)
               VALUES (?,?,?,?,?,0,NULL,NULL,?,?,?, 'ok')
               ON CONFLICT(source_id) DO UPDATE SET kind=excluded.kind, name=COALESCE(excluded.name, name),
                   last_attempt=excluded.last_attempt, last_success=excluded.last_success, consecutive_failures=0,
                   next_allowed=NULL, last_error=NULL, etag=COALESCE(excluded.etag, etag),
                   last_modified=COALESCE(excluded.last_modified, last_modified),
                   items_last=COALESCE(excluded.items_last, items_last), verified='ok'""",
            (source_id, kind, name, now, now, etag, last_modified, items),
        )

    def failure(self, source_id: str, kind: str, error: str, name: str | None = None,
                base_minutes: float = 30, max_hours: float = 24) -> int:
        row = self.db.q1("SELECT consecutive_failures FROM source_status WHERE source_id=?", (source_id,))
        n = (row["consecutive_failures"] if row else 0) + 1
        # Erste zwei Fehler ohne Sperre (Einzelausfälle), danach exponentiell bis max_hours
        delay_min = 0 if n <= 2 else min(max_hours * 60, base_minutes * (2 ** (n - 3)))
        nxt = iso(datetime.now(UTC) + timedelta(minutes=delay_min)) if delay_min else None
        now = iso(datetime.now(UTC))
        verified = "unreachable" if n >= 3 else "pending"
        self.db.x(
            """INSERT INTO source_status(source_id, kind, name, last_attempt, consecutive_failures, next_allowed,
                   last_error, verified) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(source_id) DO UPDATE SET kind=excluded.kind, name=COALESCE(excluded.name, name),
                   last_attempt=excluded.last_attempt, consecutive_failures=excluded.consecutive_failures,
                   next_allowed=excluded.next_allowed, last_error=excluded.last_error,
                   verified=CASE WHEN last_success IS NULL OR excluded.consecutive_failures >= 3
                                 THEN excluded.verified ELSE verified END""",
            (source_id, kind, name, now, n, nxt, error[:500], verified),
        )
        log.warning("Quelle %s fehlgeschlagen (%d×): %s", source_id, n, error[:200],
                    extra={"source": source_id, "failures": n})
        return n

    def cached_headers(self, source_id: str) -> dict[str, str]:
        row = self.db.q1("SELECT etag, last_modified FROM source_status WHERE source_id=?", (source_id,))
        h: dict[str, str] = {}
        if row:
            if row["etag"]:
                h["If-None-Match"] = row["etag"]
            if row["last_modified"]:
                h["If-Modified-Since"] = row["last_modified"]
        return h


class Quota:
    """Zähler für API-Kontingente (Monat/Tag)."""

    def __init__(self, db: Database, provider: str, period: str = "month") -> None:
        self.db = db
        self.provider = provider
        self.period_kind = period

    def period(self, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        return now.strftime("%Y-%m") if self.period_kind == "month" else now.strftime("%Y-%m-%d")

    def add(self, calls: int = 1, units: int = 0) -> None:
        self.db.x(
            """INSERT INTO api_usage(provider, period, calls, units) VALUES (?,?,?,?)
               ON CONFLICT(provider, period) DO UPDATE SET calls=calls+excluded.calls, units=units+excluded.units""",
            (self.provider, self.period(), calls, units),
        )

    def used(self) -> tuple[int, int]:
        row = self.db.q1("SELECT calls, units FROM api_usage WHERE provider=? AND period=?",
                         (self.provider, self.period()))
        return (row["calls"], row["units"]) if row else (0, 0)
