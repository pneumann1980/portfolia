"""Datenquellen verwalten und synchronisieren.

Status (``data_source.status``)
    ``created`` angelegt · ``connected`` verbunden (Prüfung erfolgreich, noch nicht synchronisiert) ·
    ``synced`` synchronisiert · ``partial`` teilweise synchronisiert (Abruf unvollständig: Seitenende oder Abdeckung
    unklar, Drosselung, Teilfehler – eine erfolgreiche HTTP-Antwort allein genügt nicht) · ``error`` Fehler.
    Unvollständige oder ungeklärte Zeilen sind Sache der Prüfung, nicht des Abrufs. „Deaktiviert“ (``enabled``)
    stoppt nur den Zeitplan. Ohne Connector bleibt eine Quelle „angelegt“ und wird als „manuell/noch nicht
    unterstützt“ angezeigt – Buchungen kommen dann per CSV-Import.

Zugangsdaten
    Bevorzugt verschlüsselt in der App (:mod:`app.datasources.vault`, Master-Key außerhalb der Datenbank), sonst als
    Umgebungsvariable ``PORTFOLIA_DS_<NAME>`` bzw. ``…_FILE``. Nach dem Speichern sieht der Browser nur die letzten
    vier Zeichen; der Schlüssel verlässt den Server nur als Header an die dokumentierte API des Anbieters.

Synchronisieren
    Connector → normalisierte Vorgänge (Ereignis-ID, Zeilen, Aliase) → :meth:`CsvImportService.ingest` (Stapel
    „sync“, Quelle ``sync:<anbieter>``, Kennung ``<ereignis>#<zeile>``) → Auswertung wie beim CSV-Import. „Bekannt“
    sind bereits übernommene Kennungen und dasselbe Ereignis aus anderen Quellen (gleiche Anbieter-ID). Vorgänge,
    die schon in einem offenen Prüf-Stapel warten, werden nicht noch einmal aufgenommen; neue Vorgänge werden an
    einen unberührten Prüf-Stapel angehängt. Mit ``auto_commit`` übernimmt ein Lauf je *Ereignis* nur vollständig
    neue, eindeutig zugeordnete Ereignisse ohne Prüfhinweis – alle anderen bleiben zur Prüfung, ohne die sicheren
    zu blockieren.

Abrufstand
    Der Connector rückt den Abrufstand nur nach vollständigem Abruf vor (sonst ``None`` → der nächste Lauf holt
    erneut ab); gespeichert wird er erst, nachdem die Vorgänge im Prüf-Stapel stehen – ein Abbruch, API- oder
    Datenbankfehler verliert nichts. Wird ein Prüf-Stapel verworfen, setzt :meth:`DataSourceService.rewind` den
    Abrufstand vor den ältesten offenen Vorgang zurück. „Dauerhaft ignorieren“ ist je Anbieter-Ereignis gespeichert
    (``event_decision``) und gilt für alle künftigen Läufe.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.datasources import connector as K
from app.datasources.catalog import Catalog
from app.datasources.providers import EXCHANGE, INTERVALS, KIND_LABEL, PROVIDERS, WALLET, normalize_address
from app.datasources.vault import Vault, VaultError
from app.logging_setup import get_redactor
from app.util.timeutil import iso, local_tz, parse_iso

log = logging.getLogger(__name__)

STATUS_LABEL = {"created": "angelegt", "connected": "verbunden", "synced": "synchronisiert",
                "partial": "teilweise synchronisiert", "error": "Fehler"}
STATUS_BADGE = {"created": "", "connected": "info", "synced": "good", "partial": "warn", "error": "crit"}
RUN_STATUS_LABEL = {"running": "läuft", "ok": "erfolgreich", "partial": "teilweise", "error": "Fehler"}
MAX_EVENTS = 50_000
KEY_WARN_DAYS = 14
DONE = ("known", "ignored", "committed", "merged")
_SYNC_LOCK = threading.Lock()  # ein Lauf zur Zeit (Zeitplan und „Jetzt synchronisieren“ nicht parallel)
_NAME_RE = re.compile(r"^[^\x00-\x1f<>]{1,60}$")
_KEY_RE = re.compile(r"^[\x21-\x7e]{16,1024}$")  # druckbare ASCII-Zeichen ohne Leerzeichen
_SECRETISH = re.compile(r"(?i)\b(authorization|x-api-key|api[-_ ]?key|apikey|secret|signature|passphrase|token|"
                        r"bearer)(\s*[:=]\s*|\s+)([^\s,;]+)")
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
    if isinstance(e, VaultError):
        return "config", sanitize_error(f"{K.ERROR_KINDS['config']}: {e}", secrets)
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
    """Datensatz mit berechneten Anzeigefeldern (Schlüssel-Metadaten ohne Schlüssel)."""

    row: Mapping[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.row[name]
        except (KeyError, IndexError) as e:
            raise AttributeError(name) from e

    def _get(self, name: str) -> Any:
        try:
            return self.row[name]
        except (KeyError, IndexError):
            return None

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
    def key_mode(self) -> str | None:
        """``app`` (verschlüsselt gespeichert), ``env`` (Umgebungsvariable) oder None."""
        if self._get("key_hint") is not None or self._get("key_key_id") is not None:
            return "app"
        return "env" if self.row["credential_ref"] else None

    @property
    def key_hint(self) -> str | None:
        h = self._get("key_hint")
        return f"••••{h}" if h else None

    @property
    def credential_state(self) -> str | None:
        ref = self.row["credential_ref"]
        if not ref:
            return None
        return "gesetzt" if K.Secret(ref).present else "fehlt"

    @property
    def key_expiry(self) -> date | None:
        v = self._get("key_expires_on")
        try:
            return date.fromisoformat(v) if v else None
        except ValueError:
            return None

    @property
    def key_expiry_state(self) -> str | None:
        d = self.key_expiry
        if d is None:
            return None
        today = datetime.now(local_tz()).date()
        if d < today:
            return "expired"
        return "soon" if (d - today).days <= KEY_WARN_DAYS else "ok"

    @property
    def check(self) -> dict[str, Any]:
        try:
            return json.loads(self._get("last_check_json") or "{}")
        except ValueError:
            return {}

    @property
    def coverage(self) -> dict[str, Any]:
        try:
            return json.loads(self._get("coverage_json") or "{}")
        except ValueError:
            return {}

    @property
    def needs_key(self) -> bool:
        c = K.connector_for(self.row["provider"])
        return bool(c is not None and c.needs_credentials)

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
    _SELECT = ("SELECT d.*, s.hint AS key_hint, s.key_id AS key_key_id, s.updated_at AS key_updated_at "
               "FROM data_source d LEFT JOIN data_source_secret s ON s.source_id = d.id")

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.db = ctx.db

    # -- Lesen ------------------------------------------------------------------------------------------
    def list(self) -> list[DataSource]:
        return [DataSource(r) for r in self.db.q(f"{self._SELECT} ORDER BY d.kind, lower(d.name), d.id")]

    def get(self, sid: int) -> DataSource | None:
        r = self.db.q1(f"{self._SELECT} WHERE d.id=?", (sid,))
        return DataSource(r) if r else None

    def runs(self, sid: int, limit: int = 10) -> list[Any]:
        return self.db.q("SELECT * FROM data_source_run WHERE source_id=? ORDER BY id DESC LIMIT ?", (sid, limit))

    def pending_batches(self, sid: int) -> list[Any]:
        """Offene Prüf-Stapel der Quelle (Vorschau oder teilweise übernommen mit offenen Punkten)."""
        return self.db.q("SELECT id, status, created_at FROM csv_batch WHERE kind='sync' AND datasource_id=? AND "
                         "status IN ('preview', 'partial') ORDER BY id DESC", (sid,))

    def pending_batch(self, sid: int) -> Any:
        rows = self.pending_batches(sid)
        return rows[0] if rows else None

    def open_counts(self, sid: int) -> dict[str, int]:
        """Offene Zeilen je Status über alle Prüf-Stapel der Quelle (neu, ungeklärt, mögliche Dublette …)."""
        return {r["status"]: r["n"] for r in self.db.q(
            "SELECT r.status, COUNT(*) AS n FROM csv_row r JOIN csv_batch b ON b.id = r.batch_id WHERE b.kind='sync' "
            "AND b.datasource_id=? AND b.status IN ('preview', 'partial') AND r.status NOT IN "
            "('known', 'ignored', 'committed', 'merged') GROUP BY r.status", (sid,))}

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
        expires = str(data.get("key_expires_on") or "").strip() or None
        if expires:
            try:
                expires = date.fromisoformat(expires).isoformat()
            except ValueError:
                errors.append("Ablaufdatum des API-Keys ungültig (TT.MM.JJJJ bzw. Datumsauswahl).")
                expires = None
        note = str(data.get("note") or "").strip()[:300] or None
        if address and not errors:
            dup = self.db.q1("SELECT id, name FROM data_source WHERE kind=? AND provider=? AND address=? AND id<>?",
                             (kind, pid, address, current.row["id"] if current else 0))
            if dup is not None:
                errors.append(f"Diese Adresse ist bereits als „{dup['name']}“ angelegt.")
        vals = {"kind": kind, "provider": pid, "name": name, "account": account, "address": address,
                "credential_ref": credential_ref, "sync_interval_min": max(interval, 0),
                "auto_commit": 1 if str(data.get("auto_commit") or "") in ("1", "on", "true") else 0, "note": note,
                "key_expires_on": expires}
        return vals, errors

    def create(self, data: Mapping[str, Any]) -> tuple[int | None, list[str]]:
        vals, errors = self.validate(data)
        api_key = str(data.get("api_key") or "").strip()
        if api_key:
            conn = K.connector_for(vals["provider"])
            if conn is None or not conn.needs_credentials:
                errors.append("Für diesen Anbieter gibt es (noch) keine Anbindung mit API-Key – Feld bitte leer "
                              "lassen.")
            else:
                errors += self._key_errors(api_key)
        if errors:
            return None, errors
        stamp = iso(_now())
        nxt = next_run(True, K.supported(vals["provider"]), vals["sync_interval_min"], None)
        cur = self.db.x(
            "INSERT INTO data_source(kind, provider, name, account, address, credential_ref, enabled, status, "
            "sync_interval_min, auto_commit, next_run_at, note, key_expires_on, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,1,'created',?,?,?,?,?,?,?)",
            (vals["kind"], vals["provider"], vals["name"], vals["account"], vals["address"], vals["credential_ref"],
             vals["sync_interval_min"], vals["auto_commit"], iso(nxt) if nxt else None, vals["note"],
             vals["key_expires_on"], stamp, stamp))
        sid = int(cur.lastrowid)  # type: ignore[arg-type]
        if api_key:
            errs = self.set_api_key(sid, api_key)
            if errs:  # nicht erwartet (vorab geprüft) – Quelle bleibt ohne Schlüssel angelegt
                return sid, errs
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
            "auto_commit=?, note=?, key_expires_on=?, next_run_at=?, updated_at=?"
            + (", status='created', cursor_json=NULL, last_error=NULL" if changed_target else "") + " WHERE id=?",
            (vals["provider"], vals["name"], vals["account"], vals["address"], vals["credential_ref"],
             vals["sync_interval_min"], vals["auto_commit"], vals["note"], vals["key_expires_on"],
             iso(nxt) if nxt else None, iso(_now()), sid))
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
        """Konfiguration, Zugangsdaten (sicher gelöscht) und Laufhistorie entfernen. Übernommene Buchungen bleiben
        mit Quelle und Ereignis-ID erhalten – eine neu angelegte Quelle erkennt sie wieder; offene Vorschau-Stapel
        ohne Übernahmen werden verworfen."""
        ds = self.get(sid)
        if ds is None:
            return False
        self.db.x("PRAGMA secure_delete=ON")  # gelöschte Seiten werden überschrieben (kein Schlüsselrest)
        with self.db.transaction() as c:
            c.execute("DELETE FROM data_source_secret WHERE source_id=?", (sid,))
            c.execute("DELETE FROM csv_batch WHERE kind='sync' AND datasource_id=? AND status='preview' AND NOT EXISTS "
                      "(SELECT 1 FROM journal_tx j WHERE j.batch_id=csv_batch.id AND j.status <> 'reverted')", (sid,))
            c.execute("DELETE FROM data_source WHERE id=?", (sid,))
        self._purge_wal()
        log.info("Datenquelle entfernt: %s", ds.name)
        return True

    def reset_cursor(self, sid: int) -> bool:
        """Abrufstand verwerfen – der nächste Lauf holt alle Vorgänge erneut (übernommene werden erkannt)."""
        cur = self.db.x("UPDATE data_source SET cursor_json=NULL, updated_at=? WHERE id=?", (iso(_now()), sid))
        return bool(cur.rowcount)

    def rewind(self, sid: int, before: datetime) -> None:
        """Abrufstand so zurücksetzen, dass Vorgänge ab ``before`` erneut geliefert werden (verworfener Stapel)."""
        ds = self.get(sid)
        if ds is None:
            return
        conn = K.connector_for(ds.provider)
        cursor = json.loads(ds.cursor_json) if ds.cursor_json else None
        new = conn.rewind(cursor, before) if conn is not None else None
        self.db.x("UPDATE data_source SET cursor_json=?, updated_at=? WHERE id=?",
                  (json.dumps(new) if new is not None else None, iso(_now()), sid))
        log.info("Datenquelle %s: Abrufstand zurückgesetzt (verworfene Vorgänge ab %s)", ds.name, iso(before))

    def _purge_wal(self) -> None:
        try:  # alte Seitenstände aus dem WAL entfernen (nach sicherem Löschen); ohne Leser sofort wirksam
            self.db.x("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:  # pragma: no cover - z. B. gleichzeitige Leser
            log.debug("WAL-Checkpoint nicht möglich: %s", e)

    # -- Zugangsdaten -------------------------------------------------------------------------------------
    @staticmethod
    def vault() -> Vault:
        return Vault.load()

    def _key_errors(self, value: str) -> list[str]:
        if not _KEY_RE.match(value):
            return ["Das sieht nicht nach einem API-Key aus (16–1024 Zeichen, keine Leerzeichen)."]
        if K.CREDENTIAL_RE.match(value.upper()):
            return ["Das ist der Name einer Umgebungsvariable – bitte im Feld „Umgebungsvariable“ eintragen oder den "
                    "Schlüssel selbst einfügen."]
        v = self.vault()
        if not v.available:
            return [v.error or "Master-Key fehlt – Schlüssel werden nur verschlüsselt gespeichert. Einrichtung: "
                               "PORTFOLIA_MASTER_KEY_FILE (siehe Anleitung)."]
        return []

    def set_api_key(self, sid: int, value: str, expires_on: str | None = None) -> list[str]:
        """API-Key verschlüsselt speichern bzw. ersetzen. Übernommene Buchungen und Abrufstand bleiben erhalten;
        die Verbindung ist danach neu zu prüfen."""
        ds = self.get(sid)
        if ds is None:
            return ["Datenquelle nicht gefunden."]
        v = (value or "").strip()
        if not v:
            return ["API-Key fehlt."]
        errors = self._key_errors(v)
        exp = None
        if expires_on:
            try:
                exp = date.fromisoformat(expires_on.strip()).isoformat()
            except ValueError:
                errors.append("Ablaufdatum des API-Keys ungültig.")
        if errors:
            return errors
        vault = self.vault()
        try:
            blob, kid = vault.encrypt(v, sid)
        except VaultError as e:
            return [str(e)]
        stamp = iso(_now())
        self.db.x("PRAGMA secure_delete=ON")
        with self.db.transaction() as c:
            c.execute("INSERT INTO data_source_secret(source_id, kind, ciphertext, key_id, hint, created_at, "
                      "updated_at) VALUES (?, 'api_key', ?, ?, ?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET "
                      "ciphertext=excluded.ciphertext, key_id=excluded.key_id, hint=excluded.hint, "
                      "updated_at=excluded.updated_at", (sid, blob, kid, v[-4:], stamp, stamp))
            c.execute("UPDATE data_source SET status='created', last_check_json=NULL, last_error=NULL, "
                      "key_expires_on=COALESCE(?, key_expires_on), updated_at=? WHERE id=?", (exp, stamp, sid))
        self._purge_wal()
        log.info("Datenquelle %s: API-Key verschlüsselt gespeichert (Master-Key %s)", ds.name, kid)
        return []

    def remove_api_key(self, sid: int) -> bool:
        """Gespeicherten Schlüssel sicher löschen – Buchungen bleiben; Abrufe brauchen einen neuen Schlüssel."""
        ds = self.get(sid)
        if ds is None:
            return False
        self.db.x("PRAGMA secure_delete=ON")
        with self.db.transaction() as c:
            c.execute("DELETE FROM data_source_secret WHERE source_id=?", (sid,))
            c.execute("UPDATE data_source SET status='created', last_check_json=NULL, updated_at=? WHERE id=?",
                      (iso(_now()), sid))
        self._purge_wal()
        log.info("Datenquelle %s: API-Key entfernt", ds.name)
        return True

    def rotate_keys(self) -> dict[str, Any]:
        """Alle gespeicherten Schlüssel mit dem aktuellen Master-Key neu verschlüsseln (Rotation)."""
        vault = self.vault()
        if not vault.available:
            return {"rotated": 0, "errors": [vault.error or "Master-Key fehlt."]}
        done, errors = 0, []
        for r in self.db.q("SELECT source_id, ciphertext, key_id FROM data_source_secret"):
            if r["key_id"] == vault.key_id:
                continue
            try:
                plain = vault.decrypt(r["ciphertext"], r["source_id"])
                blob, kid = vault.encrypt(plain, r["source_id"])
            except VaultError as e:
                errors.append(f"Datenquelle {r['source_id']}: {e}")
                continue
            self.db.x("UPDATE data_source_secret SET ciphertext=?, key_id=?, updated_at=? WHERE source_id=?",
                      (blob, kid, iso(_now()), r["source_id"]))
            done += 1
        if done:
            self._purge_wal()
        log.info("Zugangsdaten neu verschlüsselt: %d", done)
        return {"rotated": done, "errors": errors}

    def key_stats(self) -> dict[str, Any]:
        vault = self.vault()
        rows = self.db.q("SELECT key_id, COUNT(*) AS n FROM data_source_secret GROUP BY key_id")
        return {"total": sum(r["n"] for r in rows),
                "stale": sum(r["n"] for r in rows if r["key_id"] != vault.key_id), "vault": vault.status()}

    def _secret(self, ds: DataSource, conn: K.Connector) -> K.Secret:
        if ds.key_expiry_state == "expired":
            raise K.ConnectorError("expired", f"laut Angabe am {ds.key_expiry.strftime('%d.%m.%Y')} abgelaufen – "  # type: ignore[union-attr]
                                              "neuen API-Key erstellen und unter „Zugang & Einstellungen“ ersetzen.")
        row = self.db.q1("SELECT ciphertext FROM data_source_secret WHERE source_id=?", (ds.id,))
        if row is not None:
            try:
                return K.Secret(value=self.vault().decrypt(row["ciphertext"], int(ds.id)))
            except VaultError as e:
                raise K.ConnectorError("config", str(e)) from None
        sec = K.Secret(ds.credential_ref)
        if conn.needs_credentials and not sec.present:
            raise K.ConnectorError("config", f"Umgebungsvariable {ds.credential_ref} fehlt." if ds.credential_ref
                                   else "Kein API-Key hinterlegt – unter „Zugang & Einstellungen“ eingeben.")
        return sec

    # -- Prüfen / Synchronisieren -----------------------------------------------------------------------
    def _start_run(self, sid: int, trigger: str) -> int:
        cur = self.db.x("INSERT INTO data_source_run(source_id, trigger, started_at, status) VALUES (?,?,?, 'running')",
                        (sid, trigger, iso(_now())))
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def _finish_run(self, run_id: int, status: str, message: str, **counts: Any) -> None:
        cols = {k: v for k, v in counts.items() if v is not None}
        sets = "".join(f", {k}=?" for k in cols)
        self.db.x(f"UPDATE data_source_run SET status=?, message=?, finished_at=?{sets} WHERE id=?",
                  (status, message[:500], iso(_now()), *cols.values(), run_id))

    def check(self, sid: int) -> tuple[bool, str]:
        """Verbindung und Leserechte prüfen (nur mit Connector) – Erfolg setzt „verbunden“, sofern noch nicht
        synchronisiert. Das Ergebnis je Recht wird angezeigt."""
        ds = self.get(sid)
        if ds is None:
            return False, "Datenquelle nicht gefunden."
        conn = K.connector_for(ds.provider)
        if conn is None:
            return False, "Für diesen Anbieter gibt es noch keine automatische Anbindung – Buchungen per CSV-Import."
        run_id = self._start_run(sid, "check")
        stamp = iso(_now())
        secret = K.Secret(None)
        conn.catalog = Catalog(self.db, ds.provider)
        try:
            secret = self._secret(ds, conn)
            with get_redactor().temporary(secret.values()):
                res = conn.check(ds.config(), secret)
        except Exception as e:  # Anbieter-/Netzwerkfehler → Anzeige ohne Geheimnisse
            _, msg = describe_error(e, secret.values())
            self.db.x("UPDATE data_source SET status='error', last_error=?, last_error_at=?, last_check_json=?, "
                      "updated_at=? WHERE id=?",
                      (msg, stamp, json.dumps({"ok": False, "message": msg, "at": stamp}), stamp, sid))
            self._finish_run(run_id, "error", msg)
            log.warning("Datenquelle %s: Prüfung fehlgeschlagen: %s", ds.name, msg)
            return False, msg
        msg = sanitize_error(res.message or ("Verbindung in Ordnung." if res.ok else "Verbindung fehlgeschlagen."),
                             secret.values())
        details = {k: {"ok": bool(v.get("ok")), "text": sanitize_error(str(v.get("text") or ""), secret.values())}
                   for k, v in (res.details or {}).items()}
        check = json.dumps({"ok": res.ok, "message": msg, "details": details, "at": stamp}, ensure_ascii=False)
        if res.ok:
            self.db.x("UPDATE data_source SET status=CASE WHEN status IN ('created', 'error') THEN 'connected' "
                      "ELSE status END, last_error=NULL, last_check_json=?, updated_at=? WHERE id=?",
                      (check, stamp, sid))
        else:
            self.db.x("UPDATE data_source SET status='error', last_error=?, last_error_at=?, last_check_json=?, "
                      "updated_at=? WHERE id=?", (msg, stamp, check, stamp, sid))
        self._finish_run(run_id, "ok" if res.ok else "error", msg)
        return res.ok, msg

    def sync(self, sid: int, trigger: str = "manual") -> dict[str, Any]:
        """Vorgänge abrufen und zur Prüfung aufnehmen (bzw. eindeutige neue Ereignisse automatisch übernehmen)."""
        if not _SYNC_LOCK.acquire(blocking=False):
            return {"error": "Eine Synchronisierung läuft bereits – bitte kurz warten."}
        try:
            return self._sync(sid, trigger)
        finally:
            _SYNC_LOCK.release()

    def _fail(self, ds: DataSource, run_id: int, e: BaseException, secrets: list[str], started: datetime,
              nxt: datetime | None) -> dict[str, Any]:
        """Lauf als Fehler abschließen – Meldung ohne Geheimnisse, Wartezeit des Anbieters beachten. Der
        Abrufstand bleibt unverändert: der nächste Lauf holt dieselben Vorgänge erneut."""
        kind, msg = describe_error(e, secrets)
        if isinstance(e, K.ConnectorError) and e.retry_after_s and nxt is not None:
            nxt = max(nxt, started + timedelta(seconds=e.retry_after_s))
        stamp = iso(started)
        self.db.x("UPDATE data_source SET status='error', last_error=?, last_error_at=?, last_run_at=?, "
                  "next_run_at=?, updated_at=? WHERE id=?", (msg, stamp, stamp, iso(nxt) if nxt else None, stamp,
                                                            ds.id))
        self._finish_run(run_id, "error", msg)
        log.warning("Datenquelle %s: Synchronisierung fehlgeschlagen (%s): %s", ds.name, kind, msg)
        return {"error": msg, "kind": kind}

    def _waiting(self, provider: str) -> set[str]:
        """Ereignisse, die bereits in einem offenen Prüf-Stapel des Anbieters auf eine Entscheidung warten."""
        return {r["event_key"] for r in self.db.q(
            "SELECT DISTINCT r.event_key FROM csv_row r JOIN csv_batch b ON b.id = r.batch_id WHERE b.kind='sync' "
            "AND b.source=? AND b.status IN ('preview', 'partial') AND r.event_key IS NOT NULL AND r.status NOT IN "
            "('known', 'ignored', 'committed', 'merged')", (f"sync:{provider}",))}

    def _appendable(self, sid: int) -> int | None:
        from app.csvimport.service import csv_service

        r = self.db.q1("SELECT id FROM csv_batch WHERE kind='sync' AND datasource_id=? AND status='preview' "
                       "ORDER BY id DESC LIMIT 1", (sid,))
        return int(r["id"]) if r is not None and csv_service(self.ctx).untouched(int(r["id"])) else None

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
        run_id = self._start_run(sid, trigger)
        started = _now()
        stamp = iso(started)
        nxt = next_run(bool(ds.enabled), True, int(ds.sync_interval_min or 0), started, started)
        secret = K.Secret(None)
        conn.catalog = Catalog(self.db, ds.provider)
        try:
            secret = self._secret(ds, conn)
            cursor = json.loads(ds.cursor_json) if ds.cursor_json else None
            with get_redactor().temporary(secret.values()):
                res = conn.fetch(ds.config(), secret, cursor)
            recs = self._normalize(ds, res)
        except Exception as e:  # Anbieter-/Netzwerk-/Vertragsfehler → Anzeige ohne Geheimnisse
            return self._fail(ds, run_id, e, secret.values(), started, nxt)
        from app.csvimport.service import csv_service, rec_to_json

        csv = csv_service(self.ctx)
        waiting = self._waiting(ds.provider)
        fresh = [r for r in recs if (r.event_key or "") not in waiting]
        n_waiting = len({r.event_key for r in recs if (r.event_key or "") in waiting})
        counts: dict[str, int] = defaultdict(int)
        committed = 0
        bid = None
        try:
            if fresh:
                label = f"{ds.name} · Synchronisierung {started.astimezone(local_tz()).strftime('%d.%m.%Y %H:%M')}"
                payload = ("[" + ",".join(rec_to_json(r) for r in fresh) + "]").encode()
                bid = csv.ingest(fresh, source=f"sync:{ds.provider}", profile=f"sync:{ds.provider}",
                                 account=ds.account, label=label, datasource_id=sid, payload=payload,
                                 append_to=self._appendable(sid), skipped=res.skipped)
                keys = {r.event_key for r in fresh}
                rows = [rc for rc in csv.rows(bid) if rc.rec.event_key in keys]
                for rc in rows:
                    counts[rc.status] += 1
                open_rows = self.db.scalar("SELECT COUNT(*) FROM csv_row WHERE batch_id=? AND status NOT IN "
                                           "('known', 'ignored', 'committed', 'merged')", (bid,), default=0)
                if not open_rows and not self.db.scalar("SELECT COUNT(*) FROM csv_row WHERE batch_id=? AND status "
                                                        "IN ('committed', 'merged')", (bid,), default=0):
                    csv.discard(bid)  # nichts Neues – kein leerer Prüf-Stapel
                    bid = None
            if ds.auto_commit:  # je Ereignis – auch in offenen Stapeln, sobald sie eindeutig geworden sind
                for b in self.pending_batches(sid):
                    eligible = self._auto_eligible(csv.rows(int(b["id"])))
                    if eligible:
                        out = csv.commit(int(b["id"]), only_idx=eligible)
                        committed += int(out.get("created", 0) or 0) + int(out.get("merged", 0) or 0)
        except Exception as e:  # Fehler der Import-Pipeline: Lauf nicht als „läuft“ stehen lassen
            log.exception("Datenquelle %s: Verarbeitung fehlgeschlagen", ds.name)
            return self._fail(ds, run_id, e, secret.values(), started, nxt)
        partial = not res.complete  # unvollständige/ungeklärte Zeilen sind Sache der Prüfung, nicht des Abrufs
        status = "partial" if partial else "synced"
        notes = [sanitize_error(w, secret.values()) for w in res.warnings[:5]]
        if not res.complete:
            notes.insert(0, "Abruf unvollständig – der nächste Lauf holt erneut ab")
        overlap = counts.get("duplicate", 0) + counts.get("before", 0)
        parts = [f"{len(res.events)} Vorgänge", f"neu {counts.get('new', 0)}", f"bekannt {counts.get('known', 0)}"]
        for key, label in (("unclear", "ungeklärt"), ("duplicate", "mögliche Dubletten"), ("before", "vor Stichtag"),
                           ("invalid", "unvollständig"), ("ignored", "ignoriert")):
            if counts.get(key):
                parts.append(f"{label} {counts[key]}")
        if n_waiting:
            parts.append(f"wartet bereits auf Prüfung {n_waiting}")
        if committed:
            parts.append(f"übernommen {committed}")
        if res.skipped:
            parts.append("ohne Buchung " + ", ".join(f"{n}× {k}" for k, n in sorted(res.skipped.items())))
        msg = " · ".join(parts) + ("; " + "; ".join(notes) if notes else "")
        coverage = {**res.coverage, "complete": res.complete, "at": stamp}
        self.db.x("UPDATE data_source SET status=?, last_run_at=?, last_success_at=?, last_error=?, last_error_at=?, "
                  "next_run_at=?, cursor_json=?, coverage_json=?, updated_at=? WHERE id=?",
                  (status, stamp, stamp, "; ".join(notes) if partial else None, stamp if partial else None,
                   iso(nxt) if nxt else None,
                   json.dumps(res.cursor) if res.cursor is not None else ds.cursor_json,
                   json.dumps(coverage, ensure_ascii=False, default=str), stamp, sid))
        self._finish_run(run_id, "partial" if partial else "ok", msg, events=len(res.events),
                         rows_new=counts.get("new", 0), rows_known=counts.get("known", 0), rows_overlap=overlap,
                         rows_committed=committed, rows_unclear=counts.get("unclear", 0),
                         rows_ignored=counts.get("ignored", 0), batch_id=bid,
                         detail_json=json.dumps({"skipped": res.skipped, "warnings": notes, "coverage": coverage,
                                                 "waiting": n_waiting}, ensure_ascii=False, default=str))
        log.info("Datenquelle %s synchronisiert: %s", ds.name, msg)
        return {"status": status, "batch_id": bid, "message": msg, "committed": committed, **counts}

    @staticmethod
    def _auto_eligible(rows: list[Any]) -> set[int]:
        """Zeilen vollständig eindeutiger, neuer Ereignisse (alle Zeilen neu, fehlerfrei, ohne Prüfhinweis)."""
        by_event: dict[str, list[Any]] = defaultdict(list)
        for rc in rows:
            by_event[rc.rec.event_key or f"#{rc.idx}"].append(rc)
        out: set[int] = set()
        for lines in by_event.values():
            if all(rc.status == "new" and not rc.errors and rc.row is not None and not rc.rec.review
                   and rc.include() for rc in lines):
                out |= {rc.idx for rc in lines}
        return out

    def _normalize(self, ds: DataSource, res: K.FetchResult) -> list[Any]:
        """Ereignisse → Zeilen im Zwischenformat mit Kennung ``<ereignis>#<zeile>``; Vertrag prüfen."""
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
                rec.aliases = sorted({a for a in rec.aliases if K.EVENT_KEY_RE.match(a) and a.startswith(prefix)
                                      and a != key})
                rec.account = rec.account or ds.account
                rec.label = rec.label or ev.label
                rec.line = len(recs) + 1
                recs.append(rec)
        return recs

    def due(self, now: datetime | None = None) -> list[DataSource]:
        """Fällige Quellen: aktiv, mit Connector und Intervall."""
        now = now or _now()
        out = []
        for ds in self.list():
            if not ds.enabled or not ds.supported or int(ds.sync_interval_min or 0) <= 0:
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
