"""Vorwärtstest (0.110): Hat die Discovery tatsächlich gute Vorschläge gemacht?

Rückwirkend lässt sich die KI nicht testen – ein Sprachmodell kennt die späteren Kursverläufe aus seinen
Trainingsdaten. Deshalb wird jede **KI-Vorschau** (KI-Discovery) und jede **KI-Analyse** (per Knopf) mit Stichtag,
Kurs, S&P-500-Stand (SPY) und Score gespeichert (``disc_forward``) und nach 12 bzw. 24 Monaten ausgewertet:
Rendite der Aktie gegenüber dem S&P 500 über denselben Zeitraum. Je Unternehmen, Art und Tag höchstens ein Eintrag.

Grenzen: Kursrenditen ohne Dividenden (Kurs der Kursablage), SPY als Vergleich; Aktien, die nicht mehr notieren,
fehlen in der Auswertung (Anzahl wird ausgewiesen). Bis 12 Monate vergangen sind, ist die Auswertung nur ein
Zwischenstand.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import date, timedelta
from typing import Any

from app.util.timeutil import iso, today_local

log = logging.getLogger(__name__)

BENCH_SERIES = "yahoo:SPY"
HORIZONS = (365, 730)
REFRESH_MAX = 80
KINDS = {"vorschau": "KI-Vorschau (KI-Discovery)", "analyse": "KI-Analyse (per Knopf)"}


def _snap(ctx: Any, series: str) -> tuple[float | None, str | None]:
    """Letzter bekannter Kurs aus der Kursablage (ohne Netzabruf)."""
    try:
        from app.prices.market import market_data

        s = market_data(ctx).snapshots([series]).get(series)
    except Exception as e:  # Kurs ist Zusatz – Eintrag bleibt ohne Kurs (wird nicht ausgewertet)
        log.info("Vorwärtstest: Kurs %s nicht lesbar: %s", series, e)
        return None, None
    if s is None or not s.price_native:
        return None, None
    return float(s.price_native), s.ccy


def _bench_now(ctx: Any, fetch: bool = True) -> float | None:
    if fetch:
        try:
            from app.prices.market import market_data

            market_data(ctx).refresh([BENCH_SERIES])
        except Exception as e:
            log.info("Vorwärtstest: S&P 500 (SPY) nicht abrufbar: %s", e)
    return _snap(ctx, BENCH_SERIES)[0]


def _analysis(db: Any, cid: int, aid: int | None) -> dict[str, Any] | None:
    r = db.q1("SELECT id, nash125, ai_state, result_json FROM disc_analysis WHERE company_id=? AND (? IS NULL OR id=?) "
              "ORDER BY id DESC LIMIT 1", (cid, aid, aid))
    return dict(r) if r else None


def _record(ctx: Any, cid: int, kind: str, ref: str, rating: str | None, detail: dict[str, Any],
            aid: int | None) -> bool:
    db = ctx.db
    an = _analysis(db, cid, aid)
    res = json.loads(an["result_json"]) if an and an.get("result_json") else {}
    m = res.get("market") or {}
    co = db.q1("SELECT ticker, yahoo_symbol FROM disc_company WHERE id=?", (cid,))
    series = f"yahoo:{(co['yahoo_symbol'] or co['ticker'])}" if co else None
    price, ccy = m.get("price"), m.get("ccy")
    if price is None and series:
        price, ccy = _snap(ctx, series)
    bench = _bench_now(ctx)
    score = an.get("nash125") if an else None
    score_open = 0 if an and an.get("ai_state") in ("vollstaendig", "veraltet") else 1
    cur = db.x("INSERT OR IGNORE INTO disc_forward(company_id, kind, ref, day, series, price, ccy, bench_price, score, "
               "score_open, rating, detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (cid, kind, ref, today_local().isoformat(), series, price, ccy, bench, score, score_open, rating,
                json.dumps(detail, ensure_ascii=False, default=str), iso_now()))
    return bool(cur.rowcount)


def iso_now() -> str:
    from datetime import UTC, datetime

    return iso(datetime.now(UTC)) or ""


def record_preview(ctx: Any, cid: int, job_id: int, thesis: dict[str, Any], aid: int | None = None) -> bool:
    dis = thesis.get("disruption") or {}
    return _record(ctx, cid, "vorschau", f"job:{job_id}", dis.get("einschaetzung"),
                   {"stufen": thesis.get("stufen"), "konfidenz": thesis.get("konfidenz")}, aid)


def record_analysis(ctx: Any, cid: int, aid: int) -> bool:
    return _record(ctx, cid, "analyse", f"analyse:{aid}", None, {}, aid)


def refresh(ctx: Any) -> dict[str, Any]:
    """Kurse der Vorwärtstest-Einträge aktualisieren (wöchentlicher Lauf, ohne KI)."""
    rows = ctx.db.q("SELECT DISTINCT series FROM disc_forward WHERE series IS NOT NULL ORDER BY series LIMIT ?",
                    (REFRESH_MAX,))
    series = [r["series"] for r in rows] + [BENCH_SERIES]
    try:
        from app.prices.market import market_data

        market_data(ctx).refresh(series)
    except Exception as e:  # Teilausfall ist zulässig – fehlende Kurse bleiben unausgewertet
        log.info("Vorwärtstest: Kursabruf fehlgeschlagen: %s", e)
        return {"reihen": len(series), "fehler": f"{type(e).__name__}"}
    return {"reihen": len(series)}


def _age(day: str) -> int:
    try:
        return (today_local() - date.fromisoformat(day)).days
    except ValueError:
        return 0


def evaluate(ctx: Any, limit: int = 60) -> dict[str, Any]:
    """Auswertung je Art (Vorschau/Analyse) und Horizont; dazu die jüngsten Einträge mit Zwischenstand."""
    db = ctx.db
    rows = [dict(r) for r in db.q("SELECT f.*, c.name, c.ticker FROM disc_forward f JOIN disc_company c ON "
                                  "c.id = f.company_id ORDER BY f.day DESC, f.id DESC")]
    bench_now = _snap(ctx, BENCH_SERIES)[0]
    for r in rows:
        now, _ccy = _snap(ctx, r["series"]) if r["series"] else (None, None)
        r["age"] = _age(r["day"])
        r["ret"] = (now / r["price"] - 1) if now and r["price"] else None
        r["bench_ret"] = (bench_now / r["bench_price"] - 1) if bench_now and r["bench_price"] else None
        r["excess"] = (r["ret"] - r["bench_ret"]) if r["ret"] is not None and r["bench_ret"] is not None else None
    groups = []
    for kind, label in KINDS.items():
        mine = [r for r in rows if r["kind"] == kind]
        g: dict[str, Any] = {"kind": kind, "label": label, "n": len(mine), "horizons": []}
        for h in HORIZONS:
            ripe = [r for r in mine if r["age"] >= h]
            vals = [r["excess"] for r in ripe if r["excess"] is not None]
            g["horizons"].append({"days": h, "n": len(ripe), "n_eval": len(vals),
                                  "median_excess": statistics.median(vals) if vals else None,
                                  "beat": (sum(1 for v in vals if v > 0) / len(vals)) if vals else None,
                                  "missing": len(ripe) - len(vals)})
        vals_all = [r["excess"] for r in mine if r["excess"] is not None]
        g["interim"] = {"n": len(vals_all), "median_excess": statistics.median(vals_all) if vals_all else None,
                        "beat": (sum(1 for v in vals_all if v > 0) / len(vals_all)) if vals_all else None}
        if kind == "vorschau":
            g["by_rating"] = []
            for lvl in ("hoch", "mittel", "gering"):
                v = [r["excess"] for r in mine if r["rating"] == lvl and r["excess"] is not None and r["age"] >= 365]
                g["by_rating"].append({"rating": lvl, "n": len(v),
                                       "median_excess": statistics.median(v) if v else None})
        groups.append(g)
    first = min((r["day"] for r in rows), default=None)
    return {"groups": groups, "rows": rows[:limit], "n": len(rows), "first": first,
            "first_eval": (date.fromisoformat(first) + timedelta(days=365)).isoformat() if first else None}
