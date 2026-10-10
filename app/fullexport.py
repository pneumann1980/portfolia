"""Vollständiger Export und Neueinrichtung: App-Zustand neben dem Datenvertrag.

Der Gesamtexport (Import-ZIP, Schema 1.1) enthält die **wirksamen** Buchungen (Import mit Änderungen und
Löschungen in der App, Journal, freigegebene Sparplan-Ausführungen), Assets mit ihren Kursquellen, Konten,
Bestände und manuelle Kurse. Was der Datenvertrag nicht abbildet, liegt im Ordner ``portfolia/`` der ZIP-Datei
(andere Werkzeuge ignorieren ihn; Prüfsummen im Manifest unter ``extra_files``):

* ``state.json`` – Einstellungen, Kursquellen-Zuordnungen samt Status (auch abgelehnte Vorschläge),
  Sparplan-Wahl und verworfene Ausführungen, Datenquellen (ohne Zugangsdaten), „dauerhaft ignoriert“,
  Anbieter-IDs der Buchungen, CSV-Zuordnungen (Symbole, Konten, eigene Formate), Kennungen gelöschter CSV- und
  Sync-Buchungen (damit sie nicht erneut importiert werden) und Befunde der Diagnose, die als „geprüft“ markiert
  sind (übernommene Korrekturen stecken bereits in den Buchungen und Zuordnungen).
* ``usage.json`` – verbrauchte API-Aufrufe (CoinGecko-Monatskontingent läuft weiter).
* ``price_daily.csv``, ``series_meta.csv`` – Kurshistorie (die CoinGecko-Demo-API liefert nur 365 Tage nach) samt
  Herkunft je Tag und Prüfergebnis der Ersatzhistorie.
* ``taxdata.json`` – Steuerdaten je Jahr (Dateien mit Status/Verlauf und Datensätze samt Zuordnung);
  ``files/tax/…`` die Originaldateien.
* ``files/sources.yaml``, ``files/tax_rules/…`` – News-Quellen und lokale Steuerregeln.

Nie enthalten: API-Keys, Master-Key, Passwörter, Protokolle.

Neueinrichtung: Export-ZIP in den Importordner legen. Auf einer neuen Installation (noch keine Buchungen,
Einstellungen oder Datenquellen) übernimmt Portfolia die Zusatzdaten automatisch, sonst nach Bestätigung. Nichts
wird gelöscht: Einstellungen werden überschrieben, Zuordnungen und Entscheidungen ergänzt, gleichnamige
Datenquellen übersprungen, vorhandene Dateien vorher als ``.bak-…`` gesichert.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import logging
import os
import shutil
import threading
import uuid
from collections import defaultdict
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
META_COLS = ("series", "history_from", "history_to", "history_status", "alt_series", "alt_status", "alt_note",
             "alt_checked_at")
TAXDATA = "taxdata.json"
TAX_FILE_COLS = ("tax_year", "filename", "origin", "sha256", "size", "format", "parser", "status", "records", "matched",
                 "unmatched", "conflicts", "years_json", "warnings_json", "errors_json", "created_at", "imported_at",
                 "replaced_at", "note")
TAX_RECORD_COLS = ("line", "tax_year", "transaction_id", "external_id", "asset", "quantity", "acquisition_date",
                   "disposal_date", "acquisition_cost", "disposal_value", "holding_period_days", "taxable",
                   "gain_loss", "tax_category", "source", "comment", "match_status", "match_tx", "match_method",
                   "match_note")
DS_COLS = ("kind", "provider", "name", "account", "address", "credential_ref", "enabled", "sync_interval_min",
           "auto_commit", "note", "key_expires_on", "cursor_json", "wallet_group", "watch_json")
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
        # verknüpfte Quelldatensätze (Importprüfung): Werte und Herkunft weiterer Quellen je Buchung
        "tx_links": [r for r in _rows(db, "SELECT tx_id, source, ext_id, event_key, role, record_json, "
                                          "assessment_json, created_at FROM tx_link WHERE status='active' ORDER BY id",
                                      drop=()) if r["tx_id"] in tx_ids],
        "csv_symbols": _rows(db, "SELECT symbol, asset_id, origin FROM csv_symbol ORDER BY symbol", drop=()),
        "csv_accounts": _rows(db, "SELECT name, account FROM csv_account ORDER BY name", drop=()),
        "csv_mappings": _rows(db, "SELECT name, spec_json FROM csv_mapping ORDER BY name, id", drop=()),
        "deleted_journal": _rows(db, "SELECT * FROM journal_tx WHERE status='deleted' AND (external_id IS NOT NULL "
                                     "OR event_key IS NOT NULL) ORDER BY id", drop=("id", "form_json", "batch_id",
                                                                                    "datasource_id")),
        "diag_dismissed": _rows(db, "SELECT finding_id, kind, title, fingerprint, note, created_at FROM diag_decision "
                                    "WHERE action='dismiss' AND status='active' ORDER BY id", drop=()),
        # Ticker-/Token-Änderungen: Umbenennungen (Overlay), Umstellungen (Verweis auf exportierte Buchungen),
        # ausgeblendete Hinweise
        "asset_changes": _rows(db, "SELECT * FROM asset_change ORDER BY id"),
        # Referenzbestände (Prüfwerte der Diagnose, keine Buchungen) – auch entfernte, für die Nachvollziehbarkeit
        "reference_balances": _rows(db, "SELECT * FROM reference_balance ORDER BY id"),
        "watchlists": _rows(db, "SELECT name, position, is_default, created_at FROM watchlist ORDER BY id", drop=()),
        "watchlist_items": _rows(db, "SELECT w.name AS list_name, i.quote_source, i.quote_id, i.asset_class, "
                                     "i.asset_id, i.symbol, i.name, i.position, i.added_at FROM watchlist_item i "
                                     "JOIN watchlist w ON w.id = i.list_id ORDER BY i.list_id, i.position", drop=()),
    }
    out = {
        STATE: json.dumps(state, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8"),
        USAGE: json.dumps({"api_usage": _rows(db, "SELECT * FROM api_usage ORDER BY provider, period", drop=())},
                          sort_keys=True).encode("utf-8"),
        PRICES: _csv(PRICE_COLS, db.q(f"SELECT {', '.join(PRICE_COLS)} FROM price_daily ORDER BY series, date")),
        META: _csv(META_COLS, db.q(f"SELECT {', '.join(META_COLS)} FROM series_meta WHERE history_from IS NOT NULL "
                                   "ORDER BY series")),
    }
    tax = _taxdata_export(db, out)
    if tax:
        out[TAXDATA] = json.dumps(tax, ensure_ascii=False, sort_keys=True).encode("utf-8")
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


def _taxdata_export(db: Any, out: dict[str, bytes]) -> list[dict[str, Any]]:
    """Steuerdateien (alle Status – Verlauf bleibt nachvollziehbar) mit Datensätzen; Originaldatei, sofern lesbar."""
    try:
        files = db.q("SELECT * FROM tax_file ORDER BY id")
    except Exception:  # Tabelle fehlt (DB vor Migration 16)
        return []
    by_id = {int(f["id"]): f["sha256"] for f in files}
    res = []
    for f in files:
        item = {k: f[k] for k in TAX_FILE_COLS}
        item["replaced_by_sha"] = by_id.get(int(f["replaced_by"])) if f["replaced_by"] else None
        item["records_data"] = [{k: r[k] for k in TAX_RECORD_COLS}
                                for r in db.q("SELECT * FROM tax_record WHERE file_id=? ORDER BY line, id", (f["id"],))]
        p = Path(f["path"]) if f["path"] else None
        if p is not None and p.is_file() and not p.is_symlink() and p.stat().st_size <= MAX_FILE_BYTES:
            name = f"{f['sha256'][:12]}_{Path(f['filename']).name}"[:120]
            out[f"{FILES}tax/{name}"] = p.read_bytes()
            item["file"] = name
        res.append(item)
    return res


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
        "links": len(st.get("tx_links") or []),
        "csv": len(st.get("csv_symbols") or []) + len(st.get("csv_accounts") or []) + len(st.get("csv_mappings")
                                                                                             or []),
        "deleted": len(st.get("deleted_journal") or []),
        "checked": len(st.get("diag_dismissed") or []),
        "asset_changes": len(st.get("asset_changes") or []),
        "references": len(st.get("reference_balances") or []),
        "watchlist": len(st.get("watchlist_items") or []),
        "taxdata": len(json.loads(extras[TAXDATA].decode("utf-8"))) if extras.get(TAXDATA) else 0,
        "prices": max(0, prices.count(b"\n") - 1),
        "files": sorted(n[len(FILES):] for n in extras if n.startswith(FILES)),
    }


def _links(c: Any, rows: list[dict[str, Any]]) -> int:
    """Verknüpfte Quelldatensätze übernehmen – je Buchung, Quelle, Kennung und Zeitpunkt höchstens einmal."""
    n = 0
    for r in rows:
        if not r.get("tx_id") or not r.get("source") or not r.get("record_json"):
            continue
        if c.execute("SELECT 1 FROM tx_link WHERE tx_id=? AND source=? AND IFNULL(ext_id, '')=? AND created_at=? AND "
                     "status='active'", (r["tx_id"], r["source"], r.get("ext_id") or "", r.get("created_at") or "")
                     ).fetchone():
            continue
        c.execute("INSERT INTO tx_link(tx_id, source, ext_id, event_key, role, record_json, assessment_json, "
                  "created_at) VALUES (?,?,?,?,?,?,?,?)",
                  (r["tx_id"], r["source"], r.get("ext_id"), r.get("event_key"), r.get("role"), r["record_json"],
                   r.get("assessment_json"), r.get("created_at") or ""))
        n += 1
    return n


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
    out = {"import_id": iid, "summary": info, "state": done.get("state", "pending"), "at": done.get("at"),
           "counts": done.get("counts")}
    j = pending(ctx.db)
    if j is not None:  # unterbrochene Wiederherstellung (auch eines früheren Imports) hat Vorrang in der Anzeige
        out.update(state="incomplete", files_open=len(j.get("files") or []), error=j.get("error"),
                   attempts=j.get("attempts") or 0, restore_id=j.get("id"))
    return out


def dismiss(ctx: Any, import_id: int) -> None:
    ctx.db.set_state(f"restore.{import_id}", {"state": "dismissed", "at": iso(datetime.now(UTC))})


class RestoreError(RuntimeError):
    """Übernahme abgelehnt, bevor etwas geändert wurde (ungültige/widersprüchliche Daten, Speicherplatz, Rechte)."""


class RestoreIncomplete(RuntimeError):
    """Datenbankteil übernommen, Dateien noch nicht vollständig – wird fortgesetzt (Start, „Fortsetzen“)."""


JOURNAL_KEY = "restore.journal"  # laufende Wiederherstellung: Dateien, die nach dem DB-Commit noch zu ersetzen sind
_RESTORE_LOCK = threading.Lock()
_LIST_KEYS = ("asset_sources", "plans", "plan_dismissed", "datasources", "event_decisions", "event_aliases",
              "tx_links", "csv_symbols", "csv_accounts", "csv_mappings", "deleted_journal", "diag_dismissed",
              "asset_changes", "watchlists", "watchlist_items", "reference_balances")
_TMP_MARK = ".restore-"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate(extras: dict[str, bytes], st: dict[str, Any]) -> list[str]:
    """Zusatzdaten vor jeder Änderung prüfen: Struktur, Lesbarkeit, Widersprüche (z. B. zwei aktive Steuerdateien
    für dasselbe Jahr). Leere Liste = übernehmbar."""
    errs: list[str] = []
    if not isinstance(st.get("settings") or {}, dict):
        errs.append("Einstellungen: unerwartetes Format")
    for key in _LIST_KEYS:
        v = st.get(key)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, dict) for x in v)):
            errs.append(f"{key}: unerwartetes Format")
    raw = extras.get(TAXDATA)
    if raw:
        try:
            files = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            files = None
        if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
            errs.append("Steuerdaten: nicht lesbar")
        else:
            active: dict[Any, int] = defaultdict(int)
            for f in files:
                if f.get("status") == "active":
                    active[f.get("tax_year")] += 1
                if not f.get("sha256") or not f.get("filename"):
                    errs.append("Steuerdaten: Datei ohne Prüfsumme oder Namen")
                    break
            errs += [f"Steuerdaten: {n} aktive Dateien für {y} (widersprüchlich)" for y, n in active.items() if n > 1]
    for name in (PRICES, META):
        if extras.get(name):
            try:
                head = extras[name].decode("utf-8").split("\n", 1)[0].strip().split(",")
            except UnicodeDecodeError:
                errs.append(f"{name}: nicht lesbar")
                continue
            need = ("series", "date", "close") if name == PRICES else ("series", "history_from")
            if not set(need) <= set(head):
                errs.append(f"{name}: Spalten fehlen")
    for name, data in extras.items():
        if name.startswith(FILES) and len(data) > MAX_FILE_BYTES:
            errs.append(f"{name}: Datei zu groß")
    return errs


def _file_targets(ctx: Any, extras: dict[str, bytes]) -> list[tuple[str, Path, bytes]]:
    """(Name im Export, Zieldatei, Inhalt) – nur Dateien, deren Inhalt sich ändert; unsichere Pfade nie."""
    cfg = ctx.config
    out: list[tuple[str, Path, bytes]] = []
    for name, data in sorted(extras.items()):
        if not name.startswith(FILES):
            continue
        rel = Path(name[len(FILES):])
        if rel.is_absolute() or ".." in rel.parts or any(p.startswith(".") for p in rel.parts):
            continue
        if rel.as_posix() == "sources.yaml":
            target = cfg.sources_path
        elif rel.parts and rel.parts[0] == "tax_rules" and len(rel.parts) > 1:
            target = cfg.tax_rules_dir.joinpath(*rel.parts[1:])
        elif rel.parts and rel.parts[0] == "tax" and len(rel.parts) == 2:
            target = cfg.tax_data_dir / "uploads" / rel.parts[1]
        else:
            continue
        if target.is_file() and target.read_bytes() == data:
            continue
        out.append((name, target, data))
    return out


def _write_synced(path: Path, data: bytes) -> None:
    """Datei schreiben und auf den Datenträger bringen (übersteht einen Absturz direkt danach)."""
    with open(path, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _stage(targets: list[tuple[str, Path, bytes]], rid: str) -> list[dict[str, str]]:
    """Inhalte als temporäre Dateien neben dem Ziel ablegen. Fehler (Rechte, Speicherplatz) → alles Vorbereitete
    entfernen, nichts übernommen."""
    staged: list[dict[str, str]] = []
    need: dict[Path, int] = defaultdict(int)
    try:
        for _name, target, data in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            need[target.parent] += len(data)
        for d, n in need.items():
            free = shutil.disk_usage(d).free
            if free < n + 1024 * 1024:
                raise RestoreError(f"Nicht genug Speicherplatz in {d} ({free // 1024} KB frei, {n // 1024} KB "
                                   "benötigt) – nichts übernommen.")
        for name, target, data in targets:
            tmp = target.with_name(f".{target.name}{_TMP_MARK}{rid}")
            staged.append({"name": name, "target": str(target), "tmp": str(tmp),
                           "bak": str(target.with_name(f"{target.name}.bak-{rid}")), "sha": _sha(data)})
            _write_synced(tmp, data)
    except BaseException:
        for f in staged:
            Path(f["tmp"]).unlink(missing_ok=True)
        raise
    return staged


def apply(ctx: Any, import_id: int) -> dict[str, int]:
    """Zusatzdaten eines importierten Portfolia-Exports übernehmen (idempotent, löscht nichts).

    Ablauf (SQLite und Dateisystem bilden keine gemeinsame Transaktion):

    1. prüfen (:func:`validate`) – Fehler → nichts geändert;
    2. Dateien temporär neben dem Ziel vorbereiten (Speicherplatz, Rechte) – Fehler → nichts geändert;
    3. alle Datenbankänderungen **und** das Wiederherstellungs-Journal (welche Dateien noch zu ersetzen sind) in
       *einer* Transaktion – Fehler/Abbruch → Rollback, vorbereitete Dateien werden entfernt;
    4. Dateien ersetzen (bisherige als ``.bak-<id>``), Inhalt per Prüfsumme bestätigen, Journal schließen.

    Bricht Schritt 4 ab (Fehler, Prozess-/Container-Neustart), steht der Vorgang als „unvollständig“ im Journal und
    wird deterministisch fortgesetzt (:func:`resume`: beim Start, beim nächsten Übernehmen oder per „Fortsetzen“) –
    fehlende temporäre Dateien werden aus den in der Datenbank gespeicherten Zusatzdaten neu erzeugt. Erst danach
    gilt die Übernahme als abgeschlossen.
    """
    with _RESTORE_LOCK:
        _resume_locked(ctx)
        db = ctx.db
        extras = extras_for(db, import_id)
        st = _state(extras)
        if st is None:
            raise RestoreError("Keine gültigen Portfolia-Zusatzdaten in diesem Import.")
        errs = validate(extras, st)
        if errs:
            raise RestoreError("Zusatzdaten unvollständig oder widersprüchlich – nichts übernommen: "
                               + "; ".join(errs[:5]))
        now = iso(datetime.now(UTC)) or ""
        rid = datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        staged = _stage(_file_targets(ctx, extras), rid)
        journal = {"id": rid, "import_id": import_id, "at": now, "files": staged, "attempts": 0}
        try:
            counts = _apply_db(ctx, import_id, st, extras, now, journal)
        except BaseException:
            for f in staged:
                Path(f["tmp"]).unlink(missing_ok=True)
            ctx.invalidate_data()
            raise
        _reload(ctx)
        try:
            counts["files"] = _finish(ctx, journal, counts) if staged else 0
        except RestoreIncomplete:
            _after_apply(ctx)  # Datenbankteil gilt – Folgejobs trotzdem anstoßen
            raise
    _after_apply(ctx)
    log.info("Portfolia-Export übernommen (Import %s, Wiederherstellung %s): %s", import_id, rid, counts)
    return counts


def _reload(ctx: Any) -> None:
    ctx.settings.reload()
    ctx.invalidate_data()


def _after_apply(ctx: Any) -> None:
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


def _apply_db(ctx: Any, import_id: int, st: dict[str, Any], extras: dict[str, bytes], now: str,
              journal: dict[str, Any] | None = None) -> dict[str, int]:
    """Alle Datenbank-Änderungen der Übernahme in *einer* Transaktion (ganz oder gar nicht) – samt Journal der noch
    zu ersetzenden Dateien und Status (``incomplete`` bis die Dateien ersetzt sind)."""
    db = ctx.db
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
        counts["links"] = _links(c, st.get("tx_links") or [])
        counts["csv"] = (_insert(c, "csv_symbol", [{**r, "updated_at": now} for r in st.get("csv_symbols") or []],
                                 "OR IGNORE")
                         + _insert(c, "csv_account", [{**r, "updated_at": now} for r in st.get("csv_accounts") or []],
                                   "OR IGNORE")
                         + _mappings(c, st.get("csv_mappings") or [], now))
        counts["deleted"] = _insert(c, "journal_tx", [{**r, "status": "deleted"} for r in
                                                      st.get("deleted_journal") or []], "OR IGNORE")
        counts["checked"] = _dismissed(c, st.get("diag_dismissed") or [])
        counts["asset_changes"] = _insert(c, "asset_change", st.get("asset_changes"), "OR IGNORE")
        counts["references"] = _insert(c, "reference_balance", st.get("reference_balances"), "OR IGNORE")
        counts["usage"] = _usage(c, extras.get(USAGE))
        counts["prices"] = _prices(c, extras.get(PRICES))
        counts["series_meta"] = _meta(c, extras.get(META))
        counts["watchlist"] = _watchlists(c, st.get("watchlists") or [], st.get("watchlist_items") or [], now)
        counts["taxdata"] = _taxdata(c, extras.get(TAXDATA), ctx.config.tax_data_dir / "uploads", now)
        c.execute("INSERT INTO journal_log(at, action, ref, before_json, after_json) VALUES (?,?,?,?,?)",
                  (now, "restore_apply", f"import {import_id}", None, json.dumps(counts)))
        pending = bool(journal and journal.get("files"))
        if pending:
            _put_state(c, JOURNAL_KEY, {**(journal or {}), "counts": counts})
        _put_state(c, f"restore.{import_id}", {"state": "incomplete" if pending else "applied", "at": now,
                                               "counts": counts, "restore_id": (journal or {}).get("id")})
    return counts


def _put_state(c: Any, key: str, value: Any) -> None:
    c.execute("INSERT INTO app_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, json.dumps(value)))


def _finish(ctx: Any, journal: dict[str, Any], counts: dict[str, int] | None = None) -> int:
    """Dateien des Journals ersetzen – idempotent: bereits ersetzte (gleiche Prüfsumme) werden übersprungen, fehlende
    temporäre Dateien aus den gespeicherten Zusatzdaten neu erzeugt. Fehler → Journal bleibt offen
    (:class:`RestoreIncomplete`), nichts wird zurückgedreht."""
    db = ctx.db
    extras: dict[str, bytes] | None = None
    done, errors = 0, []
    for f in journal.get("files") or []:
        target, tmp, bak = Path(f["target"]), Path(f["tmp"]), Path(f["bak"])
        try:
            if target.is_file() and _sha(target.read_bytes()) == f["sha"]:
                tmp.unlink(missing_ok=True)
                continue
            if not (tmp.is_file() and _sha(tmp.read_bytes()) == f["sha"]):
                if extras is None:
                    extras = extras_for(db, int(journal["import_id"]))
                data = extras.get(f["name"])
                if data is None or _sha(data) != f["sha"]:
                    errors.append(f"{target.name}: Inhalt in den gespeicherten Zusatzdaten nicht mehr vorhanden")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_synced(tmp, data)
            if target.exists() and not bak.exists():
                target.replace(bak)  # bisherige Fassung sichern (nur einmal je Wiederherstellung)
            tmp.replace(target)
            done += 1
        except OSError as e:
            errors.append(f"{target.name}: {e.strerror or type(e).__name__}")
    iid = int(journal["import_id"])
    now = iso(datetime.now(UTC)) or ""
    counts = {**(journal.get("counts") or {}), **(counts or {})}
    if errors:
        journal = {**journal, "attempts": int(journal.get("attempts") or 0) + 1, "error": "; ".join(errors[:5]),
                   "failed_at": now}
        db.set_state(JOURNAL_KEY, journal)
        log.error("Wiederherstellung %s unvollständig (Dateien): %s", journal.get("id"), journal["error"])
        raise RestoreIncomplete("Wiederherstellung unvollständig: Datenbank übernommen, Dateien noch nicht ersetzt ("
                                + "; ".join(errors[:3]) + "). Wird beim nächsten Start bzw. mit „Fortsetzen“ "
                                "fortgesetzt.")
    counts["files"] = int(counts.get("files") or 0) + done
    with db.transaction() as c:
        c.execute("DELETE FROM app_state WHERE key=?", (JOURNAL_KEY,))
        _put_state(c, f"restore.{iid}", {"state": "applied", "at": now, "counts": counts,
                                         "restore_id": journal.get("id")})
        c.execute("INSERT INTO journal_log(at, action, ref, before_json, after_json) VALUES (?,?,?,?,?)",
                  (now, "restore_files", f"import {iid}", None, json.dumps({"files": done, "id": journal.get("id")})))
    return done


def pending(db: Any) -> dict[str, Any] | None:
    """Offene (unterbrochene) Wiederherstellung oder None."""
    j = db.get_state(JOURNAL_KEY)
    return j if isinstance(j, dict) and j.get("files") else None


def resume(ctx: Any) -> int | None:
    """Unterbrochene Wiederherstellung fortsetzen (Anzahl ersetzter Dateien) bzw. None, wenn keine offen ist."""
    with _RESTORE_LOCK:
        return _resume_locked(ctx)


def _resume_locked(ctx: Any) -> int | None:
    j = pending(ctx.db)
    if j is None:
        return None
    n = _finish(ctx, j)
    log.info("Unterbrochene Wiederherstellung %s abgeschlossen (%d Dateien)", j.get("id"), n)
    return n


def recover(ctx: Any) -> None:
    """Beim Start: unterbrochene Wiederherstellung fortsetzen und verwaiste temporäre Dateien entfernen (nur
    ``.<name>.restore-<id>`` in den Zielordnern, die kein offenes Journal mehr braucht). Fehler blockieren den Start
    nie – der Vorgang bleibt sichtbar „unvollständig“."""
    try:
        resume(ctx)
    except Exception as e:
        log.error("Wiederherstellung konnte nicht fortgesetzt werden: %s", e)
    keep = {f["tmp"] for f in (pending(ctx.db) or {}).get("files") or []}
    cfg = ctx.config
    dirs = {cfg.sources_path.parent, cfg.tax_data_dir / "uploads"}
    if cfg.tax_rules_dir.is_dir():
        dirs |= {p for p in cfg.tax_rules_dir.rglob("*") if p.is_dir()} | {cfg.tax_rules_dir}
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.glob(f".*{_TMP_MARK}*"):
            if p.is_file() and str(p) not in keep:
                with contextlib.suppress(OSError):
                    p.unlink()
                    log.info("Verwaiste Datei einer abgebrochenen Wiederherstellung entfernt: %s", p.name)


def _watchlists(c: Any, lists: list[dict[str, Any]], items: list[dict[str, Any]], now: str) -> int:
    """Watchlists ergänzen (gleichnamige Liste wird verwendet, gleiche Einträge nicht doppelt)."""
    ids: dict[str, int] = {}
    for w in lists:
        if not isinstance(w, dict) or not w.get("name"):
            continue
        row = c.execute("SELECT id FROM watchlist WHERE name=? ORDER BY id LIMIT 1", (w["name"],)).fetchone()
        if row is None:
            has_default = c.execute("SELECT 1 FROM watchlist WHERE is_default=1").fetchone() is not None
            cur = c.execute("INSERT INTO watchlist(name, position, is_default, created_at) VALUES (?,?,?,?)",
                            (w["name"], int(w.get("position") or 0), int(bool(w.get("is_default")) and not has_default),
                             w.get("created_at") or now))
            ids[w["name"]] = int(cur.lastrowid)
        else:
            ids[w["name"]] = int(row["id"])
    n = 0
    for it in items:
        lid = ids.get(it.get("list_name") or "")
        if lid is None or not it.get("quote_source") or not it.get("quote_id"):
            continue
        cur = c.execute("INSERT OR IGNORE INTO watchlist_item(list_id, quote_source, quote_id, asset_class, asset_id, "
                        "symbol, name, position, added_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (lid, it["quote_source"], it["quote_id"], it.get("asset_class") or "crypto", it.get("asset_id"),
                         it.get("symbol"), it.get("name"), int(it.get("position") or 0), it.get("added_at") or now))
        n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return n


def _taxdata(c: Any, raw: bytes | None, uploads: Path, now: str) -> int:
    """Steuerdateien ergänzen – gleicher Inhalt (SHA-256) nie doppelt. Ist für ein Jahr schon eine andere Datei aktiv,
    wird die übernommene nicht still aktiviert, sondern wartet auf Bestätigung (Status „pending“)."""
    if not raw:
        return 0
    try:
        files = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return 0
    sha_to_id: dict[str, int] = {}
    later: list[tuple[int, str]] = []
    n = 0
    for f in files if isinstance(files, list) else []:
        if not isinstance(f, dict) or not f.get("sha256") or not f.get("filename"):
            continue
        if c.execute("SELECT 1 FROM tax_file WHERE sha256=?", (f["sha256"],)).fetchone():
            continue
        status = f.get("status") or "pending"
        note = f.get("note")
        if status == "active" and c.execute("SELECT 1 FROM tax_file WHERE tax_year IS ? AND status='active'",
                                            (f.get("tax_year"),)).fetchone():
            status, note = "pending", "aus Gesamtexport – für das Jahr ist bereits eine Datei aktiv"
        path = str(uploads / f["file"]) if f.get("file") else None
        vals = {k: f.get(k) for k in TAX_FILE_COLS}
        vals.update(status=status, note=note, path=path, records=int(f.get("records") or 0),
                    size=int(f.get("size") or 0), created_at=f.get("created_at") or now,
                    format=f.get("format") or "?", parser=f.get("parser") or "?", origin=f.get("origin") or "upload")
        cols = list(vals)
        cur = c.execute(f"INSERT INTO tax_file({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [vals[k] for k in cols])
        fid = int(cur.lastrowid)
        sha_to_id[f["sha256"]] = fid
        if f.get("replaced_by_sha"):
            later.append((fid, f["replaced_by_sha"]))
        recs = [r for r in f.get("records_data") or [] if isinstance(r, dict)]
        c.executemany(f"INSERT INTO tax_record(file_id, {', '.join(TAX_RECORD_COLS)}) VALUES "
                      f"(?, {', '.join('?' * len(TAX_RECORD_COLS))})",
                      [(fid, *[r.get(k) for k in TAX_RECORD_COLS]) for r in recs])
        n += 1
    for fid, sha in later:
        row = c.execute("SELECT id FROM tax_file WHERE sha256=?", (sha,)).fetchone()
        ref = sha_to_id.get(sha) or (row[0] if row else None)
        if ref:
            c.execute("UPDATE tax_file SET replaced_by=? WHERE id=?", (ref, fid))
    return n

def _dismissed(c: Any, rows: list[Any]) -> int:
    """„Geprüft“-Markierungen der Diagnose ergänzen (gleicher Befund mit gleichen Daten nur einmal)."""
    n = 0
    for r in rows:
        if not isinstance(r, dict) or not r.get("finding_id") or not r.get("fingerprint"):
            continue
        if c.execute("SELECT 1 FROM diag_decision WHERE finding_id=? AND fingerprint=? AND action='dismiss' AND "
                     "status='active'", (r["finding_id"], r["fingerprint"])).fetchone():
            continue
        c.execute("INSERT INTO diag_decision(finding_id, kind, title, action, fingerprint, note, status, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?)", (r["finding_id"], r.get("kind") or "", r.get("title") or "", "dismiss",
                                              r["fingerprint"], r.get("note"), "active", r.get("created_at") or ""))
        n += 1
    return n


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
        cur = c.execute("INSERT OR IGNORE INTO series_meta(series, history_from, history_to, history_status, "
                        "alt_series, alt_status, alt_note, alt_checked_at) VALUES (?,?,?,?,?,?,?,?)",
                        (r["series"], r["history_from"], r.get("history_to") or None, r.get("history_status") or None,
                         r.get("alt_series") or None, r.get("alt_status") or None, r.get("alt_note") or None,
                         r.get("alt_checked_at") or None))
        n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return n
