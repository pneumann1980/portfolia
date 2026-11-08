"""Aktienbewertung: Multiplikatoren, drei Szenarien mit offengelegten Annahmen, eingepreistes Wachstum, Valuation Score.

Keine Kursprognose: Die Szenarien zeigen, welcher Wert sich *unter den genannten Annahmen* ergäbe. Alle Annahmen sind
Modellannahmen (``estimated``) und stehen am Ergebnis.

Szenario-Modell (5 Jahre, je Szenario):

* Umsatz_t = Umsatz_{t−1} × (1 + g_t); g_t fällt linear von g (Jahr 1) auf ``TERMINAL_G`` (Jahr 5).
* FCF_5 = Umsatz_5 × Ziel-FCF-Marge.
* Eigenkapitalwert_5 = FCF_5 × Ziel-Multiplikator (Kurs/FCF) + heutige Nettofinanzposition.
* Aktien_5 = Aktien heute × (1 + Verwässerung)^5; Wert je Aktie_5 = Eigenkapitalwert_5 / Aktien_5.
* Rendite p. a. = (Wert je Aktie_5 / Kurs)^(1/5) − 1; Barwert heute = Wert je Aktie_5 / (1 + ``DISCOUNT``)^5.

Eingepreistes Wachstum: das g, bei dem der Barwert im Basisszenario (Marge und Multiplikator der Basis) dem Kurs
entspricht (Bisektion).
"""

from __future__ import annotations

import math
from typing import Any

from app.discovery.fundamentals import Fundamentals, cagr
from app.discovery.scoring import Criterion, Fact, Score, _rule, steps
from app.discovery.text import G, P

YEARS = 5
TERMINAL_G = 0.04
DISCOUNT = 0.09
MULTIPLES = {"konservativ": 15.0, "basis": 22.0, "optimistisch": 30.0}


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def market_metrics(fu: Fundamentals, quote: dict[str, Any]) -> dict[str, Any]:
    """Multiplikatoren aus Kurs/Marktkapitalisierung (``quote``) und Fundamentaldaten.

    ``quote``: price, ccy, at, market_cap (Handelswährung), fx (Faktor Berichtswährung → Handelswährung, 1 bei gleicher
    Währung), forward_pe, inst_pct, short_pct, price_cagr_5y, price_cagr_period, source."""
    m: dict[str, Any] = {k: quote.get(k) for k in ("price", "ccy", "at", "forward_pe", "inst_pct", "short_pct",
                                                   "price_cagr_5y", "price_cagr_period", "source", "fx", "stale")}
    fx = quote.get("fx")
    rev, ni, oi, fcf = (fu.latest(k).v for k in ("revenue", "net_income", "operating_income", "fcf"))
    nc = fu.latest("net_cash").v
    shares = fu.shares_outstanding.v or fu.latest("shares_diluted").v
    notes: list[str] = []
    mcap = quote.get("market_cap")
    m["market_cap_origin"] = "reported" if mcap else "missing"
    if not mcap and shares and quote.get("price") and fx is not None:
        mcap = shares * float(quote["price"])
        m["market_cap_origin"] = "derived"
        notes.append("Marktkapitalisierung = Aktienanzahl (SEC) × Kurs")
    m["market_cap"] = mcap
    if fx is None:
        notes.append("Berichtswährung und Handelswährung nicht umrechenbar – keine Multiplikatoren")
        m["notes"] = notes
        return m
    conv = (lambda x: None if x is None else x * fx)  # noqa: E731 – Berichtswährung → Handelswährung
    if fx != 1.0:
        notes.append(f"Fundamentaldaten mit Faktor {fx:.4f} in die Handelswährung umgerechnet (aktueller Devisenkurs)")
    ev = mcap - conv(nc) if mcap is not None and nc is not None else None
    m.update({
        "ev": ev,
        "pe": _safe_div(mcap, conv(ni)) if ni and ni > 0 else None,
        "ev_sales": _safe_div(ev, conv(rev)) if rev and rev > 0 else None,
        "ev_ebit": _safe_div(ev, conv(oi)) if oi and oi > 0 else None,
        "p_fcf": _safe_div(mcap, conv(fcf)) if fcf and fcf > 0 else None,
        "fcf_yield": _safe_div(conv(fcf), mcap) if fcf is not None and mcap else None,
        "shares": shares,
    })
    g = fu.latest("revenue_growth").v
    m["growth_adj_ev_sales"] = (m["ev_sales"] / (g * 100)) if m.get("ev_sales") and g and g > 0 else None
    m["peg"] = (m["pe"] / (g * 100)) if m.get("pe") and g and g > 0 else None
    m["notes"] = notes
    return m


def _project(rev0: float, g: float, margin: float, multiple: float, net_cash: float, shares: float, dil: float
             ) -> dict[str, Any]:
    rev, path = rev0, []
    for t in range(1, YEARS + 1):
        gt = g + (TERMINAL_G - g) * (t - 1) / (YEARS - 1)
        rev *= 1 + gt
        path.append({"jahr": t, "wachstum": gt, "umsatz": rev})
    fcf5 = rev * margin
    equity5 = fcf5 * multiple + net_cash
    shares5 = shares * (1 + dil) ** YEARS
    per_share = equity5 / shares5 if shares5 > 0 else None
    return {"pfad": path, "umsatz_5": rev, "fcf_5": fcf5, "eigenkapitalwert_5": equity5, "aktien_5": shares5,
            "wert_je_aktie_5": per_share,
            "barwert_je_aktie": per_share / (1 + DISCOUNT) ** YEARS if per_share is not None else None}


def scenarios(fu: Fundamentals, market: dict[str, Any]) -> dict[str, Any]:
    """Drei Szenarien. Ohne Kurs, Umsatz oder Aktienanzahl in gleicher Währung: nicht berechenbar (Grund)."""
    price, fx = market.get("price"), market.get("fx")
    rev = fu.latest("revenue").v
    shares = market.get("shares")
    nc = fu.latest("net_cash").v
    why = [t for t, ok in (("Kurs fehlt", price), ("Umsatz fehlt", rev), ("Aktienanzahl fehlt", shares),
                           ("Währungen nicht umrechenbar", fx is not None), ("Nettofinanzposition fehlt",
                                                                            nc is not None)) if not ok]
    if why or rev is None or shares is None or nc is None or fx is None or price is None:
        return {"available": False, "reason": ", ".join(why)}
    rev_c, nc_c = rev * fx, nc * fx
    g1 = fu.latest("revenue_growth").v
    g3 = cagr(fu, "revenue", 3).v
    gs = [x for x in (g1, g3) if x is not None]
    g_base = max(-0.10, min(0.50, sum(gs) / len(gs))) if gs else TERMINAL_G
    gm = fu.latest("gross_margin").v
    fms = [x for x in fu.series("fcf_margin")[-3:] if x is not None]
    fcf_m = sum(fms) / len(fms) if fms else None
    profitable = fcf_m is not None and fcf_m > 0.02
    if fcf_m is not None and profitable:
        margins = {"konservativ": fcf_m * 0.8, "basis": fcf_m,
                   "optimistisch": max(fcf_m, min(fcf_m * 1.2, gm * 0.6 if gm else fcf_m * 1.2))}
        margin_rule = "Ø FCF-Marge der letzten bis zu 3 Jahre (konservativ × 0,8; optimistisch × 1,2, höchstens 60 % " \
                      "der Bruttomarge)"
    elif gm is not None and gm > 0:
        margins = {"konservativ": gm * 0.15, "basis": gm * 0.25, "optimistisch": gm * 0.35}
        margin_rule = "noch keine nachhaltige FCF-Marge: Ziel-FCF-Marge = Anteil der Bruttomarge (15 % / 25 % / 35 %)"
    else:
        return {"available": False, "reason": "weder FCF-Marge noch Bruttomarge für eine Zielmarge vorhanden"}
    dils = [x for x in fu.series("dilution")[-3:] if x is not None]
    d_base = max(0.0, min(0.08, sum(dils) / len(dils))) if dils else 0.02
    growths = {"konservativ": g_base * 0.5 if g_base > 0 else g_base - 0.05, "basis": g_base,
               "optimistisch": min(0.8, g_base * 1.4) if g_base > 0 else g_base + 0.05}
    dil = {"konservativ": d_base + 0.01, "basis": d_base, "optimistisch": max(0.0, d_base - 0.01)}
    out: dict[str, Any] = {"available": True, "years": YEARS, "discount": DISCOUNT, "terminal_g": TERMINAL_G,
                           "price": price, "ccy": market.get("ccy"), "profitable": profitable,
                           "assumptions": {
                               "wachstum": f"Startwachstum Basis = Mittel aus letztem Jahreswachstum und 3-J.-CAGR "
                                           f"({P(g_base)}), begrenzt auf −10 % … 50 %; konservativ × 0,5, "
                                           f"optimistisch × 1,4; fällt linear auf {P(TERMINAL_G, 0)} im Jahr {YEARS}",
                               "marge": margin_rule,
                               "multiplikator": "Kurs/FCF im Jahr 5: " + " / ".join(f"{k} {G(v)}"
                                                                                  for k, v in MULTIPLES.items()),
                               "verwaesserung": f"Ø Veränderung der Aktienanzahl ({P(d_base)}, 0–8 %); konservativ "
                                                "+1 Pp., optimistisch −1 Pp.",
                               "nettofinanzposition": "heutige Nettofinanzposition bleibt unverändert (vereinfacht)",
                               "diskontierung": f"Barwert mit {P(DISCOUNT, 0)} p. a. (geforderte Rendite, Annahme)"},
                           "cases": {}}
    for k in ("konservativ", "basis", "optimistisch"):
        res = _project(rev_c, growths[k], margins[k], MULTIPLES[k], nc_c, shares, dil[k])
        ps = res["wert_je_aktie_5"]
        res.update({"g": growths[k], "marge": margins[k], "multiplikator": MULTIPLES[k], "verwaesserung": dil[k],
                    "rendite_pa": ((ps / price) ** (1 / YEARS) - 1) if ps and ps > 0 else None})
        out["cases"][k] = res
    out["implied_growth"] = implied_growth(rev_c, margins["basis"], MULTIPLES["basis"], nc_c, shares, dil["basis"],
                                           price)
    return out


def implied_growth(rev: float, margin: float, multiple: float, net_cash: float, shares: float, dil: float,
                   price: float) -> float | None:
    """Startwachstum, bei dem der Barwert im Basismodell dem Kurs entspricht (−30 % … +150 %); ``None`` außerhalb."""
    def pv(g: float) -> float:
        return _project(rev, g, margin, multiple, net_cash, shares, dil)["barwert_je_aktie"] or 0.0

    lo, hi = -0.30, 1.50
    if pv(lo) > price or pv(hi) < price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if pv(mid) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def valuation_score(fu: Fundamentals, market: dict[str, Any], scen: dict[str, Any], coverage: float) -> Score:
    cs: list[Criterion] = []
    profitable = bool(scen.get("profitable")) if scen.get("available") else (fu.latest("fcf").v or 0) > 0
    if profitable:
        cs.append(_rule("v_niveau", "Bewertungsniveau (Free-Cashflow-Rendite)", 30,
                        "Free Cashflow (letztes Geschäftsjahr) / Marktkapitalisierung – für profitable Unternehmen.",
                        market.get("fcf_yield"),
                        [(0.06, 30), (0.04, 24), (0.025, 16), (0.015, 9), (0.005, 4)], "pct",
                        [Fact("FCF-Rendite", market.get("fcf_yield"), "pct", "derived", source="markt"),
                         Fact("Kurs/FCF", market.get("p_fcf"), "x", "derived", source="markt"),
                         Fact("KGV (letztes Geschäftsjahr)", market.get("pe"), "x", "derived", source="markt"),
                         Fact("Forward-KGV (Analystenschätzung, Yahoo)", market.get("forward_pe"), "x", "estimated",
                              source="markt")],
                        rule="Stufen der FCF-Rendite", missing="Marktkapitalisierung oder FCF fehlt"))
    else:
        cs.append(_rule("v_niveau", "Bewertungsniveau (EV/Umsatz)", 30,
                        "Unternehmenswert / Umsatz – für (noch) nicht profitable Unternehmen.", market.get("ev_sales"),
                        [(2, 30), (4, 22), (7, 14), (12, 7), (20, 3)], "num",
                        [Fact("EV/Umsatz", market.get("ev_sales"), "x", "derived", source="markt")],
                        higher_better=False, rule="Stufen von EV/Umsatz", missing="Marktdaten fehlen", unit="×"))
    if market.get("peg") is not None:
        cs.append(_rule("v_wachstum", "Wachstum relativ zur Bewertung (PEG)", 25,
                        "KGV / Umsatzwachstum in Prozent (Umsatz statt Gewinnwachstum: robuster bei Sondereffekten).",
                        market.get("peg"), [(1.0, 25), (1.5, 19), (2.0, 12), (3.0, 5)], "num",
                        [Fact("KGV", market.get("pe"), "x", "derived", source="markt"),
                         Fact("PEG (Umsatz)", market.get("peg"), "num", "derived")], higher_better=False,
                        rule="KGV ÷ (Wachstum × 100)"))
    else:
        cs.append(_rule("v_wachstum", "Wachstum relativ zur Bewertung (EV/Umsatz je Wachstumspunkt)", 25,
                        "EV/Umsatz ÷ Umsatzwachstum in Prozent (ohne positives KGV).",
                        market.get("growth_adj_ev_sales"),
                        [(0.20, 25), (0.35, 19), (0.50, 12), (0.80, 5)], "num",
                        [Fact("EV/Umsatz ÷ Wachstum %", market.get("growth_adj_ev_sales"), "num", "derived")],
                        higher_better=False, rule="(EV/Umsatz) ÷ (Wachstum × 100)",
                        missing="Marktdaten oder positives Wachstum fehlen"))
    base = (scen.get("cases") or {}).get("basis") or {}
    cs.append(_rule("v_rendite", "Erwartete Rendite im Basisszenario", 25,
                    f"Rendite p. a. bis Jahr {YEARS}, wenn die Basisannahmen eintreten (keine Prognose).",
                    base.get("rendite_pa"), [(0.15, 25), (0.10, 19), (0.06, 12), (0.02, 6)], "pct",
                    [Fact("Rendite p. a. (Basis)", base.get("rendite_pa"), "pct", "estimated"),
                     Fact("Eingepreistes Startwachstum", scen.get("implied_growth"), "pct", "estimated")],
                    rule="Stufen der Modellrendite", missing=scen.get("reason", "Szenarien nicht berechenbar")))
    unc: float | None = None
    spread = None
    if scen.get("available"):
        lo = scen["cases"]["konservativ"].get("wert_je_aktie_5")
        hi = scen["cases"]["optimistisch"].get("wert_je_aktie_5")
        mid = base.get("wert_je_aktie_5")
        if lo and hi and mid and mid > 0:
            spread = (hi - lo) / mid
            unc = steps(spread, [(0.6, 10), (1.0, 7), (1.5, 4), (99, 1)], higher_better=False)
            if coverage < 0.7 and unc is not None:
                unc = min(unc, 3.0)
    c = Criterion("v_unsicherheit", "Unsicherheit der Bewertung", 10, "rule",
                  "Spannweite optimistisch − konservativ relativ zum Basiswert; geringe Datenabdeckung kappt auf 3.",
                  ["≤ 60 %: 10", "≤ 100 %: 7", "≤ 150 %: 4", "darüber: 1", "Datenabdeckung < 70 %: höchstens 3"],
                  points=unc, rule="Stufen der Spannweite",
                  facts=[Fact("Spannweite / Basiswert", spread, "pct", "estimated"),
                         Fact("Datenabdeckung der Fundamentalwerte", coverage, "pct", "derived")])
    c.reason = f"Spannweite {P(spread, 0)} → {G(unc)} Punkte." if unc is not None and spread is not None else \
        "Nicht bewertet: Szenarien fehlen."
    if unc is None:
        c.missing.append("Szenarien nicht berechenbar")
    cs.append(c)
    nc = fu.latest("net_cash").v
    fcf = fu.latest("fcf").v
    liq = fu.latest("liquidity").v
    risk: float | None = None
    if nc is not None:
        if nc >= 0:
            risk = 10.0
        elif fcf is not None and fcf > 0:
            risk = steps(-nc / fcf, [(2, 7), (4, 4), (99, 1)], higher_better=False)
        else:
            risk = 0.0
    elif fcf is not None and fcf < 0 and liq:
        risk = steps(liq / -fcf, [(4, 6), (2, 3), (0, 0)])
    c = Criterion("v_risiko", "Finanzielle Risiken", 10, "rule",
                  "Nettofinanzposition und Schuldentilgungsfähigkeit aus dem Free Cashflow.",
                  ["Nettoliquidität: 10", "Nettoschulden ≤ 2 Jahre FCF: 7", "≤ 4 Jahre: 4", "darüber: 1",
                   "Nettoschulden bei negativem FCF: 0"], points=risk, rule="Stufen",
                  facts=[Fact("Nettofinanzposition", nc, "money", "derived"), Fact("Free Cashflow", fcf, "money",
                                                                                   "derived")])
    c.reason = f"{G(risk)} von 10 Punkten." if risk is not None else "Nicht bewertet."
    if risk is None:
        c.missing.append("Nettofinanzposition fehlt")
    cs.append(c)
    return Score("valuation", "Valuation", cs)


def price_cagr(rows: list[tuple[str, float]]) -> tuple[float | None, str]:
    """Kurs-CAGR aus (Datum, split-bereinigter Schluss) – mindestens 1 Jahr Abstand."""
    if len(rows) < 2:
        return None, ""
    (d0, p0), (d1, p1) = rows[0], rows[-1]
    from datetime import date

    years = (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25
    if years < 1 or p0 <= 0 or p1 <= 0:
        return None, ""
    return (p1 / p0) ** (1 / years) - 1, f"{d0}–{d1}"


def finite(x: Any) -> bool:
    return isinstance(x, int | float) and math.isfinite(x)
