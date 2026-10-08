"""Erweiterbare Connector-Schnittstelle.

Ein Connector holt Vorgänge einer Datenquelle (Börsenkonto, öffentliche Adresse) und liefert sie **normalisiert**:
je Ereignis eine stabile ID und eine oder mehrere Zeilen im Zwischenformat :class:`~app.csvimport.model.Rec`
(Symbole der Quelle, Mengen, Gegenwert, Gebühr, Art/Tag). Buchen, bewerten, prüfen und übernehmen übernimmt die
bestehende Import-Pipeline – ein Connector schreibt nie selbst ins Journal.

Vertrag für Connectoren
-----------------------
* ``event_key`` ist stabil und eindeutig: ``<anbieter>:<native ID>`` in der Form, die auch das CSV-Profil desselben
  Anbieters als Kennung verwendet (z. B. ``kraken:<refid>`` für Trades, ``kraken:<ledger-id>`` für Ein-/Auszahlungen,
  ``coinbase:<ID>``, ``bitpanda:<Transaktions-ID>``). Nur so werden frühere CSV-Importe exakt wiedererkannt. Ist
  die native ID nicht global eindeutig, gehört der Kontobezug in die ID. Wallet-Connectoren setzen zusätzlich
  ``Rec.txhash``; die ID enthält die eigene Adresse (dieselbe Transaktion erscheint in Sender- und Empfänger-Wallet).
* Die Zeilen eines Ereignisses (z. B. Trade + Gebühr in drittem Asset) kommen in **fester Reihenfolge** – die
  Position ist Teil der Kennung (``<event_key>#<zeile>``) und macht wiederholtes Synchronisieren idempotent.
  Wallet-Connectoren vergeben stattdessen je Bewegung eine **stabile Unterkennung** in ``Rec.ext_id`` (z. B.
  ``native``, ``fee``, ``t:<kurzhash>#0`` für den ersten von mehreren gleichartigen Token-Transfers) – die Kennung
  ``<event_key>#<unterkennung>`` bleibt dann auch gleich, wenn der Anbieter später eine weitere Bewegung desselben
  Hashes liefert.
* ``cursor`` ist ein kleines JSON-Objekt (z. B. letzter Zeitstempel/Block) für inkrementelle Abrufe; er wird nur
  nach einem erfolgreichen Lauf gespeichert. Überlappende Abrufe sind unschädlich (bekannte IDs werden erkannt).
  Bei ``complete=False`` (Limit, einzelne Endpunkte gestört) zeigt er nur bis dorthin, wo die Daten lückenlos sind –
  der nächste Lauf setzt dort fort. Höchstens ``MAX_EVENTS`` (50.000) Vorgänge je Lauf; längere Historien in
  Etappen (``complete=False`` + ``resume=True`` + Cursor): Ein Fortsetzungspunkt darf nur so weit zeigen, wie alle
  Vorgänge davor in *diesem* Ergebnis enthalten oder früher geliefert sind – er wird erst gespeichert, nachdem die
  Vorgänge im Prüf-Stapel stehen.
* Drosselung: ``ConnectorError("rate_limit", …, retry_after_s=…)`` verschiebt den nächsten geplanten Lauf
  entsprechend.
* Der Abrufstand gilt erst als verarbeitet, wenn alle Vorgänge bis dorthin im Prüf-Stapel stehen, übernommen oder
  entschieden sind: ``complete=False`` → ``cursor=None`` (nicht vorrücken, nächster Lauf holt erneut ab);
  :meth:`Connector.rewind` setzt ihn zurück, wenn ein Prüf-Stapel verworfen wird.
* Nicht eindeutig abbildbare Vorgänge nie raten: eine Zeile ``Rec(kind=REVIEW, note=<Grund>)`` („ungeklärt“) bzw.
  bei zwar abbildbaren, aber prüfbedürftigen Vorgängen ``Rec.review`` (keine automatische Übernahme).
* Fehler als :class:`ConnectorError` mit deutschem, geheimnisfreiem Text. Andere Ausnahmen werden vom Aufrufer
  in eine allgemeine Meldung übersetzt und bereinigt.
* Übertragen werden nur die für den Abruf nötigen Daten (Adresse bzw. API-Schlüssel an den jeweiligen Anbieter) –
  keine Bestände, Werte oder Kontonamen des Portfolios an Dritte.
"""

from __future__ import annotations

import os
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

from app.csvimport.model import Rec

EVENT_KEY_RE = re.compile(r"^[a-z0-9_]+:[^\s#]{1,200}$")
SUB_ID_RE = re.compile(r"^[A-Za-z0-9:._\-]{1,100}(#\d{1,4})?$")  # stabile Unterkennung einer Bewegung
CREDENTIAL_RE = re.compile(r"^PORTFOLIA_DS_[A-Z0-9_]{1,50}$")

# Fehlerarten → Anzeige (ergänzt die Meldung des Connectors)
ERROR_KINDS = {"auth": "Zugangsdaten abgelehnt", "scope": "Berechtigung fehlt", "expired": "API-Key abgelaufen",
               "key_missing": "API-Key fehlt", "forbidden": "Zugriff verweigert (HTTP 403)",
               "rate_limit": "Anbieter drosselt Anfragen", "unavailable": "Anbieter vorübergehend nicht erreichbar",
               "gone": "Endpunkt nicht mehr unterstützt", "no_data": "Keine Daten beim Anbieter",
               "config": "Einstellung unvollständig", "data": "Unerwartete Antwort des Anbieters",
               "unsupported": "Nicht unterstützt", "cancelled": "Abgebrochen"}


class ConnectorError(Exception):
    """Fehler mit Anzeige-Text (deutsch, ohne Geheimnisse)."""

    def __init__(self, kind: str, message: str, retry_after_s: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind if kind in ERROR_KINDS else "data"
        self.message = message
        self.retry_after_s = retry_after_s


# -- Abbrechen ----------------------------------------------------------------------------------------------
# Ein laufender Abruf prüft an festen Stellen (Fortschrittsmeldung, jede HTTP-Anfrage, jede Wartezeit), ob der Nutzer
# ihn abgebrochen hat. Das Signal gilt je Lauf (Thread des Abrufs); HTTP-Clients übernehmen es beim Anlegen, damit es
# auch in ihren Hilfsthreads wirkt. Ein Abbruch endet wie ein Fehler: der Abrufstand rückt nicht vor.
_CANCEL = threading.local()
CANCEL_TEXT = "Abruf abgebrochen – der Abrufstand bleibt unverändert, der nächste Lauf holt dieselben Vorgänge."


@contextmanager
def cancel_scope(event: threading.Event | None) -> Iterator[None]:
    prev = getattr(_CANCEL, "event", None)
    _CANCEL.event = event
    try:
        yield
    finally:
        _CANCEL.event = prev


def current_cancel() -> threading.Event | None:
    """Abbruchsignal des laufenden Abrufs (``None`` außerhalb eines Laufs)."""
    return getattr(_CANCEL, "event", None)


def check_cancel(event: threading.Event | None = None) -> None:
    ev = event if event is not None else current_cancel()
    if ev is not None and ev.is_set():
        raise ConnectorError("cancelled", CANCEL_TEXT)


def interruptible_sleep(seconds: float, event: threading.Event | None = None) -> None:
    """Warten, das ein Abbruch sofort beendet (sonst wie :func:`time.sleep`)."""
    ev = event if event is not None else current_cancel()
    if ev is None:
        time.sleep(seconds)
        return
    if ev.wait(max(0.0, seconds)):
        raise ConnectorError("cancelled", CANCEL_TEXT)


@dataclass(frozen=True)
class SourceConfig:
    """Was ein Connector über die Datenquelle erfährt (ohne Zugangsdaten)."""

    id: int
    kind: str
    provider: str
    name: str
    account: str
    address: str | None = None
    watch: Mapping[str, Any] = field(default_factory=dict)  # Wallets: Adressen, Kontoschlüssel, Anbieter, …


class Secret:
    """Zugangsdaten – aus dem verschlüsselten Speicher der App (``value``) oder aus einer Umgebungsvariable bzw.
    ``<NAME>_FILE`` (Docker-Secret). Nie im Klartext ausgegeben (``repr``/``str`` maskiert)."""

    def __init__(self, ref: str | None = None, *, value: str | None = None) -> None:
        self.ref = ref
        self.origin: str | None = None  # app | env
        self._value: str | None = None
        if value is not None:
            self._value = value.strip() or None
            self.origin = "app" if self._value else None
        elif ref and CREDENTIAL_RE.match(ref):
            val = os.environ.get(ref)
            file = os.environ.get(f"{ref}_FILE")
            if (val is None or not val.strip()) and file:
                try:
                    val = Path(file).read_text(encoding="utf-8")
                except OSError:
                    val = None
            self._value = val.strip() if val and val.strip() else None
            self.origin = "env" if self._value else None

    @property
    def present(self) -> bool:
        return self._value is not None

    def reveal(self) -> str:
        if self._value is None:
            where = f"Umgebungsvariable {self.ref}" if self.ref else "API-Key"
            raise ConnectorError("config", f"Zugangsdaten fehlen: {where} ist nicht gesetzt.")
        return self._value

    def values(self) -> list[str]:
        """Für die Bereinigung von Fehlermeldungen (auch Teile wie Schlüssel/Secret getrennt)."""
        if not self._value:
            return []
        parts = [self._value, *re.split(r"[:;,|\s]+", self._value)]
        return [p for p in parts if len(p) >= 6]

    def __repr__(self) -> str:
        return f"Secret({self.ref or self.origin!r}, {'gesetzt' if self.present else 'fehlt'})"

    __str__ = __repr__


@dataclass
class SourceEvent:
    event_key: str
    ts: datetime
    lines: list[Rec]
    label: str | None = None


@dataclass
class FetchResult:
    events: list[SourceEvent] = field(default_factory=list)
    cursor: dict[str, Any] | None = None  # None: Abrufstand nicht vorrücken
    complete: bool = True  # False: nur ein Teil abrufbar (z. B. Limit, einzelne Endpunkte gestört)
    warnings: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)  # bewusst ohne Buchung (Grund → Anzahl), sichtbar
    coverage: dict[str, Any] = field(default_factory=dict)  # Zeitraum, Seiten, Grenzen – für die Anzeige
    resume: bool = False  # complete=False, aber ``cursor`` ist ein sicherer Fortsetzungspunkt (Etappen)
    more: bool | None = None  # weitere Etappen ausstehend → bald fortsetzen (Standard: wie ``resume``)
    gaps: list[str] = field(default_factory=list)  # erkannte Lücken dieses Abrufs (→ nie „vollständig“)
    balances: list[Balance] | None = None  # beobachtete Bestände (Plausibilitätsprüfung), None = nicht abgefragt


@dataclass(frozen=True)
class Balance:
    """Beobachteter Bestand eines Assets laut Anbieter (Kennung wie in ``Rec`` – Symbol bzw. Token-Schlüssel)."""

    asset_key: str
    qty: Decimal
    name: str | None = None
    note: str | None = None  # z. B. „inkl. unbestätigter Eingänge“


@dataclass
class CheckResult:
    ok: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)  # je Recht/Endpunkt: {"ok": bool, "text": str}
    balances: list[Balance] | None = None


class Connector(ABC):
    provider: ClassVar[str]  # ID aus app.datasources.providers.PROVIDERS
    label: ClassVar[str]
    needs_credentials: ClassVar[bool] = False
    wallet: ClassVar[bool] = False  # Wallet-Connector (öffentliche Adressen, Schlüssel je Anbieter statt je Quelle)
    # Version der Auswertung (``Rec.raw["parser"]``): Prüfzeilen einer älteren Version werden bei einem Abruf ersetzt,
    # solange niemand sie bearbeitet hat (siehe DataSourceService._repair); None = ohne Versionsführung
    parser_version: ClassVar[int | None] = None
    # vom Dienst gesetzt: Zwischenspeicher für Stammdaten des Anbieters (z. B. Asset-ID → Symbol), spart Abrufe
    catalog: Any = None
    # vom Dienst gesetzt: Fortschritt melden (Phase, erledigt, gesamt bzw. None, Text)
    progress: Callable[[str, int, int | None, str], None] | None = None

    @abstractmethod
    def check(self, cfg: SourceConfig, secret: Secret) -> CheckResult:
        """Verbindung und Berechtigungen prüfen (nur lesend) – Erfolg ergibt den Status „verbunden“."""

    @abstractmethod
    def fetch(self, cfg: SourceConfig, secret: Secret, cursor: dict[str, Any] | None) -> FetchResult:
        """Vorgänge seit ``cursor`` (bzw. vollständig) abrufen und normalisiert liefern."""

    def report(self, stage: str, done: int = 0, total: int | None = None, text: str = "") -> None:
        check_cancel()
        if self.progress is not None:
            self.progress(stage, done, total, text)

    def rewind(self, cursor: dict[str, Any] | None, before: datetime) -> dict[str, Any] | None:
        """Abrufstand so zurücksetzen, dass Vorgänge ab ``before`` erneut geliefert werden (verworfener Prüf-Stapel).
        Standard: vollständig neu abrufen – sicher, weil bekannte Vorgänge erkannt werden."""
        return None


_REGISTRY: dict[str, type[Connector]] = {}


def register(cls: type[Connector]) -> type[Connector]:
    """Connector für einen Anbieter anmelden (Dekorator)."""
    _REGISTRY[cls.provider] = cls
    return cls


def connector_for(provider: str) -> Connector | None:
    cls = _REGISTRY.get(provider)
    return cls() if cls is not None else None


def supported(provider: str) -> bool:
    return provider in _REGISTRY


def unregister(provider: str) -> None:
    """Nur für Tests."""
    _REGISTRY.pop(provider, None)
