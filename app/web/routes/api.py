"""JSON-Endpunkte für Diagramme (werden vom Frontend per fetch geladen)."""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.analytics import periods as P
from app.analytics.allocation import donut, sunburst, treemap
from app.util.timeutil import local_tz, parse_iso, today_local
from app.web.deps import get_ctx
from app.web.routes.pages import parse_d, scope_assets

router = APIRouter(prefix="/api")

RANGE_DAYS = {"1M": 31, "3M": 92, "6M": 183, "1J": 366, "3J": 1096, "5J": 1827}


def _range_start(rng: str, end: date, first: date) -> date:
    rng = rng.upper()
    if rng == "YTD":
        return date(end.year, 1, 1)
    if rng in RANGE_DAYS:
        return max(first, end - timedelta(days=RANGE_DAYS[rng]))
    return first


def _r(x: float | None, nd: int = 2) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if np.isfinite(f) else None


@router.get("/allocation")
def allocation(request: Request, mode: str = "sunburst", level: str = "position", expand: str = "",
               account: str = "") -> JSONResponse:
    ctx = get_ctx(request)
    val = ctx.valuation(account or None)
    if val is None:
        return JSONResponse({"data": [], "total": 0})
    thr = float(ctx.settings.get("allocation.other_threshold_pct", 1.0))
    exp = {e for e in expand.split(",") if e}
    data = sunburst(val, thr, exp) if mode == "sunburst" else donut(val, level, thr, exp)
    return JSONResponse(data)


@router.get("/treemap")
def treemap_api(request: Request, account: str = "") -> JSONResponse:
    ctx = get_ctx(request)
    val = ctx.valuation(account or None)
    return JSONResponse(treemap(val) if val else {"data": []})


@router.get("/history")
def history_api(request: Request, range: str = "1J") -> JSONResponse:
    ctx = get_ctx(request)
    hist = ctx.history_with_live()
    if hist is None or hist.n == 0:
        return JSONResponse({"dates": [], "value": [], "invested": []})
    end = hist.dates[-1]
    start = _range_start(range, end, hist.start)
    a = hist.index_of(start)
    return JSONResponse({
        "dates": [d.isoformat() for d in hist.dates[a:]],
        "value": [_r(v) for v in hist.value[a:]],
        "invested": [_r(v) for v in hist.invested[a:]],
        "estimated": bool(hist.estimated_days and any(hist.estimated_days.values())),
    })


def _asset_or_404(ctx: Any, asset_id: str) -> Any:
    pf = ctx.portfolio()
    if pf is None or asset_id not in pf.assets:
        raise HTTPException(404, "Asset nicht gefunden")
    return pf, pf.assets[asset_id]


def _ohlc_from_intraday(points: list[Any], bucket_min: int) -> list[list[Any]]:
    buckets: OrderedDict[datetime, list[float]] = OrderedDict()
    for b in points:
        ts = b.ts
        key = ts.replace(minute=(ts.minute // bucket_min) * bucket_min if bucket_min < 60 else 0, second=0,
                         microsecond=0)
        if bucket_min >= 60:
            key = key.replace(hour=(key.hour // (bucket_min // 60)) * (bucket_min // 60))
        o = b.open if b.open is not None else b.close
        h = b.high if b.high is not None else b.close
        lo = b.low if b.low is not None else b.close
        if key not in buckets:
            buckets[key] = [o, b.close, lo, h]
        else:
            cur = buckets[key]
            cur[1] = b.close
            cur[2] = min(cur[2], lo)
            cur[3] = max(cur[3], h)
    return [[k.isoformat(), *v] for k, v in buckets.items()]


@router.get("/asset/{asset_id:path}/chart")
def asset_chart(request: Request, asset_id: str, range: str = "1J", kind: str = "line") -> JSONResponse:
    ctx = get_ctx(request)
    pf, a = _asset_or_404(ctx, asset_id)
    rng = range.upper()
    s = ctx.prices.series_for(a)
    val = ctx.valuation()
    pos = val.by_id().get(asset_id) if val else None
    out: dict[str, Any] = {"asset": a.name, "range": rng, "kind": kind, "intraday": rng in ("1T", "1W"),
                           "avg_cost": _r(pos.avg_cost, 6) if pos and pos.avg_cost else None, "ccy": None,
                           "points": [], "candles": [], "markers": [], "source": None}
    if a.is_fiat:
        return JSONResponse(out)
    today = today_local()
    if rng in ("1T", "1W"):
        bars = ctx.prices.intraday(a, rng)
        latest = ctx.store.latest(s) if s else None
        ccy = (latest["ccy"] if latest else None) or "EUR"
        f, _ = ctx.prices.fx_to_eur(ccy)
        f = f or 1.0
        out["ccy"] = ccy
        out["points"] = [[b.ts.isoformat(), _r(b.close * f, 6), _r(b.close, 6)] for b in bars]
        if kind == "candle" and bars:
            conv = [type(b)(ts=b.ts, close=b.close * f, open=(b.open or b.close) * f, high=(b.high or b.close) * f,
                            low=(b.low or b.close) * f) for b in bars]
            out["candles"] = [[t, _r(o, 6), _r(c, 6), _r(lo, 6), _r(h, 6)] for t, o, c, lo, h in
                              _ohlc_from_intraday(conv, 15 if rng == "1T" else 120)]
        start_dt = datetime.now(UTC) - (timedelta(days=1) if rng == "1T" else timedelta(days=7))
        since = start_dt.astimezone(local_tz()).date()
    else:
        first = min((t.date for t in pf.txs if asset_id in (t.to_asset, t.from_asset)), default=today)
        start = min(first, today) - timedelta(days=7) if rng == "MAX" else _range_start(rng, today, date(1990, 1, 1))
        since = start
        rows = ctx.store.daily_range(s, start, today) if s else []
        fx_cache: dict[str, dict[str, float]] = {}
        pts, candles = [], []
        ccy = None
        for r in rows:
            ccy = (r["ccy"] or "EUR").upper()
            rate = 1.0
            if ccy != "EUR":
                if ccy not in fx_cache:
                    fx_cache[ccy] = ctx.store.fx_series_map(ccy)
                fx = fx_cache[ccy].get(r["date"]) or ctx.store.fx_on_or_before(ccy, date.fromisoformat(r["date"]))
                val_fx = fx if isinstance(fx, float) else (fx[0] if fx else None)
                if not val_fx:
                    continue
                rate = 1.0 / val_fx
            sf = r["split_factor"] or 1.0
            close = r["close"] * rate / sf
            pts.append([r["date"], _r(close, 6), _r(r["close"] / sf, 6)])
            if kind == "candle":
                o = (r["open"] or r["close"]) * rate / sf
                h = (r["high"] or r["close"]) * rate / sf
                lo = (r["low"] or r["close"]) * rate / sf
                candles.append([r["date"], _r(o, 6), _r(close, 6), _r(lo, 6), _r(h, 6)])
        # Heute: Live-Kurs anhängen
        pi = ctx.prices.latest_eur_many([a], pf).get(asset_id)
        if pi and pi.valued and pi.kind == "quote" and (not pts or pts[-1][0] < today.isoformat()):
            pts.append([today.isoformat(), _r(pi.price_eur, 6), _r(pi.price_native, 6)])
        out["points"] = pts
        out["candles"] = candles
        out["ccy"] = ccy
        out["has_ohlc"] = any(r["open"] is not None for r in rows)
    # Transaktionsmarker (Kauf/Verkauf) mit Kurs je Einheit, split-bereinigt auf heutige Stückbasis
    # (Splits aus den Kapitalmaßnahmen des Ledgers – unabhängig von der Kursquelle)
    splits = pf.split_events().get(asset_id, [])
    markers = []
    for t in pf.txs:
        if t.date < since or not t.value_eur:
            continue
        side = None
        qty = None
        if t.to_asset == asset_id and t.type in ("buy", "trade") and t.to_qty:
            side, qty = "buy", t.to_qty
        elif t.from_asset == asset_id and t.type in ("sell", "trade") and t.from_qty:
            side, qty = "sell", t.from_qty
        if side is None or not qty:
            continue
        sf = 1.0
        for sd, ratio in splits:
            if sd > t.date:
                sf *= ratio
        unit = float(t.value_eur) / float(qty) / sf
        ts = t.ts.isoformat() if rng in ("1T", "1W") else t.date.isoformat()
        markers.append({"t": ts, "side": side, "price": _r(unit, 6), "qty": float(qty) * sf,
                        "value": _r(float(t.value_eur)), "tx": t.tx_id})
    out["markers"] = markers
    out["source"] = s
    return JSONResponse(out)


@router.get("/asset/{asset_id:path}/position")
def asset_position(request: Request, asset_id: str, range: str = "MAX") -> JSONResponse:
    ctx = get_ctx(request)
    _asset_or_404(ctx, asset_id)
    hist = ctx.history_with_live()
    if hist is None:
        return JSONResponse({"dates": [], "qty": [], "value": []})
    k = hist.asset_index().get(asset_id)
    if k is None:
        return JSONResponse({"dates": [], "qty": [], "value": []})
    nz = np.nonzero(hist.asset_qty[k])[0]
    first = int(nz[0]) if len(nz) else 0
    start = _range_start(range, hist.dates[-1], hist.dates[first])
    a = max(first, hist.index_of(start))
    return JSONResponse({
        "dates": [d.isoformat() for d in hist.dates[a:]],
        "qty": [_r(q, 8) for q in hist.asset_qty[k][a:]],
        "value": [_r(v) for v in hist.asset_value[k][a:]],
    })


def _scope_series(ctx: Any, hist: Any, scope: str) -> Any:
    assets = scope_assets(ctx, scope)
    return P.total_series(hist) if assets is None else P.group_series(hist, assets)


def _bench_series(ctx: Any, series: str, dates: list[date]) -> list[float | None]:
    if ctx.config.demo_mode and not series.startswith("demo:"):
        series = f"demo:{series}"
    rows = ctx.store.daily_closes(series)
    if not rows:
        return [None] * len(dates)
    fx_cache: dict[str, dict[str, float]] = {}
    pts: list[tuple[date, float]] = []
    for d, close, ccy in rows:
        c = (ccy or "EUR").upper()
        rate = 1.0
        if c != "EUR":
            if c not in fx_cache:
                fx_cache[c] = ctx.store.fx_series_map(c)
            fx = fx_cache[c].get(d)
            if not fx:
                continue
            rate = 1.0 / fx
        pts.append((date.fromisoformat(d), close * rate))
    out: list[float | None] = []
    j = 0
    last = None
    for d in dates:
        while j < len(pts) and pts[j][0] <= d:
            last = pts[j][1]
            j += 1
        out.append(last)
    base = next((x for x in out if x), None)
    return [(_r((x / base - 1) * 100, 3) if (x and base) else None) for x in out]


@router.get("/performance/series")
def perf_series(request: Request, scope: str = "total", period: str = "1J", start: str = "",
                end: str = "") -> JSONResponse:
    ctx = get_ctx(request)
    hist = ctx.history_with_live()
    if hist is None:
        return JSONResponse({"dates": []})
    s = _scope_series(ctx, hist, scope)
    sd, ed = parse_d(start), parse_d(end)
    a, b, inc = P.bounds(hist, None if (sd or ed) else period, start=sd, end=ed)
    dates, idx = P.index_series(hist, s, a, b, inc)
    from app.analytics.performance import drawdown

    dd = drawdown(idx + 1.0)
    benches = []
    for bm in ctx.settings.get("performance.benchmarks") or []:
        if bm.get("series"):
            benches.append({"name": bm.get("name") or bm["series"], "values": _bench_series(ctx, bm["series"], dates)})
    return JSONResponse({
        "dates": [d.isoformat() for d in dates],
        "portfolio": [_r(x * 100, 3) for x in idx],
        "drawdown": [_r(x * 100, 3) for x in dd],
        "benchmarks": benches,
    })


@router.get("/performance/annual")
def perf_annual(request: Request, scope: str = "total") -> JSONResponse:
    ctx = get_ctx(request)
    hist = ctx.history_with_live()
    if hist is None:
        return JSONResponse({"years": []})
    s = _scope_series(ctx, hist, scope)
    rows = P.annual_returns(hist, s)
    return JSONResponse({"years": [{"year": r["year"], "ttwror": _r((r["ttwror"] or 0) * 100, 3),
                                    "gain": _r(r["gain"]), "partial": r["partial"]} for r in rows]})


@router.get("/performance/contrib")
def perf_contrib(request: Request, period: str = "1J", start: str = "", end: str = "",
                 top: int = Query(12, ge=3, le=40)) -> JSONResponse:
    ctx = get_ctx(request)
    hist = ctx.history_with_live()
    pf = ctx.portfolio()
    if hist is None or pf is None:
        return JSONResponse({"items": []})
    sd, ed = parse_d(start), parse_d(end)
    a, b, inc = P.bounds(hist, None if (sd or ed) else period, start=sd, end=ed)
    items = []
    for k, aid in enumerate(hist.asset_ids):
        v0 = 0.0 if inc else float(hist.asset_value[k][a])
        lo = 0 if inc else a + 1
        net = float(np.sum(hist.asset_in[k][lo:b + 1]) - np.sum(hist.asset_out[k][lo:b + 1]))
        g = float(hist.asset_value[k][b]) - v0 - net
        if abs(g) >= 0.005:
            items.append({"id": aid, "name": pf.asset(aid).name, "gain": g})
    items.sort(key=lambda x: -abs(x["gain"]))
    head, tail = items[:top], items[top:]
    if tail:
        head.append({"id": "other", "name": f"Sonstige ({len(tail)})", "gain": sum(x["gain"] for x in tail)})
    head.sort(key=lambda x: -x["gain"])
    total = P.metrics(hist, P.total_series(hist), a, b, inc)
    return JSONResponse({"items": [{**x, "gain": _r(x["gain"])} for x in head],
                         "total": _r(total.get("gain")), "sum_assets": _r(sum(x["gain"] for x in items))})


@router.get("/status")
def status(request: Request) -> JSONResponse:
    ctx = get_ctx(request)
    jobs = ctx.job_status()
    bf = jobs.get("history_backfill") or {}
    last = parse_iso(ctx.db.get_state("last_crypto_update")) or None
    return JSONResponse({
        "price_version": ctx.prices.version,
        "data_version": ctx.data_version,
        "history_version": ctx.history_version,
        "backfill": {"running": bool(bf.get("running")), "progress": bf.get("progress")},
        "last_crypto_update": last.isoformat() if last else None,
    })
