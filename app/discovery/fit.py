"""Portfolio-Fit und Kauf-Simulation – ausschließlich lokal.

Grundlage ist die vorhandene Bewertung (:meth:`AppContext.valuation`). Branche, Land und Währung gehaltener
Wertpapiere stammen aus den Stammdaten der Kursablage (Yahoo-Info) – fehlen sie, steht die Position unter „unbekannt“
und die Abdeckung sinkt. Die Simulation rechnet eine **hypothetische** Position (Betrag in EUR) hinzu; sie erzeugt
keine Buchung, keine Order und verändert keine Daten. ETF-Bestandteile sind nicht verfügbar – ETF-Überschneidungen
werden deshalb nicht bewertet (Hinweis im Ergebnis).
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta
from typing import Any

import numpy as np

from app.discovery.identity import norm_name
from app.discovery.scoring import Criterion, Fact, Score, steps
from app.discovery.text import G, N, P
from app.util.timeutil import today_local

DEFAULT_AMOUNT = 1000.0
MIN_OVERLAP_DAYS = 120


def _info(ctx: Any, asset: Any) -> dict[str, Any]:
    series = ctx.prices.series_for(asset)
    info = (ctx.store.info(series)[0] or {}) if series else {}
    return info


def holdings(ctx: Any) -> list[dict[str, Any]]:
    """Gehaltene Positionen mit Wert (EUR), Klasse, Branche, Branche (fein), Land, Währung."""
    val = ctx.valuation()
    if val is None:
        return []
    out = []
    for p in val.positions:
        if not p.value or p.value <= 0 or p.asset.is_fiat:
            continue
        info = _info(ctx, p.asset) if p.asset.is_security else {}
        ccy = (getattr(p.price, "ccy", None) or info.get("currency") or ("EUR" if p.asset.is_fiat else None))
        out.append({"asset_id": p.asset.asset_id, "name": p.asset.name, "value": float(p.value),
                    "klasse": p.asset.segment, "sector": info.get("sector") or ("Krypto" if p.asset.is_crypto else
                                                                               None),
                    "industry": info.get("industry"), "country": info.get("country"),
                    "ccy": "Krypto" if p.asset.is_crypto else (str(ccy).upper() if ccy else None),
                    "quote_id": p.asset.quote_id, "norm": norm_name(p.asset.name), "qtype": info.get("quoteType")})
    return out


def held_match(company: dict[str, Any], hs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Positionen, die dasselbe Unternehmen sind (Ticker/Symbol ohne Börsenzusatz oder gleicher Name)."""
    tickers = {t.upper() for t in company.get("tickers") or [company.get("ticker", "")]} - {""}
    name = norm_name(company.get("name", ""))
    out = []
    for h in hs:
        qid = str(h.get("quote_id") or "").upper()
        if (qid and (qid in tickers or qid.split(".")[0] in tickers)) or (name and h["norm"] and (
                h["norm"] == name or h["norm"].startswith(name + " ") or name.startswith(h["norm"] + " "))):
            out.append(h)
    return out


def _dist(hs: list[dict[str, Any]], key: str, extra: tuple[str | None, float] | None = None) -> dict[str, float]:
    tot = sum(h["value"] for h in hs) + (extra[1] if extra else 0.0)
    d: dict[str, float] = defaultdict(float)
    for h in hs:
        d[h.get(key) or "unbekannt"] += h["value"]
    if extra:
        d[extra[0] or "unbekannt"] += extra[1]
    return {k: v / tot for k, v in sorted(d.items(), key=lambda kv: -kv[1])} if tot else {}


def _hhi(weights: list[float]) -> float:
    return sum(w * w for w in weights)


def correlation(ctx: Any, series: str | None) -> tuple[float | None, int]:
    """Korrelation der Tagesrenditen (EUR) des Kandidaten mit dem Gesamtportfolio (zahlungsbereinigt), bis 1 Jahr."""
    if not series:
        return None, 0
    hist = ctx.history_with_live()
    if hist is None or hist.n < 30:
        return None, 0
    from app.prices.market import market_data

    start = today_local() - timedelta(days=370)
    cand = {d: eur for d, eur, _n, _s, _r in market_data(ctx).eur_series(series, start)}
    rp, rc = [], []
    prev_v: float | None = None
    prev_c: float | None = None
    for i, d in enumerate(hist.dates):
        v = float(hist.value[i])
        c = cand.get(d)
        if d >= start and prev_v and prev_v > 0 and c and prev_c:
            flow = float(hist.inflow[i]) - float(hist.outflow[i])
            rp.append((v - flow) / prev_v - 1)
            rc.append(c / prev_c - 1)
        prev_v = v
        prev_c = c if c else prev_c
    n = len(rp)
    if n < MIN_OVERLAP_DAYS:
        return None, n
    a, b = np.array(rp), np.array(rc)
    if a.std() == 0 or b.std() == 0:
        return None, n
    r = float(np.corrcoef(a, b)[0, 1])
    return (r if math.isfinite(r) else None), n


def simulate(ctx: Any, company: dict[str, Any], amount: float = DEFAULT_AMOUNT) -> dict[str, Any]:
    """Vorher/Nachher einer hypothetischen Position (EUR) – keine Buchung."""
    hs = holdings(ctx)
    total = sum(h["value"] for h in hs)
    amount = max(0.0, float(amount))
    same = held_match(company, hs)
    sector, industry = company.get("sector"), company.get("industry")
    country, ccy = company.get("country"), (company.get("ccy") or "").upper() or None
    after_total = total + amount
    held_value = sum(h["value"] for h in same)
    new_w = (amount + held_value) / after_total if after_total else 0.0
    weights_before = sorted((h["value"] / total for h in hs), reverse=True) if total else []
    weights_after = sorted([h["value"] / after_total for h in hs if h not in same] + [new_w], reverse=True) \
        if after_total else []
    res = {
        "amount": amount, "total_before": total, "total_after": after_total, "new_weight": new_w,
        "held": [{"name": h["name"], "value": h["value"]} for h in same], "n_positions": len(hs),
        "sector_before": _dist(hs, "sector"), "sector_after": _dist(hs, "sector", (sector, amount)),
        "industry_before": _dist(hs, "industry"), "industry_after": _dist(hs, "industry", (industry, amount)),
        "country_before": _dist(hs, "country"), "country_after": _dist(hs, "country", (country, amount)),
        "ccy_before": _dist(hs, "ccy"), "ccy_after": _dist(hs, "ccy", (ccy, amount)),
        "class_before": _dist(hs, "klasse"), "class_after": _dist(hs, "klasse", ("Aktien", amount)),
        "top5_before": sum(weights_before[:5]), "top5_after": sum(weights_after[:5]),
        "hhi_before": _hhi(weights_before), "hhi_after": _hhi(weights_after),
        "coverage_sector": (sum(h["value"] for h in hs if h.get("sector")) / total) if total else None,
        "same_sector": [{"name": h["name"], "weight": h["value"] / total} for h in hs
                        if sector and h.get("sector") == sector] if total else [],
        "same_industry": [{"name": h["name"], "weight": h["value"] / total} for h in hs
                          if industry and h.get("industry") == industry] if total else [],
        "etf_note": "ETF-Überschneidungen nicht bewertet: keine verlässlichen ETF-Bestandteile verfügbar"
                    + (f" (gehaltene ETFs: {', '.join(h['name'] for h in hs if h.get('qtype') == 'ETF')})"
                       if any(h.get("qtype") == "ETF" for h in hs) else ""),
    }
    return res


def score(ctx: Any, company: dict[str, Any], sim: dict[str, Any], corr: tuple[float | None, int]) -> Score:
    cs: list[Criterion] = []
    empty = sim["total_before"] <= 0
    w = sim["new_weight"]
    c = Criterion("f_konzentration", "Gewicht der Position nach dem Kauf", 25, "rule",
                  "Anteil der (neuen bzw. aufgestockten) Position am Gesamtportfolio nach dem hypothetischen Kauf.",
                  ["≤ 3 %: 25", "≤ 5 %: 20", "≤ 8 %: 12", "≤ 12 %: 5", "darüber: 0"],
                  points=None if empty else steps(w, [(0.03, 25), (0.05, 20), (0.08, 12), (0.12, 5)], False),
                  rule="Betrag (+ vorhandener Bestand) / Portfolio nach Kauf",
                  facts=[Fact("Kaufbetrag (EUR)", sim["amount"], "money", "estimated", note="hypothetisch"),
                         Fact("Gewicht nach Kauf", w, "pct", "derived")])
    c.reason = "Leeres Portfolio: Gewicht nicht aussagekräftig." if empty else f"Gewicht {P(w)} → {G(c.points)} P."
    if empty:
        c.missing.append("kein bewertetes Portfolio")
    if sim["held"]:
        c.counter.append("Unternehmen bereits gehalten: " + ", ".join(h["name"] for h in sim["held"]))
    cs.append(c)
    sec = company.get("sector")
    after = sim["sector_after"].get(sec or "", None) if sec else None
    c = Criterion("f_branche", "Branchengewicht nach dem Kauf", 25, "rule",
                  "Anteil der Branche des Kandidaten (Yahoo-Sektor) am Portfolio nach dem Kauf.",
                  ["≤ 10 %: 25", "≤ 20 %: 18", "≤ 30 %: 10", "≤ 40 %: 4", "darüber: 0"],
                  points=None if empty or after is None else steps(after, [(0.10, 25), (0.20, 18), (0.30, 10),
                                                                          (0.40, 4)], False),
                  rule="Summe der Gewichte gleicher Branche + neue Position",
                  facts=[Fact("Branche", sec, "text", "reported" if sec else "missing", source="markt"),
                         Fact("Branchengewicht nach Kauf", after, "pct", "derived"),
                         Fact("Abdeckung Branchenangaben im Portfolio", sim["coverage_sector"], "pct", "derived")])
    c.reason = f"Branche „{sec}“: {P(after)} nach Kauf → {G(c.points)} P." if c.points is not None and after is not \
        None else "Nicht bewertet: Branche unbekannt oder Portfolio leer."
    if c.points is None:
        c.missing.append("Branche des Kandidaten oder Portfolio fehlt")
    cs.append(c)
    ind = company.get("industry")
    held_pts: float | None
    if empty:
        held_pts = None
    elif sim["held"]:
        held_pts = 0.0
    else:
        iw = sum(x["weight"] for x in sim["same_industry"]) if ind else None
        held_pts = steps(iw, [(0.0, 15), (0.05, 12), (0.10, 8), (0.20, 4)], False) if iw is not None else None
    c = Criterion("f_ueberschneidung", "Überschneidung mit gehaltenen Unternehmen", 15, "rule",
                  "Bereits gehalten (0 Punkte) bzw. Gewicht gehaltener Unternehmen derselben Teilbranche.",
                  ["keine Überschneidung: 15", "≤ 5 %: 12", "≤ 10 %: 8", "≤ 20 %: 4", "darüber oder bereits "
                   "gehalten: 0"], points=held_pts, rule="gleiche Yahoo-Teilbranche (industry)",
                  facts=[Fact("Gleiche Teilbranche", ", ".join(f"{x['name']} ({P(x['weight'])})"
                                                              for x in sim["same_industry"]) or "keine", "text",
                              "derived")])
    c.reason = "Nicht bewertet." if held_pts is None else f"{G(held_pts)} von 15 Punkten."
    if held_pts is None:
        c.missing.append("Teilbranche unbekannt oder Portfolio leer")
    cs.append(c)
    ccy = (company.get("ccy") or "").upper()
    ca = sim["ccy_after"].get(ccy) if ccy else None
    c = Criterion("f_waehrung", "Währungsrisiko", 10, "rule",
                  "Anteil der Handelswährung des Kandidaten am Portfolio nach dem Kauf (Krypto separat).",
                  ["≤ 40 %: 10", "≤ 60 %: 7", "≤ 75 %: 4", "darüber: 1"],
                  points=None if empty or ca is None else steps(ca, [(0.40, 10), (0.60, 7), (0.75, 4), (1.0, 1)],
                                                                False),
                  rule="Summe gleicher Währung + neue Position",
                  facts=[Fact(f"Anteil {ccy or '?'} nach Kauf", ca, "pct", "derived")])
    c.reason = "Nicht bewertet." if c.points is None else f"{ccy} {P(ca)} nach Kauf → {G(c.points)} P."
    cs.append(c)
    r, n = corr
    c = Criterion("f_korrelation", "Korrelation mit dem Portfolio", 15, "rule",
                  f"Korrelation der täglichen Renditen (EUR) mit dem Gesamtportfolio, bis 1 Jahr, mindestens "
                  f"{MIN_OVERLAP_DAYS} gemeinsame Handelstage.",
                  ["≤ 0,3: 15", "≤ 0,5: 11", "≤ 0,7: 6", "≤ 0,85: 3", "darüber: 0"],
                  points=steps(r, [(0.3, 15), (0.5, 11), (0.7, 6), (0.85, 3)], False) if r is not None else None,
                  rule="Pearson-Korrelation der Tagesrenditen (Portfolio zahlungsbereinigt)",
                  facts=[Fact("Korrelation", r, "num", "derived"), Fact("Gemeinsame Tage", n, "num", "derived")],
                  counter=["Vergangene Korrelationen können sich in Krisen stark ändern"])
    c.reason = f"ρ = {N(r)} über {n} Tage → {G(c.points)} P." if r is not None and c.points is not None else \
        f"Nicht bewertet: zu wenig gemeinsame Kursdaten ({n} Tage)."
    if r is None:
        c.missing.append("Kurshistorie des Kandidaten oder des Portfolios zu kurz")
    cs.append(c)
    cty = company.get("country")
    cta = sim["country_after"].get(cty) if cty else None
    c = Criterion("f_geografie", "Geografische Konzentration", 10, "rule",
                  "Anteil des Sitzlandes des Kandidaten am Portfolio nach dem Kauf (soweit Länderangaben vorliegen).",
                  ["≤ 40 %: 10", "≤ 60 %: 7", "≤ 80 %: 4", "darüber: 1"],
                  points=None if empty or cta is None else steps(cta, [(0.40, 10), (0.60, 7), (0.80, 4), (1.0, 1)],
                                                                 False),
                  rule="Summe gleichen Landes + neue Position (unbekannte Länder zählen nicht mit)",
                  facts=[Fact(f"Anteil {cty or '?'} nach Kauf", cta, "pct", "derived")])
    c.reason = "Nicht bewertet." if c.points is None else f"{cty} {P(cta)} → {G(c.points)} P."
    cs.append(c)
    return Score("fit", "Portfolio-Fit", cs)
