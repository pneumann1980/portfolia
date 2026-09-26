"""Länderneutrales Datenmodell und Schnittstelle der Steuer-Regelwerke.

Ein Regelwerk (``RulePack``) kapselt alles Länderspezifische: welche Vorgänge steuerbar sind,
Fristen, Freigrenzen, Formularfelder und den Aufbau der PDF-Dokumente. Zahlenwerte (Freigrenzen,
Pauschbeträge, Basiszins …) liegen je Veranlagungsjahr in einer Parameterdatei und können ohne
Codeänderung über ``/data/tax_rules/<pack>.yaml`` aktualisiert werden.

Alles hier ist rein rechnerisch: keine DB, kein Web, keine externen Anfragen.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.ledger.engine import EngineOptions, LedgerResult, Lot
from app.ledger.models import AssetInfo, Portfolio

ZERO = Decimal(0)
CENT = Decimal("0.01")


def money(v: Decimal | float | int | None) -> Decimal:
    """Auf Cent runden (kaufmännisch)."""
    if v is None:
        return ZERO
    if not isinstance(v, Decimal):
        v = Decimal(str(v))
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


# -- Formatierung de-DE (für Texte, Web und PDF) -------------------------------------------------------

def fmt_num(v: Any, decimals: int = 2) -> str:
    if v is None or v == "":
        return "–"
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    s = f"{d:,.{decimals}f}"
    s = s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return "\u2212" + s[1:] if s.startswith("-") else s


def fmt_eur(v: Any, decimals: int = 2) -> str:
    return "–" if v is None or v == "" else f"{fmt_num(v, decimals)} €"


def fmt_rate(v: Any) -> str:
    """Anteil (0,255) als Prozent mit bis zu 2 Nachkommastellen: „25,5 %“."""
    if v is None or v == "":
        return "–"
    d = (v if isinstance(v, Decimal) else Decimal(str(v))) * 100
    q = d.quantize(CENT).normalize()
    exp = -q.as_tuple().exponent if q.as_tuple().exponent < 0 else 0  # type: ignore[operator]
    return f"{fmt_num(q, int(exp))} %"


def fmt_qty(v: Any) -> str:
    if v is None or v == "":
        return "–"
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    q = d.quantize(Decimal("1e-8")).normalize()
    exp = -q.as_tuple().exponent if q.as_tuple().exponent < 0 else 0  # type: ignore[operator]
    return fmt_num(q, int(exp))


def fmt_date(v: Any) -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, datetime):
        v = v.date()
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, str) and len(v) >= 10 and v[4] == "-":
        try:
            return date.fromisoformat(v[:10]).strftime("%d.%m.%Y")
        except ValueError:
            return v
    return str(v)


def fmt_value(v: Any, kind: str) -> str:
    if kind == "eur":
        return fmt_eur(v)
    if kind == "qty":
        return fmt_qty(v)
    if kind == "date":
        return fmt_date(v)
    if kind == "int":
        return "–" if v is None or v == "" else fmt_num(v, 0)
    if kind == "pct":
        return fmt_rate(v)
    if kind == "bool":
        return "ja" if v else "nein"
    return "" if v is None else str(v)


# ----------------------------------------------------------------------------------------------------
# Ergebnisbausteine (werden von Web-Ansicht und PDF-Renderer gleichermaßen verwendet)
# ----------------------------------------------------------------------------------------------------

@dataclass
class Line:
    label: str
    amount: Decimal | None = None
    note: str = ""
    strong: bool = False
    indent: int = 0
    kind: str = "eur"  # eur | text | pct | int
    text: str = ""  # für kind=text


@dataclass
class Meter:
    """Status einer Frei- oder Höchstgrenze (z. B. Freigrenze § 23 EStG)."""

    id: str
    label: str
    value: Decimal
    limit: Decimal
    kind: str = "freigrenze"  # freigrenze (alles oder nichts) | freibetrag (bis zur Höhe)
    note: str = ""

    @property
    def ratio(self) -> float:
        if self.limit <= 0:
            return 0.0
        return max(0.0, float(self.value / self.limit))

    @property
    def state(self) -> str:
        if self.value <= 0:
            return "ok"
        if self.kind == "freigrenze":
            if self.value >= self.limit:
                return "crit"
            return "warn" if self.ratio >= 0.8 else "ok"
        return "warn" if self.value >= self.limit else "ok"


@dataclass
class Column:
    key: str
    title: str
    kind: str = "text"  # text | date | qty | eur | int | pct | bool
    width: float = 1.0  # relative Breite (PDF)
    total: bool = False  # Summenzeile bilden


@dataclass
class Table:
    id: str
    title: str
    columns: list[Column]
    rows: list[dict[str, Any]]
    note: str = ""
    landscape: bool = False
    totals: dict[str, Any] | None = None

    def with_totals(self, label_key: str | None = None, label: str = "Summe") -> Table:
        tot: dict[str, Any] = {}
        for c in self.columns:
            if c.total:
                tot[c.key] = sum((r.get(c.key) or ZERO for r in self.rows), ZERO)
        if tot:
            key = label_key or self.columns[0].key
            tot[key] = label
            self.totals = tot
        return self


@dataclass
class FormField:
    """Übertragungshilfe: Betrag für ein Feld eines amtlichen Formulars.

    ``line`` (Zeile/Kennzahl) wird nur angezeigt, wenn sie in der Parameterdatei hinterlegt ist – Zeilen
    ändern sich jährlich und werden nicht geraten.
    """

    form: str
    section: str
    field_id: str
    label: str
    amount: Decimal | None
    line: str | None = None
    note: str = ""
    text: str | None = None  # Textfelder (z. B. Bezeichnung)


@dataclass
class Issue:
    severity: str  # info | warning | critical
    code: str
    text: str
    count: int = 1


@dataclass
class Section:
    id: str
    title: str
    lines: list[Line] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class TaxResult:
    pack_id: str
    pack_name: str
    pack_version: str
    params_version: str
    year: int
    params: dict[str, Any]
    options: dict[str, Any]
    summary: list[Line] = field(default_factory=list)
    meters: list[Meter] = field(default_factory=list)
    estimate: list[Line] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    fields: list[FormField] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    has_activity: bool = False
    data: dict[str, Any] = field(default_factory=dict)  # Rohwerte für Tests/Weiterverarbeitung

    def section(self, sid: str) -> Section | None:
        return next((s for s in self.sections if s.id == sid), None)

    def summary_json(self) -> dict[str, Any]:
        return {
            "pack": self.pack_id, "pack_version": self.pack_version, "params_version": self.params_version,
            "year": self.year,
            "summary": [{"label": ln.label, "amount": str(ln.amount) if ln.amount is not None else None}
                        for ln in self.summary],
            "meters": [{"id": m.id, "value": str(m.value), "limit": str(m.limit), "state": m.state}
                       for m in self.meters],
            "issues": len([i for i in self.issues if i.severity != "info"]),
        }


# ----------------------------------------------------------------------------------------------------
# Übersicht (Web-Seite „Steuern & Haltefristen“)
# ----------------------------------------------------------------------------------------------------

@dataclass
class Kpi:
    label: str
    value: Any
    kind: str = "eur"  # eur | date | int | text
    sub: str = ""
    tone: str = ""
    hint: str = ""


@dataclass
class Release:
    date: date
    asset_id: str
    account: str
    qty: Decimal
    value: Decimal
    gain: Decimal


@dataclass
class Overview:
    kpis: list[Kpi] = field(default_factory=list)
    releases: list[Release] = field(default_factory=list)
    release_months: list[dict[str, Any]] = field(default_factory=list)
    positions: Table | None = None
    years: Table | None = None
    meters_by_year: dict[int, list[Meter]] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    has_holding_period: bool = False


# ----------------------------------------------------------------------------------------------------
# Optionen & Dokumente
# ----------------------------------------------------------------------------------------------------

@dataclass
class OptionSpec:
    key: str
    label: str
    kind: str  # bool | choice | amount | percent | text | map
    default: Any
    choices: tuple[tuple[str, str], ...] = ()
    help: str = ""
    per_year: bool = False
    group: str = "Allgemein"


@dataclass
class DocumentSpec:
    id: str
    title: str
    description: str
    default: bool = True


# ----------------------------------------------------------------------------------------------------
# Eingangsdaten (werden vom Service aus Import, Ledger und Kursdaten zusammengestellt)
# ----------------------------------------------------------------------------------------------------

PriceFn = Callable[[str, date], Decimal | None]
YearPricesFn = Callable[[str, int], tuple[tuple[date, Decimal] | None, tuple[date, Decimal] | None]]
FxFn = Callable[[str, date], Decimal | None]


@dataclass
class TaxInput:
    pf: Portfolio
    ledger: LedgerResult
    today: date
    asset_types: dict[str, str] = field(default_factory=dict)  # asset_id → share | etf_equity | … | crypto
    asset_type_source: dict[str, str] = field(default_factory=dict)  # explicit | settings | heuristic | default
    account_kinds: dict[str, str] = field(default_factory=dict)  # Konto → domestic | foreign
    account_kind_source: dict[str, str] = field(default_factory=dict)
    current_prices: dict[str, Decimal] = field(default_factory=dict)  # asset_id → EUR (aktuell)
    price_eur: PriceFn | None = None  # Tagesschlusskurs in EUR am/vor Datum
    year_prices: YearPricesFn | None = None  # (erster, letzter) Schlusskurs eines Kalenderjahres in EUR
    fx_eur: FxFn | None = None  # EUR-Wert einer Einheit Fremdwährung am/vor Datum
    import_meta: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)

    def asset(self, asset_id: str) -> AssetInfo:
        return self.pf.asset(asset_id)

    def snapshot(self, d: date) -> list[Lot]:
        return self.ledger.lot_snapshots.get(d, [])


@dataclass
class ReportMeta:
    created_at: datetime
    app_version: str
    import_id: int | None
    import_file: str | None
    import_sha: str | None
    valuation_date: str | None
    profile: dict[str, Any]


# ----------------------------------------------------------------------------------------------------
# Regelwerk-Schnittstelle
# ----------------------------------------------------------------------------------------------------

class RulePack(ABC):
    """Basisklasse eines Steuer-Regelwerks.

    Unterklassen setzen ``id``, ``name``, ``country`` und ``code_version`` und implementieren die
    abstrakten Methoden. Parameter je Jahr kommen aus ``self.params`` (siehe :mod:`app.tax.params`).
    """

    id: str = ""
    name: str = ""
    country: str | None = None
    code_version: str = "1"
    description: str = ""

    def __init__(self, params: Any) -> None:
        self.params = params  # app.tax.params.ParamSet

    # -- Metadaten -------------------------------------------------------------------------------
    @property
    def version(self) -> str:
        return f"{self.code_version}/{self.params.version}"

    def years(self) -> list[int]:
        return self.params.years()

    def year_params(self, year: int) -> dict[str, Any]:
        return self.params.for_year(year)

    # -- Konfiguration -----------------------------------------------------------------------------
    @abstractmethod
    def option_specs(self) -> list[OptionSpec]: ...

    def defaults(self) -> dict[str, Any]:
        return {o.key: o.default for o in self.option_specs()}

    def engine_options(self, base: EngineOptions, options: dict[str, Any], snapshot_years: list[int]) -> EngineOptions:
        """Ledger-Optionen für die Steuerberechnung (z. B. Verbrauchsfolge je Wallet)."""
        return base

    def holding_end(self, asset: AssetInfo, acq: date) -> date | None:
        """Erster steuerfreier Tag nach Ablauf einer Haltefrist; ``None`` = keine Haltefrist."""
        return None

    # -- Berechnung --------------------------------------------------------------------------------
    @abstractmethod
    def compute(self, inp: TaxInput, year: int, options: dict[str, Any]) -> TaxResult: ...

    @abstractmethod
    def overview(self, inp: TaxInput, options: dict[str, Any]) -> Overview: ...

    # -- Dokumente -----------------------------------------------------------------------------------
    @abstractmethod
    def documents(self) -> list[DocumentSpec]: ...

    @abstractmethod
    def build_document(self, doc_id: str, result: TaxResult, meta: ReportMeta) -> Any:
        """Liefert ein :class:`app.tax.document.Doc` (länderneutral gerendert)."""
