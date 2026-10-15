"""Vollständigkeitscockpit: getrennte Qualitätsdimensionen statt einer Gesamtnote (rein lesend).

Jede Dimension beantwortet eine eigene Frage und wird **nicht** mit den anderen verrechnet – eine vollständig
abgeglichene Prüfung macht fehlende Anschaffungsdaten nicht besser, eine lückenlose Quellenabdeckung keine
ungeklärte Bestandsdifferenz. Zustand je Dimension: ``ok`` · ``hinweis`` · ``handlung`` (mit Zahl und Weg).

Die Fallanalyse bündelt für eine Position (Konto, Asset) alles Bekannte: Bestand A/B/C, Abdeckung des Kontos, offene
Prüfvorgänge, Gruppenwidersprüche, Herkunft der Anschaffungsdaten – und nennt mögliche Belegquellen (Schnittstelle,
Exportdatei, Kontoauszug/Beleg), ohne etwas anzunehmen oder zu buchen.

Externe KI-Dienste sind standardmäßig aus und werden für Finanzdaten nie verwendet (nur optional für Nachrichten).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

STATE_LABEL = {"ok": ("in Ordnung", "good"), "hinweis": ("Hinweise", "info"), "handlung": ("Handlungsbedarf", "warn")}


@dataclass
class Dimension:
    key: str
    title: str
    question: str
    state: str
    figure: str
    details: list[str] = field(default_factory=list)
    link: str = ""


@dataclass
class Cockpit:
    dims: list[Dimension]
    ai: dict[str, Any]

    @property
    def counts(self) -> Counter[str]:
        return Counter(d.state for d in self.dims)


def _state(bad: int, warn: int = 0) -> str:
    return "handlung" if bad else "hinweis" if warn else "ok"


def build(ctx: Any) -> Cockpit:
    from app.csvimport import groups as G
    from app.csvimport import review as RV
    from app.diagnosis import actions as A
    from app.diagnosis import coverage as CV
    from app.diagnosis import stock as ST
    from app.diagnosis.engine import report_for
    from app.ledger import provenance as PV

    dims: list[Dimension] = []
    s = RV.active_summary(ctx.db)
    dims.append(Dimension(
        "pruefung", "Importprüfung", "Sind alle eingelesenen Vorgänge entschieden?", _state(s.to_review, s.open),
        f"{s.open} offen", [f"{s.to_review} manuell zu prüfen", f"{s.events['sicher']} sichere Vorschläge",
                            f"{s.events['zurueckgestellt']} zurückgestellt",
                            f"{s.history_gaps} vor dem Stichtag nicht im kuratierten Import (Hinweis)"],
        "/journal/pruefung"))
    mx = CV.build(ctx)
    gap_months = sum(len(CV.months_between(a, b)) for r in mx.rows for a, b in r.gaps)
    dims.append(Dimension(
        "abdeckung", "Quellenabdeckung", "Liegen für jeden aktiven Monat Daten vor?", _state(mx.n_gaps),
        f"{mx.n_gaps} Lücken ({gap_months} Monate)", [f"{len(mx.rows)} Konto-/Quellen-Kombinationen"],
        "/quality/abdeckung"))
    report = report_for(ctx)
    sv = ST.build(report, A.decisions(ctx.db, 400))
    unexplained = [r for r in sv.rows if r.unexplained]
    dims.append(Dimension(
        "bestand", "Bestandsabgleich", "Stimmen Buchungen mit Nachweisen (Referenz, Abruf) überein?",
        _state(len(unexplained), sv.n_unreferenced), f"{len(unexplained)} ungeklärte Differenzen",
        [f"{sv.n_no_ledger} Positionen ohne Buchung", f"{sv.n_unreferenced} Positionen ohne Bestätigung"],
        "/quality/diagnose/stock"))
    dims.append(Dimension(
        "bewertung", "Bewertung", "Hat jede Position einen belastbaren Kurs?", _state(0, sv.n_unvalued),
        f"{sv.n_unvalued} unvollständig bewertet", sv.unvalued[:5], "/quality/diagnose/stock?view=confirmed"))
    conflicts = G.scan(ctx)
    ck = Counter(c.kind for c in conflicts)
    dims.append(Dimension(
        "gruppen", "Ereignisgruppen", "Widersprechen sich Quellen zu einem Vorgang?",
        _state(ck["double"] + ck["1:n"] + ck["partial"], ck["fee_basis"] + ck["qty"] + ck["n:1"]),
        f"{len(conflicts)} Widersprüche", [f"{n}× {G.KINDS.get(k, k)}" for k, n in ck.most_common()],
        "/journal/ereignisse"))
    pv = PV.build(ctx)
    weak = [a for a in pv.assets if a.evidenced_share is not None and a.evidenced_share < 1]
    no_basis = sum((y.qty_by_class.get("ohne_basis", Decimal(0)) > 0) or (y.qty_by_class.get("unbekannt",
                                                                                            Decimal(0)) > 0)
                   for y in pv.years)
    dims.append(Dimension(
        "anschaffung", "Anschaffungsdaten", "Sind Anschaffungszeitpunkt und -kosten belegt?",
        _state(no_basis, len(weak)), f"{len(weak)} Assets nicht vollständig belegt",
        [f"{no_basis} Veräußerungsjahre mit Lots ohne Anschaffungsdaten bzw. unbekannten Kosten"],
        "/quality/anschaffung"))
    led = ctx.ledger()
    deriv = [d for d in (led.disposals if led is not None else []) if d.kind == "derivative"]
    years = sorted({d.date.year for d in deriv})
    dims.append(Dimension(
        "einordnung", "Steuerliche Einordnung", "Gibt es Vorgänge, die nicht automatisch eingeordnet werden?",
        _state(0, len(deriv)), f"{len(deriv)} Derivat-Abgänge",
        [f"Jahre: {', '.join(map(str, years))}" if years else "keine Derivate",
         "Futures/Funding/Liquidationen werden nie still als Veräußerung (§ 23) gerechnet"], "/tax"))
    open_findings = [f for f in report.findings if f.parent is None and f.status in ("belegt", "wahrscheinlich")]
    dims.append(Dimension(
        "diagnose", "Diagnose", "Gibt es belegte bzw. wahrscheinliche Befunde?", _state(len(open_findings)),
        f"{len(open_findings)} Befunde", [], "/quality/diagnose"))
    llm_on = bool(ctx.settings.get("llm.enabled", False))
    ai = {"enabled": llm_on and bool(ctx.config.secrets.anthropic_api_key), "setting": llm_on,
          "scope": "nur Nachrichten-Relevanz; Finanzdaten werden nie an externe KI-Dienste gesendet"}
    return Cockpit(dims, ai)


@dataclass
class Case:
    account: str
    asset: str
    stock: Any | None
    coverage: list[Any]
    review: list[Any]
    conflicts: list[Any]
    provenance: Any | None
    sources: list[str]


def evidence_sources(ctx: Any, account: str) -> list[str]:
    """Mögliche Belegquellen für ein Konto – nur aus Katalog und vorhandenen Datenquellen abgeleitet."""
    from app.datasources.connector import supported
    from app.datasources.providers import PROVIDERS

    out: list[str] = []
    for ds in ctx.db.q("SELECT id, provider, name FROM data_source WHERE account=?", (account,)):
        p = PROVIDERS.get(ds["provider"])
        if p is None:
            continue
        if supported(ds["provider"]):
            out.append(f"{ds['name']}: Schnittstelle – lesende Recherche im Zeitfenster")
        if p.csv_profile:
            out.append(f"{ds['name']}: Exportdatei (Format „{p.csv_profile}“ erkannt)")
        else:
            out.append(f"{ds['name']}: Exportdatei über eigene Spaltenzuordnung (CSV/XLSX/ZIP)")
    out.append("Kontoauszug bzw. Beleg (PDF/Screenshot) – Belegimport unter Buchungen")
    out.append("Referenzbestand zum Stichtag hinterlegen (macht Abweichungsfenster eingrenzbar)")
    return list(dict.fromkeys(out))


def case(ctx: Any, account: str, asset: str) -> Case:
    from app.csvimport import groups as G
    from app.csvimport import queue as Q
    from app.diagnosis import coverage as CV
    from app.diagnosis import stock as ST
    from app.diagnosis.engine import report_for
    from app.ledger import provenance as PV

    sv = ST.build(report_for(ctx), [])
    row = next((r for r in sv.all_rows if (r.account, r.asset) == (account, asset)), None)
    cov = list(CV.build(ctx, account=account).rows)
    rev = Q.build(ctx.db, "offen", flt=Q.Filter(account=account), page=0).all_items[:50]  # Vorgänge des Kontos
    conf = [c for c in G.scan(ctx) if account in c.text or asset in c.text][:30]
    prov = next((a for a in PV.build(ctx).assets if a.asset == asset), None)
    return Case(account, asset, row, cov, rev, conf, prov, evidence_sources(ctx, account))
