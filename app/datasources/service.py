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

import dataclasses
import json
import logging
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.datasources import connector as K
from app.datasources.catalog import Catalog
from app.datasources.chainhttp import ENDPOINTS
from app.datasources.overview import conflict, identity, is_xpub
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
from app.progress import Progress, view
from app.util.http import Quota
from app.util.timeutil import iso, local_tz, parse_iso, to_local_date

log = logging.getLogger(__name__)

STATUS_LABEL = {"created": "angelegt", "connected": "verbunden", "synced": "synchronisiert",
                "partial": "teilweise synchronisiert", "error": "Fehler"}
STATUS_BADGE = {"created": "", "connected": "info", "synced": "good", "partial": "warn", "error": "crit"}
RUN_STATUS_LABEL = {"running": "läuft", "ok": "erfolgreich", "partial": "teilweise", "error": "Fehler"}
CHAIN_TICKER = {"bitcoin": "BTC", "ethereum": "ETH", "bsc": "BNB", "polygon": "POL", "avalanche": "AVAX",
                "solana": "SOL", "xrp": "XRP", "cardano": "ADA", "polkadot": "DOT", "kaspa": "KAS", "tron": "TRX",
                "litecoin": "LTC", "dogecoin": "DOGE", "arbitrum": "ARB", "optimism": "OP", "base": "BASE",
                "pulsechain": "PLS", "peaq": "PEAQ", "other_chain": "…"}
MULTI_ADDRESS = ("bitcoin", "cardano")  # Chains mit mehreren Adressen je Konto (UTXO)
MAX_EVENTS = 50_000
KEY_WARN_DAYS = 14
DONE = ("known", "ignored", "committed", "merged", "linked")
# Je Datenquelle ein Lauf zur Zeit (Zeitplan und „Jetzt synchronisieren“ nie doppelt); verschiedene Quellen laufen
# unabhängig – ein langsamer Wallet-Abruf hält die Börse nicht auf. Abrufe beim Anbieter laufen parallel; Abgleich und
# Übernahme in den Prüf-Stapel bzw. ins Journal (_INGEST_LOCK) nacheinander, damit quellenübergreifende Dubletten- und
# Transfer-Erkennung stets den vollständigen Stand der anderen Quelle sieht.
_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_INGEST_LOCK = threading.RLock()
_BATCH_LOCK = threading.Lock()  # „Mehrere Konten aktualisieren“: eine Sammelaktualisierung zur Zeit
_CANCEL: dict[int, threading.Event] = {}  # Abbruchsignal je laufender Quelle
_BATCH_CANCEL = threading.Event()
BUSY_TEXT = "Für diese Datenquelle läuft bereits ein Abruf – abwarten oder abbrechen."


def _source_lock(sid: int) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(sid)
        if lock is None:
            lock = _LOCKS[sid] = threading.Lock()
        return lock


def _acquire(sid: int) -> threading.Event | None:
    """Sperre der Quelle nehmen (ohne Warten) und ein frisches Abbruchsignal anlegen – ``None``: läuft bereits."""
    if not _source_lock(sid).acquire(blocking=False):
        return None
    ev = threading.Event()
    with _LOCKS_GUARD:
        _CANCEL[sid] = ev
    return ev


def _release(sid: int) -> None:
    with _LOCKS_GUARD:
        _CANCEL.pop(sid, None)
    _source_lock(sid).release()


def is_busy(sid: int) -> bool:
    """Läuft in diesem Prozess gerade ein Abruf der Quelle?"""
    return _source_lock(sid).locked()
_NAME_RE = re.compile(r"^[^\x00-\x1f<>]{1,60}$")
ACCOUNT_MIN_MATCHES = 3  # automatische Konto-Umstellung: mindestens so viele Treffer im kuratierten Import …
ACCOUNT_SHARE = 0.9  # … und dieser Anteil unter einem Konto
_KEY_RE = re.compile(r"^[\x21-\x7e]{16,1024}$")  # druckbare ASCII-Zeichen ohne Leerzeichen
# Anbieter mit zweiteiligem Zugang (API-Key + Secret), gespeichert als „Key:Secret“
SPLIT_KEY_PROVIDERS = frozenset({"binance"})


def join_key(provider: str, key: str, secret: str | None) -> tuple[str, list[str]]:
    """Zugang zusammensetzen – nie Teile der Eingabe in Fehlertexten wiederholen."""
    k, sec = (key or "").strip(), (secret or "").strip()
    if provider not in SPLIT_KEY_PROVIDERS:
        if sec:
            return "", ["Für diesen Anbieter gibt es keinen Secret Key – Feld bitte leer lassen."]
        return k, [] if k else ["API-Key fehlt."]
    if not sec and ":" in k:  # Umgebungsvariable/Fortgeschritten: bereits „Key:Secret“
        k, _, sec = k.partition(":")
    if not k or not sec:
        return "", ["Für Binance werden API-Key und Secret Key benötigt."]
    if ":" in k or ":" in sec:
        return "", ["API-Key bzw. Secret Key enthält ein unerwartetes Zeichen (:)."]
    for part in (k, sec):
        if not _KEY_RE.match(part):
            return "", ["Das sieht nicht nach einem API-Key bzw. Secret Key aus (16–1024 Zeichen, keine Leerzeichen)."]
    return f"{k}:{sec}", []


def key_hint(provider: str, value: str) -> str:
    """Letzte 4 Zeichen des API-Keys – bei zweiteiligem Zugang nie vom Secret."""
    return (value.partition(":")[0] if provider in SPLIT_KEY_PROVIDERS else value)[-4:]
_GROUP_RE = re.compile(r"^[^\x00-\x1f<>]{1,40}$")
PROGRESS_STALE_S = 600  # ohne Lebenszeichen gilt ein Lauf als abgebrochen (Neustart des Containers)
BACKFILL_NEXT_S = 90  # Etappen des Erstabrufs: nächster Lauf nach so vielen Sekunden
MAX_ROUNDS = 40  # Etappen je manuell gestartetem Hintergrundlauf
BATCH_WORKERS = 4  # Anbieter, die beim Aktualisieren mehrerer Konten gleichzeitig abgerufen werden
MANY_ROUNDS = 3  # Etappen je Konto beim Aktualisieren mehrerer Konten (Erstabrufe setzt der Zeitplan fort)
BATCH_KEY = "datasources.batch_sync"
# Anbieter-Schlüssel (je Anbieter, nicht je Datenquelle) – nur Anbieter aus dem geprüften Katalog
PROVIDER_KEYS: dict[str, Any] = {}
for _e in ENDPOINTS.values():  # erster Endpunkt je Schlüssel bestimmt die Anzeige (z. B. „Subscan direkt“)
    if _e.key_provider:
        PROVIDER_KEYS.setdefault(_e.key_provider, _e)
_SECRETISH = re.compile(r"(?i)\b(authorization|x-api-key|api[-_ ]?key|apikey|secret|signature|passphrase|token|"
                        r"bearer)(\s*[:=]\s*|\s+)([^\s,;]+)")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s]*")


def _mask_secretish(m: re.Match) -> str:
    """Wert hinter „API-Key“/„token“ … maskieren; nach bloßem Leerzeichen nur schlüsselartige Werte (Ziffern,
    Sonderzeichen oder lang) – sonst würde z. B. „Kein API-Key hinterlegt“ zu „Kein API-Key ***“."""
    val = m.group(3)
    if m.group(2).strip() or len(val) >= 16 or not val.isalpha():
        return f"{m.group(1)}{m.group(2)}***"
    return m.group(0)


def _now() -> datetime:
    return datetime.now(UTC)


def sanitize_error(msg: str, secrets: Iterable[str] = ()) -> str:
    """Anzeigetext ohne Geheimnisse: bekannte Werte, Schlüssel=Wert-Paare und URL-Parameter werden maskiert."""
    text = str(msg or "")
    for s in sorted({x for x in secrets if x and len(x) >= 4}, key=len, reverse=True):
        text = text.replace(s, "***")
    text = get_redactor().redact(text)
    text = _SECRETISH.sub(_mask_secretish, text)
    text = _URL_QUERY.sub(lambda m: f"{m.group(1)}?…", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400] + ("…" if len(text) > 400 else "")


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
    def copy_address(self) -> str:
        """Kennung zum Kopieren (erste Adresse; ein Bitcoin-Kontoschlüssel nur, wenn es nichts anderes gibt)."""
        a = self.addresses
        xpubs = set(self.watch.xpubs)
        plain = [x for x in a if x not in xpubs]
        return (plain or a or [""])[0]

    @property
    def explorer_url(self) -> str | None:
        """Explorer-Link der Adresse (öffnet der Nutzer selbst; Portfolia ruft ihn nie ab)."""
        c = self.connector
        a = self.copy_address
        if c is None or not getattr(c, "explorer_addr", "") or not a or a in self.watch.xpubs or \
                (self.row["provider"] == "bitcoin" and is_xpub(a)):
            return None  # Kontoschlüssel (xpub/zpub) zeigt kein Explorer an
        if self.row["provider"] == "cardano" and a.startswith("stake1"):
            return f"https://cardanoscan.io/stakekey/{a}"
        return str(c.explorer_addr).format(a)  # type: ignore[attr-defined]

    @property
    def icon(self) -> str:
        """Kürzel für das Chain-Symbol der Oberfläche (CSS ``chain-icon--<provider>``)."""
        return CHAIN_TICKER.get(self.row["provider"], (self.row["provider"] or "?")[:3].upper())

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

    def account_side_evidence(self, ds: DataSource) -> Counter[str]:
        """Konten von Import-Transfers, deren Zu- bzw. Abgangsseite ein Vorgang dieser Quelle unter anderem Kontonamen
        ist (exakt gleiche Menge, ohne gemeinsamen Hash – :mod:`app.csvimport.transfer_side`): offene Prüf-Stapel
        sowie übernommene Buchungen der Quelle, auch bereits als „im Import enthalten“ entschiedene."""
        sid = int(ds.id)
        votes: Counter[str] = Counter()
        for b in self.db.q("SELECT summary_json FROM csv_batch WHERE kind='sync' AND datasource_id=? AND status IN "
                           "('preview', 'partial')", (sid,)):
            accs = json.loads(b["summary_json"] or "{}").get("side_accounts") or {}
            votes.update({str(k): int(v) for k, v in accs.items() if k})
        base, _ = self.ctx.effective_base()
        imports = {t.tx_id: t for t in base.txs} if base is not None else {}
        # mit „verknüpfen“ entschiedene Zeilen der Quelle (Seite eines Import-Transfers)
        for r in self.db.q("SELECT l.tx_id, l.role, l.record_json FROM tx_link l JOIN csv_batch b ON b.id = "
                           "l.batch_id WHERE b.datasource_id=? AND l.status='active' AND l.role IN ('in', 'out')",
                           (sid,)):
            t = imports.get(r["tx_id"])
            if t is None or t.type != "transfer":
                continue
            leg = json.loads(r["record_json"] or "{}").get("inn" if r["role"] == "in" else "out") or [None]
            acc = t.to_account if r["role"] == "in" else t.from_account
            if acc and leg[0] and acc != leg[0]:
                votes[acc] += 1
        mine = {r["tx_id"]: r for r in self.db.q(
            "SELECT tx_id, type, from_account, to_account FROM journal_tx WHERE datasource_id=? AND status='active' "
            "AND type IN ('deposit', 'withdrawal')", (sid,))}
        if not mine:
            return votes
        from app.journal.service import journal_service

        for jid, hits in journal_service(self.ctx).transfer_sides().items():
            if jid in mine:
                votes.update(h["account"] for h in hits if not h["same"] and h.get("account"))
        for r in self.db.q("SELECT journal_tx_id, import_tx_id FROM journal_import_link WHERE decision='covered'"):
            j, t = mine.get(r["journal_tx_id"]), imports.get(r["import_tx_id"])
            if j is None or t is None or t.type != "transfer":
                continue
            acc, own = (t.to_account, j["to_account"]) if j["type"] == "deposit" else (t.from_account,
                                                                                       j["from_account"])
            if acc and acc != own:
                votes[acc] += 1
        return votes

    def account_suggestion(self, ds: DataSource) -> dict[str, Any] | None:
        """Konto, unter dem der kuratierte Import bzw. die App die Vorgänge dieser Quelle führt – sofern es vom Konto
        der Quelle abweicht. Belege: gleiche Blockchain-Transaktion (Hash) und Transferseiten unter anderem
        Kontonamen (``sides``). ``auto`` (automatische Umstellung) nur aus dem Hash-Abgleich – eindeutig genug und
        solange das bisherige Konto keine Buchungen hat."""
        hashes = self.account_evidence(int(ds.id))
        sides = self.account_side_evidence(ds)
        votes = hashes + sides
        total = sum(votes.values())
        if not total:
            return None
        top, n = votes.most_common(1)[0]
        if top == ds.account:
            return None
        used = self._account_bookings(ds.account)
        h_n, h_total = hashes.get(top, 0), sum(hashes.values())
        contra = sum(sides.values()) - sides.get(top, 0)  # Transferseiten, die auf andere Konten zeigen
        return {"account": top, "matches": n, "total": total, "others": votes.most_common(4)[1:], "used": used,
                "sides": sides.get(top, 0),
                "auto": h_n >= ACCOUNT_MIN_MATCHES and h_n / max(h_total, 1) >= ACCOUNT_SHARE and not used
                and not contra}

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
            "('known', 'ignored', 'committed', 'merged', 'linked') GROUP BY r.status", (sid,))}

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
        if address and not errors and kind != WALLET:
            dup = self.db.q1("SELECT id, name FROM data_source WHERE kind=? AND provider=? AND address=? AND id<>?",
                             (kind, pid, address, current.row["id"] if current else 0))
            if dup is not None:
                errors.append(f"Diese Adresse ist bereits als „{dup['name']}“ angelegt.")
        if kind == WALLET and watch is not None and not errors:
            errors += self._overlap_errors(pid, watch, address, current)
        vals = {"kind": kind, "provider": pid, "name": name, "account": account, "address": address,
                "credential_ref": credential_ref, "sync_interval_min": max(interval, 0),
                "auto_commit": 1 if str(data.get("auto_commit") or "") in ("1", "on", "true") else 0, "note": note,
                "key_expires_on": expires, "wallet_group": group,
                "watch_json": watch.dump() if watch is not None else None}
        return vals, errors

    def _overlap_errors(self, pid: str, watch: WatchConfig, address: str | None,
                        current: DataSource | None) -> list[str]:
        """Konten derselben Chain dürfen sich nicht überschneiden (gleiche Adresse, Adresse im Kontoschlüssel eines
        anderen Kontos): sonst würden dieselben Vorgänge zweimal gebucht. Andere Chains sind getrennt – dieselbe
        0x-Adresse auf Ethereum und Polygon sind zwei Konten."""
        mine = identity(pid, watch, address)
        errors = []
        for other in self.list():
            if not other.is_wallet or other.provider != pid or (current is not None and other.id == current.id):
                continue
            theirs = identity(pid, other.watch, other.address, other.row["cursor_json"])
            why = conflict(mine, theirs)
            if why and mine[0] & theirs[0]:
                errors.append(f"Diese Adresse ist bereits als „{other.name}“ angelegt (gleiche Chain) – dieselben "
                              "Vorgänge würden doppelt gebucht.")
            elif why:
                errors.append(f"Überschneidung mit „{other.name}“: {why}. Dieselben Vorgänge würden doppelt gebucht – "
                              "die Adresse nur in einem Konto führen (bei Bitcoin bevorzugt im Konto mit "
                              "Kontoschlüssel).")
        return errors

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
        if len(raw_lines) > 1 and prov.id not in MULTI_ADDRESS:
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
            if norm and prov.id == "bitcoin" and is_xpub(norm):
                xpubs.append(norm)
            elif norm and norm not in addrs:
                addrs.append(norm)
        if not raw_lines:
            errors.append("Adresse fehlt." if prov.id != "bitcoin" else
                          "Mindestens eine Adresse oder einen öffentlichen Kontoschlüssel (xpub/ypub/zpub) angeben.")
        if prov.id == "cardano" and addrs:
            from app.datasources.chains.codec import cardano_stake_of

            stakes = {st for st in (cardano_stake_of(a) for a in addrs) if st}
            if len(stakes) > 1:
                errors.append("Die Adressen gehören zu verschiedenen Cardano-Konten (verschiedene Stake-Teile) – je "
                              "Konto ein eigenes Wallet-Konto anlegen.")
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
        api_secret = str(data.get("api_secret") or "").strip()
        if api_key or api_secret:
            conn = K.connector_for(vals["provider"])
            if conn is None or not conn.needs_credentials:
                errors.append("Für diesen Anbieter gibt es (noch) keine Anbindung mit API-Key – Feld bitte leer "
                              "lassen.")
            else:
                api_key, errs = join_key(vals["provider"], api_key, api_secret or None)
                errors += errs or self._key_errors(api_key)
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

    def set_api_key(self, sid: int, value: str, expires_on: str | None = None, secret: str | None = None) -> list[str]:
        """API-Key verschlüsselt speichern bzw. ersetzen. Übernommene Buchungen und Abrufstand bleiben erhalten;
        die Verbindung ist danach neu zu prüfen. ``secret``: zweiter Teil bei Anbietern mit Key + Secret (Binance)."""
        ds = self.get(sid)
        if ds is None:
            return ["Datenquelle nicht gefunden."]
        v, errors = join_key(ds.provider, value, secret)
        if errors:
            return errors
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
                      "updated_at=excluded.updated_at", (sid, blob, kid, key_hint(ds.provider, v), stamp, stamp))
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
        self._prepare(ds, conn, track=False)
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
        ev = _acquire(sid)
        if ev is None:
            return {"error": BUSY_TEXT, "busy": True}
        try:
            with K.cancel_scope(ev):
                return self._sync(sid, trigger)
        finally:
            _release(sid)

    def start_sync(self, sid: int, trigger: str = "manual") -> dict[str, Any]:
        """Abruf im Hintergrund starten (Wallets: Erstabruf in Etappen mit Fortschritt). Kehrt sofort zurück."""
        ds = self.get(sid)
        if ds is None:
            return {"error": "Datenquelle nicht gefunden."}
        ev = _acquire(sid)
        if ev is None:
            return {"error": BUSY_TEXT, "busy": True}
        self._set_progress(sid, {"running": True, "stage": "Start", "done": 0, "total": None, "text": "",
                                 "started_at": iso(_now())}, force=True)
        t = threading.Thread(target=self._background, args=(sid, trigger, ev), name=f"ds-sync-{sid}", daemon=True)
        try:
            t.start()
        except Exception:  # pragma: no cover - Thread-Start fehlgeschlagen
            _release(sid)
            raise
        return {"started": True}

    def cancel(self, sid: int) -> str:
        """Laufenden Abruf abbrechen. Wirkt an der nächsten Prüfstelle (Anfrage, Wartezeit, Fortschritt); das
        Einbuchen eines bereits abgerufenen Ergebnisses wird nicht mittendrin unterbrochen. Läuft in diesem Prozess
        nichts (z. B. Anzeige „Abruf läuft“ nach einem Neustart), wird nur die Anzeige beendet."""
        ds = self.get(sid)
        if ds is None:
            return "Datenquelle nicht gefunden."
        with _LOCKS_GUARD:
            ev = _CANCEL.get(sid)
        if ev is not None:
            ev.set()
            p = dict(ds.progress)
            p.update(cancel_requested=True, text="Wird abgebrochen …")
            self._set_progress(sid, p, force=True)
            log.info("Datenquelle %s: Abbruch angefordert", ds.name)
            return "Abbruch angefordert – der Abruf endet an der nächsten Prüfstelle."
        raw = ds.row["progress_json"]
        try:
            p = json.loads(raw) if raw else {}
        except ValueError:
            p = {}
        if p.get("running"):
            p.update(running=False, finished_at=iso(_now()), ok=False, result=K.CANCEL_TEXT, stale=True)
            self._set_progress(sid, p, force=True)
            return "Kein laufender Abruf gefunden – Anzeige zurückgesetzt."
        return "Es läuft kein Abruf."

    def _background(self, sid: int, trigger: str, ev: threading.Event) -> None:
        """Hintergrundlauf: so lange Etappen, bis der Erstabruf vollständig ist, ein Fehler auftritt, der Nutzer
        abbricht oder die Etappengrenze erreicht ist (dann setzt der Zeitplan fort)."""
        res: dict[str, Any] = {}
        try:
            with K.cancel_scope(ev):
                for _ in range(MAX_ROUNDS):
                    res = self._sync(sid, trigger, background=True)
                    ds = self.get(sid)
                    if res.get("error") or ds is None or not ds.backfill_pending or ev.is_set():
                        break
        except Exception as e:  # pragma: no cover - Absicherung: Hintergrundlauf darf nie hängen bleiben
            log.exception("Datenquelle %s: Hintergrundlauf abgebrochen", sid)
            res = {"error": describe_error(e)[1]}
        finally:
            try:
                p = dict(self.get(sid).progress) if self.get(sid) is not None else {}  # type: ignore[union-attr]
                p.update({"running": False, "finished_at": iso(_now()),
                          "result": res.get("error") or res.get("message") or "", "ok": not res.get("error"),
                          "batch_id": res.get("batch_id"), "cancelled": ev.is_set()})
                p.pop("cancel_requested", None)
                self._set_progress(sid, p, force=True)
            finally:
                _release(sid)
                self.db.close_thread_conn()

    # -- Mehrere Konten nacheinander (Gruppe, alle Wallets) ------------------------------------------------
    def start_sync_many(self, ids: list[int], label: str, trigger: str = "manual", *, mode: str = "sync",
                        back: str | None = None) -> dict[str, Any]:
        """Konten im Hintergrund aktualisieren: je Anbieter nacheinander, verschiedene Anbieter nebeneinander. Ein
        Fehler betrifft nur das jeweilige Konto – die übrigen laufen weiter; bisher übernommene Daten bleiben
        unverändert. ``mode="check"`` fragt nur die aktuellen Bestände ab (Verbindungsprüfung des Anbieters, keine
        Buchungen); ``back`` ist die Seite, zu der die Fortschrittsanzeige danach zurückkehrt."""
        todo = [i for i in ids if (ds := self.get(i)) is not None and ds.supported and ds.enabled]
        if not todo:
            return {"error": "Keine aktiven Konten mit automatischer Anbindung ausgewählt."}
        if not _BATCH_LOCK.acquire(blocking=False):
            return {"error": "Eine Aktualisierung mehrerer Konten läuft bereits – abwarten oder abbrechen."}
        mode = "check" if mode == "check" else "sync"
        safe_back = back if back and back.startswith("/") and not back.startswith("//") else None
        self.ctx.settings.set(BATCH_KEY, {"running": True, "label": label[:80], "total": len(todo), "done": 0,
                                          "errors": [], "started_at": iso(_now()), "updated_at": iso(_now()),
                                          "mode": mode, "back": safe_back})
        t = threading.Thread(target=self._background_many, args=(todo, trigger, mode), name="ds-sync-many",
                             daemon=True)
        try:
            t.start()
        except Exception:  # pragma: no cover - Thread-Start fehlgeschlagen
            _BATCH_LOCK.release()
            raise
        return {"started": True, "count": len(todo)}

    def cancel_many(self) -> str:
        """Sammelaktualisierung abbrechen: das laufende Konto bricht ab, die übrigen werden nicht mehr gestartet."""
        if not _BATCH_LOCK.locked():
            p = dict(self.batch_progress())
            if p.get("running") or (self.ctx.settings.get(BATCH_KEY) or {}).get("running"):
                raw = dict(self.ctx.settings.get(BATCH_KEY) or {})
                raw.update(running=False, finished_at=iso(_now()), cancelled=True)
                self.ctx.settings.set(BATCH_KEY, raw)
                return "Keine laufende Aktualisierung gefunden – Anzeige zurückgesetzt."
            return "Es läuft keine Aktualisierung mehrerer Konten."
        _BATCH_CANCEL.set()
        state = dict(self.ctx.settings.get(BATCH_KEY) or {})
        for cur in dict.fromkeys([*(state.get("current_ids") or []), state.get("current_id")]):
            if cur:
                self.cancel(int(cur))
        state.update(cancel_requested=True)
        self.ctx.settings.set(BATCH_KEY, state)
        return "Abbruch angefordert – weitere Konten werden nicht mehr gestartet."

    def _background_many(self, ids: list[int], trigger: str, mode: str = "sync") -> None:
        """Konten je Anbieter nacheinander, verschiedene Anbieter nebeneinander (höchstens ``BATCH_WORKERS``): Ein
        langsamer Anbieter (z. B. eine Kaspa-Historie) hält die übrigen nicht mehr auf. Die Anfragegrenzen je Endpunkt
        gelten ohnehin gemeinsam für alle Läufe – paralleles Abrufen verletzt sie nicht."""
        state = dict(self.batch_progress())
        _BATCH_CANCEL.clear()
        lock = threading.Lock()
        running: dict[int, str] = {}
        groups: dict[str, list[int]] = {}
        for sid in ids:
            ds = self.get(sid)
            groups.setdefault(str(ds.provider) if ds is not None else "?", []).append(sid)
        counter = {"done": 0, "skipped": 0}

        def publish(**extra: Any) -> None:  # unter ``lock`` aufrufen
            state.update(current=", ".join(running.values()) or None, current_id=next(iter(running), None),
                         current_ids=list(running), done=counter["done"], updated_at=iso(_now()), **extra)
            self.ctx.settings.set(BATCH_KEY, state)

        def one(sid: int) -> None:
            ds = self.get(sid)
            name = ds.name if ds else str(sid)
            ev = _acquire(sid)
            if ev is None:  # läuft gerade einzeln – nicht doppelt abrufen, die übrigen Konten nicht aufhalten
                with lock:
                    counter["done"] += 1
                    publish(errors=[*state.get("errors", []), f"{name}: läuft bereits separat – übersprungen"][-10:])
                return
            with lock:
                running[sid] = name
                publish()
            res: dict[str, Any] = {}
            try:
                try:
                    self._set_progress(sid, {"running": True, "stage": "Start", "done": 0, "total": None, "text": "",
                                             "started_at": iso(_now())}, force=True)
                    with K.cancel_scope(ev):
                        if mode == "check":  # nur aktuelle Bestände abfragen, keine Buchungen abrufen
                            ok, text = self.check(sid)
                            res = {"message": text} if ok else {"error": text}
                        else:
                            for _ in range(MANY_ROUNDS):
                                res = self._sync(sid, trigger, background=True)
                                cur = self.get(sid)
                                if res.get("error") or cur is None or not cur.backfill_pending or ev.is_set():
                                    break
                except Exception as e:  # ein Konto darf die übrigen nie aufhalten
                    log.exception("Datenquelle %s: Aktualisierung fehlgeschlagen", sid)
                    res = {"error": describe_error(e)[1]}
                finally:
                    try:
                        cur = self.get(sid)
                        if cur is not None:
                            p = dict(cur.progress)
                            p.update({"running": False, "finished_at": iso(_now()), "ok": not res.get("error"),
                                      "result": res.get("error") or res.get("message") or "",
                                      "batch_id": res.get("batch_id"), "cancelled": ev.is_set()})
                            p.pop("cancel_requested", None)
                            self._set_progress(sid, p, force=True)
                    finally:
                        _release(sid)
            finally:
                with lock:
                    running.pop(sid, None)
                    counter["done"] += 1
                    extra: dict[str, Any] = {}
                    if res.get("error"):
                        extra["errors"] = [*state.get("errors", []), f"{name}: {res['error']}"][-10:]
                    publish(**extra)

        def worker(group: list[int]) -> None:
            try:
                for sid in group:
                    if _BATCH_CANCEL.is_set():
                        with lock:
                            counter["skipped"] += 1
                        continue
                    one(sid)
            finally:
                self.db.close_thread_conn()

        try:
            queue = list(groups.values())
            with ThreadPoolExecutor(max_workers=max(1, min(BATCH_WORKERS, len(queue))),
                                    thread_name_prefix="ds-sync-batch") as pool:
                for fut in [pool.submit(worker, g) for g in queue]:
                    try:
                        fut.result()
                    except Exception:  # pragma: no cover - Absicherung
                        log.exception("Sammelaktualisierung: Anbietergruppe abgebrochen")
        finally:
            with lock:
                state.update(running=False, current=None, current_id=None, current_ids=[], finished_at=iso(_now()),
                             cancelled=_BATCH_CANCEL.is_set(), done=counter["done"])
                if counter["skipped"]:
                    state["skipped"] = counter["skipped"]
                state.pop("cancel_requested", None)
            try:
                self.ctx.settings.set(BATCH_KEY, state)
            finally:
                _BATCH_LOCK.release()
                self.db.close_thread_conn()

    def batch_progress(self) -> dict[str, Any]:
        """Stand der Aktualisierung mehrerer Konten; ohne Lebenszeichen gilt sie als abgebrochen."""
        v = self.ctx.settings.get(BATCH_KEY)
        p = dict(v) if isinstance(v, dict) else {}
        if p.get("running"):
            seen = parse_iso(p.get("updated_at"))
            if seen is None or (_now() - seen).total_seconds() > PROGRESS_STALE_S:
                p.update(running=False, stale=True)
        # Gesamtfortschritt: erledigte Konten + Anteil des laufenden (einheitliche Anzeige, app.progress)
        total = int(p.get("total") or 0)
        cur_pct = 0.0
        cur = self.get(int(p["current_id"])) if p.get("running") and p.get("current_id") else None
        if cur is not None and cur.progress.get("running"):
            cur_pct = float(view(cur.progress).get("pct") or 0)
            p["current_phase"] = cur.progress.get("phase_label") or cur.progress.get("stage")
        done = int(p.get("done") or 0)
        p["pct"] = round(min(100.0, (done + cur_pct / 100) / total * 100), 1) if total else 0.0
        if not p.get("running") and total and done >= total:
            p["pct"] = 100.0
        return p

    # -- Gruppen -------------------------------------------------------------------------------------------
    def groups(self) -> list[str]:
        return sorted({d.group for d in self.list() if d.is_wallet and d.group}, key=str.lower)

    def rename_group(self, old: str, new: str) -> list[str]:
        """Gruppe umbenennen bzw. mit einer bestehenden zusammenführen – nur die Zuordnung, Konten bleiben
        unverändert (eigene Chain, Adressen, Vorgänge, Synchronisierung)."""
        old = re.sub(r"\s+", " ", old or "").strip()
        new = re.sub(r"\s+", " ", new or "").strip()
        if not old:
            return ["Gruppe nicht gefunden."]
        if new and not _GROUP_RE.match(new):
            return ["Gruppenname ungültig (höchstens 40 Zeichen)."]
        cur = self.db.x("UPDATE data_source SET wallet_group=?, updated_at=? WHERE kind='wallet' AND wallet_group=?",
                        (new or None, iso(_now()), old))
        if not cur.rowcount:
            return ["Gruppe nicht gefunden."]
        log.info("Wallet-Gruppe umbenannt: %s → %s (%d Konten)", old, new or "–", cur.rowcount)
        return []

    def set_group(self, sid: int, group: str) -> list[str]:
        ds = self.get(sid)
        if ds is None or not ds.is_wallet:
            return ["Wallet-Konto nicht gefunden."]
        g = re.sub(r"\s+", " ", group or "").strip()
        if g and not _GROUP_RE.match(g):
            return ["Gruppenname ungültig (höchstens 40 Zeichen)."]
        self.db.x("UPDATE data_source SET wallet_group=?, updated_at=? WHERE id=?", (g or None, iso(_now()), sid))
        return []

    def _set_progress(self, sid: int, data: dict[str, Any], force: bool = False) -> None:
        now = _now()
        last = self._progress_written.get(sid)
        if not force and last is not None and (now - last).total_seconds() < 1.0:
            return
        self._progress_written[sid] = now
        data = {**data, "updated_at": iso(now)}
        self.db.x("UPDATE data_source SET progress_json=? WHERE id=?",
                  (json.dumps(data, ensure_ascii=False, default=str), sid))

    def _prepare(self, ds: DataSource, conn: K.Connector, track: bool = True) -> Progress | None:
        """Fortschritt (einheitlich, app.progress) und Nutzungszähler an den Connector anbinden. ``track=False``
        (Verbindungstest): nur Nutzungszähler, kein Fortschritt."""
        sid = int(ds.id)
        if not track:
            self._bind_usage(ds, conn)
            return None
        prog = Progress(lambda p: self._set_progress(sid, p, force=True), "Synchronisierung", source=ds.name,
                        unit="Vorgänge", phases=["prepare", "fetch", "process", "reconcile", "save"])
        prog.phase("prepare", text="Zugang und Abrufstand")

        def progress(stage: str, done: int, total: int | None, text: str) -> None:
            if prog.phase_key != "fetch":
                prog.phase("fetch")
            prog.update(done, total, f"{stage}: {text}" if text else stage)

        conn.progress = progress
        self._bind_usage(ds, conn)
        return prog

    def _bind_usage(self, ds: DataSource, conn: K.Connector) -> None:
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
        if kind == "cancelled":  # vom Nutzer abgebrochen: kein Fehlerzustand der Quelle, Zeitplan läuft weiter
            stamp = iso(started)
            self.db.x("UPDATE data_source SET last_run_at=?, next_run_at=?, updated_at=? WHERE id=?",
                      (stamp, iso(nxt) if nxt else None, stamp, ds.id))
            self._finish_run(run_id, "error", msg)
            log.info("Datenquelle %s: Abruf abgebrochen", ds.name)
            return {"error": msg, "kind": kind, "cancelled": True}
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

    def _open_rows(self, sid: int) -> list[Any]:
        """Zeilen mit Ereigniskennung in offenen Prüf-Stapeln dieser Datenquelle (nicht des ganzen Anbieters: ein
        zweites Konto desselben Anbieters wird nie blockiert; doppelt verbunden schützt die Kennung beim Übernehmen)."""
        return self.db.q(
            "SELECT r.id, r.batch_id, r.status, r.decision, r.value_in, r.fee_in, r.pair_ok, r.tx_id, r.rec_json, "
            "r.event_key FROM csv_row r JOIN csv_batch b ON b.id = r.batch_id WHERE b.kind='sync' AND "
            "b.datasource_id=? AND b.status IN ('preview', 'partial') AND r.event_key IS NOT NULL", (sid,))

    def _present(self, sid: int) -> dict[str, bool]:
        """Ereignis → wartet es noch auf eine Entscheidung? Enthalten sind alle Ereignisse offener Prüf-Stapel der
        Quelle – auch bekannte und ignorierte, damit ein erneuter Abruf sie nicht ein zweites Mal ablegt."""
        out: dict[str, bool] = {}
        for r in self._open_rows(sid):
            out[r["event_key"]] = out.get(r["event_key"], False) or r["status"] not in DONE_ROWS
        return out

    def _repair(self, sid: int, recs: list[Any]) -> dict[str, int]:
        """Prüfzeilen älterer Auswertungen ersetzen – je Ereignis, das dieser Abruf neu geliefert hat und dessen
        Zeilen sich geändert haben (andere Parser-Version oder andere Daten des Anbieters).

        Ersetzt werden nur unbearbeitete Zeilen (keine Entscheidung, keine Eingabe, nichts übernommen); bearbeitete
        bleiben unverändert und werden gezählt („Veraltete Zeilen neu auswerten“ im Prüf-Stapel). „Dauerhaft
        ignorieren“ hängt am Ereignis und gilt für die neuen Zeilen weiter. Wiederholbar: unveränderte Ereignisse
        bleiben stehen, ersetzte werden genau einmal neu abgelegt."""
        from app.csvimport.service import csv_service, rec_from_json

        new: dict[str, list[Any]] = defaultdict(list)
        for r in recs:
            if r.event_key:
                new[r.event_key].append(r)
        old: dict[str, list[Any]] = defaultdict(list)
        for row in self._open_rows(sid):
            if row["event_key"] in new:
                old[row["event_key"]].append(row)
        drop: list[int] = []
        batches: Counter[int] = Counter()  # Stapel → ersetzte Ereignisse
        replaced = kept = 0
        for key, rows in old.items():
            if any(r["status"] in ("committed", "merged", "linked") or r["tx_id"] for r in rows):
                continue  # (teilweise) übernommen bzw. verknüpft – bleibt; die neue Auswertung erkennt es als bekannt
            if _fingerprint(rec_from_json(r["rec_json"]) for r in rows) == _fingerprint(new[key]):
                continue
            if any(r["decision"] is not None or r["value_in"] is not None or r["fee_in"] is not None
                   or r["pair_ok"] is not None for r in rows):
                kept += 1
                continue
            drop += [int(r["id"]) for r in rows]
            batches.update({int(r["batch_id"]) for r in rows})
            replaced += 1
        if drop:
            with self.db.transaction() as c:
                for i in range(0, len(drop), 500):
                    part = drop[i:i + 500]
                    c.execute(f"DELETE FROM csv_row WHERE id IN ({','.join('?' * len(part))})", part)
            csv = csv_service(self.ctx)
            for bid in sorted(batches):
                n = self.db.scalar("SELECT COUNT(*) FROM csv_row WHERE batch_id=?", (bid,), default=0)
                if not n:
                    csv.discard(bid, rewind=False)
                    continue
                summ = json.loads(self.db.scalar("SELECT summary_json FROM csv_batch WHERE id=?", (bid,)) or "{}")
                summ.update(recs=n, rows_read=n, events=self.db.scalar(
                    "SELECT COUNT(DISTINCT event_key) FROM csv_row WHERE batch_id=?", (bid,), default=0))
                summ["replaced"] = int(summ.get("replaced", 0)) + batches[bid]
                self.db.x("UPDATE csv_batch SET summary_json=? WHERE id=?", (json.dumps(summ), bid))
                csv.evaluate(bid)  # Transfer-Vorschläge und Status der verbliebenen Zeilen neu bewerten
            log.info("Datenquelle %s: %d Vorgänge mit älterer Auswertung ersetzt", sid, replaced)
        return {"replaced": replaced, "kept": kept}

    def _parser_version(self, sid: int) -> int | None:
        ds = self.get(sid)
        conn = K.connector_for(ds.provider) if ds is not None else None
        return getattr(conn, "parser_version", None)

    def _outdated_rows(self, sid: int, bid: int | None = None) -> dict[str, list[Any]]:
        """Nicht übernommene Ereignisse offener Prüf-Stapel, deren Zeilen eine ältere Auswertung tragen."""
        version = self._parser_version(sid)
        if not version:
            return {}
        by_event: dict[str, list[Any]] = defaultdict(list)
        for r in self._open_rows(sid):
            if bid is None or int(r["batch_id"]) == bid:
                by_event[r["event_key"]].append(r)
        return {k: rows for k, rows in by_event.items()
                if not any(r["status"] in ("committed", "merged", "linked") or r["tx_id"] for r in rows)
                and any(_parser_of(r["rec_json"]) < version for r in rows)}

    def outdated(self, sid: int, bid: int | None = None) -> int:
        """Anzahl Ereignisse mit älterer Auswertung (nur Anbieter mit Versionsführung)."""
        return len(self._outdated_rows(sid, bid))

    def reset_outdated(self, sid: int, bid: int | None = None) -> int:
        """Eingaben an Zeilen älterer Auswertungen zurücksetzen – nur auf ausdrücklichen Wunsch (Prüf-Stapel →
        „Veraltete Zeilen neu auswerten“): Übernehmen ja/nein, EUR-Wert, Gebühr, Transfer-Bestätigung. Übernommene
        Ereignisse und „dauerhaft ignorieren“ (gespeichert je Ereignis) bleiben unberührt; der folgende Abruf ersetzt
        die Zeilen durch die aktuelle Auswertung."""
        from app.csvimport.service import csv_service

        stale = self._outdated_rows(sid, bid)
        ids = [int(r["id"]) for rows in stale.values() for r in rows]
        if not ids:
            return 0
        with self.db.transaction() as c:
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                c.execute("UPDATE csv_row SET decision=NULL, value_in=NULL, fee_in=NULL, pair_ok=NULL WHERE id IN "
                          f"({','.join('?' * len(part))})", part)
        csv = csv_service(self.ctx)
        for b in sorted({int(r["batch_id"]) for rows in stale.values() for r in rows}):
            csv.evaluate(b)
        log.info("Datenquelle %s: Eingaben an %d Vorgängen älterer Auswertung zurückgesetzt", sid, len(stale))
        return len(stale)

    def refetch(self, sid: int) -> dict[str, Any]:
        """Vollständig neu abrufen: Abrufstand verwerfen und sofort synchronisieren – unbearbeitete Prüfzeilen
        älterer Auswertungen werden dabei ersetzt, übernommene Buchungen und Entscheidungen bleiben."""
        if not self.reset_cursor(sid):
            return {"error": "Datenquelle nicht gefunden."}
        return self.sync(sid, "manual")

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
        prog = self._prepare(ds, conn)
        assert prog is not None
        try:
            secret = self._secret(ds, conn)
            cursor = json.loads(ds.cursor_json) if ds.cursor_json else None
            prog.phase("fetch", text=f"Abruf bei {ds.provider_label}")
            with get_redactor().temporary(secret.values()):
                res = conn.fetch(ds.config(), secret, cursor)
            prog.phase("process", len(res.events), "Vorgänge werden ausgewertet")
            recs = self._normalize(ds, res)
            prog.update(len(res.events), len(res.events))
        except Exception as e:  # Anbieter-/Netzwerk-/Vertragsfehler → Anzeige ohne Geheimnisse
            return self._fail(ds, run_id, e, secret.values(), started, nxt)
        with _INGEST_LOCK:  # Abgleich/Übernahme quellenübergreifend nacheinander (Abrufe laufen parallel)
            return self._process(sid, ds, conn, res, recs, run_id, secret, started, stamp, nxt, prog)

    def _process(self, sid: int, ds: DataSource, conn: K.Connector, res: K.FetchResult, recs: list[Any],
                 run_id: int, secret: K.Secret, started: datetime, stamp: str, nxt: datetime | None,
                 prog: Progress) -> dict[str, Any]:
        """Abgerufene Vorgänge abgleichen, in den Prüf-Stapel legen bzw. übernehmen und den Lauf abschließen."""
        from app.csvimport.service import csv_service, rec_to_json

        csv = csv_service(self.ctx)
        try:  # ältere Auswertungen je Ereignis ersetzen – auch wenn der Abruf unvollständig war
            repair = self._repair(sid, recs) if conn.parser_version else {"replaced": 0, "kept": 0}
        except Exception as e:
            log.exception("Datenquelle %s: Ersetzen älterer Prüfzeilen fehlgeschlagen", ds.name)
            return self._fail(ds, run_id, e, secret.values(), started, nxt)
        present = self._present(sid)
        fresh = [r for r in recs if (r.event_key or "") not in present]
        prog.phase("reconcile", len({r.event_key for r in fresh}), "Abgleich mit Buchungen und Prüf-Stapel")
        n_waiting = len({r.event_key for r in recs if present.get(r.event_key or "")})
        counts: dict[str, int] = defaultdict(int)
        committed = 0
        linked = 0
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
                if ds.auto_commit:  # Stufe A vor dem Verwerfen: bereits vorhandene Vorgänge mit Herkunft verknüpfen
                    from app.csvimport.batch import auto_link

                    linked += auto_link(csv, bid)
                open_rows = self.db.scalar("SELECT COUNT(*) FROM csv_row WHERE batch_id=? AND status NOT IN "
                                           "('known', 'ignored', 'committed', 'merged', 'linked')", (bid,), default=0)
                if not open_rows and not self.db.scalar("SELECT COUNT(*) FROM csv_row WHERE batch_id=? AND status "
                                                        "IN ('committed', 'merged', 'linked')", (bid,), default=0):
                    csv.discard(bid)  # nichts Neues – kein leerer Prüf-Stapel
                    bid = None
            if bid is not None:  # Konto des kuratierten Imports übernehmen, solange nichts aufgeteilt wird
                switched = self.adopt_account(ds)
                if switched:
                    ds = self.get(sid) or ds
            prog.phase("save", text="Übernehmen und speichern")
            if ds.auto_commit:  # je Ereignis – auch in offenen Stapeln, sobald sie eindeutig geworden sind
                from app.csvimport.batch import auto_link

                for b in self.pending_batches(sid):
                    csv.ensure_current(int(b["id"]))  # nie nach überholter Auswertung übernehmen
                    # Stufe A: technisch identische Vorgänge nur verknüpfen (Herkunft/Kennungen, keine Buchung)
                    linked += auto_link(csv, int(b["id"]))
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
        if repair["replaced"]:
            parts.append(f"neu ausgewertet {repair['replaced']} (ältere Prüfzeilen ersetzt)")
        if repair["kept"]:
            parts.append(f"{repair['kept']} bearbeitete Vorgänge mit älterer Auswertung unverändert – im Prüf-Stapel "
                         "„Veraltete Zeilen neu auswerten“")
        if n_waiting:
            parts.append(f"wartet bereits auf Prüfung {n_waiting}")
        if committed:
            parts.append(f"übernommen {committed}")
        if linked:
            parts.append(f"automatisch verknüpft {linked} (bereits vorhanden)")
        if res.skipped:
            parts.append("ohne Buchung " + ", ".join(f"{n}× {k}" for k, n in sorted(res.skipped.items())))
        msg = " · ".join(parts) + ("; " + "; ".join(notes) if notes else "")
        resume = more
        coverage = {**res.coverage, "complete": res.complete, "at": stamp, "resume": resume,
                    "gaps": [sanitize_error(g, secret.values()) for g in res.gaps[:20]], "repair": repair}
        # Belege zur Vollständigkeit der Historie: nur aus einem vollständigen Abruf ab Beginn, sonst der letzte Stand
        prev_cov = json.loads(ds.coverage_json or "{}")
        if res.complete and res.coverage.get("mode") == "vollständig":
            coverage["history"] = self._history(sid, recs, stamp)
        elif isinstance(prev_cov, dict) and prev_cov.get("history"):
            coverage["history"] = prev_cov["history"]
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
        # „erfolgreich“ nur nach vollständigem Abruf (bzw. einer Etappe des Erstabrufs) – ein abgebrochener Lauf
        # zieht weder Abrufstand noch Erfolgszeitpunkt vor
        success = stamp if (not partial or more) else ds.last_success_at
        self.db.x("UPDATE data_source SET status=?, last_run_at=?, last_success_at=?, last_error=?, last_error_at=?, "
                  "next_run_at=?, cursor_json=?, coverage_json=?, updated_at=? WHERE id=?",
                  (status, stamp, success, "; ".join(notes) if partial else None, stamp if partial else None,
                   iso(nxt) if nxt else None, cursor_json,
                   json.dumps(coverage, ensure_ascii=False, default=str), stamp, sid))
        self._finish_run(run_id, "partial" if partial else "ok", msg, events=len(res.events),
                         rows_new=counts.get("new", 0), rows_known=counts.get("known", 0), rows_overlap=overlap,
                         rows_committed=committed, rows_unclear=counts.get("unclear", 0),
                         rows_ignored=counts.get("ignored", 0), batch_id=bid,
                         detail_json=json.dumps({"skipped": res.skipped, "warnings": notes, "coverage": coverage,
                                                 "waiting": n_waiting}, ensure_ascii=False, default=str))
        log.info("Datenquelle %s synchronisiert: %s", ds.name, msg)
        return {"status": status, "batch_id": bid, "message": msg, "committed": committed, "linked": linked,
                **counts}

    def _history(self, sid: int, recs: list[Any], stamp: str) -> dict[str, Any]:
        """Kennzahlen eines vollständigen Abrufs der Historie: Zeitraum, Vorgänge je Monat, Vorgänge ohne Zeitpunkt
        und übernommene bzw. verknüpfte Vorgänge dieser Quelle, die der Abruf nicht (mehr) liefert."""
        first_ts: dict[str, datetime] = {}
        no_ts: set[str] = set()
        for r in recs:
            key = r.event_key or r.ext_id or ""
            if r.ts_missing:
                no_ts.add(key)
            elif key not in first_ts or r.ts < first_ts[key]:
                first_ts[key] = r.ts
        months: Counter[str] = Counter(to_local_date(ts).strftime("%Y-%m") for ts in first_ts.values())
        delivered = {r.event_key for r in recs if r.event_key}
        committed = {r["event_key"] for r in self.db.q(
            "SELECT DISTINCT event_key FROM journal_tx WHERE datasource_id=? AND status IN ('active', 'merged') AND "
            "event_key IS NOT NULL", (sid,))}
        linked = {r["event_key"] for r in self.db.q(
            "SELECT DISTINCT r.event_key FROM csv_row r JOIN csv_batch b ON b.id = r.batch_id WHERE "
            "b.datasource_id=? AND r.status='linked' AND r.event_key IS NOT NULL", (sid,))}
        missing = sorted((committed | linked) - delivered)
        times = sorted(first_ts.values())
        return {"at": stamp, "events": len(delivered | set(first_ts)), "first": iso(times[0]) if times else None,
                "last": iso(times[-1]) if times else None, "months": dict(sorted(months.items())),
                "no_ts": len(no_ts), "missing": len(missing), "missing_examples": missing[:10],
                "missing_committed": len(set(missing) & committed), "missing_linked": len(set(missing) & linked)}

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

    def run_due(self, wait: bool = True) -> dict[str, Any]:
        """Fällige Quellen abrufen – je Quelle ein eigener Thread, damit ein langsamer Abruf (z. B. Wallet mit
        Ratenlimit) die übrigen nicht aufhält. ``wait=False`` (Zeitplan): nur starten; eine noch laufende Quelle
        wird übersprungen und beim nächsten Takt wieder geprüft."""
        done: dict[str, Any] = {}
        threads: list[threading.Thread] = []
        for ds in self.due():
            sid = int(ds.id)
            ev = _acquire(sid)
            if ev is None:
                done[ds.name] = "läuft bereits"
                continue

            def run(sid: int = sid, ev: threading.Event = ev, name: str = ds.name) -> None:
                try:
                    with K.cancel_scope(ev):
                        res = self._sync(sid, "schedule")
                    done[name] = res.get("status") or res.get("error") or res.get("skipped") or res.get(
                        "unsupported")
                except Exception as e:  # pragma: no cover - Absicherung: eine Quelle hält die übrigen nie auf
                    log.exception("Datenquelle %s: geplanter Abruf fehlgeschlagen", sid)
                    done[name] = describe_error(e)[1]
                finally:
                    _release(sid)
                    self.db.close_thread_conn()

            t = threading.Thread(target=run, name=f"ds-due-{sid}", daemon=True)
            try:
                t.start()
            except Exception:  # pragma: no cover - Thread-Start fehlgeschlagen
                _release(sid)
                raise
            threads.append(t)
            done.setdefault(ds.name, "gestartet")
        if wait:
            for t in threads:
                t.join()
        return {"ran": len(threads), "results": done}


DONE_ROWS = ("known", "ignored", "committed", "merged", "linked")  # Zeilen ohne offene Entscheidung


def _parser_of(rec_json: str) -> int:
    """Version der Auswertung einer gespeicherten Zeile (``raw.parser``; ältere Zeilen ohne Angabe → 0)."""
    try:
        raw = json.loads(rec_json).get("raw") or {}
        return int(raw.get("parser") or 0)
    except (ValueError, TypeError, AttributeError):
        return 0


def _fingerprint(recs: Iterable[Any]) -> list[str]:
    """Vergleichsform der Zeilen eines Ereignisses – ohne Zeilennummer; ohne Zeitpunkt auch ohne den
    Abrufzeitpunkt, der dort nur Sortierhilfe ist."""
    from app.csvimport.service import rec_to_json

    out = []
    for r in recs:
        c = dataclasses.replace(r, line=0)
        if c.ts_missing:
            c.ts = datetime(1970, 1, 1, tzinfo=UTC)
        out.append(rec_to_json(c))
    return sorted(out)


def datasource_service(ctx: Any) -> DataSourceService:
    return DataSourceService(ctx)
