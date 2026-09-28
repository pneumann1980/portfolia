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
* ``cursor`` ist ein kleines JSON-Objekt (z. B. letzter Zeitstempel/Block) für inkrementelle Abrufe; er wird nur
  nach einem erfolgreichen Lauf gespeichert. Überlappende Abrufe sind unschädlich (bekannte IDs werden erkannt).
  Bei ``complete=False`` (Limit, einzelne Endpunkte gestört) zeigt er nur bis dorthin, wo die Daten lückenlos sind –
  der nächste Lauf setzt dort fort. Höchstens ``MAX_EVENTS`` (50.000) Vorgänge je Lauf; längere Historien in
  Etappen (``complete=False`` + Cursor).
* Drosselung: ``ConnectorError("rate_limit", …, retry_after_s=…)`` verschiebt den nächsten geplanten Lauf
  entsprechend.
* Fehler als :class:`ConnectorError` mit deutschem, geheimnisfreiem Text. Andere Ausnahmen werden vom Aufrufer
  in eine allgemeine Meldung übersetzt und bereinigt.
* Übertragen werden nur die für den Abruf nötigen Daten (Adresse bzw. API-Schlüssel an den jeweiligen Anbieter) –
  keine Bestände, Werte oder Kontonamen des Portfolios an Dritte.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from app.csvimport.model import Rec

EVENT_KEY_RE = re.compile(r"^[a-z0-9_]+:[^\s#]{1,200}$")
CREDENTIAL_RE = re.compile(r"^PORTFOLIA_DS_[A-Z0-9_]{1,50}$")

# Fehlerarten → Anzeige (ergänzt die Meldung des Connectors)
ERROR_KINDS = {"auth": "Zugangsdaten abgelehnt", "rate_limit": "Anbieter drosselt Anfragen",
               "unavailable": "Anbieter nicht erreichbar", "config": "Einstellung unvollständig",
               "data": "Unerwartete Antwort des Anbieters", "unsupported": "Nicht unterstützt"}


class ConnectorError(Exception):
    """Fehler mit Anzeige-Text (deutsch, ohne Geheimnisse)."""

    def __init__(self, kind: str, message: str, retry_after_s: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind if kind in ERROR_KINDS else "data"
        self.message = message
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class SourceConfig:
    """Was ein Connector über die Datenquelle erfährt (ohne Zugangsdaten)."""

    id: int
    kind: str
    provider: str
    name: str
    account: str
    address: str | None = None


class Secret:
    """Zugangsdaten aus einer Umgebungsvariable (oder ``<NAME>_FILE`` für Docker-Secrets); nie im Klartext
    ausgegeben (``repr``/``str`` maskiert)."""

    def __init__(self, ref: str | None) -> None:
        self.ref = ref
        self._value: str | None = None
        if ref and CREDENTIAL_RE.match(ref):
            val = os.environ.get(ref)
            file = os.environ.get(f"{ref}_FILE")
            if (val is None or not val.strip()) and file:
                try:
                    val = Path(file).read_text(encoding="utf-8")
                except OSError:
                    val = None
            self._value = val.strip() if val and val.strip() else None

    @property
    def present(self) -> bool:
        return self._value is not None

    def reveal(self) -> str:
        if self._value is None:
            raise ConnectorError("config", f"Zugangsdaten fehlen: Umgebungsvariable {self.ref or '(nicht angegeben)'} "
                                           "ist nicht gesetzt.")
        return self._value

    def values(self) -> list[str]:
        """Für die Bereinigung von Fehlermeldungen (auch Teile wie Schlüssel/Secret getrennt)."""
        if not self._value:
            return []
        parts = [self._value, *re.split(r"[:;,|\s]+", self._value)]
        return [p for p in parts if len(p) >= 6]

    def __repr__(self) -> str:
        return f"Secret({self.ref!r}, {'gesetzt' if self.present else 'fehlt'})"

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
    cursor: dict[str, Any] | None = None
    complete: bool = True  # False: nur ein Teil abrufbar (z. B. Limit, einzelne Endpunkte gestört)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    ok: bool
    message: str = ""


class Connector(ABC):
    provider: ClassVar[str]  # ID aus app.datasources.providers.PROVIDERS
    label: ClassVar[str]
    needs_credentials: ClassVar[bool] = False

    @abstractmethod
    def check(self, cfg: SourceConfig, secret: Secret) -> CheckResult:
        """Verbindung und Berechtigungen prüfen (nur lesend) – Erfolg ergibt den Status „verbunden“."""

    @abstractmethod
    def fetch(self, cfg: SourceConfig, secret: Secret, cursor: dict[str, Any] | None) -> FetchResult:
        """Vorgänge seit ``cursor`` (bzw. vollständig) abrufen und normalisiert liefern."""


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
