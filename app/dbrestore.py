"""Sicherung der Datenbank in der App wiederherstellen (beim nächsten Start, bevor die Datenbank geöffnet wird).

Ablauf: :func:`request` merkt eine Sicherung aus ``/data/backups`` vor (Datei ``restore-request.json`` im
Datenverzeichnis) und :func:`restart_process` beendet den Prozess geordnet – Docker startet ihn neu
(``restart: unless-stopped``). Beim Start ruft :func:`apply_pending` *vor* dem Öffnen der Datenbank:

1. Sicherung entpacken und prüfen (``PRAGMA integrity_check``, Tabelle ``imports`` vorhanden);
2. den aktuellen Stand selbst als Sicherung ablegen („vor Wiederherstellung“ – so ist auch die Wiederherstellung
   umkehrbar), konsistent über die SQLite-Backup-API (inklusive WAL);
3. ``-wal``/``-shm`` des alten Stands entfernen (sonst würde altes WAL auf die neue Datei angewandt) und die geprüfte
   Datei atomar an die Stelle von ``app.sqlite`` setzen.

Schlägt ein Schritt vor 3 fehl, bleibt der aktuelle Stand unverändert. Das Ergebnis steht in ``restore-result.json``
(Anzeige unter „Was ich geändert habe“). Migrationen laufen danach wie bei jedem Start.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import os
import shutil
import signal
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
REQUEST_FILE = "restore-request.json"
RESULT_FILE = "restore-result.json"
LABELS_FILE = "labels.json"  # im Sicherungsordner: Dateiname → Anlass
CONFIRM_WORD = "WIEDERHERSTELLEN"
RESTART_DELAY_S = 1.5


# ----------------------------------------------------------------------------------------------------
# Anlass je Sicherung (Datei im Sicherungsordner – überlebt die Wiederherstellung, anders als die Datenbank)
# ----------------------------------------------------------------------------------------------------

def labels(backup_dir: Path) -> dict[str, str]:
    try:
        raw = json.loads((backup_dir / LABELS_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def set_label(backup_dir: Path, name: str, text: str) -> None:
    cur = labels(backup_dir)
    cur[name] = text[:120]
    present = {p.name for p in backup_dir.iterdir()} if backup_dir.is_dir() else set()
    cur = {k: v for k, v in cur.items() if k in present or k == name}
    tmp = backup_dir / f".{LABELS_FILE}.tmp"
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=0), "utf-8")
    os.replace(tmp, backup_dir / LABELS_FILE)


# ----------------------------------------------------------------------------------------------------
# Vormerken (laufende App)
# ----------------------------------------------------------------------------------------------------

def pending(config: Any) -> dict[str, Any] | None:
    try:
        raw = json.loads((config.data_dir / REQUEST_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("file") else None


def request(ctx: Any, name: str) -> list[str]:
    """Sicherung für den nächsten Start vormerken; Rückgabe: Fehler (leer = vorgemerkt)."""
    from app.jobs.maintenance import backup_path

    if backup_path(ctx, name) is None:
        return ["Diese Sicherung gibt es nicht (mehr)."]
    data = {"file": name, "requested_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    target = ctx.config.data_dir / REQUEST_FILE
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), "utf-8")
    os.replace(tmp, target)
    log.warning("Wiederherstellung vorgemerkt: %s (wird beim nächsten Start ausgeführt)", name)
    return []


def cancel(config: Any) -> bool:
    try:
        (config.data_dir / REQUEST_FILE).unlink()
    except FileNotFoundError:
        return False
    log.info("Vorgemerkte Wiederherstellung verworfen")
    return True


def result(config: Any) -> dict[str, Any] | None:
    try:
        raw = json.loads((config.data_dir / RESULT_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def dismiss_result(config: Any) -> None:
    with contextlib.suppress(FileNotFoundError):
        (config.data_dir / RESULT_FILE).unlink()


def _kill() -> None:
    os.kill(os.getpid(), signal.SIGTERM)  # geordnetes Beenden (Uvicorn), Docker startet neu


def restart_process(delay_s: float = RESTART_DELAY_S) -> None:
    """Prozess nach kurzer Pause geordnet beenden (die Antwort an den Browser geht vorher noch hinaus)."""
    t = threading.Timer(delay_s, _kill)
    t.daemon = True
    t.start()


# ----------------------------------------------------------------------------------------------------
# Ausführen (beim Start, Datenbank noch geschlossen)
# ----------------------------------------------------------------------------------------------------

def _check(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Integritätsprüfung der Sicherung fehlgeschlagen")
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='imports'").fetchone() is None:
            raise RuntimeError("Die Datei ist keine Portfolia-Datenbank")
    finally:
        con.close()


def _snapshot_current(db_path: Path, backup_dir: Path) -> str:
    """Aktuellen Stand (mit WAL) als Sicherung ablegen; Rückgabe: Dateiname."""
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    name = f"app-{stamp}.sqlite.gz"
    raw = backup_dir / f".restore-before-{stamp}.sqlite.tmp"
    gz_tmp = backup_dir / f".restore-before-{stamp}.gz.tmp"
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with raw.open("rb") as f, gzip.open(gz_tmp, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g, 1 << 20)
        os.replace(gz_tmp, backup_dir / name)
    finally:
        for p in (raw, gz_tmp):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
    return name


def apply_pending(config: Any) -> dict[str, Any] | None:
    """Vorgemerkte Wiederherstellung ausführen (``None``: nichts vorgemerkt). Nie bei geöffneter Datenbank aufrufen."""
    req = pending(config)
    if req is None:
        return None
    from app.jobs.maintenance import BACKUP_RE

    data_dir, backup_dir, db_path = Path(config.data_dir), Path(config.backup_dir), Path(config.db_path)
    name = str(req["file"])
    out: dict[str, Any] = {"ok": False, "file": name, "requested_at": req.get("requested_at"),
                           "at": datetime.now().astimezone().isoformat(timespec="seconds"), "error": ""}
    tmp = data_dir / ".restore.sqlite.tmp"
    try:
        src = backup_dir / name
        if not BACKUP_RE.match(name) or not src.is_file():
            raise RuntimeError("Sicherung nicht gefunden")
        with gzip.open(src, "rb") as g, tmp.open("wb") as f:
            shutil.copyfileobj(g, f, 1 << 20)
        _check(tmp)
        if db_path.exists():
            before = _snapshot_current(db_path, backup_dir)
            set_label(backup_dir, before, f"vor Wiederherstellung von {name}")
            out["before"] = before
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                Path(str(db_path) + suffix).unlink()
        os.replace(tmp, db_path)
        out["ok"] = True
        log.warning("Datenbank aus Sicherung %s wiederhergestellt (vorheriger Stand: %s)", name, out.get("before"))
    except Exception as e:  # aktueller Stand bleibt unverändert
        out["error"] = f"{type(e).__name__}: {e}"[:300]
        log.error("Wiederherstellung aus %s fehlgeschlagen – aktueller Stand unverändert: %s", name, out["error"])
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        with contextlib.suppress(FileNotFoundError):
            (data_dir / REQUEST_FILE).unlink()
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / RESULT_FILE).write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    return out
