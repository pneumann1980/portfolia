"""Vollständiger Export und Neueinrichtung: App-Zustand neben dem Datenvertrag.

Der Gesamtexport (Import-ZIP, Schema 1.1) enthält die **wirksamen** Buchungen (Import mit Änderungen und
Löschungen in der App, Journal, freigegebene Sparplan-Ausführungen), Assets mit ihren Kursquellen, Konten,
Bestände und manuelle Kurse. Was der Datenvertrag nicht abbildet, liegt im Ordner ``portfolia/`` der ZIP-Datei
(andere Werkzeuge ignorieren ihn; Prüfsummen im Manifest unter ``extra_files``):

* ``state.json`` – Einstellungen, Kursquellen-Zuordnungen samt Status (auch abgelehnte Vorschläge),
  Sparplan-Wahl und verworfene Ausführungen, Datenquellen (ohne Zugangsdaten), „dauerhaft ignoriert“,
  Anbieter-IDs der Buchungen, CSV-Zuordnungen (Symbole, Konten, eigene Formate) und Kennungen gelöschter CSV- und
  Sync-Buchungen (damit sie nicht erneut importiert werden).
* ``usage.json`` – verbrauchte API-Aufrufe (CoinGecko-Monatskontingent läuft weiter).
* ``price_daily.csv``, ``series_meta.csv`` – Kurshistorie (die CoinGecko-Demo-API liefert nur 365 Tage nach).
* ``files/sources.yaml``, ``files/tax_rules/…`` – News-Quellen und lokale Steuerregeln.

Nie enthalten: API-Keys, Master-Key, Passwörter, Protokolle.

Neueinrichtung: Export-ZIP in den Importordner legen. Auf einer neuen Installation (noch keine Buchungen,
Einstellungen oder Datenquellen) übernimmt Portfolia die Zusatzdaten automatisch, sonst nach Bestätigung. Nichts
wird gelöscht: Einstellungen werden überschrieben, Zuordnungen und Entscheidungen ergänzt, gleichnamige
Datenquellen übersprungen, vorhandene Dateien vorher als ``.bak-…`` gesichert.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import __version__
from app.util.timeutil import iso

log = logging.getLogger(__name__)

FORMAT = "portfolia-state"
VERSION = 1
STATE = "state.json"
USAGE = "usage.json"
PRICES = "price_daily.csv"
META = "series_meta.csv"
FILES = "files/"
PRICE_COLS = ("series", "date", "open", "high", "low", "close", "volume", "split_factor", "ccy", "source")
META_COLS = ("series", "history_from", "history_to", "history_status")
DS_COLS = ("kind", "provider", "name", "account", "address", "credential_ref", "enabled", "sync_interval_min",
           "auto_commit", "note", "key_expires_on", "cursor_json")
MAX_FILE_BYTES = 5 * 1024 * 1024


def _rows(db: Any, sql: str, params: tuple[Any, ...] = (), drop: tuple[str, ...] = ("id",)) -> list[dict[str, Any]]:
    return [{k: r[k] for k in r.keys() if k not in drop}  # noqa: SIM118 – sqlite3.Row liefert beim Iterieren Werte
            for r in db.q(sql, params)]


def _csv(cols: tuple[str, ...], rows: Any) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r[c] is None else r[c] for c in cols])
    return buf.getvalue().encode("utf-8")


# ----------------------------------------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------------------------------------

def collect(ctx: Any, tx_ids: set[str]) -> dict[str, bytes]:
    """Zusatzdateien für den Gesamtexport (``tx_ids`` = exportierte Buchungen, für die Anbieter-IDs)."""
    db = ctx.db
    state: dict[str, Any] = {
        "format": FORMAT, "version": VERSION, "app_version": __version__,
        "settings": {r["key"]: json.loads(r["value_json"]) for r in db.q("SELECT key, value_json FROM settings "
                                                                        "ORDER BY key")},
        "asset_sources": _rows(db, "SELECT * FROM asset_source ORDER BY asset_id", drop=()),
        "plans": _rows(db, "SELECT * FROM plan ORDER BY key"),
        "plan_dismissed": _rows(db, "SELECT * FROM tx_estimate WHERE status='dismissed' ORDER BY plan_key, due_date"),
        "datasources": [{k: r[k] for k in DS_COLS} for r in db.q("SELECT * FROM data_source ORDER BY id")],
        "event_decisions": _rows(db, "SELECT event_key, decision, reason, decided_at FROM event_decision "
                                     "ORDER BY event_key", drop=()),
        "event_aliases": [r for r in _rows(db, "SELECT key, tx_id FROM journal_event_alias ORDER BY tx_id, key",
                                           drop=()) if r["tx_id"] in tx_ids],
        "csv_symbols": _rows(db, "SELECT symbol, asset_id FROM csv_symbol ORDER BY symbol", drop=()),
        "csv_accounts": _rows(db, "SELECT name, account FROM csv_account ORDER BY name", drop=()),
        "csv_mappings": _rows(db, "SELECT name, spec_json FROM csv_mapping ORDER BY name, id", drop=()),
        "deleted_journal": _rows(db, "SELECT * FROM journal_tx WHERE status='deleted' AND (external_id IS NOT NULL "
                                     "OR event_key IS NOT NULL) ORDER BY id", drop=("id", "form_json", "batch_id",
                                                                                    "datasource_id")),
    }
    out = {
        STATE: json.dumps(state, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8"),
        USAGE: json.dumps({"api_usage": _rows(db, "SELECT * FROM api_usage ORDER BY provider, period", drop=())},
                          sort_keys=True).encode("utf-8"),
        PRICES: _csv(PRICE_COLS, db.q(f"SELECT {', '.join(PRICE_COLS)} FROM price_daily ORDER BY series, date")),
        META: _csv(META_COLS, db.q(f"SELECT {', '.join(META_COLS)} FROM series_meta WHERE history_from IS NOT NULL "
                                   "ORDER BY series")),
    }
    cfg = ctx.config
    if cfg.sources_path.is_file() and cfg.sources_path.stat().st_size <= MAX_FILE_BYTES:
        out[f"{FILES}sources.yaml"] = cfg.sources_path.read_bytes()
    rules = cfg.tax_rules_dir
    if rules.is_dir():
        for p in sorted(rules.rglob("*")):
            if p.is_file() and not p.is_symlink() and p.stat().st_size <= MAX_FILE_BYTES \
                    and not any(part.startswith(".") for part in p.relative_to(rules).parts):
                out[f"{FILES}tax_rules/{p.relative_to(rules).as_posix()}"] = p.read_bytes()
    return out


# ----------------------------------------------------------------------------------------------------
# Neueinrichtung
# ----------------------------------------------------------------------------------------------------

def extras_for(db: Any, import_id: int) -> dict[str, bytes]:
    return {r["name"]: bytes(r["data"]) for r in db.q("SELECT name, data FROM import_extra WHERE import_id=?",
                                                       (import_id,))}


def _state(extras: dict[str, bytes]) -> dict[str, Any] | None:
    raw = extras.get(STATE)
    if raw is None:
        return None
    try:
        st = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(st, dict) or st.get("format") != FORMAT or int(st.get("version") or 0) > VERSION:
        return None
    return st


def summary(extras: dict[str, bytes]) -> dict[str, Any] | None:
    """Was eine Übernahme bringen würde (für die Rückfrage)."""
    st = _state(extras)
    if st is None:
        return None
    prices = extras.get(PRICES, b"")
    return {
        "app_version": st.get("app_version"),
        "settings": len(st.get("settings") or {}),
        "asset_sources": len(st.get("asset_sources") or []),
        "plans": len(st.get("plans") or []) + len(st.get("plan_dismissed") or []),
        "datasources": len(st.get("datasources") or []),
        "decisions": len(st.get("event_decisions") or []),
        "csv": len(st.get("csv_symbols") or []) + len(st.get("csv_accounts") or []) + len(st.get("csv_mappings")
                                                                                             or []),
        "deleted": len(st.get("deleted_journal") or []),
        "prices": max(0, prices.count(b"\n") - 1),
        "files": sorted(n[len(FILES):] for n in extras if n.startswith(FILES)),
    }


def is_fresh(db: Any) -> bool:
    """Neue Installation: noch keine Buchungen, Einstellungen, Datenquellen oder erfolgreichen Importe."""
    return not any(db.scalar(sql, default=0) for sql in (
        "SELECT COUNT(*) FROM journal_tx", "SELECT COUNT(*) FROM settings", "SELECT COUNT(*) FROM data_source",
        "SELECT COUNT(*) FROM imports WHERE status IN ('active', 'archived')", "SELECT COUNT(*) FROM tx_override"))


def status(ctx: Any) -> dict[str, Any] | None:
    """Zusatzdaten des aktiven Imports: noch offen, übernommen oder verworfen."""
    iid = ctx.active_import_id()
    if iid is None:
        return None
    extras = extras_for(ctx.db, iid)
    info = summary(extras) if extras else None
    if info is None:
        return None
    done = ctx.db.get_state(f"restore.{iid}") or {}
    return {"import_id": iid, "summary": info, "state": done.get("state", "pending"), "at": done.get("at"),
            "counts": done.get("counts")}


def dismiss(ctx: Any, import_id: int) -> None:
    ctx.db.set_state(f"restore.{import_id}", {"state": "dismissed", "at": iso(datetime.now(UTC))})


def _backup_write(path: Path, data: bytes, stamp: str) -> bool:
    if path.exists():
        if path.read_bytes() == data:
            return False
        path.replace(path.with_name(f"{path.name}.bak-{stamp}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def apply(ctx: Any, import_id: int) -> dict[str, int]:
    """Zusatzdaten eines importierten Portfolia-Exports übernehmen (idempotent, löscht nichts)."""
    db = ctx.db
    extras = extras_for(db, import_id)
    st = _state(extras)
    if st is None:
        raise ValueError("Keine gültigen Portfolia-Zusatzdaten in diesem Import.")
    now = iso(datetime.now(UTC)) or ""
    counts: dict[str, int] = {}
    with db.transaction() as c:
        n = 0
        for key, value in (st.get("settings") or {}).items():
            c.execute("INSERT INTO settings(key, value_json, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                      "SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                      (str(key), json.dumps(value, ensure_ascii=False), now))
            n += 1
        counts["settings"] = n
        counts["asset_sources"] = _insert(c, "asset_source", st.get("asset_sources"), "OR REPLACE")
        counts["plans"] = _upsert_plans(c, st.get("plans") or [])
        counts["plan_dismissed"] = _insert(c, "tx_estimate", st.get("plan_dismissed"), "OR IGNORE")
        counts["datasources"] = _datasources(c, st.get("datasources") or [], now)
        counts["decisions"] = _insert(c, "event_decision", st.get("event_decisions"), "OR IGNORE")
        counts["aliases"] = _insert(c, "journal_event_alias", st.get("event_aliases"), "OR IGNORE")
        counts["csv"] = (_insert(c, "csv_symbol", [{**r, "updated_at": now} for r in st.get("csv_symbols") or []],
                                 "OR IGNORE")
                         + _insert(c, "csv_account", [{**r, "updated_at": now} for r in st.get("csv_accounts") or []],
                                   "OR IGNORE")
                         + _mappings(c, st.get("csv_mappings") or [], now))
        counts["deleted"] = _insert(c, "journal_tx", [{**r, "status": "deleted"} for r in
                                                      st.get("deleted_journal") or []], "OR IGNORE")
        counts["usage"] = _usage(c, extras.get(USAGE))
        counts["prices"] = _prices(c, extras.get(PRICES))
        counts["series_meta"] = _meta(c, extras.get(META))
        c.execute("INSERT INTO journal_log(at, action, ref, before_json, after_json) VALUES (?,?,?,?,?)",
                  (now, "restore_apply", f"import {import_id}", None, json.dumps(counts)))
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    written = 0
    cfg = ctx.config
    for name, data in extras.items():
        if not name.startswith(FILES):
            continue
        rel = Path(name[len(FILES):])
        if rel.is_absolute() or ".." in rel.parts:
            continue
        if rel.as_posix() == "sources.yaml":
            written += _backup_write(cfg.sources_path, data, stamp)
        elif rel.parts and rel.parts[0] == "tax_rules" and len(rel.parts) > 1:
            written += _backup_write(cfg.tax_rules_dir.joinpath(*rel.parts[1:]), data, stamp)
    counts["files"] = written
    db.set_state(f"restore.{import_id}", {"state": "applied", "at": now, "counts": counts})
    ctx.settings.reload()
    ctx.invalidate_data()
    sched = getattr(ctx, "scheduler", None)
    if sched is not None:
        sched.reschedule_prices()
        try:
            from app.jobs.maintenance import reschedule_backup

            reschedule_backup(ctx)
        except Exception as e:  # Wartungsmodul optional
            log.debug("Backup-Zeitplan nicht angepasst: %s", e)
        for job, delay in (("history_backfill", 5), ("plans_update", 20), ("prices_crypto", 3),
                           ("prices_securities", 4)):
            sched.trigger(job, delay)
    log.info("Portfolia-Export übernommen (Import %s): %s", import_id, counts)
    return counts


def _insert(c: Any, table: str, rows: Any, mode: str) -> int:
    n = 0
    cols_ok = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        cols = [k for k in r if k in cols_ok and k != "id"]
        if not cols:
            continue
        cur = c.execute(f"INSERT {mode} INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [r[k] for k in cols])
        n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return n


def _upsert_plans(c: Any, rows: list[dict[str, Any]]) -> int:
    """Erkannte Sparpläne samt Wahl des Nutzers (aktiv/aus, eigener Betrag); die Erkennung aktualisiert den Rest."""
    n = 0
    cols_ok = {r[1] for r in c.execute("PRAGMA table_info(plan)")}
    for r in rows:
        cols = [k for k in r if k in cols_ok and k != "id"]
        if "key" not in cols:
            continue
        c.execute(f"INSERT INTO plan({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) ON CONFLICT(key) DO "
                  "UPDATE SET enabled=excluded.enabled, user_amount=excluded.user_amount", [r[k] for k in cols])
        n += 1
    return n


def _datasources(c: Any, rows: list[dict[str, Any]], now: str) -> int:
    """Datenquellen ohne Zugangsdaten (API-Keys neu eingeben); gleiche Anbieter/Konto/Name bleiben unberührt."""
    n = 0
    for r in rows:
        if not isinstance(r, dict) or not r.get("provider") or not r.get("name"):
            continue
        if c.execute("SELECT 1 FROM data_source WHERE provider=? AND account IS ? AND name=?",
                     (r["provider"], r.get("account"), r["name"])).fetchone():
            continue
        vals = {k: r.get(k) for k in DS_COLS}
        cols = [k for k, v in vals.items() if v is not None]
        c.execute(f"INSERT INTO data_source({', '.join(cols)}, status, created_at, updated_at) VALUES "
                  f"({', '.join('?' * len(cols))}, 'created', ?, ?)", [vals[k] for k in cols] + [now, now])
        n += 1
    return n


def _mappings(c: Any, rows: list[dict[str, Any]], now: str) -> int:
    n = 0
    for r in rows:
        if not r.get("name") or not r.get("spec_json"):
            continue
        if c.execute("SELECT 1 FROM csv_mapping WHERE name=?", (r["name"],)).fetchone():
            continue
        c.execute("INSERT INTO csv_mapping(name, spec_json, created_at, updated_at) VALUES (?,?,?,?)",
                  (r["name"], r["spec_json"], now, now))
        n += 1
    return n


def _usage(c: Any, raw: bytes | None) -> int:
    if not raw:
        return 0
    try:
        rows = json.loads(raw.decode("utf-8")).get("api_usage") or []
    except (UnicodeDecodeError, ValueError, AttributeError):
        return 0
    n = 0
    for r in rows:
        c.execute("INSERT INTO api_usage(provider, period, calls, units) VALUES (?,?,?,?) ON CONFLICT(provider, "
                  "period) DO UPDATE SET calls=MAX(calls, excluded.calls), units=MAX(units, excluded.units)",
                  (r.get("provider"), r.get("period"), int(r.get("calls") or 0), int(r.get("units") or 0)))
        n += 1
    return n


def _csv_rows(raw: bytes | None) -> list[dict[str, str]]:
    if not raw:
        return []
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8"), newline="")))


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _prices(c: Any, raw: bytes | None) -> int:
    """Kurshistorie ergänzen – vorhandene Tageskurse bleiben (sie sind mindestens so aktuell)."""
    now = iso(datetime.now(UTC))
    rows = []
    for r in _csv_rows(raw):
        close = _num(r.get("close"))
        if not r.get("series") or not r.get("date") or close is None or close <= 0:
            continue
        rows.append((r["series"], r["date"], _num(r.get("open")), _num(r.get("high")), _num(r.get("low")), close,
                     _num(r.get("volume")), _num(r.get("split_factor")) or 1.0, r.get("ccy") or None,
                     r.get("source") or "export", now))
    before = c.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0]
    c.executemany("INSERT OR IGNORE INTO price_daily(series, date, open, high, low, close, volume, split_factor, ccy, "
                  "source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return int(c.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0] - before)


def _meta(c: Any, raw: bytes | None) -> int:
    n = 0
    for r in _csv_rows(raw):
        if not r.get("series") or not r.get("history_from"):
            continue
        cur = c.execute("INSERT OR IGNORE INTO series_meta(series, history_from, history_to, history_status) "
                        "VALUES (?,?,?,?)", (r["series"], r["history_from"], r.get("history_to") or None,
                                             r.get("history_status") or None))
        n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return n
