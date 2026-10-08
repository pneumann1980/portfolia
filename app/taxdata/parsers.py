"""Steuerdaten-Dateien → einheitliches internes Modell (:class:`TaxRecord`).

Formate
* **JSON (bevorzugt):** Liste von Datensätzen oder Objekt ``{"taxYear": 2025, "records": [...]}``; Feldnamen wie im
  Modell (camelCase oder snake_case).
* **CSV (Austausch):** Kopfzeile mit Feldnamen des Modells oder deutschen Bezeichnungen (Trennzeichen und Kodierung
  werden erkannt, Dezimalkomma erlaubt).

Weitere Anbieterformate (Blockpit, Koinly, CoinTracking) lassen sich als zusätzliche Parser registrieren
(:data:`PARSERS`) – sie liefern dasselbe Modell. Fehlende Werte sind erlaubt; was sich nicht lesen lässt, wird als
Warnung je Zeile gemeldet und nie geraten.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_RECORDS = 200_000

# Feld des Modells → akzeptierte Namen (klein, ohne Leer-/Sonderzeichen verglichen)
FIELDS: dict[str, tuple[str, ...]] = {
    "tax_year": ("taxyear", "steuerjahr", "jahr", "year"),
    "transaction_id": ("transactionid", "txid", "portfoliatxid", "buchungsid", "transaktionsid"),
    "external_id": ("externalid", "externeid", "refid", "reference", "referenz", "sourceid"),
    "asset": ("asset", "symbol", "coin", "waehrung", "währung", "wertpapier"),
    "quantity": ("quantity", "amount", "menge", "anzahl", "stueck", "stück"),
    "acquisition_date": ("acquisitiondate", "boughtdate", "kaufdatum", "anschaffungsdatum", "erwerbsdatum"),
    "disposal_date": ("disposaldate", "solddate", "date", "verkaufsdatum", "veraeusserungsdatum",
                      "veräußerungsdatum", "datum"),
    "acquisition_cost": ("acquisitioncost", "costbasis", "cost", "anschaffungskosten", "einstand"),
    "disposal_value": ("disposalvalue", "proceeds", "veraeusserungserloes", "veräußerungserlös", "erloes", "erlös"),
    "holding_period": ("holdingperiod", "holdingperioddays", "haltedauer", "haltedauertage"),
    "taxable": ("taxable", "steuerpflichtig", "steuerbar"),
    "gain_loss": ("gainloss", "gain", "pnl", "gewinnverlust", "gewinn", "ergebnis"),
    "tax_category": ("taxcategory", "category", "kategorie", "steuerkategorie", "art"),
    "source": ("source", "quelle", "exchange", "boerse", "börse", "wallet"),
    "comment": ("comment", "note", "kommentar", "notiz", "bemerkung"),
}
_ALIAS = {a: f for f, names in FIELDS.items() for a in (*names, f.replace("_", ""))}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]", "", str(name).strip().lower())


@dataclass
class TaxRecord:
    line: int
    tax_year: int | None = None
    transaction_id: str | None = None
    external_id: str | None = None
    asset: str | None = None
    quantity: Decimal | None = None
    acquisition_date: date | None = None
    disposal_date: date | None = None
    acquisition_cost: Decimal | None = None
    disposal_value: Decimal | None = None
    holding_period_days: int | None = None
    taxable: bool | None = None
    gain_loss: Decimal | None = None
    tax_category: str | None = None
    source: str | None = None
    comment: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ParseResult:
    records: list[TaxRecord] = field(default_factory=list)
    year: int | None = None  # Jahr laut Kopfdaten der Datei
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# -- Werte --------------------------------------------------------------------------------------------------

def _dec(v: Any) -> Decimal | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, int | float | Decimal) and not isinstance(v, bool):
        return Decimal(str(v))
    s = str(v).strip().replace("€", "").replace("EUR", "").replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"keine Zahl: {str(v)[:30]}") from None


def _date(v: Any) -> date | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        raise ValueError(f"kein Datum: {s[:30]}") from None


def _bool(v: Any) -> bool | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "ja", "yes", "y", "x", "steuerpflichtig", "taxable"):
        return True
    if s in ("0", "false", "nein", "no", "n", "steuerfrei", "nontaxable", "non-taxable"):
        return False
    raise ValueError(f"kein Ja/Nein-Wert: {s[:20]}")


def _int(v: Any) -> int | None:
    d = _dec(v)
    return int(d) if d is not None else None


def _text(v: Any, n: int = 200) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:n] or None


CONVERT: dict[str, Callable[[Any], Any]] = {
    "tax_year": _int, "transaction_id": _text, "external_id": _text, "asset": lambda v: _text(v, 80),
    "quantity": _dec, "acquisition_date": _date, "disposal_date": _date, "acquisition_cost": _dec,
    "disposal_value": _dec, "holding_period": _int, "taxable": _bool, "gain_loss": _dec,
    "tax_category": lambda v: _text(v, 80), "source": lambda v: _text(v, 80), "comment": lambda v: _text(v, 500),
}


def record_from(raw: dict[str, Any], line: int, warnings: list[str]) -> TaxRecord | None:
    rec = TaxRecord(line=line)
    used = False
    for k, v in raw.items():
        f = _ALIAS.get(_key(k))
        if f is None:
            continue
        try:
            val = CONVERT[f](v)
        except ValueError as e:
            warnings.append(f"Zeile {line}: {k} – {e}")
            continue
        if val is None:
            continue
        used = True
        setattr(rec, "holding_period_days" if f == "holding_period" else f, val)
    if not used:
        return None
    if rec.holding_period_days is None and rec.acquisition_date and rec.disposal_date:
        rec.holding_period_days = (rec.disposal_date - rec.acquisition_date).days
    if rec.gain_loss is None and rec.disposal_value is not None and rec.acquisition_cost is not None:
        rec.gain_loss = rec.disposal_value - rec.acquisition_cost
    return rec


# -- Parser -------------------------------------------------------------------------------------------------

class TaxJsonParser:
    id = "tax-json"
    label = "JSON (Portfolia-Steuerformat)"
    format = "json"

    @staticmethod
    def accepts(filename: str, data: bytes) -> bool:
        return filename.lower().endswith(".json") or data.lstrip()[:1] in (b"{", b"[")

    def parse(self, data: bytes) -> ParseResult:
        res = ParseResult()
        try:
            doc = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as e:
            res.errors.append(f"JSON nicht lesbar: {e}"[:200])
            return res
        rows: Any = doc
        if isinstance(doc, dict):
            for k, v in doc.items():
                if _ALIAS.get(_key(k)) == "tax_year":
                    try:
                        res.year = _int(v)
                    except ValueError:
                        res.warnings.append("Steuerjahr im Kopf nicht lesbar")
            rows = next((v for k, v in doc.items() if _key(k) in ("records", "transactions", "items", "data",
                                                                  "datensaetze", "datensätze")), None)
        if not isinstance(rows, list):
            res.errors.append("Keine Datensatzliste gefunden (erwartet: Liste oder Objekt mit „records“).")
            return res
        for i, raw in enumerate(rows[:MAX_RECORDS], 1):
            if not isinstance(raw, dict):
                res.warnings.append(f"Datensatz {i}: kein Objekt – übersprungen")
                continue
            rec = record_from(raw, i, res.warnings)
            if rec is not None:
                res.records.append(rec)
        if len(rows) > MAX_RECORDS:
            res.errors.append(f"Mehr als {MAX_RECORDS} Datensätze – Datei bitte teilen.")
        return res


class TaxCsvParser:
    id = "tax-csv"
    label = "CSV (Austauschformat)"
    format = "csv"

    @staticmethod
    def accepts(filename: str, data: bytes) -> bool:
        return filename.lower().endswith((".csv", ".txt"))

    def parse(self, data: bytes) -> ParseResult:
        from app.csvimport.reader import CsvError, read_table

        res = ParseResult()

        def matcher(cells: list[str]) -> bool:
            return sum(1 for c in cells if _ALIAS.get(_key(c))) >= 2

        try:
            table = read_table(data, matcher)
        except CsvError as e:
            res.errors.append(str(e))
            return res
        known = [h for h in table.header if _ALIAS.get(_key(h))]
        if len(known) < 2:
            res.errors.append("Kopfzeile ohne erkennbare Steuerfelder (z. B. asset, quantity, disposalDate, "
                              "gainLoss).")
            return res
        for line, row in zip(table.lines, table.rows, strict=True):
            raw = dict(zip(table.header, row, strict=False))
            rec = record_from(raw, line, res.warnings)
            if rec is not None:
                res.records.append(rec)
            if len(res.records) >= MAX_RECORDS:
                res.errors.append(f"Mehr als {MAX_RECORDS} Datensätze – Datei bitte teilen.")
                break
        return res


# Reihenfolge = Erkennung; weitere Anbieter-Parser (Blockpit, Koinly, CoinTracking) hier anfügen
PARSERS: list[Any] = [TaxJsonParser(), TaxCsvParser()]


def detect(filename: str, data: bytes) -> Any | None:
    return next((p for p in PARSERS if p.accepts(filename, data)), None)
