"""Scorecards: Disruption (100), Quality (100), Growth/GARP (100), Tom-Nash-inspirierte Fundamentalanalyse (125).

Grundsätze
----------
* Jeder Score ist die **Summe seiner Kriterien** – keine Normierung, kein versteckter Faktor. Ein Kriterium ohne
  ausreichende Daten erhält **0 Punkte** und gilt als „nicht bewertet“; die Abdeckung (bewertete Maximalpunkte /
  Gesamtmaximum) steht neben dem Score. Unter ``COMPLETE`` Abdeckung ist der Score „unvollständig“.
* Regel-Kriterien (``rule``) rechnen deterministisch aus Kennzahlen mit offengelegten Stufen.
* KI-Kriterien (``ai``) übernehmen eine Stufe 0–4 aus der strukturierten KI-Einschätzung; Punkte = Stufe / 4 × Maximum.
  Eine Stufe über 1 setzt **mindestens einen gültigen Beleg** (Quellen-ID aus der Eingabe) voraus, eine Stufe über 2
  zusätzlich Konfidenz ≥ 0,5 – sonst wird gekappt und der Grund angezeigt.
* Gemischte Kriterien (``mixed``) = Regelteil + KI-Teil, beide einzeln ausgewiesen.
* Alles Berechnete ist reproduzierbar: :func:`recompute` ergibt aus den gespeicherten Kriterien dieselbe Summe.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

from app.discovery.fundamentals import Fundamentals, Value, cagr
from app.discovery.text import G, N, P

CALC_VERSION = 1
COMPLETE = 0.7  # Mindestabdeckung für einen vollständigen Score
LEVELS = {0: "nicht erkennbar", 1: "schwach / unbelegt", 2: "vorhanden", 3: "stark", 4: "herausragend, gut belegt"}


@dataclass
class Fact:
    label: str
    value: Any
    fmt: str = "num"  # pct | num | money | x | text | years
    origin: str = "derived"
    period: str = ""
    source: str = ""  # Quellen-ID (siehe Quellenverzeichnis)
    note: str = ""


@dataclass
class Criterion:
    key: str
    label: str
    max: float
    kind: str  # rule | ai | mixed
    definition: str
    scale: list[str]
    points: float | None = None
    rule: str = ""
    reason: str = ""
    facts: list[Fact] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    counter: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    confidence: float | None = None
    parts: list[dict[str, Any]] = field(default_factory=list)  # gemischte Kriterien: je Teil Punkte/Maximum/Art
    eval_max: float | None = None  # gemischte Kriterien: Maximum der tatsächlich bewerteten Teile

    @property
    def evaluated(self) -> bool:
        return self.points is not None

    @property
    def evaluated_max(self) -> float:
        if self.eval_max is not None:
            return self.eval_max
        return self.max if self.evaluated else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["facts"] = [f.__dict__ for f in self.facts]
        return d


@dataclass
class Score:
    key: str
    label: str
    criteria: list[Criterion]

    @property
    def max(self) -> float:
        return sum(c.max for c in self.criteria)

    @property
    def total(self) -> float:
        return round(sum(c.points or 0.0 for c in self.criteria), 1)

    @property
    def coverage(self) -> float:
        return sum(c.evaluated_max for c in self.criteria) / self.max if self.max else 0.0

    @property
    def complete(self) -> bool:
        return self.coverage >= COMPLETE

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "max": self.max, "total": self.total,
                "coverage": round(self.coverage, 3), "complete": self.complete,
                "criteria": [c.to_dict() for c in self.criteria]}


def recompute(score: dict[str, Any]) -> float:
    """Summe aus gespeicherten Teilpunkten (Prüfung der Reproduzierbarkeit)."""
    return round(sum(float(c.get("points") or 0.0) for c in score["criteria"]), 1)


# ----------------------------------------------------------------------------------------------------------------
# Hilfen
# ----------------------------------------------------------------------------------------------------------------

def steps(value: float | None, table: list[tuple[float, float]], higher_better: bool = True) -> float | None:
    """Stufenfunktion: erste Schwelle, die erreicht wird (absteigend für ``higher_better``)."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    for thr, pts in table:
        if (value >= thr) if higher_better else (value <= thr):
            return float(pts)
    return 0.0


def _scale(table: list[tuple[float, float]], fmt: str, higher_better: bool = True, unit: str = "") -> list[str]:
    op = "≥" if higher_better else "≤"
    out = []
    for thr, pts in table:
        t = f"{P(thr, 0)}" if fmt == "pct" else f"{G(thr)}{unit}"
        out.append(f"{op} {t}: {G(pts)} Punkte")
    out.append("sonst: 0 Punkte")
    return out


def _fact(label: str, v: Value, fmt: str, source: str = "") -> Fact:
    src = source or (f"sec:{v.accn}" if v.accn else "K" if v.v is not None and v.origin != "missing" else "")
    return Fact(label, v.v, fmt, v.origin, v.period, src, v.note or v.formula)


def _avg(vals: list[float | None]) -> float | None:
    xs = [x for x in vals if x is not None]
    return sum(xs) / len(xs) if xs else None


def _last(fu: Fundamentals, key: str, n: int) -> list[float | None]:
    return fu.series(key)[-n:]


def _rule(key: str, label: str, mx: float, definition: str, value: float | None, table: list[tuple[float, float]],
          fmt: str, facts: list[Fact], *, higher_better: bool = True, rule: str, missing: str = "",
          counter: list[str] | None = None, unit: str = "") -> Criterion:
    c = Criterion(key, label, mx, "rule", definition, _scale(table, fmt, higher_better, unit), rule=rule, facts=facts)
    pts = steps(value, table, higher_better)
    if pts is None:
        c.missing.append(missing or "Eingangswert fehlt")
        c.reason = "Nicht bewertet: " + (missing or "Eingangswert fehlt") + "."
    else:
        c.points = min(mx, pts)
        shown = f"{P(value)}" if fmt == "pct" else f"{N(value)}{unit}"
        c.reason = f"Wert {shown} → {G(c.points)} von {G(mx)} Punkten nach der Skala."
    c.counter = counter or []
    return c


# ----------------------------------------------------------------------------------------------------------------
# KI-Einschätzungen (qualitativ)
# ----------------------------------------------------------------------------------------------------------------

AI_KEYS = {
    "technologie": "Technologische Differenzierung",
    "innovation": "Innovationspotenzial",
    "marktgroesse": "Marktgröße und langfristige Marktentwicklung",
    "skalierbarkeit": "Skalierbarkeit des Geschäftsmodells",
    "wettbewerbsvorteile": "Wettbewerbsvorteile",
    "netzwerk": "Netzwerkeffekte oder Wechselkosten",
    "umsetzung": "Fähigkeit zur Umsetzung",
    "risiken": "Geschäftsmodellrisiken (hohe Stufe = geringe Risiken)",
    "management": "Managementqualität",
    "branchentrend": "Langfristige Branchentrends",
    "produktrelevanz": "Wirtschaftliche Relevanz der Produkte",
    "organisation": "Organisation und Talentgewinnung",
}


def ai_criterion(key: str, label: str, mx: float, definition: str, ai: dict[str, Any] | None, ai_key: str,
                 *, kind: str = "ai") -> Criterion:
    scale = [f"Stufe {lvl} ({txt}): {G(lvl / 4 * mx)} Punkte" for lvl, txt in LEVELS.items()]
    c = Criterion(key, label, mx, kind, definition, scale,
                  rule="Punkte = Stufe / 4 × Maximum; Stufe > 1 nur mit Beleg, Stufe > 2 nur mit Konfidenz ≥ 0,5")
    a = (ai or {}).get(ai_key)
    if not a:
        c.missing.append("keine KI-Einschätzung (KI aus, Budget erschöpft oder Antwort ungültig)")
        c.reason = "Nicht bewertet: qualitative Einschätzung fehlt."
        return c
    level = int(a.get("stufe", 0))
    conf = float(a.get("konfidenz", 0.0))
    srcs = list(a.get("belege") or [])
    caps = []
    if level > 1 and not srcs:
        level = 1
        caps.append("ohne gültigen Beleg auf Stufe 1 gekappt")
    if level > 2 and conf < 0.5:
        level = 2
        caps.append("Konfidenz < 0,5: auf Stufe 2 gekappt")
    c.points = round(level / 4 * mx, 1)
    c.confidence = conf
    c.sources = srcs
    c.reason = str(a.get("begruendung") or "")
    c.counter = list(a.get("gegenargumente") or [])
    c.missing = list(a.get("fehlend") or [])
    c.facts = [Fact("Stufe", f"{level} – {LEVELS[level]}", "text", "estimated", note="; ".join(caps))]
    if caps:
        c.reason = (c.reason + " " if c.reason else "") + "(" + "; ".join(caps) + ")"
    return c


def _mixed(key: str, label: str, definition: str, rule_c: Criterion, ai_c: Criterion) -> Criterion:
    c = Criterion(key, label, rule_c.max + ai_c.max, "mixed", definition, [f"Regelteil (max. {G(rule_c.max)}): "
                  + s for s in rule_c.scale] + [f"KI-Teil (max. {G(ai_c.max)}): " + s for s in ai_c.scale],
                  rule=f"Regelteil: {rule_c.rule} · KI-Teil: {ai_c.rule}")
    c.parts = [{"art": "Regel", "points": rule_c.points, "max": rule_c.max, "reason": rule_c.reason},
               {"art": "KI", "points": ai_c.points, "max": ai_c.max, "reason": ai_c.reason}]
    if rule_c.points is not None or ai_c.points is not None:
        c.points = round((rule_c.points or 0.0) + (ai_c.points or 0.0), 1)
        c.eval_max = (rule_c.max if rule_c.points is not None else 0.0) + (ai_c.max if ai_c.points is not None else 0.0)
    c.facts = rule_c.facts + ai_c.facts
    c.sources, c.counter, c.confidence = ai_c.sources, ai_c.counter, ai_c.confidence
    c.missing = rule_c.missing + ai_c.missing
    c.reason = f"Regelteil: {rule_c.reason} KI-Teil: {ai_c.reason}"
    return c


# ----------------------------------------------------------------------------------------------------------------
# Gemeinsame Regel-Bausteine
# ----------------------------------------------------------------------------------------------------------------

def _growth_latest(fu: Fundamentals) -> tuple[float | None, list[Fact]]:
    v = fu.latest("revenue_growth")
    facts = [_fact("Umsatzwachstum (letztes Geschäftsjahr)", v, "pct")]
    q = (fu.quarter or {}).get("revenue") or {}
    if "yoy" in q:
        facts.append(Fact("Umsatzwachstum letztes Quartal ggü. Vorjahresquartal", q["yoy"], "pct", "derived",
                          q["value"].get("period", ""), f"sec:{q['value'].get('accn', '')}",
                          "Quartalsumsatz / Vorjahresquartal − 1 (nur Information)"))
    return v.v, facts


def _net_cash_rule(fu: Fundamentals) -> tuple[float | None, list[Fact], str]:
    """Verschuldungsgrad in Jahren Free Cashflow (Nettofinanzposition ≥ 0 → 0 Jahre)."""
    nc, fcf = fu.latest("net_cash"), fu.latest("fcf")
    facts = [_fact("Nettofinanzposition", nc, "money"), _fact("Free Cashflow", fcf, "money"),
             _fact("Finanzverbindlichkeiten", fu.latest("debt"), "money")]
    if nc.v is None:
        return None, facts, "Nettofinanzposition nicht berechenbar"
    if nc.v >= 0:
        return 0.0, facts, ""
    if fcf.v is None or fcf.v <= 0:
        return 99.0, facts, ""
    return -nc.v / fcf.v, facts, ""


def _runway(fu: Fundamentals) -> tuple[float | None, list[Fact]]:
    """Reichweite der Liquidität in Jahren bei negativem Free Cashflow (≥ 0 → unbegrenzt = 99)."""
    liq, fcf = fu.latest("liquidity"), fu.latest("fcf")
    facts = [_fact("Liquidität", liq, "money"), _fact("Free Cashflow", fcf, "money")]
    if fcf.v is None:
        return None, facts
    if fcf.v >= 0:
        return 99.0, facts
    if liq.v is None:
        return None, facts
    return liq.v / -fcf.v, facts


def _dilution_avg(fu: Fundamentals) -> tuple[float | None, list[Fact]]:
    vals = _last(fu, "dilution", 3)
    avg = _avg(vals)
    return avg, [Fact("Ø Veränderung der Aktienanzahl (bis 3 Jahre)", avg, "pct", "derived",
                      f"{fu.years[-3] if len(fu.years) >= 3 else ''}–{fu.years[-1] if fu.years else ''}",
                      note="Durchschnitt der jährlichen Veränderung der verwässerten Aktienanzahl")]


# ----------------------------------------------------------------------------------------------------------------
# Quality
# ----------------------------------------------------------------------------------------------------------------

def quality(fu: Fundamentals, ai: dict[str, Any] | None) -> Score:
    cs: list[Criterion] = []
    g3 = cagr(fu, "revenue", 3)
    if g3.v is None:
        g3 = cagr(fu, "revenue", 2)
    cs.append(_rule("q_wachstum", "Umsatzwachstum über mehrere Jahre", 15,
                    "Durchschnittliches jährliches Umsatzwachstum über 3 Jahre (ersatzweise 2 Jahre).", g3.v,
                    [(0.20, 15), (0.12, 12), (0.07, 9), (0.03, 5), (0.0, 2)], "pct",
                    [_fact("Umsatz-CAGR", g3, "pct")], rule="CAGR = (Umsatz Ende / Umsatz Anfang)^(1/n) − 1",
                    missing=g3.note))
    om = fu.latest("op_margin")
    cs.append(_rule("q_marge", "Operative Marge", 12, "Operatives Ergebnis / Umsatz im letzten Geschäftsjahr.", om.v,
                    [(0.30, 12), (0.20, 10), (0.12, 7), (0.05, 4), (0.0, 1)], "pct",
                    [_fact("Operative Marge", om, "pct")], rule="Stufen der operativen Marge"))
    oms = [x for x in _last(fu, "op_margin", 5) if x is not None]
    stab: float | None = None
    if len(oms) >= 3:
        sd = statistics.pstdev(oms)
        pos = sum(1 for x in oms if x > 0)
        stab = 8.0 if pos == len(oms) and sd <= 0.05 else 5.5 if pos == len(oms) and sd <= 0.10 else \
            3.0 if pos >= len(oms) - 1 else 0.0
    c = Criterion("q_stabil", "Nachhaltigkeit der Margen", 8, "rule",
                  "Anzahl Jahre mit positiver operativer Marge und deren Schwankung (Standardabweichung) über bis zu "
                  "5 Jahre.", ["alle positiv, Schwankung ≤ 5 Pp.: 8", "alle positiv, ≤ 10 Pp.: 5,5",
                               "höchstens ein Verlustjahr: 3", "sonst: 0"],
                  points=stab, rule="mindestens 3 Geschäftsjahre nötig",
                  facts=[Fact("Operative Margen", ", ".join(f"{P(x)}" for x in oms), "text", "derived")])
    c.reason = (f"{sum(1 for x in oms if x > 0)} von {len(oms)} Jahren positiv, Schwankung "
                f"{P(statistics.pstdev(oms))} → {G(stab)} Punkte.") if stab is not None else "Nicht bewertet."
    if stab is None:
        c.missing.append("weniger als 3 Geschäftsjahre mit operativer Marge")
    cs.append(c)
    fm = _avg(_last(fu, "fcf_margin", 3))
    cs.append(_rule("q_fcf", "Free Cashflow", 15, "Durchschnittliche Free-Cashflow-Marge der letzten bis zu 3 Jahre.",
                    fm, [(0.25, 15), (0.15, 12), (0.08, 8), (0.03, 4), (0.0, 2)], "pct",
                    [Fact("Ø FCF-Marge", fm, "pct", "derived", note="Mittel der FCF-Margen"),
                     _fact("Free Cashflow (letztes Jahr)", fu.latest("fcf"), "money")],
                    rule="FCF = operativer Cashflow − Investitionsausgaben; Marge = FCF / Umsatz",
                    missing="Free Cashflow nicht berechenbar"))
    roic = _avg(_last(fu, "roic", 3))
    rv = fu.latest("roic")
    cs.append(_rule("q_roic", "Kapitalrendite (ROIC)", 15,
                    "Durchschnittlicher ROIC der letzten bis zu 3 Jahre (NOPAT / (Eigenkapital + "
                    "Finanzverbindlichkeiten)).", roic, [(0.25, 15), (0.15, 11), (0.10, 7), (0.05, 3)], "pct",
                    [Fact("Ø ROIC", roic, "pct", rv.origin if rv.v is not None else "missing", note=rv.formula),
                     _fact("ROIC (letztes Jahr)", rv, "pct")], rule="Stufen des Durchschnitts",
                    missing="ROIC nicht berechenbar (investiertes Kapital ≤ 0 oder Daten fehlen)",
                    counter=["ROIC ohne Abzug der Liquidität: bei sehr liquiden Unternehmen eher unterschätzt"]))
    lev, facts, miss = _net_cash_rule(fu)
    cs.append(_rule("q_schulden", "Verschuldung", 10,
                    "Nettofinanzverschuldung in Jahren Free Cashflow (Nettoliquidität = 0 Jahre).", lev,
                    [(0.0, 10), (1.0, 8), (2.0, 6), (3.0, 4), (5.0, 2)], "num", facts, higher_better=False,
                    rule="(Finanzverbindlichkeiten − Liquidität) / FCF; negativer FCF bei Nettoschulden = 0 P.",
                    missing=miss, unit=" J."))
    cr = fu.latest("current_ratio")
    cs.append(_rule("q_liquiditaet", "Liquidität", 5,
                    "Current Ratio (Umlaufvermögen / kurzfristige Verbindlichkeiten).", cr.v,
                    [(2.0, 5), (1.5, 4), (1.0, 2)], "num", [_fact("Current Ratio", cr, "x")],
                    rule="Stufen der Current Ratio", unit="×"))
    dil, dfacts = _dilution_avg(fu)
    cs.append(_rule("q_kapitalallokation", "Kapitalallokation (Aktienanzahl)", 8,
                    "Ø jährliche Veränderung der verwässerten Aktienanzahl – Rückkäufe positiv, Verwässerung negativ.",
                    dil, [(-0.01, 8), (0.01, 6), (0.03, 4), (0.06, 2)], "pct", dfacts, higher_better=False,
                    rule="Stufen der Ø Veränderung", missing="Aktienanzahl über mindestens 2 Jahre fehlt",
                    counter=["Rückkäufe zu hohen Kursen können Kapital vernichten – nicht erfasst"]))
    gr = [x for x in _last(fu, "revenue_growth", 5) if x is not None]
    declines = sum(1 for x in gr if x < 0) if len(gr) >= 2 else None
    cs.append(_rule("q_stetigkeit", "Stabilität der Geschäftsentwicklung", 4,
                    "Anzahl Jahre mit Umsatzrückgang in den letzten bis zu 5 Jahren.", declines,
                    [(0, 4), (1, 2.5), (2, 1)], "num",
                    [Fact("Jahre mit Umsatzrückgang", declines, "num", "derived", note=f"von {len(gr)} Jahren")],
                    higher_better=False, rule="weniger Rückgänge = mehr Punkte",
                    missing="weniger als 2 Wachstumsjahre"))
    cs.append(ai_criterion("q_burggraben", "Wettbewerbsvorteile", 8,
                           "Belegte, dauerhafte Vorteile gegenüber Wettbewerbern (Kosten, Technologie, Marke, "
                           "Wechselkosten, Netzwerk).", ai, "wettbewerbsvorteile"))
    return Score("quality", "Quality", cs)


# ----------------------------------------------------------------------------------------------------------------
# Growth / GARP
# ----------------------------------------------------------------------------------------------------------------

def garp(fu: Fundamentals, ai: dict[str, Any] | None, market: dict[str, Any]) -> Score:
    cs: list[Criterion] = []
    gl, gfacts = _growth_latest(fu)
    cs.append(_rule("g_wachstum", "Aktuelles Umsatzwachstum", 20, "Umsatzwachstum im letzten Geschäftsjahr.", gl,
                    [(0.40, 20), (0.25, 16), (0.15, 12), (0.08, 7), (0.03, 3)], "pct", gfacts,
                    rule="Stufen des Jahreswachstums"))
    g3 = cagr(fu, "revenue", 3)
    cs.append(_rule("g_wachstum3", "Wachstum über 3 Jahre", 10, "Umsatz-CAGR über 3 Jahre.", g3.v,
                    [(0.30, 10), (0.20, 8), (0.12, 6), (0.06, 3)], "pct", [_fact("Umsatz-CAGR 3 J.", g3, "pct")],
                    rule="CAGR-Stufen", missing=g3.note))
    gm = fu.latest("gross_margin")
    cs.append(_rule("g_brutto", "Bruttomarge", 10, "Bruttogewinn / Umsatz (Spielraum für Skalierung).", gm.v,
                    [(0.70, 10), (0.55, 8), (0.40, 5), (0.25, 2)], "pct", [_fact("Bruttomarge", gm, "pct")],
                    rule="Stufen der Bruttomarge"))
    gms = fu.series("gross_margin")
    dgm = (gms[-1] - gms[-4]) if len(gms) >= 4 and gms[-1] is not None and gms[-4] is not None else None
    cs.append(_rule("g_brutto_trend", "Entwicklung der Bruttomarge", 5, "Veränderung der Bruttomarge über 3 Jahre.",
                    dgm, [(0.03, 5), (0.0, 3), (-0.03, 1)], "pct",
                    [Fact("Δ Bruttomarge 3 J. (Prozentpunkte)", dgm, "pct", "derived")], rule="Stufen der Änderung",
                    missing="Bruttomarge über 4 Jahre fehlt"))
    oms = fu.series("op_margin")
    dom = (oms[-1] - oms[-4]) if len(oms) >= 4 and oms[-1] is not None and oms[-4] is not None else None
    cs.append(_rule("g_skalierung", "Operative Skalierbarkeit", 10,
                    "Veränderung der operativen Marge über 3 Jahre (operativer Hebel).", dom,
                    [(0.10, 10), (0.05, 7), (0.01, 4), (-0.02, 2)], "pct",
                    [Fact("Δ operative Marge 3 J.", dom, "pct", "derived")], rule="Stufen der Änderung",
                    missing="operative Marge über 4 Jahre fehlt"))
    fms = fu.series("fcf_margin")
    fl = fms[-1] if fms else None
    fprev = fms[-3] if len(fms) >= 3 else None
    fcf_pts: float | None = None
    if fl is not None:
        fcf_pts = 10.0 if fl >= 0.15 else 7.0 if fl > 0 and fprev is not None and fl > fprev else 5.0 if fl > 0 \
            else 3.0 if fprev is not None and fl > fprev else 0.0
    c = Criterion("g_fcf", "Entwicklung des Free Cashflows", 10, "rule",
                  "Höhe und Richtung der Free-Cashflow-Marge (letztes Jahr ggü. zwei Jahre zuvor).",
                  ["≥ 15 %: 10", "positiv und steigend: 7", "positiv: 5", "negativ, aber steigend: 3", "sonst: 0"],
                  points=fcf_pts, rule="Stufen nach Höhe und Richtung",
                  facts=[Fact("FCF-Marge", fl, "pct", "derived"), Fact("FCF-Marge 2 Jahre zuvor", fprev, "pct",
                                                                        "derived")])
    c.reason = f"FCF-Marge {P(fl)} → {G(fcf_pts)} Punkte." if fcf_pts is not None and fl is not None else \
        "Nicht bewertet: Free Cashflow fehlt."
    if fcf_pts is None:
        c.missing.append("Free Cashflow fehlt")
    cs.append(c)
    gav = market.get("growth_adj_ev_sales")
    cs.append(_rule("g_bewertung", "Wachstum relativ zur Bewertung", 15,
                    "EV/Umsatz geteilt durch das Umsatzwachstum in Prozent (je niedriger, desto günstiger je "
                    "Wachstumspunkt).", gav, [(0.20, 15), (0.35, 11), (0.50, 7), (0.80, 3)], "num",
                    [Fact("EV/Umsatz", market.get("ev_sales"), "x", "derived", source="markt"),
                     Fact("Wachstum (letztes Jahr)", gl, "pct", "derived"),
                     Fact("EV/Umsatz ÷ Wachstum %", gav, "num", "derived")], higher_better=False,
                    rule="(EV / Umsatz) / (Wachstum × 100)",
                    missing="Marktdaten (Kurs/Marktkapitalisierung) oder positives Wachstum fehlen"))
    rw, rfacts = _runway(fu)
    cs.append(_rule("g_finanzierung", "Finanzierungsbedarf", 10,
                    "Reichweite der Liquidität in Jahren bei negativem Free Cashflow (positiver FCF = kein Bedarf).",
                    rw, [(99, 10), (4, 7), (2, 4), (1, 2)], "num", rfacts,
                    rule="Liquidität / |FCF|; FCF ≥ 0 = volle Punkte", missing="FCF oder Liquidität fehlt",
                    unit=" J."))
    dil, dfacts = _dilution_avg(fu)
    cs.append(_rule("g_verwaesserung", "Verwässerung", 5, "Ø jährliche Zunahme der verwässerten Aktienanzahl.", dil,
                    [(0.01, 5), (0.03, 4), (0.06, 2), (0.10, 1)], "pct", dfacts, higher_better=False,
                    rule="Stufen der Ø Veränderung", missing="Aktienanzahl über mindestens 2 Jahre fehlt"))
    a1 = (ai or {}).get("marktgroesse")
    a2 = (ai or {}).get("wettbewerbsvorteile")
    c = ai_criterion("g_potenzial", "Marktpotenzial und Wettbewerbsfähigkeit", 5,
                     "Mittel der KI-Stufen „Marktgröße“ und „Wettbewerbsvorteile“ (siehe Disruption).",
                     {"x": _merge_ai(a1, a2)} if a1 and a2 else None, "x")
    cs.append(c)
    return Score("garp", "Growth / GARP", cs)


def _merge_ai(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {"stufe": (int(a.get("stufe", 0)) + int(b.get("stufe", 0))) // 2,
            "konfidenz": min(float(a.get("konfidenz", 0)), float(b.get("konfidenz", 0))),
            "belege": sorted(set(a.get("belege") or []) | set(b.get("belege") or [])),
            "begruendung": f"Marktgröße: {a.get('begruendung', '')} Wettbewerb: {b.get('begruendung', '')}",
            "gegenargumente": list(a.get("gegenargumente") or []) + list(b.get("gegenargumente") or []),
            "fehlend": list(a.get("fehlend") or []) + list(b.get("fehlend") or [])}


# ----------------------------------------------------------------------------------------------------------------
# Disruption
# ----------------------------------------------------------------------------------------------------------------

def disruption(fu: Fundamentals, ai: dict[str, Any] | None) -> Score:
    cs: list[Criterion] = []
    cs.append(ai_criterion("d_technologie", "Technologische Differenzierung", 12,
                           "Wie schwer ist das Angebot technisch nachzubauen? Belegt durch Produkte, Patente, "
                           "Leistungsdaten oder Kundenaussagen.", ai, "technologie"))
    c = ai_criterion("d_innovation", "Innovationspotenzial", 8,
                     "Fähigkeit, neue Produkte/Märkte zu erschließen (Pipeline, F&E, neue Anwendungen).", ai,
                     "innovation")
    c.facts.append(_fact("F&E-Quote (Information)", fu.latest("rnd_ratio"), "pct"))
    cs.append(c)
    cs.append(ai_criterion("d_markt", "Marktgröße und Marktentwicklung", 12,
                           "Größe und Wachstum des adressierbaren Marktes laut belegten Quellen.", ai, "marktgroesse"))
    gm = fu.latest("gross_margin")
    rule_c = _rule("d_skal_r", "Skalierbarkeit (Regel)", 5, "Bruttomarge als Maß für Grenzkosten.", gm.v,
                   [(0.70, 5), (0.50, 3.5), (0.35, 2), (0.20, 1)], "pct", [_fact("Bruttomarge", gm, "pct")],
                   rule="Stufen der Bruttomarge")
    cs.append(_mixed("d_skalierung", "Skalierbarkeit des Geschäftsmodells",
                     "Wächst der Umsatz schneller als die Kosten? Regelteil: Bruttomarge; KI-Teil: Geschäftsmodell.",
                     rule_c, ai_criterion("d_skal_ai", "Skalierbarkeit (KI)", 5, "", ai, "skalierbarkeit")))
    cs.append(ai_criterion("d_vorteile", "Wettbewerbsvorteile", 14,
                           "Belegte Marktstellung, Kundenbindung, Kostenvorteile; wer könnte sie gefährden?", ai,
                           "wettbewerbsvorteile"))
    cs.append(ai_criterion("d_netzwerk", "Netzwerkeffekte oder Wechselkosten", 8,
                           "Steigt der Nutzen mit der Nutzerzahl bzw. ist ein Wechsel für Kunden teuer?", ai,
                           "netzwerk"))
    gl, gfacts = _growth_latest(fu)
    cs.append(_rule("d_nachfrage", "Nachgewiesene Nachfrage", 12,
                    "Berichtetes Umsatzwachstum im letzten Geschäftsjahr (Nachfrage, die bezahlt wird).", gl,
                    [(0.40, 12), (0.25, 9), (0.15, 6), (0.05, 3)], "pct", gfacts, rule="Stufen des Wachstums",
                    counter=["Wachstum kann durch Zukäufe entstehen – nicht bereinigt"]))
    rev, fm = fu.latest("revenue"), fu.latest("fcf_margin")
    fms = fu.series("fcf_margin")
    mon: float | None = None
    if rev.v is not None:
        mon = (4.0 if rev.v >= 1e8 else 2.0 if rev.v >= 1e7 else 0.0)
        mon += 3.0 if (gm.v or 0) >= 0.5 else 1.5 if (gm.v or 0) >= 0.3 else 0.0
        mon += 3.0 if (fm.v or -1) > 0 else 1.5 if len(fms) >= 2 and fms[-1] is not None and fms[-2] is not None \
            and fms[-1] > fms[-2] else 0.0
    c = Criterion("d_monetarisierung", "Wirtschaftliche Monetarisierung", 10, "rule",
                  "Wird die Technologie zu Geld? Umsatzgröße, Bruttomarge, Free Cashflow.",
                  ["Umsatz ≥ 100 Mio.: 4 (≥ 10 Mio.: 2)", "Bruttomarge ≥ 50 %: 3 (≥ 30 %: 1,5)",
                   "FCF-Marge > 0: 3 (steigend: 1,5)"], points=mon, rule="Summe der drei Teile",
                  facts=[_fact("Umsatz", rev, "money"), _fact("Bruttomarge", gm, "pct"), _fact("FCF-Marge", fm, "pct")])
    c.reason = f"{G(mon)} von 10 Punkten aus Umsatzgröße, Bruttomarge und Free Cashflow." if mon is not None else \
        "Nicht bewertet: Umsatz fehlt."
    if mon is None:
        c.missing.append("Umsatz fehlt")
    cs.append(c)
    gr = [x for x in _last(fu, "revenue_growth", 3) if x is not None]
    oms = fu.series("op_margin")
    exe: float | None = None
    if len(gr) >= 2 and len(oms) >= 3 and oms[-1] is not None and oms[-3] is not None:
        exe = (2.0 if all(x > 0 for x in gr) else 0.0) + (2.0 if oms[-1] >= oms[-3] else 0.0)
    rule_e = Criterion("d_ums_r", "Umsetzung (Regel)", 4, "rule", "Wachstum in jedem Jahr und steigende Marge.",
                       ["Umsatz in jedem der letzten Jahre gewachsen: 2", "operative Marge nicht gesunken: 2"],
                       points=exe, rule="Summe der zwei Teile",
                       facts=[Fact("Wachstumsjahre", ", ".join(f"{P(x, 0)}" for x in gr), "text", "derived")])
    rule_e.reason = f"{G(exe)} von 4 Punkten." if exe is not None else "Nicht bewertet: zu wenige Jahre."
    if exe is None:
        rule_e.missing.append("weniger als 3 Geschäftsjahre")
    cs.append(_mixed("d_umsetzung", "Fähigkeit zur Umsetzung",
                     "Liefert das Unternehmen, was es ankündigt? Regelteil: Zahlenhistorie; KI-Teil: Belege.", rule_e,
                     ai_criterion("d_ums_ai", "Umsetzung (KI)", 4, "", ai, "umsetzung")))
    cs.append(ai_criterion("d_risiken", "Risiken für das Geschäftsmodell", 6,
                           "Abhängigkeiten (Kunden, Lieferanten, Regulierung, Technologie). Hohe Stufe = geringe "
                           "Risiken.", ai, "risiken"))
    return Score("disruption", "Disruption", cs)


# ----------------------------------------------------------------------------------------------------------------
# Tom-Nash-inspirierte Scorecard (125 Punkte)
# ----------------------------------------------------------------------------------------------------------------

def nash(fu: Fundamentals, ai: dict[str, Any] | None, market: dict[str, Any]) -> Score:
    cs: list[Criterion] = []
    g3 = cagr(fu, "revenue", 3)
    if g3.v is None:
        g3 = cagr(fu, "revenue", 2)
    cs.append(_rule("n_umsatz", "Umsatzwachstum", 15, "Umsatz-CAGR über 3 Jahre (ersatzweise 2).", g3.v,
                    [(0.30, 15), (0.20, 12), (0.12, 9), (0.06, 5), (0.0, 2)], "pct", [_fact("Umsatz-CAGR", g3, "pct")],
                    rule="CAGR-Stufen", missing=g3.note))
    om = fu.latest("op_margin")
    cs.append(_rule("n_marge", "Operative Margen", 10, "Operative Marge im letzten Geschäftsjahr.", om.v,
                    [(0.25, 10), (0.15, 8), (0.08, 5), (0.0, 2)], "pct", [_fact("Operative Marge", om, "pct")],
                    rule="Stufen der operativen Marge"))
    lev, facts, miss = _net_cash_rule(fu)
    cs.append(_rule("n_bilanz", "Bilanz und Verschuldung", 10, "Nettofinanzverschuldung in Jahren Free Cashflow.", lev,
                    [(0.0, 10), (1.0, 8), (2.0, 6), (3.0, 3)], "num", facts, higher_better=False,
                    rule="Nettoliquidität = volle Punkte", missing=miss, unit=" J."))
    rw, rfacts = _runway(fu)
    cr = fu.latest("current_ratio")
    liq_pts: float | None = None
    if rw is not None or cr.v is not None:
        liq_pts = (steps(rw, [(99, 6), (4, 4), (2, 2), (1, 1)]) or 0.0) + (steps(cr.v, [(1.5, 4), (1.0, 2)]) or 0.0)
    c = Criterion("n_liquiditaet", "Liquidität", 10, "rule", "Reichweite der Liquidität (6) und Current Ratio (4).",
                  ["FCF ≥ 0: 6; Reichweite ≥ 4 J.: 4; ≥ 2 J.: 2; ≥ 1 J.: 1", "Current Ratio ≥ 1,5: 4; ≥ 1,0: 2"],
                  points=liq_pts, rule="Summe beider Teile", facts=[*rfacts, _fact("Current Ratio", cr, "x")])
    c.reason = f"{G(liq_pts)} von 10 Punkten." if liq_pts is not None else "Nicht bewertet."
    if liq_pts is None:
        c.missing.append("Liquiditätsdaten fehlen")
    cs.append(c)
    perf = market.get("price_cagr_5y")
    cs.append(_rule("n_kurs", "Historische Aktienperformance", 10,
                    "Kursentwicklung p. a. über bis zu 5 Jahre (Schlusskurse, split-bereinigt, Handelswährung).", perf,
                    [(0.25, 10), (0.15, 8), (0.08, 5), (0.0, 2)], "pct",
                    [Fact("Kurs-CAGR", perf, "pct", "derived", market.get("price_cagr_period", ""), "markt")],
                    rule="CAGR aus erstem und letztem Schlusskurs", missing="keine ausreichende Kurshistorie",
                    counter=["Vergangene Kursentwicklung sagt nichts über künftige Renditen"]))
    cs.append(ai_criterion("n_vorteile", "Wettbewerbsvorteile", 15, "Belegte, dauerhafte Wettbewerbsvorteile.", ai,
                           "wettbewerbsvorteile"))
    cs.append(ai_criterion("n_management", "Managementqualität", 10,
                           "Belegte Umsetzungs- und Kapitalallokationsbilanz der Führung; Eigentümerorientierung.", ai,
                           "management"))
    inst = market.get("inst_pct")
    inst_pts = None if inst is None else 5.0 if 0.40 <= inst <= 0.90 else 3.0 if inst >= 0.20 else 1.0
    c = Criterion("n_institutionell", "Institutionelle Beteiligung", 5, "rule",
                  "Anteil institutioneller Anleger (Yahoo Finance). Breite Unterstützung ohne Überfüllung.",
                  ["40–90 %: 5", "20–40 % oder > 90 %: 3", "< 20 %: 1"], points=inst_pts, rule="Bandbreiten",
                  facts=[Fact("Institutioneller Anteil", inst, "pct", "reported" if inst is not None else "missing",
                              source="markt")])
    c.reason = f"{P(inst, 0)} → {G(inst_pts)} Punkte." if inst is not None and inst_pts is not None else \
        "Nicht bewertet: Anteil nicht verfügbar."
    if inst is None:
        c.missing.append("Institutioneller Anteil nicht verfügbar")
    cs.append(c)
    short = market.get("short_pct")
    cs.append(_rule("n_short", "Short Interest", 5, "Leerverkaufte Aktien in % des Streubesitzes (Yahoo Finance).",
                    short, [(0.03, 5), (0.06, 4), (0.10, 2), (0.20, 1)], "pct",
                    [Fact("Short Interest", short, "pct", "reported" if short is not None else "missing",
                          source="markt")], higher_better=False, rule="niedriger = mehr Punkte",
                    missing="Short Interest nicht verfügbar"))
    cs.append(ai_criterion("n_skalierung", "Skalierbarkeit", 10, "Kann das Geschäft ohne proportionale Kosten wachsen?",
                           ai, "skalierbarkeit"))
    cs.append(ai_criterion("n_trend", "Langfristige Branchentrends", 10,
                           "Stützen belegte, langfristige Entwicklungen die Nachfrage?", ai, "branchentrend"))
    cs.append(ai_criterion("n_relevanz", "Wirtschaftliche Relevanz der Produkte", 10,
                           "Wie unverzichtbar sind die Produkte für zahlende Kunden?", ai, "produktrelevanz"))
    cs.append(ai_criterion("n_organisation", "Organisation und Talentgewinnung", 5,
                           "Belege für die Fähigkeit, Fachkräfte zu gewinnen und zu halten.", ai, "organisation"))
    return Score("nash", "Fundamental (Tom-Nash-inspiriert)", cs)
