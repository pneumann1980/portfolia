"""Fundamentaldaten aus SEC ``companyfacts`` (XBRL) – mit Herkunft je Wert.

Herkunftsklassen (``origin``):

* ``reported`` – unverändert aus einem bei der SEC eingereichten Bericht (Element, Formular, Einreichungsdatum,
  Accession-Nummer werden mitgeführt);
* ``derived`` – deterministisch aus berichteten Werten berechnet (Formel steht am Wert);
* ``estimated`` – Modellannahme (z. B. Steuersatz für den ROIC, wenn er nicht ableitbar ist);
* ``missing`` – kein belastbarer Wert. Fehlende Jahre werden nie aufgefüllt.

Geschäftsjahre werden über das **Periodenende** bezeichnet (``fy`` in companyfacts ist das Geschäftsjahr der
*Einreichung*, nicht der Periode – Vergleichszahlen älterer Jahre tragen dort das neue Jahr). Liegen für eine Periode
mehrere Einreichungen mit abweichendem Wert vor (Korrektur/Restatement), gilt die jüngste bis ``as_of``; die Abweichung
wird vermerkt. ``as_of`` erlaubt eine Sicht ohne spätere Informationen (historische Fallstudien).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT")
QUARTER_FORMS = ("10-Q", "10-Q/A", "6-K")
MAX_YEARS = 6

# Kennzahl → XBRL-Elemente in Vorrangreihenfolge (us-gaap bzw. ifrs-full). ``flow`` = Zeitraumwert, sonst Stichtag.
CONCEPTS: dict[str, tuple[bool, tuple[str, ...]]] = {
    "revenue": (True, ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap:Revenues",
                       "us-gaap:SalesRevenueNet", "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
                       "ifrs-full:Revenue")),
    "cost_of_revenue": (True, ("us-gaap:CostOfRevenue", "us-gaap:CostOfGoodsAndServicesSold",
                               "ifrs-full:CostOfSales")),
    "gross_profit": (True, ("us-gaap:GrossProfit", "ifrs-full:GrossProfit")),
    "operating_income": (True, ("us-gaap:OperatingIncomeLoss", "ifrs-full:ProfitLossFromOperatingActivities")),
    "pretax_income": (True, (
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethod"
        "Investments", "ifrs-full:ProfitLossBeforeTax")),
    "income_tax": (True, ("us-gaap:IncomeTaxExpenseBenefit", "ifrs-full:IncomeTaxExpenseContinuingOperations")),
    "net_income": (True, ("us-gaap:NetIncomeLoss", "ifrs-full:ProfitLossAttributableToOwnersOfParent",
                          "ifrs-full:ProfitLoss")),
    "rnd": (True, ("us-gaap:ResearchAndDevelopmentExpense",
                   "us-gaap:ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
                   "ifrs-full:ResearchAndDevelopmentExpense")),
    "sbc": (True, ("us-gaap:ShareBasedCompensation", "us-gaap:AllocatedShareBasedCompensationExpense")),
    "ocf": (True, ("us-gaap:NetCashProvidedByUsedInOperatingActivities",
                   "ifrs-full:CashFlowsFromUsedInOperatingActivities")),
    "capex": (True, ("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", "us-gaap:PaymentsToAcquireProductiveAssets",
                     "ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities")),
    "shares_diluted": (True, ("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
                              "ifrs-full:AdjustedWeightedAverageShares")),
    "cash": (False, ("us-gaap:CashAndCashEquivalentsAtCarryingValue",
                     "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                     "ifrs-full:CashAndCashEquivalents")),
    "st_investments": (False, ("us-gaap:ShortTermInvestments", "us-gaap:MarketableSecuritiesCurrent",
                               "us-gaap:AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                               "us-gaap:AvailableForSaleSecuritiesDebtSecurities")),
    "debt_total": (False, ("us-gaap:LongTermDebt", "us-gaap:DebtInstrumentCarryingAmount")),
    "debt_noncurrent": (False, ("us-gaap:LongTermDebtNoncurrent", "us-gaap:ConvertibleNotesPayableNoncurrent",
                                "ifrs-full:NoncurrentBorrowings")),
    "debt_current": (False, ("us-gaap:LongTermDebtCurrent", "us-gaap:DebtCurrent", "ifrs-full:CurrentBorrowings")),
    "equity": (False, ("us-gaap:StockholdersEquity", "ifrs-full:EquityAttributableToOwnersOfParent",
                       "ifrs-full:Equity")),
    "assets_current": (False, ("us-gaap:AssetsCurrent", "ifrs-full:CurrentAssets")),
    "liabilities_current": (False, ("us-gaap:LiabilitiesCurrent", "ifrs-full:CurrentLiabilities")),
}
LABELS = {
    "revenue": "Umsatz", "revenue_growth": "Umsatzwachstum", "gross_profit": "Bruttogewinn",
    "gross_margin": "Bruttomarge", "operating_income": "Operatives Ergebnis", "op_margin": "Operative Marge",
    "net_income": "Nettoergebnis", "net_margin": "Nettomarge", "ocf": "Operativer Cashflow",
    "capex": "Investitionsausgaben", "fcf": "Free Cashflow", "fcf_margin": "Free-Cashflow-Marge",
    "cash": "Liquide Mittel", "st_investments": "Kurzfristige Anlagen", "liquidity": "Liquidität gesamt",
    "debt": "Finanzverbindlichkeiten", "net_cash": "Nettofinanzposition", "equity": "Eigenkapital",
    "shares_diluted": "Aktien (verwässert, Ø)", "dilution": "Veränderung Aktienanzahl", "roic": "ROIC",
    "rnd": "Forschung & Entwicklung", "rnd_ratio": "F&E-Quote", "sbc": "Aktienbasierte Vergütung",
    "current_ratio": "Current Ratio", "income_tax": "Ertragsteuern", "pretax_income": "Ergebnis vor Steuern",
}
PCT = {"revenue_growth", "gross_margin", "op_margin", "net_margin", "fcf_margin", "dilution", "roic", "rnd_ratio"}
ASSUMED_TAX = 0.21  # Annahme, wenn der effektive Steuersatz nicht ableitbar ist (US-Bundessteuersatz)


@dataclass
class Value:
    v: float | None
    origin: str  # reported | derived | estimated | missing
    unit: str = ""
    period: str = ""
    tag: str = ""
    form: str = ""
    filed: str = ""
    accn: str = ""
    formula: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in ("", None) or k in ("v", "origin")}


MISSING = Value(None, "missing")


@dataclass
class Fundamentals:
    currency: str | None
    years: list[str] = field(default_factory=list)  # Periodenenden, älteste zuerst
    rows: dict[str, dict[str, Value]] = field(default_factory=dict)  # Kennzahl → Periodenende → Wert
    quarter: dict[str, Any] = field(default_factory=dict)
    corrections: list[dict[str, Any]] = field(default_factory=list)
    filings: dict[str, dict[str, str]] = field(default_factory=dict)  # accn → {form, filed}
    shares_outstanding: Value = field(default_factory=lambda: MISSING)
    as_of: str | None = None
    notes: list[str] = field(default_factory=list)

    def get(self, key: str, year: str) -> Value:
        return self.rows.get(key, {}).get(year, MISSING)

    def series(self, key: str) -> list[float | None]:
        return [self.get(key, y).v for y in self.years]

    def latest(self, key: str) -> Value:
        for y in reversed(self.years):
            val = self.get(key, y)
            if val.v is not None:
                return val
        return MISSING

    def n_years(self, key: str) -> int:
        return sum(1 for y in self.years if self.get(key, y).v is not None)

    def to_dict(self) -> dict[str, Any]:
        return {"currency": self.currency, "years": self.years, "as_of": self.as_of, "notes": self.notes,
                "rows": {k: {y: v.to_dict() for y, v in r.items()} for k, r in self.rows.items()},
                "quarter": self.quarter, "corrections": self.corrections, "filings": self.filings,
                "shares_outstanding": self.shares_outstanding.to_dict()}


def _days(a: str | None, b: str) -> int | None:
    if not a:
        return None
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except ValueError:
        return None


def _entries(facts: dict[str, Any], tag: str, as_of: str | None) -> tuple[str, list[dict[str, Any]]]:
    """(Einheit, Einträge) eines Elements; Geldwerte in der Berichtswährung, Aktien in ``shares``."""
    tax, name = tag.split(":", 1)
    node = ((facts.get("facts") or {}).get(tax) or {}).get(name) or {}
    units = node.get("units") or {}
    for unit in ("USD", "EUR", "GBP", "CAD", "JPY", "CHF", "TWD", "CNY", "ILS", "AUD", "shares", "DKK", "SEK", "NOK"):
        if unit in units:
            rows = [e for e in units[unit] if isinstance(e, dict) and e.get("val") is not None and e.get("end")
                    and (as_of is None or str(e.get("filed", "")) <= as_of)]
            return unit, rows
    return "", []


def _pick(rows: list[dict[str, Any]], forms: tuple[str, ...], flow: bool, lo: int, hi: int
          ) -> dict[str, list[dict[str, Any]]]:
    """Einträge je Periodenende (gleicher Zeitraum), nach Einreichung sortiert."""
    out: dict[str, list[dict[str, Any]]] = {}
    for e in rows:
        if e.get("form") not in forms:
            continue
        if flow:
            d = _days(e.get("start"), e["end"])
            if d is None or not (lo <= d <= hi):
                continue
        elif e.get("start"):
            continue
        out.setdefault(e["end"], []).append(e)
    for v in out.values():
        v.sort(key=lambda e: (str(e.get("filed", "")), str(e.get("accn", ""))))
    return out


def _annual_ends(facts: dict[str, Any], as_of: str | None) -> list[str]:
    """Geschäftsjahresenden: Periodenenden der Jahres-Umsätze bzw. -Ergebnisse (gemeinsame Zeitachse)."""
    ends: set[str] = set()
    for key in ("revenue", "net_income", "operating_income", "ocf"):
        for tag in CONCEPTS[key][1]:
            _u, rows = _entries(facts, tag, as_of)
            ends |= set(_pick(rows, ANNUAL_FORMS, True, 340, 380))
    # zwei Enden weniger als 300 Tage auseinander: Umstellung des Geschäftsjahres – das spätere bleibt
    out: list[str] = []
    for e in sorted(ends):
        if out and (_days(out[-1], e) or 0) < 300:
            out[-1] = e
        else:
            out.append(e)
    return out[-MAX_YEARS:]


def _near(target: str, have: dict[str, list[dict[str, Any]]], tol: int = 14) -> list[dict[str, Any]] | None:
    if target in have:
        return have[target]
    for k, v in have.items():
        d = _days(k, target)
        if d is not None and abs(d) <= tol:
            return v
    return None


def parse(facts: dict[str, Any], as_of: str | None = None) -> Fundamentals:
    years = _annual_ends(facts, as_of)
    fu = Fundamentals(currency=None, years=years, as_of=as_of)
    ccys: set[str] = set()
    for key, (flow, tags) in CONCEPTS.items():
        row: dict[str, Value] = {}
        for tag in tags:
            unit, rows = _entries(facts, tag, as_of)
            if not rows:
                continue
            by_end = _pick(rows, ANNUAL_FORMS, flow, 340, 380)
            for y in years:
                if y in row:
                    continue
                cands = _near(y, by_end)
                if not cands:
                    continue
                last = cands[-1]
                first = cands[0]
                val = Value(float(last["val"]), "reported", unit, y, tag, str(last.get("form", "")),
                            str(last.get("filed", "")), str(last.get("accn", "")))
                if abs(float(first["val"]) - float(last["val"])) > max(1.0, abs(float(last["val"])) * 0.005):
                    val.note = (f"korrigiert: ursprünglich {first['val']:,.0f} (eingereicht {first.get('filed')}), "
                                f"jetzt {last['val']:,.0f} (eingereicht {last.get('filed')})")
                    fu.corrections.append({"concept": key, "period": y, "first": float(first["val"]),
                                           "first_filed": first.get("filed"), "last": float(last["val"]),
                                           "last_filed": last.get("filed"), "tag": tag})
                row[y] = val
                fu.filings[val.accn] = {"form": val.form, "filed": val.filed}
                if unit != "shares":
                    ccys.add(unit)
        if row:
            fu.rows[key] = row
    if len(ccys) > 1:
        fu.notes.append(f"Mehrere Berichtswährungen gefunden ({', '.join(sorted(ccys))}) – Werte nicht vermischt.")
    fu.currency = sorted(ccys)[0] if len(ccys) == 1 else (sorted(ccys)[0] if ccys else None)
    _shares_outstanding(facts, fu, as_of)
    _split_adjust(fu)
    _derive(fu)
    _quarter(facts, fu, as_of)
    return fu


def _shares_outstanding(facts: dict[str, Any], fu: Fundamentals, as_of: str | None) -> None:
    unit, rows = _entries(facts, "dei:EntityCommonStockSharesOutstanding", as_of)
    if not rows:
        return
    last = max(rows, key=lambda e: (str(e["end"]), str(e.get("filed", ""))))
    # mehrere Aktiengattungen am selben Stichtag (z. B. Class A/B): Summe der jüngsten Einreichung
    same = [e for e in rows if e["end"] == last["end"] and e.get("accn") == last.get("accn")]
    total = sum(float(e["val"]) for e in same)
    fu.shares_outstanding = Value(total, "reported" if len(same) == 1 else "derived", unit, last["end"],
                                  "dei:EntityCommonStockSharesOutstanding", str(last.get("form", "")),
                                  str(last.get("filed", "")), str(last.get("accn", "")),
                                  formula="Summe der Aktiengattungen" if len(same) > 1 else "")


SPLIT_RATIOS = (2, 3, 4, 5, 8, 10, 15, 20, 25, 30, 40, 50)


def _split_adjust(fu: Fundamentals) -> None:
    """Aktienanzahl über Aktiensplits vergleichbar machen. Ältere Jahre stammen oft aus Berichten vor einem Split
    (nicht rückwirkend angepasst). Ein Split gilt nur als **nachgewiesen**, wenn die SEC-Daten dieselbe Periode in
    einer späteren Einreichung mit einem üblichen Split-Faktor (±3 %) neu berichten (Restatement). Dann werden Sprünge
    um genau diesen Faktor zwischen zwei Jahren bereinigt: frühere Jahre × Faktor (``derived``, Vermerk am Wert).
    Sprünge ohne Nachweis (z. B. Börsengang, Umwandlung von Aktiengattungen) bleiben unverändert."""
    confirmed: set[float] = set()
    for c in fu.corrections:
        if c["concept"] == "shares_diluted" and c["first"] > 0:
            r = c["last"] / c["first"]
            for k in SPLIT_RATIOS:
                for factor in (float(k), 1 / k):
                    if abs(r / factor - 1) <= 0.03:
                        confirmed.add(factor)
                        c["split"] = factor
    fu.corrections = [c for c in fu.corrections if "split" not in c]  # Split ist keine inhaltliche Korrektur
    if not confirmed:
        return
    row = fu.rows.get("shares_diluted") or {}
    ys = [y for y in fu.years if row.get(y) is not None and row[y].v]
    for i in range(len(ys) - 1, 0, -1):
        a, b = row[ys[i - 1]].v or 0.0, row[ys[i]].v or 0.0
        if a <= 0 or b <= 0:
            continue
        factor = next((f for f in sorted(confirmed) if abs((b / a) / f - 1) <= 0.03), None)
        if factor is None:
            continue
        for y in ys[:i]:
            old = row[y]
            row[y] = Value((old.v or 0.0) * factor, "derived", old.unit, y, old.tag, old.form, old.filed, old.accn,
                           formula=f"berichteter Wert × {factor:g} (Aktiensplit)",
                           note=f"Aktiensplit zwischen {ys[i - 1]} und {ys[i]} (Faktor {factor:g}); berichtet: "
                                f"{old.v:,.0f}")
        fu.notes.append(f"Aktiensplit zwischen {ys[i - 1]} und {ys[i]} (Faktor {factor:g}, durch spätere "
                        "SEC-Einreichung belegt); frühere Aktienzahlen angepasst.")


def _ratio(a: Value, b: Value, formula: str, *, positive_base: bool = True) -> Value:
    if a.v is None or b.v is None or b.v == 0 or (positive_base and b.v < 0):
        return Value(None, "missing", formula=formula, note="Grundwert fehlt" if a.v is None or b.v is None else
                     "Basis ≤ 0")
    return Value(a.v / b.v, "derived", "ratio", a.period or b.period, formula=formula)


def _derive(fu: Fundamentals) -> None:
    def put(key: str, y: str, v: Value) -> None:
        fu.rows.setdefault(key, {})[y] = v

    ccy = fu.currency or ""
    for i, y in enumerate(fu.years):
        g = fu.get
        rev = g("revenue", y)
        gp = g("gross_profit", y)
        if gp.v is None and rev.v is not None and g("cost_of_revenue", y).v is not None:
            gp = Value(rev.v - (g("cost_of_revenue", y).v or 0.0), "derived", ccy, y,
                       formula="Umsatz − Umsatzkosten")
            put("gross_profit", y, gp)
        put("gross_margin", y, _ratio(gp, rev, "Bruttogewinn / Umsatz"))
        put("op_margin", y, _ratio(g("operating_income", y), rev, "Operatives Ergebnis / Umsatz"))
        put("net_margin", y, _ratio(g("net_income", y), rev, "Nettoergebnis / Umsatz"))
        put("rnd_ratio", y, _ratio(g("rnd", y), rev, "F&E / Umsatz"))
        ocf, capex = g("ocf", y), g("capex", y)
        if ocf.v is not None and capex.v is not None:
            fcf = Value(ocf.v - abs(capex.v), "derived", ccy, y, formula="Operativer Cashflow − Investitionsausgaben")
        elif ocf.v is not None:
            fcf = Value(None, "missing", formula="Operativer Cashflow − Investitionsausgaben",
                        note="Investitionsausgaben nicht berichtet")
        else:
            fcf = MISSING
        put("fcf", y, fcf)
        put("fcf_margin", y, _ratio(fcf, rev, "Free Cashflow / Umsatz"))
        cash, sti = g("cash", y), g("st_investments", y)
        if cash.v is not None:
            put("liquidity", y, Value(cash.v + (sti.v or 0.0), "derived", ccy, y,
                                      formula="Liquide Mittel + kurzfristige Anlagen" if sti.v is not None
                                      else "Liquide Mittel (keine kurzfristigen Anlagen berichtet)"))
        dt, dn, dc = g("debt_total", y), g("debt_noncurrent", y), g("debt_current", y)
        if dt.v is not None:
            debt = Value(dt.v, "reported", dt.unit, y, dt.tag, dt.form, dt.filed, dt.accn)
        elif dn.v is not None or dc.v is not None:
            debt = Value((dn.v or 0.0) + (dc.v or 0.0), "derived", ccy, y,
                         formula="langfristige + kurzfristige Finanzverbindlichkeiten")
        else:
            debt = Value(None, "missing", note="keine Finanzverbindlichkeiten berichtet (kann 0 bedeuten)")
        put("debt", y, debt)
        liq = g("liquidity", y)
        if liq.v is not None:
            put("net_cash", y, Value(liq.v - (debt.v or 0.0), "derived", ccy, y,
                                     formula="Liquidität − Finanzverbindlichkeiten",
                                     note="" if debt.v is not None else "Verbindlichkeiten nicht berichtet: als 0 "
                                     "angesetzt"))
        put("current_ratio", y, _ratio(g("assets_current", y), g("liabilities_current", y),
                                       "Umlaufvermögen / kurzfristige Verbindlichkeiten"))
        # ROIC = NOPAT / investiertes Kapital; NOPAT = operatives Ergebnis × (1 − Steuersatz); investiertes Kapital =
        # Eigenkapital + Finanzverbindlichkeiten (Liquidität nicht abgezogen – sonst bei hoher Liquidität sinnlos)
        oi, eq = g("operating_income", y), g("equity", y)
        tax, pre = g("income_tax", y), g("pretax_income", y)
        if tax.v is not None and pre.v is not None and pre.v > 0 and 0 <= tax.v / pre.v <= 0.5:
            rate, rate_origin = tax.v / pre.v, "derived"
        else:
            rate, rate_origin = ASSUMED_TAX, "estimated"
        ic = (eq.v + (debt.v or 0.0)) if eq.v is not None else None
        if oi.v is not None and ic is not None and ic > 0:
            put("roic", y, Value(oi.v * (1 - rate) / ic, "derived" if rate_origin == "derived" else "estimated",
                                 "ratio", y, formula=f"Operatives Ergebnis × (1 − Steuersatz {rate:.0%}) / "
                                 "(Eigenkapital + Finanzverbindlichkeiten)",
                                 note="" if rate_origin == "derived" else
                                 f"Steuersatz angenommen ({ASSUMED_TAX:.0%}), da nicht ableitbar"))
        else:
            put("roic", y, Value(None, "missing", formula="NOPAT / investiertes Kapital",
                                 note="investiertes Kapital ≤ 0 oder Eingangswerte fehlen" if oi.v is not None
                                 else "operatives Ergebnis fehlt"))
        if i > 0:
            prev = fu.years[i - 1]
            put("revenue_growth", y, _growth(g("revenue", prev), rev, "Umsatz / Umsatz Vorjahr − 1"))
            put("dilution", y, _growth(g("shares_diluted", prev), g("shares_diluted", y),
                                       "Aktien (verwässert) / Vorjahr − 1"))
        else:
            put("revenue_growth", y, Value(None, "missing", note="kein Vorjahr im Betrachtungszeitraum"))
            put("dilution", y, Value(None, "missing", note="kein Vorjahr im Betrachtungszeitraum"))


def _growth(a: Value, b: Value, formula: str) -> Value:
    if a.v is None or b.v is None or a.v <= 0:
        return Value(None, "missing", formula=formula, note="Vorjahreswert fehlt oder ≤ 0")
    return Value(b.v / a.v - 1, "derived", "ratio", b.period, formula=formula)


def cagr(fu: Fundamentals, key: str, years: int) -> Value:
    """Durchschnittliches jährliches Wachstum über ``years`` Jahre (nur mit beiden Endwerten > 0)."""
    if len(fu.years) < years + 1:
        return Value(None, "missing", note=f"weniger als {years + 1} Geschäftsjahre vorhanden")
    a, b = fu.get(key, fu.years[-1 - years]), fu.get(key, fu.years[-1])
    if a.v is None or b.v is None or a.v <= 0 or b.v <= 0:
        return Value(None, "missing", note="Anfangs- oder Endwert fehlt oder ≤ 0")
    return Value((b.v / a.v) ** (1 / years) - 1, "derived", "ratio", f"{fu.years[-1 - years]}–{fu.years[-1]}",
                 formula=f"(Endwert / Anfangswert)^(1/{years}) − 1")


def _quarter(facts: dict[str, Any], fu: Fundamentals, as_of: str | None) -> None:
    """Jüngstes Quartal (10-Q, ca. 3 Monate) mit Vorjahresquartal für Umsatz, Bruttogewinn, operatives Ergebnis."""
    out: dict[str, Any] = {}
    for key in ("revenue", "gross_profit", "operating_income", "net_income"):
        best: tuple[str, str, str, dict[str, list[dict[str, Any]]]] | None = None
        for tag in CONCEPTS[key][1]:  # Element mit dem jüngsten Quartal (Unternehmen wechseln Elemente)
            unit, rows = _entries(facts, tag, as_of)
            by_end = _pick(rows, QUARTER_FORMS + ANNUAL_FORMS, True, 80, 100)
            if by_end and (best is None or max(by_end) > best[0]):
                best = (max(by_end), tag, unit, by_end)
        if best is None:
            continue
        end, tag, unit, by_end = best
        last = by_end[end][-1]
        prior = _near(date.fromisoformat(end).replace(year=date.fromisoformat(end).year - 1).isoformat(), by_end,
                      tol=10)
        cur = Value(float(last["val"]), "reported", unit, end, tag, str(last.get("form", "")),
                    str(last.get("filed", "")), str(last.get("accn", "")))
        row: dict[str, Any] = {"value": cur.to_dict()}
        if prior:
            p = prior[-1]
            row["prior"] = Value(float(p["val"]), "reported", unit, p["end"], tag, str(p.get("form", "")),
                                 str(p.get("filed", "")), str(p.get("accn", ""))).to_dict()
            if float(p["val"]) > 0:
                row["yoy"] = float(last["val"]) / float(p["val"]) - 1
        out[key] = row
    fu.quarter = out
