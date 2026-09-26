"""Strukturierte Logs auf stdout (JSON) + Fehlerprotokoll in der App-Datenbank.

Sicherheitsrelevant: :class:`SecretRedactor` entfernt API-Keys aus *jeder* Logzeile, auch aus
Meldungen von Drittbibliotheken (httpx loggt z. B. vollständige URLs inkl. ``key=``-Parameter).
"""

from __future__ import annotations

import collections
import json
import logging
import re
import sqlite3
import sys
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path

# Nur in URL-Kontext (?key=… / &token=…) redigieren – normale Logtexte wie "Auth=basic" bleiben lesbar.
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:key|api_key|apikey|token|access_token|x_cg_demo_api_key|x_cg_pro_api_key|x-api-key)=)([^&\s\"']+)"
)


class SecretRedactor(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 6]

    def set_secrets(self, secrets: list[str]) -> None:
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def redact(self, text: str) -> str:
        for s in self._secrets:
            if s in text:
                text = text.replace(s, "***")
        return _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}***", text)

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
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self.redactor.redact(super().format(record))


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


_redactor = SecretRedactor()


def get_redactor() -> SecretRedactor:
    return _redactor


def setup_logging(level: str = "INFO", fmt: str = "json", secrets: list[str] | None = None) -> SecretRedactor:
    _redactor.set_secrets(secrets or [])
    root = logging.getLogger()
    for h in list(root.handlers):
        if not isinstance(h, DbEventHandler):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(_redactor) if fmt == "json" else TextFormatter(_redactor))
    handler.addFilter(_redactor)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Drittbibliotheken leiser stellen – httpx loggt sonst jede URL (inkl. Query-Parametern).
    for noisy in ("httpx", "httpcore", "urllib3", "yfinance", "peewee", "apscheduler.executors.default",
                  "curl_cffi", "hpack", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    # yfinance loggt jeden fehlgeschlagenen Ticker einzeln – Fehler fassen wir selbst je Quelle zusammen
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
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
