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
    erneut ab) bzw. – bei langen Historien in Etappen – nur bis zu einem sicheren Fortsetzungspunkt (``resume``);
    gespeichert wird er erst, nachdem die Vorgänge im Prüf-Stapel stehen – ein Abbruch, API- oder
    Datenbankfehler verliert nichts. Wird ein Prüf-Stapel verworfen, setzt :meth:`DataSourceService.rewind` den
    Abrufstand vor den ältesten offenen Vorgang zurück. „Dauerhaft ignorieren“ ist je Anbieter-Ereignis gespeichert
    (``event_decision``) und gilt für alle künftigen Läufe.

Wallets
    Öffentliche Adressen bzw. Kontoschlüssel je Chain (:mod:`app.datasources.wallet`), gruppiert (z. B. „Ledger“).
    API-Keys gelten je Anbieter (Etherscan, Routescan, Helius) und liegen verschlüsselt in ``provider_secret``. Ein
    Abruf läuft im Hintergrund mit Fortschrittsanzeige; der historische Erstabruf erfolgt in Etappen mit sicherem
    Fortsetzungspunkt und setzt sich selbst fort. Beobachtete Bestände (``ds_balance``) werden den Buchungen des
    Kontos gegenübergestellt („beobachtet“ vs. „durch Portfolia-Buchungen erklärt“). „Synchronisiert“ heißt: die
    unterstützten Daten sind ohne erkannte Lücke abgerufen; Abdeckungsgrenzen werden immer angezeigt.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.datasources import connector as K
from app.datasources.catalog import Catalog
from app.datasources.chainhttp import ENDPOINTS
from app.datasources.providers import (
    EXCHANGE,
    INTERVALS,
    KIND_LABEL,
    PROVIDERS,
    WALLET,
    contains_secret,
    normalize_address,
)
from app.datasources.vault import Vault, VaultError
from app.datasources.wallet import GAP_DEFAULT, GAP_MAX, MAX_ADDRESSES, SCRIPT_TYPES, WatchConfig, new_watch_id, short
from app.logging_setup import get_redactor
from app.util.http import Quota
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
ACCOUNT_MIN_MATCHES = 3  # automatische Konto-Umstellung: mindestens so viele Treffer im kuratierten Import …
ACCOUNT_SHARE = 0.9  # … und dieser Anteil unter einem Konto
_KEY_RE = re.compile(r"^[\x21-\x7e]{16,1024}$")  # druckbare ASCII-Zeichen ohne Leerzeichen
_GROUP_RE = re.compile(r"^[^\x00-\x1f<>]{1,40}$")
PROGRESS_STALE_S = 600  # ohne Lebenszeichen gilt ein Lauf als abgebrochen (Neustart des Containers)
BACKFILL_NEXT_S = 90  # Etappen des Erstabrufs: nächster Lauf nach so vielen Sekunden
MAX_ROUNDS = 40  # Etappen je manuell gestartetem Hintergrundlauf
# Anbieter-Schlüssel (je Anbieter, nicht je Datenquelle) – nur Anbieter aus dem geprüften Katalog
PROVIDER_KEYS = {e.key_provider: e for e in ENDPOINTS.values() if e.key_provider}
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
        return K.SourceConfig(r["id"], r["kind"], r["provider"], r["name"], r["account"], r["address"],
                              watch=self.watch.as_dict() if self.is_wallet else {})

    # -- Wallets ------------------------------------------------------------------------------------------
    @property
    def is_wallet(self) -> bool:
        return self.row["kind"] == WALLET

    @property
    def watch(self) -> WatchConfig:
        try:
            return WatchConfig.load(self._get("watch_json"))
        except (ValueError, TypeError):
            return WatchConfig()

    @property
    def group(self) -> str:
        return (self._get("wallet_group") or "").strip()

    @property
    def addresses(self) -> list[str]:
        """Öffentliche Kennungen des Kontos (Kontoschlüssel zuerst, dann Adressen)."""
        w = self.watch
        out = [*w.xpubs, *w.addresses]
        return out or ([self.row["address"]] if self.row["address"] else [])

    @property
    def short_address(self) -> str:
        a = self.addresses
        if not a:
            return ""
        return short(a[0], 8) + (f" (+{len(a) - 1})" if len(a) > 1 else "")

    @property
    def connector(self) -> K.Connector | None:
        return K.connector_for(self.row["provider"])

    @property
    def endpoint(self) -> Any:
        c = self.connector
        if c is None or not getattr(c, "wallet", False):
            return None
        return c.endpoint(self.config())  # type: ignore[attr-defined]

    @property
    def endpoint_options(self) -> list[Any]:
        c = self.connector
        return [ENDPOINTS[e] for e in getattr(c, "endpoints", ())] if c is not None else []

    @property
    def limits(self) -> list[str]:
        cov = self.coverage
        if cov.get("limits"):
            return list(cov["limits"])
        c = self.connector
        if c is not None and getattr(c, "wallet", False):
            return c.coverage_limits(self.config())  # type: ignore[attr-defined]
        return []

    @property
    def gaps(self) -> list[str]:
        return list(self.coverage.get("gaps") or [])

    @property
    def progress(self) -> dict[str, Any]:
        """Fortschritt des laufenden bzw. letzten Abrufs; ein Lauf ohne Lebenszeichen gilt als abgebrochen."""
        try:
            p = json.loads(self._get("progress_json") or "{}")
        except ValueError:
            return {}
        if p.get("running"):
            seen = parse_iso(p.get("updated_at"))
            if seen is None or (_now() - seen).total_seconds() > PROGRESS_STALE_S:
                p["running"] = False
                p["stale"] = True
        return p

    @property
    def backfill_pending(self) -> bool:
        """Erstabruf in Etappen noch nicht abgeschlossen (bleibt auch nach einem Fehler bestehen)."""
        return bool(self.coverage.get("resume"))

    @property
    def sync_state(self) -> tuple[str, str]:
        """(Text, Badge) – „vollständig synchronisiert“ nur ohne erkannte Lücke."""
        if self.progress.get("running"):
            return "Abruf läuft", "info"
        st = self.row["status"]
        cov = self.coverage
        if st == "synced" and cov.get("complete") and not cov.get("gaps"):
            return ("vollständig synchronisiert" if self.is_wallet else "synchronisiert"), "good"
        if cov.get("resume"):
            return ("Erstabruf unvollständig – wird fortgesetzt" if st != "error" else
                    "Erstabruf unterbrochen – Fehler, neuer Versuch folgt"), "warn"
        return self.status_label, self.status_badge


def _env_name(provider: str) -> str:
    return f"PORTFOLIA_DS_{provider.upper()}"


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
        self._progress_written: dict[int, datetime] = {}

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

    # -- Konto aus dem Abgleich mit dem kuratierten Import -------------------------------------------------
    def account_evidence(self, sid: int) -> Counter[str]:
        """Konten der Gegenbuchungen (kuratierter Import, App) laut Abgleich der offenen Prüf-Stapel dieser Quelle."""
        votes: Counter[str] = Counter()
        for b in self.db.q("SELECT summary_json FROM csv_batch WHERE kind='sync' AND datasource_id=? AND status IN "
                           "('preview', 'partial')", (sid,)):
            accs = (json.loads(b["summary_json"] or "{}").get("recon") or {}).get("accounts") or {}
            votes.update({str(k): int(v) for k, v in accs.items() if k})
        return votes

    def account_switch(self, sid: int) -> dict[str, Any] | None:
        """Letzte Konto-Umstellung aus dem Abgleich (automatisch oder per Klick, ggf. zurückgenommen)."""
        v = self.ctx.settings.get(f"datasource.{sid}.account_switch")
        return v if isinstance(v, dict) else None

    def _account_bookings(self, account: str) -> int:
        pf = self.ctx.recorded_portfolio()
        return sum(1 for t in (pf.txs if pf is not None else []) if account in (t.from_account, t.to_account))

    def account_suggestion(self, ds: DataSource) -> dict[str, Any] | None:
        """Konto, unter dem der kuratierte Import bzw. die App die Vorgänge dieser Quelle führt – sofern es vom Konto
        der Quelle abweicht. ``auto``: eindeutig genug für die automatische Umstellung."""
        votes = self.account_evidence(int(ds.id))
        total = sum(votes.values())
        if not total:
            return None
        top, n = votes.most_common(1)[0]
        if top == ds.account:
            return None
        used = self._account_bookings(ds.account)
        return {"account": top, "matches": n, "total": total, "others": votes.most_common(4)[1:], "used": used,
                "auto": n >= ACCOUNT_MIN_MATCHES and n / total >= ACCOUNT_SHARE and not used}

    def adopt_account(self, ds: DataSource) -> str | None:
        """Konto der Quelle automatisch auf das Konto des kuratierten Imports umstellen – nur einmal, nur bei
        eindeutigem Abgleich und solange das bisherige Konto keine Buchungen hat (nichts wird aufgeteilt)."""
        if self.account_switch(int(ds.id)) is not None:
            return None  # schon umgestellt oder zurückgenommen: Entscheidung des Nutzers gilt
        sug = self.account_suggestion(ds)
        if sug is None or not sug["auto"]:
            return None
        self.switch_account(int(ds.id), sug["account"], auto=True, matches=int(sug["matches"]))
        return str(sug["account"])

    def switch_account(self, sid: int, account: str, *, auto: bool = False, matches: int = 0,
                       undo: bool = False) -> list[str]:
        """Konto der Quelle ändern, ohne neu abzurufen: offene Prüf-Stapel ohne Übernahme folgen und werden neu
        bewertet; bereits übernommene Buchungen bleiben auf ihrem Konto."""
        ds = self.get(sid)
        if ds is None:
            return ["Datenquelle nicht gefunden."]
        account = re.sub(r"\s+", " ", account or "").strip()
        if not _NAME_RE.match(account):
            return ["Kontoname fehlt oder ist zu lang (höchstens 60 Zeichen)."]
        old = ds.account
        if account == old:
            return []
        stamp = iso(_now())
        self.db.x("UPDATE data_source SET account=?, updated_at=? WHERE id=?", (account, stamp, sid))
        self.ctx.settings.set(f"datasource.{sid}.account_switch",
                              {"from": old, "to": account, "auto": auto, "matches": matches, "undone": undo,
                               "at": stamp})
        log.info("Datenquelle %s: Konto %s → %s (%s)", ds.name, old, account,
                 "zurückgenommen" if undo else "automatisch aus dem Abgleich" if auto else "per Klick")
        from app.csvimport.service import csv_service

        csv = csv_service(self.ctx)
        for b in self.db.q("SELECT id FROM csv_batch WHERE kind='sync' AND datasource_id=? AND status='preview'",
                           (sid,)):
            csv.rebook(int(b["id"]), old, account)
            csv.evaluate(int(b["id"]))
        return []

    def undo_account_switch(self, sid: int) -> list[str]:
        sw = self.account_switch(sid)
        ds = self.get(sid)
        if ds is None or not sw or sw.get("undone") or ds.account != sw.get("to"):
            return ["Keine Umstellung zum Zurücknehmen."]
        return self.switch_account(sid, str(sw.get("from") or ""), undo=True)

    def open_counts(self, sid: int) -> dict[str, int]:
        """Offene Zeilen je Status über alle Prüf-Stapel der Quelle (neu, ungeklärt, mögliche Dublette …)."""
        return {r["status"]: r["n"] for r in self.db.q(
            "SELECT r.status, COUNT(*) AS n FROM csv_row r JOIN csv_batch b ON b.id = r.batch_id WHERE b.kind='sync' "
            "AND b.datasource_id=? AND b.status IN ('preview', 'partial') AND r.status NOT IN "
            "('known', 'ignored', 'committed', 'merged') GROUP BY r.status", (sid,))}

    def balances(self, sid: int) -> list[Any]:
        return self.db.q("SELECT asset_key, qty, name, note, observed_at FROM ds_balance WHERE source_id=? "
                         "ORDER BY asset_key", (sid,))

    def holdings(self, ds: DataSource) -> dict[str, Any]:
        """Beobachteter On-Chain-Bestand (Anbieter) neben dem durch Portfolia-Buchungen erklärten Bestand des Kontos.

        Zuordnung der Kennungen wie im Prüf-Stapel (gespeicherte Zuordnungen, sonst eindeutiges Symbol; Tokens nur
        über ihre gespeicherte Zuordnung). Abweichungen werden angezeigt, nie automatisch ausgeglichen."""
        from decimal import Decimal, InvalidOperation

        from app.csvimport.service import SymbolResolver, csv_service

        rows = self.balances(int(ds.id))
        csv = csv_service(self.ctx)
        resolver = SymbolResolver(csv.known_assets(), csv.saved_symbols())
        led = self.ctx.ledger()
        held = led.holdings_by_account(ds.account) if led is not None else {}
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        observed_at = None
        for r in rows:
            try:
                q = Decimal(r["qty"])
            except (InvalidOperation, TypeError):
                continue
            aid, how = resolver.resolve(r["asset_key"])
            exp = held.get(aid) if aid else None
            if aid:
                seen.add(aid)
            state = ("ignored" if how == "ignored" else "unmapped" if aid is None else
                     "ok" if (exp or Decimal(0)) == q else "diff")
            if state == "unmapped" and q == 0:
                continue
            items.append({"key": r["asset_key"], "name": r["name"], "note": r["note"], "observed": q,
                          "asset_id": aid, "explained": exp if exp is not None else (Decimal(0) if aid else None),
                          "diff": (q - (exp or Decimal(0))) if aid else None, "state": state})
            observed_at = observed_at or r["observed_at"]
        for aid, q in sorted(held.items()):
            if aid not in seen and q:
                items.append({"key": aid, "name": None, "note": None, "observed": None, "asset_id": aid,
                              "explained": q, "diff": None, "state": "only_portfolia"})
        return {"items": items, "observed_at": observed_at,
                "diffs": sum(1 for i in items if i["state"] in ("diff", "only_portfolia")),
                "unmapped": sum(1 for i in items if i["state"] == "unmapped")}

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
        watch: WatchConfig | None = None
        group = None
        if kind == WALLET and prov is not None:
            address, watch, errs = self._wallet_fields(prov, data, current)
            errors += errs
            group = re.sub(r"\s+", " ", str(data.get("wallet_group") or "")).strip() or None
            if group is not None and not _GROUP_RE.match(group):
                errors.append("Wallet-Gruppe ungültig (höchstens 40 Zeichen).")
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
                "key_expires_on": expires, "wallet_group": group,
                "watch_json": watch.dump() if watch is not None else None}
        return vals, errors

    @staticmethod
    def _wallet_fields(prov: Any, data: Mapping[str, Any], current: DataSource | None) \
            -> tuple[str | None, WatchConfig | None, list[str]]:
        """Öffentliche Adresse(n) bzw. Kontoschlüssel, Anbieter und Optionen eines Wallet-Kontos prüfen.

        Bitcoin: mehrere Adressen (eine je Zeile) und/oder ein öffentlicher Kontoschlüssel mit Adresstyp und
        Gap-Limit. Andere Chains: genau eine Adresse. Fehlertexte wiederholen die Eingabe nie."""
        errors: list[str] = []
        prev = current.watch if current is not None else None
        raw_text = str(data.get("address") or "")
        if contains_secret(raw_text):
            return None, None, ["Das sieht nach einem privaten Schlüssel oder einer Seed-Phrase aus – bitte niemals "
                                "eingeben. Benötigt werden nur öffentliche Adressen bzw. der öffentliche "
                                "Kontoschlüssel (xpub/ypub/zpub)."]
        raw_lines = [x for x in re.split(r"[\s,;]+", raw_text) if x]
        if len(raw_lines) > 1 and prov.id != "bitcoin":
            errors.append("Bitte genau eine Adresse eingeben – für weitere Adressen ein eigenes Konto anlegen.")
        if len(raw_lines) > MAX_ADDRESSES:
            errors.append(f"Höchstens {MAX_ADDRESSES} Adressen je Konto.")
        addrs: list[str] = []
        xpubs: list[str] = []
        for line in raw_lines[:MAX_ADDRESSES]:
            norm, err = normalize_address(prov, line)
            if err:
                errors.append(err)
                continue
            if norm and norm[1:4] == "pub":
                xpubs.append(norm)
            elif norm and norm not in addrs:
                addrs.append(norm)
        if not raw_lines:
            errors.append("Adresse fehlt." if prov.id != "bitcoin" else
                          "Mindestens eine Adresse oder einen öffentlichen Kontoschlüssel (xpub/ypub/zpub) angeben.")
        if len(xpubs) > 1:
            errors.append("Bitte nur einen Kontoschlüssel je Konto – für weitere Konten ein eigenes Konto anlegen.")
        script = str(data.get("script") or "").strip() or None
        if xpubs:
            from app.datasources.chains.btckeys import parse_xpub

            default = parse_xpub(xpubs[0]).default_script
            script = script if script in SCRIPT_TYPES else default
        else:
            script = None
        try:
            gap = int(str(data.get("gap") or GAP_DEFAULT))
        except ValueError:
            gap = -1
        if xpubs and not 5 <= gap <= GAP_MAX:
            errors.append(f"Gap-Limit zwischen 5 und {GAP_MAX} wählen (Standard {GAP_DEFAULT}).")
        conn = K.connector_for(prov.id)
        provider = str(data.get("chain_provider") or "").strip() or None
        allowed = getattr(conn, "endpoints", ()) if conn is not None else ()
        if provider and provider not in allowed:
            errors.append("Anbieter für diese Chain nicht verfügbar.")
            provider = None
        if data.get("tokens_shown"):  # Formular mit Kontrollkästchen
            tokens = str(data.get("tokens") or "") in ("1", "on", "true")
        else:
            tokens = prev.tokens if prev is not None else True
        watch = WatchConfig(addresses=addrs, xpubs=xpubs[:1], script=script, gap=gap if xpubs else GAP_DEFAULT,
                            provider=provider, watch_id=(prev.watch_id if prev and prev.watch_id else ""),
                            tokens=tokens)
        primary = (xpubs or addrs or [None])[0]
        return primary, watch, errors

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
        if vals["watch_json"]:
            w = WatchConfig.load(vals["watch_json"])
            w.watch_id = w.watch_id or new_watch_id()
            vals["watch_json"] = w.dump()
        cur = self.db.x(
            "INSERT INTO data_source(kind, provider, name, account, address, credential_ref, enabled, status, "
            "sync_interval_min, auto_commit, next_run_at, note, key_expires_on, wallet_group, watch_json, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,1,'created',?,?,?,?,?,?,?,?,?)",
            (vals["kind"], vals["provider"], vals["name"], vals["account"], vals["address"], vals["credential_ref"],
             vals["sync_interval_min"], vals["auto_commit"], iso(nxt) if nxt else None, vals["note"],
             vals["key_expires_on"], vals["wallet_group"], vals["watch_json"], stamp, stamp))
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
        addr_default = "\n".join(ds.addresses) if ds.is_wallet else (ds.address or "")
        merged = {"provider": ds.provider, "address": addr_default, **dict(data)}
        vals, errors = self.validate(merged, current=ds)
        if errors:
            return errors
        old_w, new_w = ds.watch, WatchConfig.load(vals["watch_json"]) if vals["watch_json"] else None
        changed_watch = ds.is_wallet and new_w is not None and (
            (sorted(old_w.addresses), old_w.xpubs, old_w.script, old_w.gap, old_w.tokens)
            != (sorted(new_w.addresses), new_w.xpubs, new_w.script, new_w.gap, new_w.tokens))
        changed_target = changed_watch or \
            (vals["provider"], vals["address"], vals["account"]) != (ds.provider, ds.address, ds.account)
        changed_endpoint = ds.is_wallet and new_w is not None and old_w.provider != new_w.provider
        nxt = next_run(bool(ds.enabled), K.supported(vals["provider"]), vals["sync_interval_min"],
                       parse_iso(ds.last_run_at))
        with self.db.transaction() as c:
            c.execute(
                "UPDATE data_source SET provider=?, name=?, account=?, address=?, credential_ref=?, "
                "sync_interval_min=?, auto_commit=?, note=?, key_expires_on=?, next_run_at=?, wallet_group=?, "
                "watch_json=?, updated_at=?"
                + (", status='created', cursor_json=NULL, last_error=NULL, coverage_json=NULL" if changed_target else
                   ", status='created', last_check_json=NULL" if changed_endpoint else "") + " WHERE id=?",
                (vals["provider"], vals["name"], vals["account"], vals["address"], vals["credential_ref"],
                 vals["sync_interval_min"], vals["auto_commit"], vals["note"], vals["key_expires_on"],
                 iso(nxt) if nxt else None, vals["wallet_group"], vals["watch_json"] or ds.row["watch_json"],
                 iso(_now()), sid))
            if changed_target:
                c.execute("DELETE FROM ds_balance WHERE source_id=?", (sid,))
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
        """Alle gespeicherten Schlüssel (je Datenquelle und je Anbieter) mit dem aktuellen Master-Key neu
        verschlüsseln (Rotation)."""
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
        for r in self.db.q("SELECT provider, ciphertext, key_id FROM provider_secret"):
            if r["key_id"] == vault.key_id:
                continue
            try:
                plain = vault.decrypt_provider(r["ciphertext"], r["provider"])
                blob, kid = vault.encrypt_provider(plain, r["provider"])
            except VaultError as e:
                errors.append(f"Anbieter {r['provider']}: {e}")
                continue
            self.db.x("UPDATE provider_secret SET ciphertext=?, key_id=?, updated_at=? WHERE provider=?",
                      (blob, kid, iso(_now()), r["provider"]))
            done += 1
        if done:
            self._purge_wal()
        log.info("Zugangsdaten neu verschlüsselt: %d", done)
        return {"rotated": done, "errors": errors}

    def key_stats(self) -> dict[str, Any]:
        vault = self.vault()
        rows = [*self.db.q("SELECT key_id, COUNT(*) AS n FROM data_source_secret GROUP BY key_id"),
                *self.db.q("SELECT key_id, COUNT(*) AS n FROM provider_secret GROUP BY key_id")]
        return {"total": sum(r["n"] for r in rows),
                "stale": sum(r["n"] for r in rows if r["key_id"] != vault.key_id), "vault": vault.status()}

    # -- Anbieter-Schlüssel (Wallets) ----------------------------------------------------------------------
    def provider_keys(self) -> list[dict[str, Any]]:
        """Je Anbieter mit Schlüssel: Status ohne Schlüssel (letzte 4 Zeichen, Herkunft, Nutzung heute)."""
        rows = {r["provider"]: r for r in self.db.q("SELECT provider, key_id, hint, updated_at FROM provider_secret")}
        used = {r["provider"]: r["calls"] for r in self.db.q(
            "SELECT provider, calls FROM api_usage WHERE period=?", (_now().strftime("%Y-%m-%d"),))}
        out = []
        for pid, ep in PROVIDER_KEYS.items():
            r = rows.get(pid)
            env = K.Secret(_env_name(pid))
            users = [ds for ds in self.list() if ds.is_wallet and ds.endpoint is not None
                     and ds.endpoint.key_provider == pid]
            out.append({"id": pid, "label": ep.label, "required": ep.key_required, "terms": ep.terms,
                        "docs": ep.docs, "hint": f"••••{r['hint']}" if r is not None and r["hint"] else None,
                        "key_id": r["key_id"] if r is not None else None,
                        "updated_at": r["updated_at"] if r is not None else None,
                        "mode": "app" if r is not None else ("env" if env.present else None), "env": _env_name(pid),
                        "users": [u.name for u in users], "calls_today": used.get(f"wallet:{ep.id}", 0)})
        return out

    def set_provider_key(self, provider: str, value: str) -> list[str]:
        if provider not in PROVIDER_KEYS:
            return ["Unbekannter Anbieter."]
        v = (value or "").strip()
        if not v:
            return ["API-Key fehlt."]
        errors = self._key_errors(v)
        if errors:
            return errors
        try:
            blob, kid = self.vault().encrypt_provider(v, provider)
        except VaultError as e:
            return [str(e)]
        stamp = iso(_now())
        self.db.x("PRAGMA secure_delete=ON")
        with self.db.transaction() as c:
            c.execute("INSERT INTO provider_secret(provider, ciphertext, key_id, hint, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET ciphertext=excluded.ciphertext, "
                      "key_id=excluded.key_id, hint=excluded.hint, updated_at=excluded.updated_at",
                      (provider, blob, kid, v[-4:], stamp, stamp))
            # betroffene Konten neu prüfen lassen
            for ds in self.list():
                if ds.is_wallet and ds.endpoint is not None and ds.endpoint.key_provider == provider:
                    c.execute("UPDATE data_source SET last_check_json=NULL, updated_at=? WHERE id=?", (stamp, ds.id))
        self._purge_wal()
        log.info("API-Key für Anbieter %s verschlüsselt gespeichert (Master-Key %s)", provider, kid)
        return []

    def remove_provider_key(self, provider: str) -> bool:
        if provider not in PROVIDER_KEYS:
            return False
        self.db.x("PRAGMA secure_delete=ON")
        cur = self.db.x("DELETE FROM provider_secret WHERE provider=?", (provider,))
        self._purge_wal()
        log.info("API-Key für Anbieter %s entfernt", provider)
        return bool(cur.rowcount)

    def provider_secret(self, provider: str) -> K.Secret:
        """Schlüssel eines Anbieters: verschlüsselt in der App, sonst Umgebungsvariable ``PORTFOLIA_DS_<ANBIETER>``."""
        row = self.db.q1("SELECT ciphertext FROM provider_secret WHERE provider=?", (provider,))
        if row is not None:
            try:
                return K.Secret(value=self.vault().decrypt_provider(row["ciphertext"], provider))
            except VaultError as e:
                raise K.ConnectorError("config", str(e)) from None
        return K.Secret(_env_name(provider))

    def _secret(self, ds: DataSource, conn: K.Connector) -> K.Secret:
        if conn.wallet:
            ep = conn.endpoint(ds.config())  # type: ignore[attr-defined]
            if not ep.key_provider:
                return K.Secret(None)
            sec = self.provider_secret(ep.key_provider)
            if ep.key_required and not sec.present:
                raise K.ConnectorError("config", f"{ep.label} verlangt einen Schlüssel des Anbieters – unter "
                                                 "„Anbieter-Schlüssel“ hinterlegen (wird verschlüsselt gespeichert).")
            return sec
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
        self._prepare(ds, conn)
        try:
            secret = self._secret(ds, conn)
            with get_redactor().temporary(secret.values()):
                res = conn.check(ds.config(), secret)
            if res.balances is not None:
                self._store_balances(sid, res.balances)
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

    def start_sync(self, sid: int, trigger: str = "manual") -> dict[str, Any]:
        """Abruf im Hintergrund starten (Wallets: Erstabruf in Etappen mit Fortschritt). Kehrt sofort zurück."""
        ds = self.get(sid)
        if ds is None:
            return {"error": "Datenquelle nicht gefunden."}
        if not _SYNC_LOCK.acquire(blocking=False):
            return {"error": "Eine Synchronisierung läuft bereits – bitte kurz warten."}
        self._set_progress(sid, {"running": True, "stage": "Start", "done": 0, "total": None, "text": "",
                                 "started_at": iso(_now())}, force=True)
        t = threading.Thread(target=self._background, args=(sid, trigger), name=f"ds-sync-{sid}", daemon=True)
        try:
            t.start()
        except Exception:  # pragma: no cover - Thread-Start fehlgeschlagen
            _SYNC_LOCK.release()
            raise
        return {"started": True}

    def _background(self, sid: int, trigger: str) -> None:
        """Hintergrundlauf: so lange Etappen, bis der Erstabruf vollständig ist, ein Fehler auftritt oder die
        Etappengrenze erreicht ist (dann setzt der Zeitplan fort)."""
        res: dict[str, Any] = {}
        try:
            for _ in range(MAX_ROUNDS):
                res = self._sync(sid, trigger, background=True)
                ds = self.get(sid)
                if res.get("error") or ds is None or not ds.backfill_pending:
                    break
        except Exception as e:  # pragma: no cover - Absicherung: Hintergrundlauf darf nie hängen bleiben
            log.exception("Datenquelle %s: Hintergrundlauf abgebrochen", sid)
            res = {"error": describe_error(e)[1]}
        finally:
            try:
                p = dict(self.get(sid).progress) if self.get(sid) is not None else {}  # type: ignore[union-attr]
                p.update({"running": False, "finished_at": iso(_now()),
                          "result": res.get("error") or res.get("message") or "", "ok": not res.get("error"),
                          "batch_id": res.get("batch_id")})
                self._set_progress(sid, p, force=True)
            finally:
                _SYNC_LOCK.release()
                self.db.close_thread_conn()

    def _set_progress(self, sid: int, data: dict[str, Any], force: bool = False) -> None:
        now = _now()
        last = self._progress_written.get(sid)
        if not force and last is not None and (now - last).total_seconds() < 1.0:
            return
        self._progress_written[sid] = now
        data = {**data, "updated_at": iso(now)}
        self.db.x("UPDATE data_source SET progress_json=? WHERE id=?",
                  (json.dumps(data, ensure_ascii=False, default=str), sid))

    def _prepare(self, ds: DataSource, conn: K.Connector) -> None:
        """Fortschritt und Nutzungszähler an den Connector anbinden."""
        sid = int(ds.id)
        base = {"running": True, "started_at": iso(_now())}

        def progress(stage: str, done: int, total: int | None, text: str) -> None:
            self._set_progress(sid, {**base, "stage": stage, "done": done, "total": total, "text": text[:200]})

        conn.progress = progress
        if conn.wallet:
            ep = conn.endpoint(ds.config())  # type: ignore[attr-defined]
            quota = Quota(self.db, f"wallet:{ep.id}", "day")
            conn.usage = quota.add  # type: ignore[attr-defined]

    def _store_balances(self, sid: int, balances: list[K.Balance]) -> None:
        stamp = iso(_now())
        with self.db.transaction() as c:
            c.execute("DELETE FROM ds_balance WHERE source_id=?", (sid,))
            c.executemany("INSERT OR REPLACE INTO ds_balance(source_id, asset_key, qty, name, note, observed_at) "
                          "VALUES (?,?,?,?,?,?)",
                          [(sid, b.asset_key[:120], format(b.qty.normalize(), "f") if b.qty else "0",
                            (b.name or None) and str(b.name)[:80], b.note, stamp) for b in balances])

    def _fail(self, ds: DataSource, run_id: int, e: BaseException, secrets: list[str], started: datetime,
              nxt: datetime | None) -> dict[str, Any]:
        """Lauf als Fehler abschließen – Meldung ohne Geheimnisse, Wartezeit des Anbieters beachten. Der
        Abrufstand bleibt unverändert: der nächste Lauf holt dieselben Vorgänge erneut."""
        kind, msg = describe_error(e, secrets)
        if ds.backfill_pending and ds.enabled:  # Erstabruf nicht liegen lassen: nach einer Pause erneut versuchen
            nxt = max(nxt or started, started + timedelta(minutes=10))
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

    def _sync(self, sid: int, trigger: str, background: bool = False) -> dict[str, Any]:
        res: dict[str, Any] = {}
        try:
            res = self._sync_once(sid, trigger)
            return res
        finally:
            if not background:  # Hintergrundläufe schließen den Fortschritt nach der letzten Etappe selbst ab
                ds = self.get(sid)
                if ds is not None and ds.row["progress_json"]:
                    p = dict(ds.progress)
                    if p.get("running"):
                        p.update({"running": False, "finished_at": iso(_now()), "ok": not res.get("error"),
                                  "result": res.get("error") or res.get("message") or ""})
                        self._set_progress(sid, p, force=True)

    def _sync_once(self, sid: int, trigger: str) -> dict[str, Any]:
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
        self._prepare(ds, conn)
        try:
            secret = self._secret(ds, conn)
            cursor = json.loads(ds.cursor_json) if ds.cursor_json else None
            with get_redactor().temporary(secret.values()):
                res = conn.fetch(ds.config(), secret, cursor)
            recs = self._normalize(ds, res)
        except Exception as e:  # Anbieter-/Netzwerk-/Vertragsfehler → Anzeige ohne Geheimnisse
            return self._fail(ds, run_id, e, secret.values(), started, nxt)
        conn.report("Prüfung", len(res.events), len(res.events), "Vorgänge werden ausgewertet")
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
            if bid is not None:  # Konto des kuratierten Imports übernehmen, solange nichts aufgeteilt wird
                switched = self.adopt_account(ds)
                if switched:
                    ds = self.get(sid) or ds
            if ds.auto_commit:  # je Ereignis – auch in offenen Stapeln, sobald sie eindeutig geworden sind
                for b in self.pending_batches(sid):
                    eligible = self._auto_eligible(csv.rows(int(b["id"])))
                    if eligible:
                        out = csv.commit(int(b["id"]), only_idx=eligible)
                        committed += int(out.get("created", 0) or 0) + int(out.get("merged", 0) or 0)
        except Exception as e:  # Fehler der Import-Pipeline: Lauf nicht als „läuft“ stehen lassen
            log.exception("Datenquelle %s: Verarbeitung fehlgeschlagen", ds.name)
            return self._fail(ds, run_id, e, secret.values(), started, nxt)
        # unvollständige/ungeklärte Zeilen sind Sache der Prüfung, nicht des Abrufs; erkannte Lücken dagegen nicht
        partial = not res.complete or bool(res.gaps)
        status = "partial" if partial else "synced"
        notes = [sanitize_error(w, secret.values()) for w in [*res.gaps, *res.warnings][:6]]
        more = bool(res.resume and not res.complete and res.cursor is not None)
        if res.more is not None:
            more = more and bool(res.more)
        if not res.complete and not res.gaps:
            notes.insert(0, "Erstabruf in Etappen – wird automatisch fortgesetzt" if more else
                         "Abruf unvollständig – der nächste Lauf holt erneut ab")
        elif more:
            notes.insert(0, "Erstabruf in Etappen – wird automatisch fortgesetzt")
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
        resume = more
        coverage = {**res.coverage, "complete": res.complete, "at": stamp, "resume": resume,
                    "gaps": [sanitize_error(g, secret.values()) for g in res.gaps[:20]]}
        if conn.wallet:
            coverage["limits"] = conn.coverage_limits(ds.config())  # type: ignore[attr-defined]
        if resume and ds.enabled:  # Erstabruf in Etappen: bald fortsetzen, unabhängig vom Intervall
            soon = started + timedelta(seconds=BACKFILL_NEXT_S)
            nxt = min(nxt, soon) if nxt is not None else soon
        # Abrufstand nur nach vollständigem Abruf bzw. bis zu einem sicheren Fortsetzungspunkt vorrücken
        cursor_json = json.dumps(res.cursor) if res.cursor is not None and (res.complete or res.resume) \
            else ds.cursor_json
        if res.balances is not None:
            self._store_balances(sid, res.balances)
        self.db.x("UPDATE data_source SET status=?, last_run_at=?, last_success_at=?, last_error=?, last_error_at=?, "
                  "next_run_at=?, cursor_json=?, coverage_json=?, updated_at=? WHERE id=?",
                  (status, stamp, stamp, "; ".join(notes) if partial else None, stamp if partial else None,
                   iso(nxt) if nxt else None, cursor_json,
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
            # Transfer-Vorschläge mit bereits übernommenen Buchungen (j:…) verändern deren Lots – nie automatisch;
            # ebenso unklare Transfers (mittlere Sicherheit, Gegenbuchung nur im kuratierten Import): sie brauchen eine
            # Entscheidung, als einfacher Zu-/Abgang gebucht gingen Einstand und Haltedauer verloren
            if all(rc.status == "new" and not rc.errors and rc.row is not None and not rc.rec.review
                   and rc.include() and not (rc.pair_ref or "").startswith("j:") and not rc.transfer_unclear
                   for rc in lines):
                out |= {rc.idx for rc in lines}
        return out

    def _normalize(self, ds: DataSource, res: K.FetchResult) -> list[Any]:
        """Ereignisse → Zeilen im Zwischenformat mit Kennung ``<ereignis>#<zeile>`` (Wallets:
        ``<ereignis>#<unterkennung>``); Vertrag prüfen."""
        conn_subs = ds.is_wallet
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
            subs: set[str] = set()
            for i, rec in enumerate(ev.lines):
                rec.event_key, rec.event_line = key, i
                sub = rec.ext_id if conn_subs else None
                if sub is not None:
                    if not K.SUB_ID_RE.match(sub) or sub in subs:
                        raise K.ConnectorError("data", "Ungültige oder doppelte Unterkennung einer Bewegung.")
                    subs.add(sub)
                rec.ext_id = f"{key}#{sub if sub is not None else i}"
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
            if not ds.enabled or not ds.supported or (int(ds.sync_interval_min or 0) <= 0
                                                      and not ds.backfill_pending):
                continue
            if ds.progress.get("running"):
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
