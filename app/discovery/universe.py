"""Research-Universum und deterministisches Fundamentalscreening – unabhängig von News, Watchlist und Portfolio.

Ablauf (jeder Schritt einzeln fortsetzbar, Fortschritt in ``disc_universe``):

1. **Universum pflegen** – alle an Nasdaq/NYSE/CBOE/TXSE notierten Emittenten aus dem SEC-Verzeichnis (eine Zeile je
   CIK, Hauptticker; OTC und Emittenten ohne Börse bleiben außen vor). Nicht mehr gelistete → ``active = 0``.
2. **Neue Einreichungen erkennen** – EDGAR-Tagesindex der letzten Tage (10-K, 10-Q, 20-F, 40-F): Emittenten mit neuen
   Zahlen werden beim nächsten Abgleich erneut betrachtet.
3. **Vorauswahl** (ganzes Universum, wenige Anfragen) – SEC-„Frames“: je Element und Kalenderjahr ein Wert je
   Emittent. Geschäftsjahre werden von der SEC dem nächstliegenden Kalenderjahr zugeordnet → *Näherung*.
4. **Bestätigung** (Stapel von ``discovery.screen_batch`` Emittenten je Lauf, nach Vorauswahl-Punkten und
   Größenklasse gemischt) – vollständige ``companyfacts`` je Emittent, exakte Geschäftsjahre, dieselben Regeln.
   Bestätigt → Unternehmen + Signal „Fundamentaler Wendepunkt“; sonst verworfen mit Begründung.
5. **Mögliche Fehlbewertung** – für bestätigte Wendepunkte: Kurs über 12 Monate relativ zum S&P 500. Bleibt der Kurs
   trotz besserem Geschäft ≥ 15 Prozentpunkte zurück, wird geprüft, ob Merkmale einer *Value Trap* vorliegen
   (schrumpfender Umsatz, sinkende FCF-Marge, starke Verwässerung, Nettoschulden ohne FCF). Ein fallender Kurs allein
   ist nie ein positives Signal.

Alle Regeln und Schwellen stehen in :data:`RULES` und in ``docs/DISCOVERY_INTELLIGENCE.md``. Keine Favoriten, keine
Firmenlisten im Code.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, timedelta
from typing import Any

from app.discovery import fundamentals as F
from app.discovery.identity import EXCHANGE_RANK, directory, is_spac
from app.discovery.sec import SecClient, SecError
from app.util.timeutil import today_local

log = logging.getLogger(__name__)

MAIN_EXCHANGES = ("Nasdaq", "NYSE", "CBOE", "TXSE")
FRAME_TAGS: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (("RevenueFromContractWithCustomerExcludingAssessedTax", "USD"), ("Revenues", "USD"),
                ("SalesRevenueNet", "USD")),
    "op_income": (("OperatingIncomeLoss", "USD"),),
    "ocf": (("NetCashProvidedByUsedInOperatingActivities", "USD"),),
    "capex": (("PaymentsToAcquirePropertyPlantAndEquipment", "USD"),),
    "shares": (("WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),),
}
RULES = {
    "beschleunigung": "Umsatzwachstum ≥ 15 % und mindestens 10 Prozentpunkte über dem Vorjahreswachstum",
    "marge": "operative Marge um mindestens 5 Prozentpunkte verbessert",
    "profitabel": "Übergang zur Profitabilität: operatives Ergebnis von negativ auf positiv",
    "fcf_wende": "Free Cashflow von negativ auf positiv (sinkender Finanzierungsbedarf)",
    "hebel": "Umsatz wächst > 10 % und mindestens 5 Prozentpunkte schneller als die Gesamtkosten (operativer Hebel)",
    "fcf_marge": "Free-Cashflow-Marge um mindestens 10 Prozentpunkte verbessert",
    "umsatzrueckgang": "negativ: Umsatz um mehr als 10 % gesunken",
    "margenverfall": "negativ: operative Marge um mindestens 10 Prozentpunkte gesunken",
    "verwaesserung": "negativ: Aktienanzahl um mindestens 15 % gestiegen",
}
WEIGHTS = {"beschleunigung": 3, "marge": 2, "profitabel": 3, "fcf_wende": 2, "hebel": 2, "fcf_marge": 1,
           "umsatzrueckgang": -3, "margenverfall": -2, "verwaesserung": -2}
CANDIDATE_MIN = 4  # Vorauswahl ab 4 Punkten (z. B. Beschleunigung + Hebel oder Profitabilitätswende + Marge)
SIZES = (("gross", 10e9), ("mittel", 1e9), ("klein", 100e6), ("mikro", 0.0))
SIZE_LABELS = {"gross": "groß (Umsatz ≥ 10 Mrd. USD)", "mittel": "mittel (1–10 Mrd.)", "klein": "klein (0,1–1 Mrd.)",
               "mikro": "Mikro (< 100 Mio.)"}
MISPRICING_GAP = -0.15
STATES = {"offen": "noch nicht vorausgewählt", "unauffaellig": "ohne Auffälligkeit",
          "vorauswahl": "Vorauswahl – Bestätigung ausstehend", "bestaetigt": "bestätigt", "verworfen": "verworfen",
          "keine_daten": "zu wenige Daten", "fehler": "Fehler beim Abruf"}
_LOCK = threading.Lock()


def _now() -> str:
    from app.discovery.service import now_iso

    return now_iso()


def latest_year(today: date | None = None) -> int:
    """Jüngstes vollständig berichtetes Kalenderjahr (Jahresberichte bis Mitte April eingereicht)."""
    t = today or today_local()
    return t.year - 1 if (t.month, t.day) >= (4, 15) else t.year - 2


def size_bucket(revenue: float | None) -> str | None:
    if revenue is None:
        return None
    return next((k for k, lo in SIZES if revenue >= lo), "mikro")  # auch ≤ 0 (Korrekturbuchungen in Frames)


# ------------------------------------------------------------------------------------------------------------
# 1. Universum
# ------------------------------------------------------------------------------------------------------------

def sync(ctx: Any, sec: SecClient) -> dict[str, int]:
    """Universum aus dem SEC-Verzeichnis abgleichen (nur Hauptbörsen, eine Zeile je CIK)."""
    d = directory(sec)
    now = _now()
    seen: set[int] = set()
    added = 0
    rows = []
    for iss in d.by_cik.values():
        idx = next((i for i, ex in enumerate(iss.exchanges) if ex in MAIN_EXCHANGES), None)
        if idx is None:
            continue
        seen.add(iss.cik)
        rows.append((iss.cik, iss.tickers[idx], iss.name, iss.exchanges[idx], now, now))
    with ctx.db.transaction() as c:
        before = int(c.execute("SELECT COUNT(*) FROM disc_universe").fetchone()[0])
        c.executemany("INSERT INTO disc_universe(cik, ticker, name, exchange, added_at, updated_at) "
                      "VALUES (?,?,?,?,?,?) ON CONFLICT(cik) DO UPDATE SET ticker=excluded.ticker, name=excluded.name, "
                      "exchange=excluded.exchange, active=1", rows)
        added = int(c.execute("SELECT COUNT(*) FROM disc_universe").fetchone()[0]) - before
        gone = [r[0] for r in c.execute("SELECT cik FROM disc_universe WHERE active=1") if r[0] not in seen]
        c.executemany("UPDATE disc_universe SET active=0, updated_at=? WHERE cik=?", [(now, x) for x in gone])
    ctx.db.set_state("disc.universe.synced", now)
    return {"aktiv": len(seen), "neu": added, "entfernt": len(gone)}


def scan_filings(ctx: Any, sec: SecClient, days: int = 7) -> dict[str, int]:
    """Neue Jahres-/Quartalsberichte der letzten ``days`` Tage erkennen (EDGAR-Tagesindex)."""
    done = set(ctx.db.get_state("disc.index.done", []) or [])
    today = today_local()
    hits = n_days = 0
    for i in range(1, days + 1):
        day = (today - timedelta(days=i)).isoformat()
        if day in done:
            continue
        try:
            rows = sec.daily_index(day)
        except SecError:
            rows = []
            if i <= 1:
                continue  # Index des Vortags erscheint ggf. erst später
        n_days += 1
        filers = {(cik, d) for form, cik, d in rows if form in ("10-K", "10-Q", "20-F", "40-F", "10-K/A", "10-KT")}
        for cik, d in filers:
            iso_d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            hits += ctx.db.x("UPDATE disc_universe SET filed_seen=? WHERE cik=? AND (filed_seen IS NULL OR "
                             "filed_seen<?)", (iso_d, cik, iso_d)).rowcount or 0
        done.add(day)
    ctx.db.set_state("disc.index.done", sorted(done)[-90:])
    return {"tage": n_days, "neue_einreichungen": hits}


# ------------------------------------------------------------------------------------------------------------
# Regeln (für Vorauswahl und Bestätigung identisch)
# ------------------------------------------------------------------------------------------------------------

def _g(a: float | None, b: float | None) -> float | None:
    return (b / a - 1) if a and b is not None and a > 0 else None


def evaluate(rev: list[float | None], oi: list[float | None], ocf: list[float | None], capex: list[float | None],
             shares: list[float | None]) -> dict[str, Any]:
    """Kennzahlen und Merkmale aus drei aufeinanderfolgenden Jahren (älteste zuerst)."""
    def fcf(i: int) -> float | None:
        o = ocf[i]
        return (o - abs(capex[i] or 0.0)) if o is not None else None

    def div(a: float | None, b: float | None) -> float | None:
        return a / b if a is not None and b else None

    def sub(a: float | None, b: float | None) -> float | None:
        return a - b if a is not None and b is not None else None

    g1, g0 = _g(rev[1], rev[2]), _g(rev[0], rev[1])
    om = [div(oi[i], rev[i]) for i in range(3)]
    fm = [div(fcf(i), rev[i]) for i in range(3)]
    cost = [sub(rev[i], oi[i]) for i in range(3)]
    gc = _g(cost[1], cost[2])
    dil = _g(shares[1], shares[2])
    flags: list[str] = []
    if g1 is not None and g0 is not None and g1 >= 0.15 and g1 - g0 >= 0.10:
        flags.append("beschleunigung")
    om1, om2, fm1, fm2 = om[1], om[2], fm[1], fm[2]
    if om2 is not None and om1 is not None and om2 - om1 >= 0.05:
        flags.append("marge")
    oi1, oi2 = oi[1], oi[2]
    if oi1 is not None and oi2 is not None and oi1 < 0 < oi2:
        flags.append("profitabel")
    f1, f2 = fcf(1), fcf(2)
    if f1 is not None and f2 is not None and f1 < 0 < f2:
        flags.append("fcf_wende")
    if g1 is not None and gc is not None and g1 > 0.10 and g1 - gc >= 0.05:
        flags.append("hebel")
    if fm2 is not None and fm1 is not None and fm2 - fm1 >= 0.10:
        flags.append("fcf_marge")
    if g1 is not None and g1 < -0.10:
        flags.append("umsatzrueckgang")
    if om2 is not None and om1 is not None and om2 - om1 <= -0.10:
        flags.append("margenverfall")
    if dil is not None and dil >= 0.15:
        flags.append("verwaesserung")
    score = sum(WEIGHTS[f] for f in flags) + (1 if g1 is not None and g1 >= 0.30 and "beschleunigung" in flags else 0)
    return {"revenue": rev[2], "g1": g1, "g0": g0, "om": om, "fcf_m": fm, "cost_growth": gc, "dilution": dil,
            "flags": flags, "score": float(score)}


def describe(ev: dict[str, Any]) -> str:
    from app.discovery.text import P

    parts = [RULES[f] for f in ev["flags"] if WEIGHTS[f] > 0]
    nums = []
    if ev.get("g1") is not None:
        nums.append(f"Umsatzwachstum {P(ev['g1'])} (Vorjahr {P(ev.get('g0'))})")
    om = ev.get("om") or [None] * 3
    if om[2] is not None:
        nums.append(f"operative Marge {P(om[2])} (Vorjahr {P(om[1])})")
    neg = [RULES[f] for f in ev["flags"] if WEIGHTS[f] < 0]
    return "; ".join(parts) + (". Kennzahlen: " + ", ".join(nums) if nums else "") + \
        (". Gegenanzeichen: " + "; ".join(neg) if neg else "")


# ------------------------------------------------------------------------------------------------------------
# 3. Vorauswahl über SEC-Frames
# ------------------------------------------------------------------------------------------------------------

def _frame_values(sec: SecClient, key: str, year: int, job: Any = None) -> dict[int, float]:
    out: dict[int, float] = {}
    for tag, unit in FRAME_TAGS[key]:  # Vorrang: erstes Element, das ein Emittent berichtet
        try:
            rows = sec.frame(tag, f"CY{year}", unit)
        except SecError as e:
            log.info("Discovery: Frame %s CY%s nicht verfügbar: %s", tag, year, e)
            continue
        for r in rows:
            try:
                out.setdefault(int(r["cik"]), float(r["val"]))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def prescreen(ctx: Any, sec: SecClient, job: Any = None, min_revenue: float = 20e6) -> dict[str, Any]:
    """Ganzes Universum über SEC-Frames bewerten (Näherung). Fortschritt: ``pre_at`` je Emittent."""
    y = latest_year()
    years = (y - 2, y - 1, y)
    data: dict[str, list[dict[int, float]]] = {}
    for n, key in enumerate(FRAME_TAGS, 1):
        if job is not None:
            job.step(f"Vorauswahl: SEC-Frames {n}/{len(FRAME_TAGS)} ({key}, {years[0]}–{years[-1]})")
        data[key] = [_frame_values(sec, key, yr) for yr in years]
    now = _now()
    stats: dict[str, int] = {"bewertet": 0, "vorauswahl": 0, "keine_daten": 0, "negativ": 0,
                             "unter_mindestumsatz": 0}
    updates = []
    for r in ctx.db.q("SELECT cik, state, reason, filed_seen, verified_at FROM disc_universe WHERE active=1"):
        cik = int(r["cik"])
        vals = {k: [data[k][i].get(cik) for i in range(3)] for k in FRAME_TAGS}
        # bereits mit vollständigen Daten geprüft und seither keine neue Einreichung → Ergebnis bleibt bestehen
        keep = r["state"] in ("bestaetigt", "verworfen") and not (
            r["filed_seen"] and r["verified_at"] and r["filed_seen"] > str(r["verified_at"])[:10])
        if vals["revenue"][1] is None or vals["revenue"][2] is None:
            stats["keine_daten"] += 1
            state, reason, score, ev_json, size = "keine_daten", (
                "keine Umsatzangaben in den SEC-Frames der letzten zwei Jahre (z. B. ausländische Emittenten mit IFRS, "
                "Banken, Fonds)"), None, None, None
        else:
            ev = evaluate(vals["revenue"], vals["op_income"], vals["ocf"], vals["capex"], vals["shares"])
            stats["bewertet"] += 1
            score, ev_json, size = ev["score"], json.dumps(ev), size_bucket(vals["revenue"][2])
            if (vals["revenue"][2] or 0) < min_revenue:
                stats["unter_mindestumsatz"] += 1
                state, reason = "unauffaellig", f"Umsatz unter Mindestumsatz ({min_revenue / 1e6:.0f} Mio. USD)"
            elif ev["score"] >= CANDIDATE_MIN:
                stats["vorauswahl"] += 1
                state, reason = "vorauswahl", describe(ev)
            else:
                stats["negativ"] += 1 if any(WEIGHTS[f] < 0 for f in ev["flags"]) else 0
                state, reason = "unauffaellig", describe(ev) if ev["flags"] else "keine Auffälligkeit nach den Regeln"
        if keep:
            state, reason = r["state"], r["reason"]
        updates.append((state, reason, score, ev_json, size, now, now, cik))
    with ctx.db.transaction() as c:
        c.executemany("UPDATE disc_universe SET state=?, reason=?, pre_score=?, pre_json=?, "
                      "size_bucket=COALESCE(?, size_bucket), pre_at=?, updated_at=? WHERE cik=?", updates)
    out: dict[str, Any] = {"jahre": f"{years[0]}–{years[-1]}", **stats}
    ctx.db.set_state("disc.prescreen", {"at": now, **out})
    return out


# ------------------------------------------------------------------------------------------------------------
# 4. Bestätigung mit vollständigen SEC-Daten (Stapel)
# ------------------------------------------------------------------------------------------------------------

def next_batch(db: Any, n: int) -> list[dict[str, Any]]:
    """Nächste Emittenten zur Bestätigung: je Größenklasse abwechselnd, innerhalb nach Vorauswahl-Punkten und
    jüngster Einreichung – so kommen große, mittlere und kleine Unternehmen gleichermaßen an die Reihe."""
    by_size: dict[str, list[dict[str, Any]]] = {}
    for r in db.q("SELECT * FROM disc_universe WHERE active=1 AND state='vorauswahl' ORDER BY pre_score DESC, "
                  "COALESCE(filed_seen, '') DESC LIMIT ?", (n * 8,)):
        by_size.setdefault(r["size_bucket"] or "mikro", []).append(dict(r))
    out: list[dict[str, Any]] = []
    order = [k for k, _ in SIZES]
    while len(out) < n and any(by_size.values()):
        for k in order:
            if by_size.get(k) and len(out) < n:
                out.append(by_size[k].pop(0))
    return out


def _fy_eval(fu: F.Fundamentals) -> dict[str, Any] | None:
    ys = fu.years[-3:]
    if len(ys) < 3:
        return None
    def s(key: str) -> list[float | None]:
        return [fu.get(key, y).v for y in ys]
    ev = evaluate(s("revenue"), s("operating_income"), s("ocf"), s("capex"), s("shares_diluted"))
    ev["years"] = ys
    return ev


def verify(ctx: Any, sec: SecClient, row: dict[str, Any], job_id: int | None) -> dict[str, Any]:
    """Einen vorausgewählten Emittenten mit vollständigen Geschäftsjahresdaten prüfen."""
    from app.discovery import service as SV
    from app.discovery import signals as SG
    from app.discovery.identity import Issuer

    db, cik, now = ctx.db, int(row["cik"]), _now()
    try:
        fu = F.parse(sec.facts(cik))
    except SecError as e:
        db.x("UPDATE disc_universe SET state='fehler', reason=?, verified_at=?, updated_at=? WHERE cik=?",
             (f"SEC-Kennzahlen nicht abrufbar: {e}"[:300], now, now, cik))
        return {"cik": cik, "state": "fehler"}
    ev = _fy_eval(fu)
    if ev is None or ev["score"] < CANDIDATE_MIN - 1 or not any(WEIGHTS[f] > 0 for f in ev["flags"]):
        why = "weniger als drei Geschäftsjahre in den SEC-Daten" if ev is None else \
            "Geschäftsjahresdaten bestätigen die Frames-Näherung nicht" + (f" ({describe(ev)})" if ev["flags"] else "")
        db.x("UPDATE disc_universe SET state='verworfen', reason=?, verified_at=?, updated_at=? WHERE cik=?",
             (why[:500], now, now, cik))
        SV.record_candidate(db, job_id, "wendepunkt", name=row["name"], ticker=row["ticker"], cik=cik,
                            status="verworfen", reason=why, priority=None, evidence=[{"art": "Vorauswahl",
                                                                                      "text": row.get("reason")}])
        return {"cik": cik, "state": "verworfen"}
    known = db.q1("SELECT id FROM disc_company WHERE cik=?", (cik,))
    iss = Issuer(cik, row["name"], [row["ticker"]], [row["exchange"]])
    cid, why = SV.upsert_issuer(ctx, iss, "screening")
    if cid is None:
        db.x("UPDATE disc_universe SET state='verworfen', reason=?, verified_at=?, updated_at=? WHERE cik=?",
             (why[:500], now, now, cik))
        return {"cik": cik, "state": "verworfen"}
    detail = describe(ev) + f" (Geschäftsjahre {ev['years'][0]}–{ev['years'][-1]}, SEC companyfacts)"
    SG.add_signal(db, cid, kind="wendepunkt", title="Fundamentaler Wendepunkt (Screening)", origin="sec",
                  key=f"wendepunkt:{ev['years'][-1]}", detail=detail, ref=f"CIK{cik:010d}", direction="positiv",
                  strength_=min(1.0, ev["score"] / 10), published_at=ev["years"][-1])
    SV.record_candidate(db, job_id, "wendepunkt", name=row["name"], ticker=row["ticker"], cik=cik, company_id=cid,
                        status="bekannt" if known else "neu", reason=detail, priority=ev["score"] * 10,
                        evidence=[{"art": "SEC companyfacts", "jahre": ev["years"], "merkmale": ev["flags"]}])
    db.x("UPDATE disc_universe SET state='bestaetigt', reason=?, verified_at=?, company_id=?, updated_at=?, "
         "pre_json=? WHERE cik=?", (detail[:500], now, cid, now, json.dumps(ev), cik))
    return {"cik": cik, "state": "bestaetigt", "company_id": cid, "new": known is None}


# ------------------------------------------------------------------------------------------------------------
# 5. Mögliche Marktfehlbewertung (kritisch geprüft)
# ------------------------------------------------------------------------------------------------------------

def one_year_return(rows: list[tuple[str, float]]) -> tuple[float | None, str]:
    if len(rows) < 2:
        return None, ""
    end = rows[-1][0]
    start = (date.fromisoformat(end) - timedelta(days=365)).isoformat()
    first = next(((d, v) for d, v in rows if d >= start), None)
    if first is None or first[0] > (date.fromisoformat(start) + timedelta(days=10)).isoformat():
        return None, ""
    return rows[-1][1] / first[1] - 1, f"{first[0]}–{end}"


def trap_checks(fu: F.Fundamentals) -> list[str]:
    """Merkmale einer Value Trap (leer = keine gefunden)."""
    out = []
    g = fu.latest("revenue_growth").v
    if g is None or g <= 0:
        out.append("Umsatz wächst nicht (mehr)")
    fms = [x for x in fu.series("fcf_margin")[-2:] if x is not None]
    if len(fms) == 2 and fms[1] < fms[0] - 0.02:
        out.append("Free-Cashflow-Marge sinkt")
    dil = fu.latest("dilution").v
    if dil is not None and dil >= 0.10:
        out.append(f"starke Verwässerung ({dil:.0%} p. a.)")
    nc, fcf = fu.latest("net_cash").v, fu.latest("fcf").v
    if nc is not None and nc < 0 and (fcf is None or fcf <= 0):
        out.append("Nettoschulden ohne positiven Free Cashflow")
    return out


def mispricing(ctx: Any, sec: SecClient, cid: int, job_id: int | None) -> dict[str, Any]:
    from app.discovery import service as SV
    from app.discovery import signals as SG

    db = ctx.db
    co = SV.company(db, cid)
    if co is None or not co.get("cik"):
        return {"status": "uebersprungen"}
    stock = SV._adj_history(ctx, co.get("yahoo_symbol") or co["ticker"])
    bench = next((b for b in (SV._adj_history(ctx, s) for s in SV.BENCHMARKS) if b), [])
    rs, period = one_year_return(stock)
    rb, _ = one_year_return(bench)
    db.x("UPDATE disc_universe SET updated_at=? WHERE company_id=?", (_now(), cid))
    if rs is None or rb is None:
        return {"status": "keine_kurse"}
    gap = rs - rb
    if gap > MISPRICING_GAP:
        return {"status": "kein_rueckstand", "gap": gap}
    try:
        fu = F.parse(sec.facts(int(co["cik"])))
    except SecError:
        return {"status": "fehler"}
    traps = trap_checks(fu)
    from app.discovery.text import N, P

    detail = (f"Aktie {P(rs)} gegenüber S&P 500 {P(rb)} in 12 Monaten ({period}) – Rückstand "
              f"{N(gap * 100, 1)} Prozentpunkte trotz "
              "fundamentaler Verbesserung (siehe Wendepunkt-Signal).")
    if traps:
        SV.record_candidate(db, job_id, "fehlbewertung", name=co["name"], ticker=co["ticker"], cik=co["cik"],
                            company_id=cid, status="verworfen", reason="mögliche Value Trap: " + "; ".join(traps),
                            evidence=[{"art": "Kurs", "text": detail}])
        SG.add_signal(db, cid, kind="fehlbewertung", title="Kursrückstand – Merkmale einer Value Trap",
                      origin="markt", key=f"fehlbewertung:{period}", detail=detail + " Gegenanzeichen: "
                      + "; ".join(traps), direction="negativ", strength_=0.2, published_at=period[-10:])
        return {"status": "value_trap", "gap": gap, "traps": traps}
    SG.add_signal(db, cid, kind="fehlbewertung", title="Mögliche Marktfehlbewertung", origin="markt",
                  key=f"fehlbewertung:{period}", detail=detail + " Keine Value-Trap-Merkmale nach den Regeln "
                  "(Umsatz wächst, FCF-Marge stabil, keine starke Verwässerung, keine ungedeckten Nettoschulden).",
                  direction="positiv", strength_=0.6, published_at=period[-10:])
    SV.record_candidate(db, job_id, "fehlbewertung", name=co["name"], ticker=co["ticker"], cik=co["cik"],
                        company_id=cid, status="neu", reason=detail, priority=60.0,
                        evidence=[{"art": "Kurs relativ zum S&P 500", "text": detail}])
    return {"status": "moeglich", "gap": gap}


# ------------------------------------------------------------------------------------------------------------
# Gesamtablauf und Fortschritt
# ------------------------------------------------------------------------------------------------------------

def screening(ctx: Any, job: Any = None, batch: int = 20, min_revenue: float = 20e6) -> dict[str, Any]:
    """Schritte 1–5 (begrenzt, fortsetzbar). Läuft höchstens einmal gleichzeitig."""
    if not _LOCK.acquire(blocking=False):
        return {"skipped": "Fundamentalscreening läuft bereits"}
    try:
        sec = SecClient(ctx)
        out: dict[str, Any] = {"errors": []}
        step = job.step if job is not None else (lambda *a, **k: None)
        synced = ctx.db.get_state("disc.universe.synced")
        if not synced or synced < (today_local() - timedelta(days=7)).isoformat():
            step("Universum mit dem SEC-Verzeichnis abgleichen")
            try:
                out["universum"] = sync(ctx, sec)
            except SecError as e:
                out["errors"].append(f"SEC-Verzeichnis: {e}")
                return out
        step("Neue Einreichungen im EDGAR-Tagesindex suchen")
        out["einreichungen"] = scan_filings(ctx, sec)
        pre = ctx.db.get_state("disc.prescreen") or {}
        if not pre or str(pre.get("at", "")) < (today_local() - timedelta(days=7)).isoformat() or \
                out["einreichungen"]["neue_einreichungen"]:
            out["vorauswahl"] = prescreen(ctx, sec, job, min_revenue)
        rows = next_batch(ctx.db, batch)
        done: list[dict[str, Any]] = []
        for i, r in enumerate(rows, 1):
            step(f"Bestätigung {i}/{len(rows)}: {r['name']} ({r['ticker']})", bestaetigung=f"{i}/{len(rows)}")
            try:
                done.append(verify(ctx, sec, r, getattr(job, "id", None)))
            except Exception as e:  # ein fehlerhafter Datensatz darf den Stapel nicht abbrechen – Grund je Emittent
                log.warning("Discovery: Bestätigung %s fehlgeschlagen: %s", r["ticker"], e)
                ctx.db.x("UPDATE disc_universe SET state='fehler', reason=?, verified_at=?, updated_at=? WHERE cik=?",
                         (f"{type(e).__name__}: {e}"[:300], _now(), _now(), r["cik"]))
                out["errors"].append(f"{r['ticker']}: {type(e).__name__}")
        out["bestaetigung"] = {"geprueft": len(done), "bestaetigt": sum(1 for d in done if d["state"] == "bestaetigt"),
                               "neu": sum(1 for d in done if d.get("new")),
                               "verworfen": sum(1 for d in done if d["state"] == "verworfen")}
        mp: list[dict[str, Any]] = []
        for d in done:
            if d["state"] == "bestaetigt":
                step(f"Kurs gegen S&P 500 prüfen (Fehlbewertung): CIK {d['cik']}")
                mp.append(mispricing(ctx, sec, int(d["company_id"]), getattr(job, "id", None)))
        out["fehlbewertung"] = {"geprueft": len(mp), "moeglich": sum(1 for m in mp if m["status"] == "moeglich"),
                                "value_trap": sum(1 for m in mp if m["status"] == "value_trap")}
        out["neue_unternehmen"] = [d["company_id"] for d in done if d.get("new")]
        out["bestaetigte_unternehmen"] = [d["company_id"] for d in done if d["state"] == "bestaetigt"]
        return out
    finally:
        _LOCK.release()


def progress(db: Any) -> dict[str, Any]:
    rows = {r["state"]: int(r["n"]) for r in db.q("SELECT state, COUNT(*) AS n FROM disc_universe WHERE active=1 "
                                                   "GROUP BY state")}
    pre = db.get_state("disc.prescreen") or {}
    sizes = {r["size_bucket"]: int(r["n"]) for r in db.q("SELECT size_bucket, COUNT(*) AS n FROM disc_universe WHERE "
                                                          "active=1 AND state='bestaetigt' GROUP BY size_bucket")}
    return {"total": sum(rows.values()), "states": rows, "prescreen": pre, "sizes_confirmed": sizes,
            "pending": rows.get("vorauswahl", 0),
            "checked": rows.get("bestaetigt", 0) + rows.get("verworfen", 0) + rows.get("fehler", 0),
            "synced": db.get_state("disc.universe.synced")}


def top_evidence(db: Any, n: int = 25) -> list[dict[str, Any]]:
    """Deterministische Ausgangsbelege für die KI-Themensuche: Emittenten mit den stärksten Wendepunkten."""
    out = []
    for r in db.q("SELECT cik, ticker, name, pre_json, state, size_bucket FROM disc_universe WHERE active=1 AND "
                  "pre_score >= ? AND state IN ('vorauswahl', 'bestaetigt') ORDER BY pre_score DESC LIMIT ?",
                  (CANDIDATE_MIN, n * 3)):
        ev = json.loads(r["pre_json"] or "{}")
        if not ev:
            continue
        out.append({"cik": r["cik"], "ticker": r["ticker"], "name": r["name"], "ev": ev, "state": r["state"],
                    "size": r["size_bucket"]})
    by_size: dict[str, list[dict[str, Any]]] = {}
    for e in out:
        by_size.setdefault(e["size"] or "mikro", []).append(e)
    mixed: list[dict[str, Any]] = []
    while len(mixed) < n and any(by_size.values()):
        for k, _ in SIZES:
            if by_size.get(k) and len(mixed) < n:
                mixed.append(by_size[k].pop(0))
    return mixed


__all__ = ["EXCHANGE_RANK", "RULES", "is_spac"]
