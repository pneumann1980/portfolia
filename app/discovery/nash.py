"""Tom Nash 125 – Public Reconstruction v1.

Eigenständige Scorecard nach der öffentlich erläuterten 125-Punkte-Methode von Tom Nash (13 Kriterien, 0/5/10 Punkte,
„Recession Resistance“ höchstens 5). Getrennt von „Portfolia Fundamental v1“ (``scoring.fundamental_v1``).

Belegstand (Einzelheiten und Quellen: ``docs/DISCOVERY_INTELLIGENCE.md``, Abschnitt „Tom Nash 125“):

* ``belegt`` – Regel in einer öffentlichen Wiedergabe des Videos „The UNTHINKABLE is About to Happen to Stocks (but
  most will invest wrong)“ (youtube.com/watch?v=6pm5KqQw6RI, Zusammenfassung youtubesummary.com/summary/6pm5KqQw6RI)
  ausdrücklich genannt: Cash vs. Debt, Revenue Growth, Operating Margin, 5-Year Return vs. S&P 500 (Stufen 5 und 10),
  Score-Bereiche, Scalability als Vergleich Umsatz- gegen Kostenwachstum.
* ``auftrag`` – Kriterium in der Quelle genannt, Schwellen nur aus der Auftragsvorgabe (nicht in einer zugänglichen
  Primärquelle verifiziert): Short Interest, Institutional Ownership, Maximalpunkte je Kriterium.
* ``annahme`` – Portfolia-Interpretation, wo die Quelle schweigt (z. B. Lücke zwischen 0 und 10 % Mehrrendite,
  Stufen der Scalability, Stufen der qualitativen Kriterien, Messzeitraum des Umsatzwachstums).

Die Bezeichnung „Original“ wird nicht verwendet: Die Primärquelle (Video/Transkript) war nicht vollständig
zugänglich; die Rekonstruktion ist so gut wie die öffentlichen Belege.
"""

from __future__ import annotations

from typing import Any

from app.discovery.fundamentals import Fundamentals
from app.discovery.scoring import Criterion, Fact, Score, _fact
from app.discovery.text import G, N, P

METHOD = "Tom Nash 125 – Public Reconstruction v1"
METHOD_VERSION = 1
LABEL = "Tom Nash 125"
SOURCE_VIDEO = "https://www.youtube.com/watch?v=6pm5KqQw6RI"
SOURCE_SUMMARY = "https://youtubesummary.com/summary/6pm5KqQw6RI"
PROVENANCE = {
    "belegt": "öffentlich belegt (Wiedergabe des Videos von Tom Nash)",
    "auftrag": "Kriterium belegt, Schwellen aus der Auftragsvorgabe – nicht in einer Primärquelle verifiziert",
    "annahme": "Portfolia-Annahme – die öffentliche Quelle nennt keine Regel",
}

# Qualitative Kriterien (KI-Teil „nash“): Schlüssel → (Bezeichnung, Maximalpunkte, Definition)
AI_CRITERIA: dict[str, tuple[str, float, str]] = {
    "moat": ("Competitive Moat", 10.0,
             "Dauerhafte, belegte Wettbewerbsvorteile (Kosten, Technologie, Marke, Netzwerk, Wechselkosten), die "
             "Margen und Marktanteile gegen Wettbewerber schützen."),
    "ceo": ("CEO Quality", 10.0,
            "Belegte Leistung der Unternehmensführung: Umsetzung angekündigter Ziele, Kapitalallokation, "
            "Eigentümerorientierung, Kontinuität."),
    "rezession": ("Recession Resistance", 5.0,
                  "Wie stabil sind Nachfrage und Umsatz in einem Abschwung (wiederkehrende Umsätze, Notwendigkeit "
                  "der Produkte, Kundenstruktur, Verhalten in früheren Abschwüngen laut Quellen)?"),
    "trend": ("Secular Trend", 10.0,
              "Profitiert das Geschäft von einer langfristigen, belegten Entwicklung (z. B. KI, Elektrifizierung, "
              "Digitalisierung) – nicht nur von einem Zyklus?"),
    "kritikalitaet": ("Product / Mission Criticality", 10.0,
                      "Wie unverzichtbar ist das Produkt für die Kunden (Kernprozess, schwer ersetzbar) bzw. wie "
                      "zentral ist die Mission für zahlende Kunden?"),
    "talent": ("Talent / Culture Quality", 10.0,
               "Belege für die Fähigkeit, Fachkräfte zu gewinnen und zu halten, und für eine leistungsfähige "
               "Unternehmenskultur."),
}
AI_LEVELS = {0: "schwach / nicht erfüllt", 1: "teilweise erfüllt", 2: "klar erfüllt"}
BANDS = (("exceptional", 100.0, "Exceptional (100–125)"), ("watchlist", 75.0, "Watchlist (75 bis unter 100)"),
         ("niedrig", 0.0, "Unter 75 – niedrige Priorität / No-Go nach der Methode"))


def _pp(v: float | None) -> str:
    """Differenz zweier Quoten in Prozentpunkten (z. B. 0,248 → „24,8 Pp.“)."""
    return "–" if v is None else f"{N(v * 100, 1)} Pp."


def _three(value: float | None, low: float, high: float, *, strict_high: bool = True, inverse: bool = False
           ) -> float | None:
    """0/5/10-Stufe. Normal: < low → 0, low … high → 5, > high (bzw. ≥ high) → 10; ``inverse`` kehrt die Richtung um."""
    if value is None:
        return None
    if inverse:
        if value < low:
            return 10.0
        return 5.0 if value < high else 0.0
    if value < low:
        return 0.0
    if (value > high) if strict_high else (value >= high):
        return 10.0
    return 5.0


def _crit(key: str, label: str, mx: float, definition: str, scale: list[str], rule: str, prov: str,
          points: float | None, facts: list[Fact], reason: str, missing: str = "", counter: list[str] | None = None,
          calc: str = "") -> Criterion:
    c = Criterion(key, label, mx, "rule", definition, scale, points=points, rule=rule, facts=facts,
                  counter=counter or [])
    c.parts = [{"art": "Herkunft der Regel", "text": PROVENANCE[prov], "prov": prov}]
    if calc:
        c.parts.append({"art": "Berechnung", "text": calc})
    if points is None:
        c.missing.append(missing or "Eingangswert fehlt")
        c.reason = "Nicht bewertet: " + (missing or "Eingangswert fehlt") + "."
    else:
        c.reason = reason
        c.confidence = 1.0 if all(f.origin in ("reported", "derived") for f in facts if f.value is not None) else 0.6
    return c


def _cash_debt(fu: Fundamentals) -> Criterion:
    liq, debt = fu.latest("liquidity"), fu.latest("debt")
    facts = [_fact("Liquidität (Kasse + kurzfristige Anlagen)", liq, "money"),
             _fact("Finanzverbindlichkeiten", debt, "money")]
    pts: float | None = None
    calc = ""
    miss = ""
    if liq.v is None:
        miss = "Liquidität nicht berichtet"
    else:
        d = debt.v if debt.v is not None else 0.0
        if debt.v is None:
            facts[1].note = "keine Finanzverbindlichkeiten berichtet – als 0 angesetzt (kann fehlende Angabe sein)"
        ratio = (liq.v / d) if d > 0 else None
        pts = 10.0 if (d <= 0 and liq.v > 0) or (ratio is not None and ratio >= 2) else \
            5.0 if liq.v > d else 0.0
        calc = (f"Liquidität / Schulden = {N(ratio)}×" if ratio is not None else
                "keine Finanzverbindlichkeiten → Liquidität übersteigt Schulden mindestens zweifach")
        facts.append(Fact("Verhältnis Liquidität / Schulden", ratio, "x", "derived", note="Liquidität ÷ Schulden"))
    return _crit("nash_cash", "Cash vs. Debt", 10,
                 "Liquide Mittel (inkl. kurzfristiger Anlagen) im Verhältnis zu den Finanzverbindlichkeiten zum "
                 "letzten Bilanzstichtag.",
                 ["mehr Schulden als liquide Mittel: 0", "mehr liquide Mittel als Schulden: 5",
                  "liquide Mittel mindestens doppelt so hoch wie Schulden: 10"],
                 "Liquidität ≥ 2 × Schulden → 10; Liquidität > Schulden → 5; sonst 0", "belegt", pts, facts,
                 f"{calc} → {G(pts)} von 10 Punkten." if pts is not None else "", miss,
                 ["Leasingverbindlichkeiten und Pensionslasten sind nicht als Schulden erfasst"], calc)


def _growth(fu: Fundamentals) -> Criterion:
    v = fu.latest("revenue_growth")
    pts = _three(v.v, 0.10, 0.20)
    facts = [_fact("Umsatzwachstum (letztes Geschäftsjahr ggü. Vorjahr)", v, "pct")]
    q = (fu.quarter or {}).get("revenue") or {}
    if "yoy" in q:
        facts.append(Fact("Information: letztes Quartal ggü. Vorjahresquartal", q["yoy"], "pct", "derived",
                          q["value"].get("period", ""), f"sec:{q['value'].get('accn', '')}"))
    return _crit("nash_umsatz", "Revenue Growth", 10,
                 "Umsatzwachstum des letzten Geschäftsjahres gegenüber dem Vorjahr.",
                 ["unter 10 %: 0", "10–20 %: 5", "über 20 %: 10"], "< 10 % → 0; 10–20 % → 5; > 20 % → 10", "belegt",
                 pts, facts, f"Wachstum {P(v.v)} → {G(pts)} von 10 Punkten." if pts is not None else "",
                 v.note or "Umsatzwachstum nicht berechenbar",
                 ["Messzeitraum (Geschäftsjahr statt letzte 12 Monate) ist eine Portfolia-Annahme",
                  "Wachstum durch Zukäufe ist nicht bereinigt"], "Umsatz / Umsatz Vorjahr − 1")


def _margin(fu: Fundamentals) -> Criterion:
    v = fu.latest("op_margin")
    pts = _three(v.v, 0.10, 0.25)
    return _crit("nash_marge", "Operating Margin", 10, "Operatives Ergebnis / Umsatz im letzten Geschäftsjahr.",
                 ["unter 10 %: 0", "10–25 %: 5", "über 25 %: 10"], "< 10 % → 0; 10–25 % → 5; > 25 % → 10", "belegt",
                 pts, [_fact("Operative Marge", v, "pct")],
                 f"Operative Marge {P(v.v)} → {G(pts)} von 10 Punkten." if pts is not None else "",
                 "operative Marge nicht berechenbar", calc="Operatives Ergebnis / Umsatz")


def _short(market: dict[str, Any]) -> Criterion:
    v = market.get("short_pct")
    pts = _three(v, 0.05, 0.10, inverse=True)
    return _crit("nash_short", "Short Interest", 10, "Leerverkaufte Aktien in % des Streubesitzes (Yahoo Finance).",
                 ["unter 5 %: 10", "5 bis unter 10 %: 5", "10 % und mehr: 0"],
                 "< 5 % → 10; 5–10 % → 5; ≥ 10 % → 0", "auftrag", pts,
                 [Fact("Short Interest", v, "pct", "reported" if v is not None else "missing", source="markt")],
                 f"Short Interest {P(v)} → {G(pts)} von 10 Punkten." if pts is not None else "",
                 "Short Interest beim Datenanbieter nicht verfügbar",
                 ["Meldedaten sind zeitverzögert (halbmonatlich)"])


def _institutional(market: dict[str, Any]) -> Criterion:
    v = market.get("inst_pct")
    pts = None if v is None else 10.0 if v > 0.60 else 5.0 if v >= 0.30 else 0.0
    return _crit("nash_institutionell", "Institutional Ownership", 10,
                 "Anteil institutioneller Anleger an den ausstehenden Aktien (Yahoo Finance).",
                 ["unter 30 %: 0", "30–60 %: 5", "über 60 %: 10"], "< 30 % → 0; 30–60 % → 5; > 60 % → 10", "auftrag",
                 pts, [Fact("Institutioneller Anteil", v, "pct", "reported" if v is not None else "missing",
                            source="markt")],
                 f"Institutioneller Anteil {P(v, 0)} → {G(pts)} von 10 Punkten." if pts is not None else "",
                 "institutioneller Anteil beim Datenanbieter nicht verfügbar")


def _scalability(fu: Fundamentals) -> Criterion:
    g, o = fu.latest("revenue_growth"), fu.latest("opex_growth")
    ox = fu.latest("opex")
    diff = (g.v - o.v) if g.v is not None and o.v is not None and g.period == o.period else None
    pts = None if diff is None else 10.0 if diff >= 0.05 else 5.0 if diff > 0 else 0.0
    facts = [_fact("Umsatzwachstum", g, "pct"), _fact("Wachstum der operativen Aufwendungen", o, "pct"),
             _fact("Operative Aufwendungen", ox, "money"),
             Fact("Differenz (Prozentpunkte)", diff, "pct", "derived", note="Umsatzwachstum − Kostenwachstum")]
    return _crit("nash_skalierung", "Scalability", 10,
                 "Wächst der Umsatz schneller als die operativen Aufwendungen (gleiches Geschäftsjahr)?",
                 ["Umsatz wächst mindestens 5 Prozentpunkte schneller als die Kosten: 10",
                  "Umsatz wächst schneller (weniger als 5 Pp.): 5", "Kosten wachsen gleich schnell oder schneller: 0"],
                 "Vergleich belegt (Beispiel: Umsatz +20 % bei Kosten +5 % = skalierbar); die Stufen 0/5/10 sind eine "
                 "Portfolia-Annahme", "annahme", pts, facts,
                 f"Umsatz {P(g.v)} gegenüber Kosten {P(o.v)} (Differenz {_pp(diff)}) → {G(pts)} von 10 Punkten."
                 if pts is not None else "", "Umsatz- oder Kostenwachstum desselben Jahres fehlt",
                 ["Einmalaufwendungen (Restrukturierung, Abschreibungen) verzerren das Kostenwachstum"],
                 "Umsatzwachstum − Wachstum operative Aufwendungen")


def _relative_return(market: dict[str, Any]) -> Criterion:
    tr, bench = market.get("tr_5y"), market.get("spx_tr_5y")
    diff = (tr - bench) if tr is not None and bench is not None else None
    pts = None if diff is None else 10.0 if diff > 0.20 else 5.0 if diff >= 0.10 else 0.0
    facts = [Fact("Gesamtrendite Aktie (5 Jahre)", tr, "pct", "derived" if tr is not None else "missing",
                  market.get("tr_period", ""), "markt", market.get("tr_note", "")),
             Fact("Gesamtrendite S&P 500 (gleicher Zeitraum)", bench, "pct",
                  "derived" if bench is not None else "missing", market.get("tr_period", ""), "markt",
                  market.get("tr_bench_note", "")),
             Fact("Mehrrendite (Prozentpunkte)", diff, "pct", "derived", note="Aktie − S&P 500")]
    return _crit("nash_rendite", "5-Year Return vs. S&P 500", 10,
                 "Kumulierte Gesamtrendite (Kurs, Splits, Dividenden) der Aktie über 5 Jahre im Vergleich zum "
                 "S&P 500 über exakt denselben Zeitraum, in US-Dollar.",
                 ["mehr als 20 Prozentpunkte besser: 10", "10–20 Prozentpunkte besser: 5",
                  "schlechter oder weniger als 10 Prozentpunkte besser: 0"],
                 "> 20 Pp. → 10; 10–20 Pp. → 5; Unterperformance → 0 (belegt); 0–10 Pp. → 0 (Portfolia-Annahme: die "
                 "Quelle nennt diesen Bereich nicht)", "belegt", pts, facts,
                 f"Aktie {P(tr, 0)} gegenüber S&P 500 {P(bench, 0)} (Differenz {_pp(diff)}) → {G(pts)} von 10 "
                 "Punkten." if pts is not None else "",
                 market.get("tr_missing") or "Gesamtrendite über 5 Jahre nicht verfügbar",
                 ["Vergangene Renditen sagen nichts über künftige", "Ob Tom Nash Kurs- oder Gesamtrendite vergleicht, "
                  "ist nicht belegt (Portfolia: Gesamtrendite)"],
                 "Rendite = Endwert / Anfangswert − 1 (dividenden- und splitbereinigte Schlusskurse)")


def _ai(key: str, ai: dict[str, Any] | None) -> Criterion:
    label, mx, definition = AI_CRITERIA[key]
    scale = [f"Stufe {lvl} ({txt}): {G(lvl / 2 * mx)} Punkte" for lvl, txt in AI_LEVELS.items()]
    c = Criterion(f"nash_{key}", label, mx, "ai", definition, scale,
                  rule="Punkte = Stufe / 2 × Maximum. Bewertung nur mit mindestens einem Beleg aus den Quellen; "
                       "Stufe 2 nur mit wörtlich gefundener Belegstelle und Konfidenz ≥ 0,5, sonst Stufe 1.")
    c.parts = [{"art": "Herkunft der Regel", "prov": "annahme",
                "text": "Kriterium öffentlich belegt; Stufen 0/5/10 (Recession Resistance 0/2,5/5) und Belegpflicht "
                        "sind Portfolia-Regeln – Tom Nash bewertet diese Kriterien nach eigener Einschätzung"}]
    a = (ai or {}).get(key)
    if not a:
        c.missing.append("keine KI-Einschätzung (KI aus, Budget erschöpft, Teil fehlgeschlagen oder noch nicht "
                         "erstellt)")
        c.reason = "Nicht bewertet: qualitative Einschätzung fehlt."
        return c
    belege = [b for b in (a.get("belege") or []) if isinstance(b, dict)]
    ids = [b["id"] for b in belege if b.get("id")]
    verified = [b for b in belege if b.get("geprueft")]
    level = int(a.get("stufe", 0))
    conf = float(a.get("konfidenz", 0.0))
    caps = []
    if not ids:
        c.missing.append("kein Beleg aus den Quellen – Kriterium bleibt unbewertet")
        c.reason = "Nicht bewertet: die KI-Einschätzung nennt keinen Beleg. " + str(a.get("begruendung") or "")
        c.counter = list(a.get("gegenargumente") or [])
        return c
    if level == 2 and not verified:
        level = 1
        caps.append("keine wörtlich gefundene Belegstelle: auf Stufe 1 gekappt")
    if level == 2 and conf < 0.5:
        level = 1
        caps.append("Konfidenz < 0,5: auf Stufe 1 gekappt")
    c.points = round(level / 2 * mx, 1)
    c.confidence = conf
    c.sources = sorted(set(ids))
    c.reason = str(a.get("begruendung") or "") + (f" ({'; '.join(caps)})" if caps else "")
    c.counter = list(a.get("gegenargumente") or [])
    c.missing = list(a.get("fehlend") or [])
    c.facts = [Fact("Stufe", f"{level} – {AI_LEVELS[level]}", "text", "estimated", note="; ".join(caps)),
               Fact("Datenqualität laut KI", a.get("datenqualitaet") or "–", "text", "estimated")]
    for b in belege[:4]:
        c.facts.append(Fact(f"Beleg {b.get('id')}", b.get("zitat") or "(nur Quellen-ID)", "text",
                            "reported" if b.get("geprueft") else "estimated", source=str(b.get("id") or ""),
                            note="Belegstelle wörtlich in der Quelle gefunden" if b.get("geprueft") else
                            "Belegstelle nicht wörtlich gefunden – nur als Hinweis"))
    return c


def nash125(fu: Fundamentals, ai: dict[str, Any] | None, market: dict[str, Any]) -> Score:
    cs = [_cash_debt(fu), _growth(fu), _margin(fu), _short(market), _institutional(market), _scalability(fu),
          _relative_return(market)]
    cs += [_ai(k, ai) for k in ("moat", "ceo", "rezession", "trend", "kritikalitaet", "talent")]
    return Score("nash125", LABEL, cs)


def band(score: dict[str, Any]) -> dict[str, Any]:
    """Einordnung nach den öffentlich beschriebenen Bereichen – bei fehlenden Kriterien nur, wenn sie feststeht.

    ``lo`` = erreichte Punkte, ``hi`` = erreichbar, wenn alle fehlenden Kriterien die Höchstpunktzahl erhielten."""
    crit = score.get("criteria") or []
    lo = round(sum(float(c.get("points") or 0.0) for c in crit), 1)
    open_max = sum(float(c.get("max") or 0.0) for c in crit if c.get("points") is None)
    hi = round(lo + open_max, 1)
    n_open = sum(1 for c in crit if c.get("points") is None)
    for key, thr, label in BANDS:
        nxt = {"exceptional": 125.01, "watchlist": 100.0, "niedrig": 75.0}[key]
        if lo >= thr and hi < nxt:
            return {"key": key, "label": label, "sure": True, "lo": lo, "hi": hi, "open": n_open,
                    "note": "" if not n_open else f"steht trotz {n_open} fehlender Kriterien fest"}
    return {"key": "offen", "label": "Nicht bestimmbar", "sure": False, "lo": lo, "hi": hi, "open": n_open,
            "note": f"{n_open} Kriterien fehlen – möglich sind {G(lo)} bis {G(hi)} Punkte"}
