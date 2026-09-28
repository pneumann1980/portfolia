"""Datenquellen verwalten und synchronisieren.

Status (``data_source.status``)
    ``created`` angelegt · ``connected`` verbunden (Prüfung erfolgreich, noch nicht synchronisiert) ·
    ``synced`` synchronisiert · ``partial`` teilweise synchronisiert (Anbieter lieferte nicht alle Daten, z. B.
    Abruflimit) · ``error`` Fehler (letzter Lauf fehlgeschlagen). Unvollständige Zeilen (unbekanntes Asset, fehlender
    Wert) sind Sache der Prüfung, nicht des Abrufs. „Deaktiviert“ ist davon unabhängig
    (``enabled``) und stoppt nur den Zeitplan. Ohne Connector bleibt eine Quelle „angelegt“ und wird als
    „manuell/noch nicht unterstützt“ angezeigt – Buchungen kommen dann per CSV-Import.

Synchronisieren
    Connector → normalisierte Vorgänge (Ereignis-ID, Zeilen) → :meth:`CsvImportService.ingest` (Stapel „sync“,
    Quelle ``sync:<anbieter>``, Kennung ``<ereignis>#<zeile>``) → Auswertung wie beim CSV-Import. Bereits
    übernommene Kennungen sind „bekannt“ (idempotent), gleiche Ereignisse aus anderen Quellen „mögliche Dublette“.
    Ohne neue Zeilen wird der Stapel verworfen. Mit ``auto_commit`` wird ein Lauf nur dann ohne Durchsicht
    übernommen, wenn er ausschließlich neue (bzw. bekannte) Zeilen enthält – eine Überschneidung (Dublette, vor dem
    Stichtag) oder eine ungültige Zeile schickt den ganzen Lauf zur Prüfung.

Offener Prüf-Stapel
    Solange ein Lauf zur Prüfung offen ist (Stapel „Vorschau“), ruft die Quelle nichts Neues ab – weder nach Zeitplan
    noch manuell. Der Abrufstand (``cursor``) rückt nach jedem erfolgreichen Abruf vor; wer einen Stapel verwirft,
    bekommt dessen Vorgänge erst nach „Abrufstand zurücksetzen“ erneut (bereits übernommene werden dann erkannt).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.datasources import connector as K
from app.datasources.providers import EXCHANGE, INTERVALS, KIND_LABEL, PROVIDERS, WALLET, normalize_address
from app.logging_setup import get_redactor
from app.util.timeutil import iso, local_tz, parse_iso

log = logging.getLogger(__name__)

STATUS_LABEL = {"created": "angelegt", "connected": "verbunden", "synced": "synchronisiert",
                "partial": "teilweise synchronisiert", "error": "Fehler"}
STATUS_BADGE = {"created": "", "connected": "info", "synced": "good", "partial": "warn", "error": "crit"}
RUN_STATUS_LABEL = {"running": "läuft", "ok": "erfolgreich", "partial": "teilweise", "error": "Fehler"}
MAX_EVENTS = 50_000
_SYNC_LOCK = threading.Lock()  # ein Lauf zur Zeit (Zeitplan und „Jetzt synchronisieren“ nicht parallel)
_NAME_RE = re.compile(r"^[^\x00-\x1f<>]{1,60}$")
_SECRETISH = re.compile(r"(?i)\b(authorization|api[-_ ]?key|apikey|secret|signature|passphrase|token|bearer)"
                        r"(\s*[:=]\s*|\s+)([^\s,;]+)")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s]*")


def _now() -> datetime:
    return datetime.now(UTC)


def sanitize_error(msg: str, secrets: Iterable[str] = ()) -> str:
    """Anzeigetext ohne Geheimnisse: bekannte Werte, Schlüssel=Wert-Paare und URL-Parameter werden maskiert."""
    text = str(msg or "")
    for s in sorted({x for x in secrets if x and len(x) >= 4}, key=len, reverse=True):
        text = text.replace(s, "***")
    text = get_redactor().redact(text)
    text = _SECRETISH.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    text = _URL_QUERY.sub(lambda m: f"{m.group(1)}?…", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300] + ("…" if len(text) > 300 else "")


def describe_error(e: BaseException, secrets: Iterable[str] = ()) -> tuple[str, str]:
    """(Art, bereinigte Meldung) für die Anzeige."""
    if isinstance(e, K.ConnectorError):
        return e.kind, sanitize_error(f"{K.ERROR_KINDS[e.kind]}: {e.message}", secrets)
    if isinstance(e, httpx.TimeoutException):
        return "unavailable", "Anbieter nicht erreichbar: Zeitüberschreitung – der nächste Lauf versucht es erneut."
    if isinstance(e, httpx.ConnectError | httpx.NetworkError):
        return "unavailable", "Anbieter nicht erreichbar (Netzwerk/DNS) – der nächste Lauf versucht es erneut."
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code in (401, 403):
            return "auth", f"Zugangsdaten abgelehnt (HTTP {code}) – Schlüssel und Leserechte prüfen."
        if code == 429:
            return "rate_limit", "Anbieter drosselt Anfragen (HTTP 429) – der nächste Lauf versucht es erneut."
        return "unavailable", f"Anbieter antwortete mit HTTP {code}."
    return "data", sanitize_error(f"Unerwarteter Fehler ({type(e).__name__}): {e}", secrets)


@dataclass
class DataSource:
    """Datensatz mit berechneten Anzeigefeldern."""

    row: Mapping[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.row[name]
        except (KeyError, IndexError) as e:
            raise AttributeError(name) from e

    @property
    def provider_obj(self) -> Any:
        return PROVIDERS.get(self.row["provider"])

    @property
    def provider_label(self) -> str:
        p = self.provider_obj
        return p.label if p else self.row["provider"]

    @property
    def kind_label(self) -> str:
        return KIND_LABEL.get(self.row["kind"], self.row["kind"])

    @property
    def supported(self) -> bool:
        return K.supported(self.row["provider"])

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.row["status"], self.row["status"])

    @property
    def status_badge(self) -> str:
        return STATUS_BADGE.get(self.row["status"], "")

    @property
    def interval_label(self) -> str:
        m = int(self.row["sync_interval_min"] or 0)
        return INTERVALS.get(m, f"alle {m} Minuten")

    @property
    def credential_state(self) -> str | None:
        ref = self.row["credential_ref"]
        if not ref:
            return None
        return "gesetzt" if K.Secret(ref).present else "fehlt"

    def config(self) -> K.SourceConfig:
        r = self.row
        return K.SourceConfig(r["id"], r["kind"], r["provider"], r["name"], r["account"], r["address"])


def next_run(enabled: bool, supported: bool, interval_min: int, last_run: datetime | None,
             now: datetime | None = None) -> datetime | None:
    """Nächster geplanter Lauf – nur aktiv, mit Connector und Intervall; nie in der Vergangenheit."""
    if not enabled or not supported or interval_min <= 0:
        return None
    now = now or _now()
    if last_run is None:
        return now
    return max(now, last_run + timedelta(minutes=interval_min))


class DataSourceService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.db = ctx.db

    # -- Lesen ------------------------------------------------------------------------------------------
    def list(self) -> list[DataSource]:
        return [DataSource(r) for r in self.db.q("SELECT * FROM data_source ORDER BY kind, lower(name), id")]

    def get(self, sid: int) -> DataSource | None:
        r = self.db.q1("SELECT * FROM data_source WHERE id=?", (sid,))
        return DataSource(r) if r else None

    def runs(self, sid: int, limit: int = 10) -> list[Any]:
        return self.db.q("SELECT * FROM data_source_run WHERE source_id=? ORDER BY id DESC LIMIT ?", (sid, limit))

    def pending_batch(self, sid: int) -> Any:
        """Offener Prüf-Stapel der Quelle (Vorschau, noch nichts übernommen)."""
        return self.db.q1("SELECT id, status, created_at FROM csv_batch WHERE kind='sync' AND datasource_id=? AND "
                          "status='preview' ORDER BY id DESC LIMIT 1", (sid,))

    def _waiting(self) -> set[int]:
        return {int(r["datasource_id"]) for r in self.db.q(
            "SELECT DISTINCT datasource_id FROM csv_batch WHERE kind='sync' AND status='preview' AND "
            "datasource_id IS NOT NULL")}

    def accounts(self) -> list[str]:
        pf = self.ctx.recorded_portfolio()
        return pf.all_accounts() if pf is not None else []

    # -- Anlegen / Bearbeiten -----------------------------------------------------------------------------
    def validate(self, data: Mapping[str, Any], current: DataSource | None = None) -> tuple[dict[str, Any], list[str]]:
        errors: list[str] = []
        kind = current.row["kind"] if current else str(data.get("kind") or "")
        if kind not in (EXCHANGE, WALLET):
            errors.append("Art wählen: Börse oder Wallet-Adresse.")
        pid = str(data.get("provider") or "")
        prov = PROVIDERS.get(pid)
        if prov is None or prov.kind != kind:
            errors.append("Anbieter bzw. Chain wählen.")
        name = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()
        if not _NAME_RE.match(name):
            errors.append("Name fehlt oder ist zu lang (höchstens 60 Zeichen).")
        account = re.sub(r"\s+", " ", str(data.get("account") or "")).strip() or name
        if not _NAME_RE.match(account):
            errors.append("Konto in Portfolia ungültig (höchstens 60 Zeichen).")
        address = None
        if kind == WALLET and prov is not None:
            address, err = normalize_address(prov, str(data.get("address") or ""))
            if err:
                errors.append(err)
        credential_ref = None
        if kind == EXCHANGE:
            raw = str(data.get("credential_ref") or "").strip().upper()
            if raw:
                if not K.CREDENTIAL_RE.match(raw):
                    errors.append("Zugangsdaten: Name einer Umgebungsvariable mit Präfix PORTFOLIA_DS_ angeben "
                                  "(z. B. PORTFOLIA_DS_KRAKEN) – nie den Schlüssel selbst.")
                else:
                    credential_ref = raw
        try:
            interval = int(str(data.get("sync_interval_min") or "0"))
        except ValueError:
            interval = -1
        if interval not in INTERVALS:
            errors.append("Synchronisierungsintervall ungültig.")
        note = str(data.get("note") or "").strip()[:300] or None
        if address and not errors:
            dup = self.db.q1("SELECT id, name FROM data_source WHERE kind=? AND provider=? AND address=? AND id<>?",
                             (kind, pid, address, current.row["id"] if current else 0))
            if dup is not None:
                errors.append(f"Diese Adresse ist bereits als „{dup['name']}“ angelegt.")
        vals = {"kind": kind, "provider": pid, "name": name, "account": account, "address": address,
                "credential_ref": credential_ref, "sync_interval_min": max(interval, 0),
                "auto_commit": 1 if str(data.get("auto_commit") or "") in ("1", "on", "true") else 0, "note": note}
        return vals, errors

    def create(self, data: Mapping[str, Any]) -> tuple[int | None, list[str]]:
        vals, errors = self.validate(data)
        if errors:
            return None, errors
        stamp = iso(_now())
        nxt = next_run(True, K.supported(vals["provider"]), vals["sync_interval_min"], None)
        cur = self.db.x(
            "INSERT INTO data_source(kind, provider, name, account, address, credential_ref, enabled, status, "
            "sync_interval_min, auto_commit, next_run_at, note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,1,'created',?,?,?,?,?,?)",
            (vals["kind"], vals["provider"], vals["name"], vals["account"], vals["address"], vals["credential_ref"],
             vals["sync_interval_min"], vals["auto_commit"], iso(nxt) if nxt else None, vals["note"], stamp, stamp))
        sid = int(cur.lastrowid)  # type: ignore[arg-type]
        log.info("Datenquelle angelegt: %s (%s, %s)", vals["name"], vals["kind"], vals["provider"])
        return sid, []

    def update(self, sid: int, data: Mapping[str, Any]) -> list[str]:
        ds = self.get(sid)
        if ds is None:
            return ["Datenquelle nicht gefunden."]
        merged = {"provider": ds.provider, "address": ds.address or "", **dict(data)}
        vals, errors = self.validate(merged, current=ds)
        if errors:
            return errors
        changed_target = (vals["provider"], vals["address"], vals["account"]) != (ds.provider, ds.address, ds.account)
        nxt = next_run(bool(ds.enabled), K.supported(vals["provider"]), vals["sync_interval_min"],
                       parse_iso(ds.last_run_at))
        self.db.x(
            "UPDATE data_source SET provider=?, name=?, account=?, address=?, credential_ref=?, sync_interval_min=?, "
            "auto_commit=?, note=?, next_run_at=?, updated_at=?"
            + (", status='created', cursor_json=NULL, last_error=NULL" if changed_target else "") + " WHERE id=?",
            (vals["provider"], vals["name"], vals["account"], vals["address"], vals["credential_ref"],
             vals["sync_interval_min"], vals["auto_commit"], vals["note"], iso(nxt) if nxt else None,
             iso(_now()), sid))
        return []

    def set_enabled(self, sid: int, enabled: bool) -> bool:
        ds = self.get(sid)
        if ds is None:
            return False
        nxt = next_run(enabled, ds.supported, int(ds.sync_interval_min or 0), parse_iso(ds.last_run_at))
        self.db.x("UPDATE data_source SET enabled=?, next_run_at=?, updated_at=? WHERE id=?",
                  (1 if enabled else 0, iso(nxt) if nxt else None, iso(_now()), sid))
        return True

    def delete(self, sid: int) -> bool:
        """Konfiguration und Laufhistorie entfernen. Übernommene Buchungen bleiben (Quelle, Ereignis-ID) erhalten –
        eine neu angelegte Quelle erkennt sie wieder; offene Prüf-Stapel ohne Übernahmen werden verworfen."""
        ds = self.get(sid)
        if ds is None:
            return False
        with self.db.transaction() as c:
            c.execute("DELETE FROM csv_batch WHERE kind='sync' AND datasource_id=? AND status='preview' AND NOT EXISTS "
                      "(SELECT 1 FROM journal_tx j WHERE j.batch_id=csv_batch.id AND j.status <> 'reverted')", (sid,))
            c.execute("DELETE FROM data_source WHERE id=?", (sid,))
        log.info("Datenquelle entfernt: %s", ds.name)
        return True

    def reset_cursor(self, sid: int) -> bool:
        """Abrufstand verwerfen – der nächste Lauf holt alle Vorgänge erneut (übernommene werden erkannt)."""
        cur = self.db.x("UPDATE data_source SET cursor_json=NULL, updated_at=? WHERE id=?", (iso(_now()), sid))
        return bool(cur.rowcount)

    # -- Prüfen / Synchronisieren -----------------------------------------------------------------------
    def _start_run(self, sid: int, trigger: str) -> int:
        cur = self.db.x("INSERT INTO data_source_run(source_id, trigger, started_at, status) VALUES (?,?,?, 'running')",
                        (sid, trigger, iso(_now())))
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def _finish_run(self, run_id: int, status: str, message: str, **counts: int | None) -> None:
        cols = {k: v for k, v in counts.items() if v is not None}
        sets = "".join(f", {k}=?" for k in cols)
        self.db.x(f"UPDATE data_source_run SET status=?, message=?, finished_at=?{sets} WHERE id=?",
                  (status, message[:500], iso(_now()), *cols.values(), run_id))

    def check(self, sid: int) -> tuple[bool, str]:
        """Verbindung prüfen (nur mit Connector) – Erfolg setzt „verbunden“, sofern noch nicht synchronisiert."""
        ds = self.get(sid)
        if ds is None:
            return False, "Datenquelle nicht gefunden."
        conn = K.connector_for(ds.provider)
        if conn is None:
            return False, "Für diesen Anbieter gibt es noch keine automatische Anbindung – Buchungen per CSV-Import."
        secret = K.Secret(ds.credential_ref)
        run_id = self._start_run(sid, "check")
        stamp = iso(_now())
        try:
            if conn.needs_credentials and not secret.present:
                raise K.ConnectorError("config", f"Umgebungsvariable {ds.credential_ref or '(nicht angegeben)'} fehlt.")
            res = conn.check(ds.config(), secret)
        except Exception as e:  # Anbieter-/Netzwerkfehler → Anzeige ohne Geheimnisse
            _, msg = describe_error(e, secret.values())
            self.db.x("UPDATE data_source SET status='error', last_error=?, last_error_at=?, last_run_at=?, "
                      "updated_at=? WHERE id=?", (msg, stamp, stamp, stamp, sid))
            self._finish_run(run_id, "error", msg)
            log.warning("Datenquelle %s: Prüfung fehlgeschlagen: %s", ds.name, msg)
            return False, msg
        msg = sanitize_error(res.message or ("Verbindung in Ordnung." if res.ok else "Verbindung fehlgeschlagen."),
                             secret.values())
        if res.ok:
            self.db.x("UPDATE data_source SET status=CASE WHEN status IN ('created', 'error') THEN 'connected' "
                      "ELSE status END, last_error=NULL, updated_at=? WHERE id=?", (stamp, sid))
        else:
            self.db.x("UPDATE data_source SET status='error', last_error=?, last_error_at=?, updated_at=? WHERE id=?",
                      (msg, stamp, stamp, sid))
        self._finish_run(run_id, "ok" if res.ok else "error", msg)
        return res.ok, msg

    def sync(self, sid: int, trigger: str = "manual") -> dict[str, Any]:
        """Vorgänge abrufen und als Prüf-Stapel aufnehmen (bzw. einen Abruf ohne Überschneidung übernehmen)."""
        if not _SYNC_LOCK.acquire(blocking=False):
            return {"error": "Eine Synchronisierung läuft bereits – bitte kurz warten."}
        try:
            return self._sync(sid, trigger)
        finally:
            _SYNC_LOCK.release()

    def _fail(self, ds: DataSource, run_id: int, e: BaseException, secret: K.Secret, started: datetime,
              nxt: datetime | None) -> dict[str, Any]:
        """Lauf als Fehler abschließen – Meldung ohne Geheimnisse, Wartezeit des Anbieters beachten."""
        kind, msg = describe_error(e, secret.values())
        if isinstance(e, K.ConnectorError) and e.retry_after_s and nxt is not None:
            nxt = max(nxt, started + timedelta(seconds=e.retry_after_s))
        stamp = iso(started)
        self.db.x("UPDATE data_source SET status='error', last_error=?, last_error_at=?, last_run_at=?, "
                  "next_run_at=?, updated_at=? WHERE id=?", (msg, stamp, stamp, iso(nxt) if nxt else None, stamp,
                                                            ds.id))
        self._finish_run(run_id, "error", msg)
        log.warning("Datenquelle %s: Synchronisierung fehlgeschlagen (%s): %s", ds.name, kind, msg)
        return {"error": msg}

    def _sync(self, sid: int, trigger: str) -> dict[str, Any]:
        ds = self.get(sid)
        if ds is None:
            return {"error": "Datenquelle nicht gefunden."}
        if not ds.enabled and trigger == "schedule":
            return {"skipped": "deaktiviert"}
        conn = K.connector_for(ds.provider)
        if conn is None:
            msg = "Manuell/noch nicht unterstützt – Buchungen per CSV-Import erfassen."
            self.db.x("UPDATE data_source SET next_run_at=NULL, updated_at=? WHERE id=?", (iso(_now()), sid))
            return {"unsupported": msg}
        pending = self.pending_batch(sid)
        if pending is not None:
            return {"pending": int(pending["id"]),
                    "error": "Ein früherer Abruf wartet auf Prüfung – zuerst übernehmen oder verwerfen."}
        secret = K.Secret(ds.credential_ref)
        run_id = self._start_run(sid, trigger)
        started = _now()
        stamp = iso(started)
        nxt = next_run(bool(ds.enabled), True, int(ds.sync_interval_min or 0), started, started)
        nxt_s = iso(nxt) if nxt else None
        try:
            if conn.needs_credentials and not secret.present:
                raise K.ConnectorError("config", f"Umgebungsvariable {ds.credential_ref or '(nicht angegeben)'} fehlt.")
            cursor = json.loads(ds.cursor_json) if ds.cursor_json else None
            res = conn.fetch(ds.config(), secret, cursor)
            recs, payload = self._normalize(ds, res)
        except Exception as e:  # Anbieter-/Netzwerk-/Vertragsfehler → Anzeige ohne Geheimnisse
            return self._fail(ds, run_id, e, secret, started, nxt)
        from app.csvimport.service import csv_service

        csv = csv_service(self.ctx)
        counts: dict[str, int] = {}
        committed = 0
        bid = None
        try:
            if recs:
                label = f"{ds.name} · Synchronisierung {started.astimezone(local_tz()).strftime('%d.%m.%Y %H:%M')}"
                bid = csv.ingest(recs, source=f"sync:{ds.provider}", profile=f"sync:{ds.provider}",
                                 account=ds.account, label=label, datasource_id=sid, payload=payload)
                counts = {r["status"]: r["n"] for r in self.db.q(
                    "SELECT status, COUNT(*) AS n FROM csv_row WHERE batch_id=? GROUP BY status", (bid,))}
                clean = not any(counts.get(k) for k in ("duplicate", "before", "invalid"))
                if ds.auto_commit and counts.get("new") and clean:
                    committed = int(csv.commit(bid).get("created", 0) or 0)
                open_rows = self.db.scalar("SELECT COUNT(*) FROM csv_row WHERE batch_id=? AND status NOT IN "
                                           "('known', 'committed', 'merged')", (bid,), default=0)
                if not open_rows and not committed:
                    csv.discard(bid)  # nichts Neues – kein leerer Prüf-Stapel
                    bid = None
        except Exception as e:  # Fehler der Import-Pipeline: Lauf nicht als „läuft“ stehen lassen
            log.exception("Datenquelle %s: Verarbeitung fehlgeschlagen", ds.name)
            return self._fail(ds, run_id, e, secret, started, nxt)
        partial = not res.complete  # unvollständige Zeilen sind Sache der Prüfung, nicht des Abrufs
        status = "partial" if partial else "synced"
        notes = [sanitize_error(w, secret.values()) for w in res.warnings[:5]]
        if not res.complete:
            notes.insert(0, "Anbieter lieferte nicht alle Daten")
        overlap = counts.get("duplicate", 0) + counts.get("before", 0)
        msg = (f"{len(res.events)} Vorgänge · neu {counts.get('new', 0)} · bekannt {counts.get('known', 0)} · "
               f"Überschneidung {overlap}" + (f" · unvollständig {counts['invalid']}" if counts.get("invalid") else "")
               + (f" · übernommen {committed}" if committed else "") + ("; " + "; ".join(notes) if notes else ""))
        self.db.x("UPDATE data_source SET status=?, last_run_at=?, last_success_at=?, last_error=?, last_error_at=?, "
                  "next_run_at=?, cursor_json=?, updated_at=? WHERE id=?",
                  (status, stamp, stamp, "; ".join(notes) if partial else None, stamp if partial else None, nxt_s,
                   json.dumps(res.cursor) if res.cursor is not None else ds.cursor_json, stamp, sid))
        self._finish_run(run_id, "partial" if partial else "ok", msg, events=len(res.events),
                         rows_new=counts.get("new", 0), rows_known=counts.get("known", 0), rows_overlap=overlap,
                         rows_committed=committed, batch_id=bid)
        log.info("Datenquelle %s synchronisiert: %s", ds.name, msg)
        return {"status": status, "batch_id": bid, "message": msg, "committed": committed, **counts}

    def _normalize(self, ds: DataSource, res: K.FetchResult) -> tuple[list[Any], bytes]:
        """Ereignisse → Zeilen im Zwischenformat mit Kennung ``<ereignis>#<zeile>``; Vertrag prüfen."""
        from app.csvimport.service import rec_to_json

        if len(res.events) > MAX_EVENTS:
            raise K.ConnectorError("data", f"Zu viele Vorgänge in einem Lauf ({len(res.events)} > {MAX_EVENTS}).")
        recs = []
        seen: set[str] = set()
        prefix = f"{ds.provider}:"
        for ev in res.events:
            key = (ev.event_key or "").strip()
            if not K.EVENT_KEY_RE.match(key) or not key.startswith(prefix):
                raise K.ConnectorError("data", f"Ungültige Ereignis-ID (erwartet „{prefix}…“).")
            if key in seen:
                continue  # doppelt geliefert (überlappende Abrufseiten)
            seen.add(key)
            for i, rec in enumerate(ev.lines):
                rec.event_key, rec.event_line = key, i
                rec.ext_id = f"{key}#{i}"
                rec.account = rec.account or ds.account
                rec.label = rec.label or ev.label
                rec.line = len(recs) + 1
                recs.append(rec)
        payload = ("[" + ",".join(rec_to_json(r) for r in recs) + "]").encode()
        return recs, payload

    def due(self, now: datetime | None = None) -> list[DataSource]:
        """Fällige Quellen: aktiv, mit Connector und Intervall, ohne offenen Prüf-Stapel."""
        now = now or _now()
        waiting = self._waiting()
        out = []
        for ds in self.list():
            if not ds.enabled or not ds.supported or int(ds.sync_interval_min or 0) <= 0 or ds.id in waiting:
                continue
            nxt = parse_iso(ds.next_run_at)
            if nxt is not None and nxt <= now:
                out.append(ds)
        return out

    def run_due(self) -> dict[str, Any]:
        done = {}
        for ds in self.due():
            res = self.sync(int(ds.id), "schedule")
            done[ds.name] = res.get("status") or res.get("error") or res.get("skipped") or res.get("unsupported")
        return {"ran": len(done), "results": done}


def datasource_service(ctx: Any) -> DataSourceService:
    return DataSourceService(ctx)
