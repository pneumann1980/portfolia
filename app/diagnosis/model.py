"""Befunde der Diagnose: Art, Status, Belege, Szenario.

Ein Befund trennt, was Portfolia aus den Daten **weiß** (``known``), von dem, was nur **vermutet** wird
(``suspected``). Auswirkungen werden ausschließlich als **Szenario** gezeigt – hypothetisch, nie gebucht.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

# Befundarten (Reihenfolge = Anzeige)
KINDS: dict[str, str] = {
    "duplicate": "Wahrscheinliche Dublette",
    "transfer": "Möglicher interner Transfer",
    "loss": "Ungeklärter Vermögensabgang / möglicher Verlust",
    "asset": "Falsche oder mehrdeutige Asset-Zuordnung",
    "holdings": "Bestand: beobachtet ≠ berechnet",
    "inactive": "Inaktives Konto mit Restbestand",
    "history": "Unvollständige Transaktionshistorie",
    "estimated": "Rekonstruiert oder geschätzt",
    "price": "Fehlender oder veralteter Kurs",
    "migration": "Möglicher Token-Migrationsvorgang",
    "document": "Beleg ergänzt Buchung",
}
KIND_ORDER = {k: i for i, k in enumerate(KINDS)}

# Bereiche der Diagnoseansicht (Kurzwahl) – Befundarten, die zusammen gezeigt werden
SECTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "bestand": ("Bestandsabweichungen", ("holdings",)),
    "dubletten": ("Mögliche Doppelbuchungen", ("duplicate",)),
    "transfers": ("Ungeklärte Transfers", ("transfer",)),
    "inaktiv": ("Inaktive Konten", ("inactive",)),
    "verluste": ("Potenzielle Verluste", ("loss",)),
}

# Einordnung eines ungeklärten Abgangs (Befundart „loss“) – nie aus Inaktivität oder Kursverfall allein
LOSS_CLASS: dict[str, tuple[str, str]] = {
    "A": ("Technischer Buchungsfehler", "Vermögenswert wahrscheinlich weiterhin vorhanden, aber falsch abgebildet"),
    "B": ("Ungeklärter Abgang", "es fehlen Informationen für eine sichere Bewertung"),
    "C": ("Dokumentierter Verlust", "konkretes Verlustereignis dokumentiert (z. B. als Verlust/Diebstahl gebucht) – "
                                    "eine Kennzeichnung des Kontos als kompromittiert allein genügt nicht"),
}

# Befundstatus: wie belastbar ist die Aussage?
STATUS: dict[str, str] = {
    "belegt": "folgt unmittelbar aus den Daten (z. B. Kennzeichnung, fehlender Kurs, Differenz zur Börse)",
    "wahrscheinlich": "mehrere unabhängige Belege, in den Daten keine legitime Erklärung erkennbar",
    "verdacht": "Muster passt, aber ein entscheidender Beleg fehlt oder eine legitime Erklärung ist möglich",
    "hinweis": "zur Einordnung – kein Fehler festgestellt",
}
STATUS_BADGE = {"belegt": "crit", "wahrscheinlich": "warn", "verdacht": "info", "hinweis": ""}

# Stand eines Befunds bzw. Einzelvorgangs aus Sicht der Nutzerentscheidung (Bezeichnung, Badge)
CASE_STATES: dict[str, tuple[str, str]] = {
    "offen": ("offen", "warn"),
    "spaeter": ("später prüfen", "info"),
    "ungeklaert": ("ungeklärt belassen", ""),
    "abgelehnt": ("abgelehnt – kein Duplikat bzw. Vorschlag falsch", ""),
    "uebernommen": ("bestätigt und übernommen", "good"),
    "rueckgaengig": ("rückgängig gemacht", ""),
    "ueberholt": ("durch Datenänderung überholt – erneut prüfen", "warn"),
}
MARK_STATE = {"defer": "spaeter", "dismiss": "ungeklaert", "reject": "abgelehnt"}

# Bestandsabgleich je Konto und Asset
HOLDING_STATUS: dict[str, tuple[str, str, str]] = {
    # Schlüssel: (Bezeichnung, Badge, Erklärung)
    "extern_ok": ("mit externer Quelle abgestimmt", "good",
                  "Börse bzw. Blockchain meldet denselben Bestand; Abruf aktuell und ohne erkannte Lücke"),
    "extern_diff": ("Differenz zur externen Quelle", "crit",
                    "Börse bzw. Blockchain meldet einen anderen Bestand (aktueller, vollständiger Abruf)"),
    "extern_unsicher": ("extern nicht bestätigt", "warn",
                        "externer Bestand liegt vor, aber der Abruf ist veraltet oder unvollständig – kein „stimmt“"),
    "intern_ok": ("intern konsistent", "info",
                  "aus den Buchungen reproduzierbar (Soll laut kuratiertem Import) – nicht extern geprüft"),
    "intern_app": ("Import-Soll + Änderungen in Portfolia", "info",
                   "die Buchungen des Imports ergeben das Soll; die Abweichung stammt vollständig aus Änderungen in "
                   "Portfolia (ausgeblendete, geänderte oder ergänzte Buchungen, Sparplan-Schätzungen) – nicht extern "
                   "geprüft"),
    "intern_diff": ("intern abweichend", "warn", "Buchungen ergeben einen anderen Bestand als das Soll des Imports"),
    "ref_ok": ("mit Referenzbestand abgestimmt", "good",
               "vom Nutzer hinterlegter Referenzbestand (z. B. Kontoauszug) = Soll aus den Buchungen zum selben "
               "Stichtag"),
    "ref_diff": ("Differenz zum Referenzbestand", "crit",
                 "Soll aus den Buchungen zum Stichtag des Referenzbestands weicht vom hinterlegten Bestand ab"),
    "offen": ("ohne Abgleich", "", "weder externer Bestand noch Soll- oder Referenzbestand vorhanden – ein "
                                   "fehlender Referenzbestand gilt als unbekannt, nicht als 0"),
}


@dataclass
class TxRef:
    """Betroffene Buchung (Anzeige, unverändert)."""

    tx_id: str
    ts: datetime
    type: str
    tag: str | None
    out: tuple[str, str, Decimal] | None  # (Konto, Asset, Menge)
    inn: tuple[str, str, Decimal] | None
    fee: tuple[str, Decimal] | None
    value_eur: Decimal | None
    source: str | None
    source_ref: str | None
    origin: str
    hashes: list[str] = field(default_factory=list)
    event_index: str | None = None
    flags: list[str] = field(default_factory=list)
    note: str | None = None
    edit_url: str | None = None  # Bearbeiten im Journal (Import-Buchung: Überlagerung; App-Buchung: falls bearbeitbar)


@dataclass
class Scenario:
    """Hypothetische Auswirkung – nur Anzeige, niemals gebucht."""

    text: str
    rows: list[tuple[str, str, str]] = field(default_factory=list)  # (Bezeichnung, aktuell, im Szenario)


@dataclass
class Finding:
    kind: str
    status: str
    title: str
    known: list[str] = field(default_factory=list)
    suspected: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    txs: list[TxRef] = field(default_factory=list)
    pairs: list[tuple[TxRef, TxRef, str]] = field(default_factory=list)  # Gegenüberstellung + Begründung
    accounts: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    positions: list[tuple[str, str]] = field(default_factory=list)  # betroffene (Konto, Asset) – Bestandsabgleich
    scenario: Scenario | None = None
    decision: str | None = None  # Entscheidung des Nutzers, die eine spätere Korrektur bräuchte
    priority: int = 2  # 1 = zuerst ansehen … 3 = Einordnung
    weight: Decimal = Decimal(0)  # Sortierung innerhalb gleicher Priorität (z. B. betroffener Wert)
    key: str = ""  # stabile Kennung (aus Art und betroffenen Buchungen/Schlüsseln)
    data: dict[str, Any] = field(default_factory=dict)  # maschinenlesbar für Empfehlungen (Buchungen, Asset, Coin …)
    # Einzelvorgänge (M28): ein Sammelbefund zeigt die Übersicht, jeder enthaltene Vorgang ist ein eigener, einzeln
    # prüf- und freigebbarer Befund (``parent`` = Sammelbefund). ``state`` = Stand der Nutzerentscheidung (Anzeige).
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    state: str = ""

    @property
    def id(self) -> str:
        return "f-" + hashlib.sha256(f"{self.kind}|{self.key or self.title}".encode()).hexdigest()[:12]

    @property
    def kind_label(self) -> str:
        return KINDS[self.kind]

    def sort_key(self) -> tuple[Any, ...]:
        return (self.priority, KIND_ORDER[self.kind], -self.weight, self.title, self.key)


@dataclass
class HoldingRow:
    account: str
    asset: str
    name: str
    computed: Decimal
    observed: Decimal | None = None
    observed_at: datetime | None = None
    observed_by: str | None = None  # Datenquelle bzw. Anbieter
    observed_state: str | None = None  # Zustand des Abrufs (z. B. „vollständig synchronisiert“)
    expected: Decimal | None = None  # Soll laut kuratiertem Import (holdings_check)
    expected_as_of: date | None = None
    status: str = "offen"
    explanations: list[str] = field(default_factory=list)
    # Soll-Ist zum selben Stichtag (M27): Ist = beobachteter bzw. vom Nutzer bestätigter Bestand, Soll = Bestand aus
    # allen wirksamen Buchungen bis zu genau diesem Zeitpunkt
    platform: str = ""  # Börse/Wallet (Broker bzw. Anbieter der Datenquelle)
    identity: str = ""  # eindeutige Asset-Identität (Asset-ID, ggf. Netzwerk und Contract)
    computed_at_obs: Decimal | None = None  # Soll zum Abrufzeitpunkt des beobachteten Bestands
    reference: Decimal | None = None  # vom Nutzer hinterlegter Referenzbestand (Prüfwert, keine Buchung)
    reference_at: date | None = None
    reference_ts: datetime | None = None  # exakter Zeitpunkt des Referenzbestands (sonst Ende des Stichtags)
    reference_note: str = ""
    soll_at_ref: Decimal | None = None  # Soll zum Stichtag des Referenzbestands
    last_sync: datetime | None = None  # letzte erfolgreiche Synchronisation der Datenquelle(n) des Kontos
    quality: str = ""  # Datenqualität (Abrufzustand bzw. Herkunft des Solls)
    confidence: str = ""  # Sicherheit der Diagnose (belegt | wahrscheinlich | verdacht | hinweis)
    tx_ids: list[str] = field(default_factory=list)  # Buchungen der erkannten möglichen Ursachen
    families: list[tuple[str, Decimal, int, datetime, datetime]] = field(default_factory=list)  # je Quelle

    @property
    def ref_diff(self) -> Decimal | None:
        """Ist (Referenz) − Soll zum selben Stichtag."""
        return self.reference - self.soll_at_ref if self.reference is not None and self.soll_at_ref is not None \
            else None

    @property
    def diff(self) -> Decimal | None:
        """Ist (beobachtet) − Soll zum Abrufzeitpunkt (ohne Buchungen danach)."""
        if self.observed is None:
            return None
        return self.observed - (self.computed_at_obs if self.computed_at_obs is not None else self.computed)

    @property
    def internal_diff(self) -> Decimal | None:
        return self.expected - self.computed if self.expected is not None else None


@dataclass
class Report:
    findings: list[Finding]
    holdings: list[HoldingRow]
    generated_for: date
    stats: dict[str, int] = field(default_factory=dict)  # Kennzahlen der Prüfung (z. B. legitime Hash-Gruppen)
    snapshot: Any = field(default=None, repr=False, compare=False)  # Grundlage (für Empfehlungen und Vorschau)
    index: Any = field(default=None, repr=False, compare=False)  # Nachschlage-Index der Regeln (Buchungsanzeige)
    # Abweichungsfenster je (Konto, Asset) mit mindestens einem Referenzbestand (M29, rein lesend)
    traces: dict[tuple[str, str], Any] = field(default_factory=dict, repr=False, compare=False)

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {k: {} for k in KINDS}
        for f in self.findings:
            out[f.kind][f.status] = out[f.kind].get(f.status, 0) + 1
        return out

    def signature(self) -> list[tuple[str, str, str]]:
        """Kurzform für Vergleiche (gleiche Daten → gleiche Befunde)."""
        return [(f.id, f.kind, f.status) for f in self.findings]

    def holding_counts(self) -> dict[str, int]:
        out = dict.fromkeys(HOLDING_STATUS, 0)
        for h in self.holdings:
            out[h.status] = out.get(h.status, 0) + 1
        return out

    def by_id(self, fid: str) -> Finding | None:
        cache = getattr(self, "_by_id", None)
        if cache is None or len(cache) != len(self.findings):
            cache = {f.id: f for f in self.findings}
            self._by_id = cache
        return cache.get(fid)

    @property
    def top(self) -> list[Finding]:
        """Befunde ohne Einzelvorgänge (Übersicht)."""
        return [f for f in self.findings if f.parent is None]
