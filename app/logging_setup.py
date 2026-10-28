"""Strukturierte Logs auf stdout (JSON) + Fehlerprotokoll in der App-Datenbank.

Sicherheitsrelevant: :class:`SecretRedactor` entfernt API-Keys aus *jeder* Logzeile, auch aus
Meldungen von Drittbibliotheken (httpx loggt z. B. vollständige URLs inkl. ``key=``-Parameter bzw. die
HMAC-``signature`` signierter Börsenabfragen).
"""

from __future__ import annotations

import collections
import contextlib
import contextvars
import json
import logging
import re
import sqlite3
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

# Nur in URL-Kontext (?key=… / &token=…) redigieren – normale Logtexte wie "Auth=basic" bleiben lesbar.
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:key|api_key|apikey|token|access_token|x_cg_demo_api_key|x_cg_pro_api_key|x-api-key|signature)=)"
    r"([^&\s\"']+)"
    r"|((?:x-api-key|authorization)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)([^\s,;\"'}]+)"
)


class SecretRedactor(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 6]
        self._temp: dict[str, int] = {}  # zur Laufzeit entschlüsselte Schlüssel (Referenzzähler)
        self._temp_lock = threading.Lock()

    def set_secrets(self, secrets: list[str]) -> None:
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    @contextlib.contextmanager
    def temporary(self, values: list[str]) -> Iterator[None]:
        """Werte für die Dauer eines Vorgangs zusätzlich redigieren (z. B. entschlüsselter API-Key beim Abruf)."""
        vals = [v for v in values if v and len(v) >= 6]
        with self._temp_lock:
            for v in vals:
                self._temp[v] = self._temp.get(v, 0) + 1
        try:
            yield
        finally:
            with self._temp_lock:
                for v in vals:
                    n = self._temp.get(v, 0) - 1
                    if n > 0:
                        self._temp[v] = n
                    else:
                        self._temp.pop(v, None)

    def redact(self, text: str) -> str:
        with self._temp_lock:
            temp = list(self._temp)
        for s in (*self._secrets, *temp):
            if s in text:
                text = text.replace(s, "***")
        return _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1) or m.group(3)}***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - defekte Format-Args
            msg = str(record.msg)
        red = self.redact(msg)
        if red != msg or record.args:
            record.msg = red
            record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            # Exception-Text wird im Formatter erzeugt; dort nochmals redigiert.
            pass
        return True


_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {"message", "asctime"}

# Kontext der laufenden Anfrage bzw. des laufenden Jobs: wird jeder Logzeile angehängt, damit sich alles zu einem
# Vorgang mit ``docker logs | grep <rid>`` finden lässt.
request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
job_name: contextvars.ContextVar[str] = contextvars.ContextVar("job_name", default="")


class ContextFilter(logging.Filter):
    """Hängt ``rid`` (Anfrage) und ``job`` an jeden Datensatz, sofern gesetzt und nicht schon explizit angegeben."""

    def filter(self, record: logging.LogRecord) -> bool:
        rid, job = request_id.get(), job_name.get()
        if rid and not hasattr(record, "rid"):
            record.rid = rid
        if job and not hasattr(record, "job"):
            record.job = job
        return True


def _extras(record: logging.LogRecord) -> dict[str, object]:
    # color_message: Uvicorn-Duplikat der Meldung mit ANSI-Farben
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")
            and k != "color_message"}


class FloodFilter(logging.Filter):
    """Begrenzt gleiche Warnungen/Fehler (gleicher Logger, gleiche Meldungsvorlage) auf ``limit`` je ``window_s``.

    Der Textabruf erzeugte an einem Tag 82 gleichartige Netzwerkfehler – einzeln ist das Rauschen, das die eigentlichen
    Fehler verdeckt. Die nächste Meldung nach dem Fenster nennt, wie viele unterdrückt wurden. Nur WARNING und höher;
    das Fehlerprotokoll in der Datenbank sieht weiterhin alles (eigener Handler ohne diesen Filter).
    """

    def __init__(self, limit: int = 5, window_s: float = 60.0) -> None:
        super().__init__()
        self.limit = limit
        self.window_s = window_s
        self._seen: dict[tuple[str, int, str], list[float]] = {}  # Schlüssel -> [Fensterbeginn, Anzahl, unterdrückt]
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        key = (record.name, record.levelno, str(record.msg)[:200])
        now = time.monotonic()
        with self._lock:
            st = self._seen.get(key)
            if st is None or now - st[0] >= self.window_s:
                dropped = int(st[2]) if st else 0
                if len(self._seen) > 2000:
                    self._seen.clear()
                self._seen[key] = [now, 1, 0]
                if dropped:
                    record.suppressed = dropped
                return True
            st[1] += 1
            if st[1] <= self.limit:
                return True
            st[2] += 1
            return False


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                try:
                    json.dumps(v)
                    payload[k] = v
                except TypeError:
                    payload[k] = str(v)
        if record.exc_info:
            payload["exc"] = self.redactor.redact("".join(traceback.format_exception(*record.exc_info)))
        return self.redactor.redact(json.dumps(payload, ensure_ascii=False))


class TextFormatter(logging.Formatter):
    """Lesbares Format für ``docker logs``: ``Zeit LEVEL logger: Meldung key=value``; Ausnahmen mehrzeilig darunter."""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        line = self.formatTime(record, self.datefmt) + f" {record.levelname:<7} {record.name}: {record.message}"
        extras = " ".join(f"{k}={_short(v)}" for k, v in _extras(record).items())
        if extras:
            line += "  " + extras
        if record.exc_info and record.exc_info[1] is not None:
            line += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return self.redactor.redact(line)


def _short(v: object) -> str:
    text = str(v)
    text = text if len(text) <= 120 else text[:117] + "…"
    return f'"{text}"' if " " in text else text


class DbEventHandler(logging.Handler):
    """Schreibt WARNING+ in die Tabelle ``event_log`` (für die Datenqualitäts-Seite).

    Schreibt gepuffert, damit Logging nie eine DB-Transaktion blockiert; ein Hintergrund-Thread
    leert den Puffer. Fehler beim Schreiben werden verschluckt (Logging darf nie crashen).
    """

    def __init__(self, db_path: Path, redactor: SecretRedactor, max_rows: int = 2000) -> None:
        super().__init__(level=logging.WARNING)
        self.db_path = db_path
        self.redactor = redactor
        self.max_rows = max_rows
        self._buf: collections.deque = collections.deque(maxlen=500)
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="event-log-writer", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("app.logging"):
                return
            ctx = {}
            for k, v in record.__dict__.items():
                if k not in _RESERVED and not k.startswith("_"):
                    ctx[k] = v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
            msg = self.redactor.redact(record.getMessage())
            if record.exc_info and record.exc_info[1] is not None:
                msg += f" ({type(record.exc_info[1]).__name__}: {self.redactor.redact(str(record.exc_info[1]))})"
            with self._lock:
                self._buf.append((
                    datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
                    record.levelname, record.name, msg[:2000],
                    json.dumps(ctx, ensure_ascii=False, default=str)[:2000],
                ))
            self._event.set()
        except Exception:  # noqa: S110 - Logging darf niemals Exceptions werfen
            pass

    def _run(self) -> None:
        while not self._stop:
            self._event.wait(timeout=5)
            self._event.clear()
            self.flush_now()

    def flush_now(self) -> None:
        with self._lock:
            rows = list(self._buf)
            self._buf.clear()
        if not rows:
            return
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            try:
                conn.executemany(
                    "INSERT INTO event_log(ts, level, logger, message, context_json) VALUES (?,?,?,?,?)", rows
                )
                conn.execute(
                    "DELETE FROM event_log WHERE id < (SELECT COALESCE(MAX(id),0) - ? FROM event_log)",
                    (self.max_rows,),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: S110
            pass

    def close(self) -> None:
        self._stop = True
        self._event.set()
        self.flush_now()
        super().close()


class _DropAsgiTraceback(logging.Filter):
    """Uvicorn meldet unbehandelte Fehler zusätzlich mit ~100 Zeilen Framework-Traceback und ohne Pfad. Die App
    protokolliert denselben Fehler bereits kompakt (``app.web``, mit Methode, Pfad, Anfrage-ID und Fundstelle)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Exception in ASGI application")


class RingBufferHandler(logging.Handler):
    """Hält die letzten ``capacity`` Zeilen (ab INFO, redigiert) im Speicher: Download auf der Diagnose-Seite, falls der
    Zugriff auf ``docker logs`` unbequem ist (z. B. Unraid). Geht beim Neustart verloren."""

    def __init__(self, redactor: SecretRedactor, capacity: int = 2000) -> None:
        super().__init__(level=logging.INFO)
        self.setFormatter(TextFormatter(redactor))
        self._buf: collections.deque[str] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            with self._lock:
                self._buf.append(line)
        except Exception:  # noqa: S110 - Logging darf niemals Exceptions werfen
            pass

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._buf)


_redactor = SecretRedactor()
_ring: RingBufferHandler | None = None


def recent_lines() -> list[str]:
    """Die letzten Logzeilen seit dem Start (leer, solange das Logging nicht eingerichtet ist)."""
    return _ring.lines() if _ring is not None else []


def parse_levels(spec: str) -> dict[str, int]:
    """``"app.news=DEBUG,app.db=WARNING"`` -> {Logger: Level}; ungültige Einträge werden übergangen."""
    out: dict[str, int] = {}
    for part in (spec or "").split(","):
        name, _, lvl = part.strip().partition("=")
        level = getattr(logging, lvl.strip().upper(), None)
        if name.strip() and isinstance(level, int):
            out[name.strip()] = level
    return out


def get_redactor() -> SecretRedactor:
    return _redactor


def setup_logging(level: str = "INFO", fmt: str = "text", secrets: list[str] | None = None,
                  levels: str = "") -> SecretRedactor:
    global _ring
    _redactor.set_secrets(secrets or [])
    root = logging.getLogger()
    for h in list(root.handlers):
        if not isinstance(h, DbEventHandler):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(_redactor) if fmt == "json" else TextFormatter(_redactor))
    # Reihenfolge: Kontext ergänzen, Flut begrenzen (sieht noch die Meldungsvorlage), zuletzt Geheimnisse entfernen
    handler.addFilter(ContextFilter())
    handler.addFilter(FloodFilter())
    handler.addFilter(_redactor)
    root.addHandler(handler)
    _ring = RingBufferHandler(_redactor)
    _ring.addFilter(ContextFilter())
    _ring.addFilter(_redactor)
    root.addHandler(_ring)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Drittbibliotheken leiser stellen – httpx loggt sonst jede URL (inkl. Query-Parametern).
    for noisy in ("httpx", "httpcore", "urllib3", "yfinance", "peewee", "apscheduler.executors.default",
                  "curl_cffi", "hpack", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    # yfinance loggt jeden fehlgeschlagenen Ticker einzeln – Fehler fassen wir selbst je Quelle zusammen
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    uv_err = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _DropAsgiTraceback) for f in uv_err.filters):
        uv_err.addFilter(_DropAsgiTraceback())
    for name, lvl in parse_levels(levels).items():  # gezielt einzelne Bereiche anheben/absenken (LOG_LEVELS)
        logging.getLogger(name).setLevel(lvl)
    return _redactor


def attach_db_handler(db_path: Path) -> DbEventHandler:
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, DbEventHandler):
            root.removeHandler(h)
            h.close()
    h = DbEventHandler(db_path, _redactor)
    h.addFilter(_redactor)
    root.addHandler(h)
    return h
