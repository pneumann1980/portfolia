"""SQLite-Zugriff (WAL) mit thread-lokalen Verbindungen und einfachen Migrationen."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Weitere Migrationen werden hier angehängt: (version, sql)
MIGRATIONS: list[tuple[int, str]] = [
    (1, "__schema__"),
    (2, """
CREATE TABLE IF NOT EXISTS yt_channel (
  handle      TEXT PRIMARY KEY,
  channel_id  TEXT,
  title       TEXT,
  subscribers INTEGER,
  status      TEXT NOT NULL,          -- ok | unresolvable | pending
  error       TEXT,
  method      TEXT,                   -- api | page | pinned
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_news_channel ON news_item(channel_id);
CREATE INDEX IF NOT EXISTS ix_news_video ON news_item(video_id);
"""),
    (3, """
-- Erkannte Sparpläne (abgeleitet aus dem Import) und geschätzte Ausführungen seit dem Importstand.
CREATE TABLE IF NOT EXISTS plan (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  key           TEXT NOT NULL UNIQUE,       -- Konto|Asset
  account       TEXT NOT NULL,
  asset_id      TEXT NOT NULL,
  freq          TEXT NOT NULL,              -- weekly | biweekly | semimonthly | monthly | bimonthly | quarterly
  days_json     TEXT,                       -- Ausführungstag(e) im Monat
  weekday       INTEGER,                    -- 0 = Montag (wöchentlich/zweiwöchentlich)
  time_local    TEXT,
  date_only     INTEGER NOT NULL DEFAULT 0,
  amount_eur    TEXT NOT NULL,
  fee_eur       TEXT NOT NULL DEFAULT '0',
  qty_decimals  INTEGER NOT NULL DEFAULT 6,
  funding_asset TEXT,
  funding       TEXT NOT NULL DEFAULT 'cash', -- cash (vom Kontoguthaben) | external (Lastschrift)
  weekend_shift INTEGER NOT NULL DEFAULT 1,
  executions    INTEGER NOT NULL DEFAULT 0,
  first_date    TEXT,
  last_date     TEXT,
  next_due      TEXT,
  confidence    TEXT NOT NULL,              -- hoch | mittel | niedrig
  status        TEXT NOT NULL,              -- active | paused | ended
  enabled       INTEGER,                    -- NULL = automatisch nach Sicherheit, 1/0 = Wahl des Nutzers
  user_amount   TEXT,                       -- vom Nutzer geänderte Sparrate
  detected_at   TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tx_estimate (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_key          TEXT NOT NULL,
  tx_id             TEXT NOT NULL UNIQUE,
  due_date          TEXT NOT NULL,          -- planmäßiger Termin (Idempotenz)
  ts_utc            TEXT NOT NULL,
  date_only         INTEGER NOT NULL DEFAULT 0,
  account           TEXT NOT NULL,
  asset_id          TEXT NOT NULL,
  qty               TEXT NOT NULL,
  price_eur         TEXT NOT NULL,
  value_eur         TEXT NOT NULL,
  fee_eur           TEXT NOT NULL DEFAULT '0',
  funding_asset     TEXT,
  funding           TEXT NOT NULL DEFAULT 'cash',
  price_source      TEXT,
  price_final       INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL,          -- estimated | confirmed | superseded | missing | dismissed
  user_edited       INTEGER NOT NULL DEFAULT 0,
  matched_tx_id     TEXT,
  missing_import_id INTEGER,
  import_id         INTEGER,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  confirmed_at      TEXT,
  note              TEXT,
  UNIQUE(plan_key, due_date)
);
CREATE INDEX IF NOT EXISTS ix_tx_estimate_status ON tx_estimate(status);
"""),
]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()

    # -- Verbindungen --------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-8000")  # ~8 MB pro Verbindung
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def close_thread_conn(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            with contextlib.suppress(Exception):
                c.close()
            self._local.conn = None

    # -- Migration ---------------------------------------------------------------------------
    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = self.conn
        current = c.execute("PRAGMA user_version").fetchone()[0]
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            script = SCHEMA_FILE.read_text(encoding="utf-8") if sql == "__schema__" else sql
            log.info("DB-Migration auf Version %s", version)
            c.executescript("BEGIN;" + script + f";PRAGMA user_version={version};COMMIT;")
        # Laufende Jobs aus einem vorherigen Prozess sind nicht mehr aktiv.
        c.execute("UPDATE job_status SET running=0 WHERE running=1")

    # -- Helfer -----------------------------------------------------------------------------
    def q(self, sql: str, params: Iterable[Any] | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def q1(self, sql: str, params: Iterable[Any] | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] | dict = (), default: Any = None) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return default if row is None or row[0] is None else row[0]

    def x(self, sql: str, params: Iterable[Any] | dict = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.conn.execute(sql, params)

    def xmany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._write_lock, self.transaction() as c:
            c.executemany(sql, rows)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Schreibtransaktion (BEGIN IMMEDIATE). Verschachtelte Aufrufe laufen in der äußeren mit."""
        c = self.conn
        if c.in_transaction:
            yield c
            return
        with self._write_lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    # -- app_state ---------------------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.q1("SELECT value FROM app_state WHERE key=?", (key,))
        if row is None or row[0] is None:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return row[0]

    def set_state(self, key: str, value: Any) -> None:
        self.x(
            "INSERT INTO app_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def backup_to(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(target)
        try:
            self.conn.backup(dst)
        finally:
            dst.close()
