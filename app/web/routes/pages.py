"""HTML-Seiten (serverseitig gerendert)."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.analytics import periods as P
from app.analytics.colors import asset_colors
from app.ledger.engine import DUST, REALIZED_KINDS
from app.prices.fallback import DEFAULT_MAX_AGE as FB_DEFAULT
from app.prices.fallback import SETTING_KEYS as FB_KEYS
from app.util.timeutil import add_years, local_tz, parse_iso, today_local
from app.web.deps import get_ctx, render
from app.web.svg import sparkline

router = APIRouter()
log = logging.getLogger(__name__)

SORT_KEYS = {
    "name": lambda p: p.asset.name.lower(),
    "qty": lambda p: p.qty,
    "price": lambda p: p.price.price_eur,
    "value": lambda p: p.value,
    "day": lambda p: p.day_change_pct if p.day_change_pct is not None else -1e9,
    "cost": lambda p: p.cost,
    "gain": lambda p: p.unrealized,
    "gain_pct": lambda p: p.unrealized_pct if p.unrealized_pct is not None else -1e9,
    "weight": lambda p: p.weight,
}


def _account_filters(ctx: Any) -> list[dict[str, str]]:
    pf = ctx.portfolio()
    if pf is None:
        return []
    groups: dict[str, list[str]] = defaultdict(list)
    for a in pf.all_accounts():
        groups[pf.depot_group(a)].append(a)
    out = []
    for g in sorted(groups):
        accs = groups[g]
        if len(accs) > 1 or accs[0] != g:
            out.append({"value": f"grp:{g}", "label": f"{g} (Depot)"})
        for a in sorted(accs):
            out.append({"value": f"acc:{a}", "label": f"{a}" if a == g else f"  {a}"})
    return out


def _kpis(ctx: Any, val: Any, hist: Any) -> dict[str, Any]:
    k: dict[str, Any] = {"ytd": None, "irr": None, "ttwror_max": None}
    if hist is None or hist.n < 2:
        return k
    s = P.total_series(hist)
    a, b, inc = P.bounds(hist, "YTD")
    k["ytd"] = P.metrics(hist, s, a, b, inc)
    a, b, inc = P.bounds(hist, "MAX")
    mx = P.metrics(hist, s, a, b, inc)
    k["irr"] = mx.get("irr")
    k["ttwror_max"] = mx.get("ttwror")
    return k


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    ctx = get_ctx(request)
    val = ctx.valuation()
    if val is None:
        return render(request, "empty.html", active="dashboard")
    hist = ctx.history_with_live()
    movers = [p for p in val.positions if p.day_change_pct is not None and not p.asset.is_fiat and p.value > 0]
    gainers = sorted([p for p in movers if p.day_change_pct > 0], key=lambda p: -p.day_change_pct)[:5]
    losers = sorted([p for p in movers if p.day_change_pct < 0], key=lambda p: p.day_change_pct)[:5]
    news = _latest_news(ctx, int(ctx.settings.get("news.dashboard_count", 5)))
    from app.fullexport import status as restore_status

    return render(request, "dashboard.html", active="dashboard", val=val, kpi=_kpis(ctx, val, hist), gainers=gainers,
                  losers=losers, news=news, threshold=ctx.settings.get("allocation.other_threshold_pct", 1.0),
                  default_range=ctx.settings.get("ui.default_range", "1J"), restore=restore_status(ctx))


def _latest_news(ctx: Any, n: int) -> list[Any]:
    min_rel = float(ctx.settings.get("news.min_relevance", 0.2))
    return ctx.db.q(
        "SELECT * FROM news_item WHERE hidden_reason IS NULL AND relevance>=? ORDER BY published_at DESC LIMIT ?",
        (min_rel, n))


@router.get("/positions", response_class=HTMLResponse)
def positions(request: Request, account: str = "", segment: str = "", category: str = "", sort: str = "value",
              dir: str = "desc", view: str = "table") -> HTMLResponse:
    ctx = get_ctx(request)
    val = ctx.valuation(account or None)
    if val is None:
        return render(request, "empty.html", active="positions")
    rows = [p for p in val.positions if (not segment or p.segment == segment)
            and (not category or p.category == category)]
    keyf = SORT_KEYS.get(sort, SORT_KEYS["value"])
    rows.sort(key=keyf, reverse=(dir != "asc"))
    hist = ctx.history_with_live()
    sparks: dict[str, Any] = {}
    if hist is not None and hist.n > 1:
        idx = hist.asset_index()
        lo = max(0, hist.n - 31)
        for p in rows:
            k = idx.get(p.asset_id)
            if k is not None and not p.asset.is_fiat:
                sparks[p.asset_id] = sparkline(hist.asset_price[k][lo:].tolist(), label=p.asset.symbol)
    segments = sorted({p.segment for p in val.positions})
    categories = sorted({p.category for p in val.positions if not segment or p.segment == segment})
    totals = {
        "value": sum(p.value for p in rows), "cost": sum(p.cost for p in rows if not p.asset.is_fiat),
        "gain": sum(p.unrealized for p in rows), "day": sum(p.day_change or 0 for p in rows),
        "weight": sum(p.weight for p in rows),
    }
    params = {"account": account, "segment": segment, "category": category, "sort": sort, "dir": dir, "view": view}
    tpl = "partials/positions_table.html" if request.headers.get("hx-target") == "positions-body" else "positions.html"
    return render(request, tpl, active="positions", val=val, rows=rows, sparks=sparks, segments=segments,
                  categories=categories, accounts=_account_filters(ctx), params=params, totals=totals)


# -- Detailansicht ---------------------------------------------------------------------------------

def tax_free_date(acq: date) -> date:
    """§ 23 EStG: Veräußerung nach Ablauf eines Jahres steuerfrei → Anschaffung + 1 Jahr + 1 Tag."""
    return add_years(acq, 1) + timedelta(days=1)


def tax_lots(ctx: Any, a: Any) -> tuple[list[Any], Any, str | None]:
    """Lots und Haltefrist-Regel des aktiven Steuer-Regelwerks (z. B. FIFO je Wallet); Fallback: Anzeige-Ledger."""
    try:
        from app.tax.service import tax_service

        svc = tax_service(ctx)
        pack = svc.pack()
        led = svc.ledger(pack, svc.options(pack))
        return led.lots_for(a.asset_id), pack.holding_end, pack.name
    except Exception as e:  # Steuermodul optional – Detailansicht darf nicht daran scheitern
        log.debug("Steuer-Lots nicht verfügbar: %s", e)
        led = ctx.ledger()
        return (led.lots_for(a.asset_id) if led else [],
                lambda asset, acq: tax_free_date(acq) if asset.is_crypto else None, None)


def asset_context(ctx: Any, asset_id: str) -> dict[str, Any]:
    pf = ctx.portfolio()
    led = ctx.ledger()
    if pf is None or led is None or asset_id not in pf.assets:
        raise HTTPException(status_code=404, detail="Asset nicht gefunden")
    a = pf.assets[asset_id]
    val = ctx.valuation()
    pos = val.by_id().get(asset_id) if val else None
    prices = ctx.prices.latest_eur_many([a], pf)
    pi = prices.get(asset_id)
    today = today_local()
    lots = []
    lot_list, holding_end, pack_name = tax_lots(ctx, a) if a.is_crypto else (led.lots_for(asset_id), None, None)
    for lot in lot_list:
        tf = holding_end(a, lot.acq_date) if holding_end is not None and lot.origin != "phantom" else None
        cur = float(lot.qty) * (pi.price_eur if pi and pi.valued else 0)
        lots.append({"lot": lot, "tax_free": tf, "is_free": tf is not None and tf <= today, "value": cur,
                     "gain": cur - float(lot.cost), "days_left": (tf - today).days if tf and tf > today else 0})
    by_account: dict[str, dict[str, float]] = defaultdict(lambda: {"qty": 0.0, "cost": 0.0})
    for lot in led.lots_for(asset_id):
        by_account[lot.account]["qty"] += float(lot.qty)
        by_account[lot.account]["cost"] += float(lot.cost)
    balances = {acc: float(q) for (acc, asset), q in led.balances.items() if asset == asset_id and abs(q) > DUST}
    realized = sum(float(d.gain) for d in led.disposals if d.asset == asset_id and d.kind in REALIZED_KINDS)
    income = led.income_by_asset().get(asset_id, 0)
    fees = sum(float(f.eur) for f in led.fees if f.asset == asset_id)
    txids = set(led.tx_by_asset.get(asset_id, []))
    txs = [t for t in pf.txs if t.tx_id in txids]
    txs.sort(key=lambda t: t.ts, reverse=True)
    info, info_at = ctx.prices.asset_info(a)
    series = ctx.prices.series_for(a)
    meta = ctx.store.meta(series) if series else None
    perf = None
    hist = ctx.history_with_live()
    if hist is not None and asset_id in hist.asset_index():
        s = P.group_series(hist, [asset_id])
        k = hist.asset_index()[asset_id]
        nz = [i for i in range(hist.n) if hist.asset_qty[k][i] != 0]
        if nz:
            first = nz[0]
            # ab Erstzugang: Basis = Vortag (Wert 0), damit der erste Tag mitzählt
            a_i, b_i, inc = (first - 1, hist.n - 1, False) if first > 0 else P.bounds(hist, "MAX")
            perf = P.metrics(hist, s, a_i, b_i, inc)
    news = ctx.db.q(
        """SELECT n.* FROM news_item n JOIN news_asset na ON na.item_id=n.id
           WHERE na.asset_id=? AND n.hidden_reason IS NULL ORDER BY n.published_at DESC LIMIT 12""", (asset_id,))
    return {
        "a": a, "pos": pos, "pi": pi, "lots": lots, "by_account": dict(by_account), "balances": balances,
        "realized": realized, "income": float(income), "fees": fees, "txs": txs[:60], "tx_total": len(txs),
        "info": info or {}, "info_at": info_at, "series": series, "meta": meta, "perf": perf, "news": news,
        "colors": asset_colors(asset_id, a.segment), "account_scope": ctx.settings.get("ledger.scope", "global"),
        "tax_pack": pack_name, "quality": hist.quality.get(asset_id) if hist is not None else None,
    }


@router.get("/asset/{asset_id:path}", response_class=HTMLResponse)
def asset_page(request: Request, asset_id: str) -> HTMLResponse:
    ctx = get_ctx(request)
    return render(request, "asset.html", active="positions", **asset_context(ctx, asset_id))


@router.get("/panel/asset/{asset_id:path}", response_class=HTMLResponse)
def asset_panel(request: Request, asset_id: str) -> HTMLResponse:
    ctx = get_ctx(request)
    return render(request, "partials/asset_panel.html", alerts=[], in_panel=True, **asset_context(ctx, asset_id))


# -- Performance -------------------------------------------------------------------------------------

def scope_options(ctx: Any) -> list[dict[str, str]]:
    pf = ctx.portfolio()
    val = ctx.valuation()
    if pf is None or val is None:
        return []
    opts = [{"value": "total", "label": "Gesamtportfolio"}]
    held_ever = {ev.asset for ev in ctx.ledger().qty_events}
    segs = sorted({pf.asset(a).segment for a in held_ever})
    opts += [{"value": f"segment:{s}", "label": f"Segment: {s}"} for s in segs]
    cats = sorted({pf.asset(a).category_label for a in held_ever if not pf.asset(a).is_fiat})
    opts += [{"value": f"category:{c}", "label": f"Kategorie: {c}"} for c in cats]
    assets = sorted((pf.asset(a) for a in held_ever if not pf.asset(a).is_fiat), key=lambda x: x.name.lower())
    opts += [{"value": f"asset:{a.asset_id}", "label": f"Position: {a.name}"} for a in assets]
    return opts


def scope_assets(ctx: Any, scope: str) -> list[str] | None:
    pf = ctx.portfolio()
    held_ever = {ev.asset for ev in ctx.ledger().qty_events}
    kind, _, name = scope.partition(":")
    if kind == "segment":
        return [a for a in held_ever if pf.asset(a).segment == name]
    if kind == "category":
        return [a for a in held_ever if pf.asset(a).category_label == name]
    if kind == "asset":
        return [name]
    return None


def parse_d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


@router.get("/performance", response_class=HTMLResponse)
def performance(request: Request, period: str = "1J", scope: str = "total", start: str = "",
                end: str = "") -> HTMLResponse:
    ctx = get_ctx(request)
    hist = ctx.history_with_live()
    if hist is None:
        return render(request, "empty.html", active="performance")
    assets = scope_assets(ctx, scope)
    s = P.total_series(hist) if assets is None else P.group_series(hist, assets)
    rows = []
    for key in P.PERIODS:
        a, b, inc = P.bounds(hist, key)
        m = P.metrics(hist, s, a, b, inc)
        rows.append({"key": key, "label": P.PERIOD_LABELS[key], **m})
    sd, ed = parse_d(start), parse_d(end)
    if sd or ed:
        a, b, inc = P.bounds(hist, start=sd, end=ed)
        sel = P.metrics(hist, s, a, b, inc)
        sel_label = f"{(sd or hist.start).strftime('%d.%m.%Y')} – {(ed or hist.dates[-1]).strftime('%d.%m.%Y')}"
    else:
        a, b, inc = P.bounds(hist, period)
        sel = P.metrics(hist, s, a, b, inc)
        sel_label = P.PERIOD_LABELS.get(period.upper(), period)
    est = {k: v for k, v in hist.estimated_days.items() if v and (assets is None or k in assets)}
    val = ctx.valuation()
    # nur heute gehaltene Positionen ohne gültigen Kurs (wie Übersicht); verkaufte stehen unter Datenqualität
    unvalued = [p.asset_id for p in (val.unvalued if val else []) if assets is None or p.asset_id in assets]
    pf = ctx.portfolio()
    return render(request, "performance.html", active="performance", rows=rows, sel=sel, sel_label=sel_label,
                  period=period.upper(), scope=scope, scopes=scope_options(ctx), start=start, end=end,
                  estimated=[(pf.asset(k).name, v) for k, v in est.items()],
                  unvalued=[pf.asset(x).name for x in unvalued],
                  benchmarks=ctx.settings.get("performance.benchmarks") or [], periods=P.PERIODS,
                  period_labels=P.PERIOD_LABELS, hist_start=hist.start, hist_end=hist.dates[-1])


# -- Datenqualität ------------------------------------------------------------------------------------

@router.get("/quality", response_class=HTMLResponse)
def quality(request: Request, import_id: int | None = None) -> HTMLResponse:
    ctx = get_ctx(request)
    imports = ctx.db.q("SELECT * FROM imports ORDER BY id DESC LIMIT 25")
    active = ctx.active_import_id()
    sel = import_id or active or (imports[0]["id"] if imports else None)
    cur = ctx.db.q1("SELECT * FROM imports WHERE id=?", (sel,)) if sel else None
    report = json.loads(cur["report_json"]) if cur and cur["report_json"] else None
    diff = json.loads(cur["diff_json"]) if cur and cur["diff_json"] else None
    check = json.loads(cur["check_json"]) if cur and cur["check_json"] else None
    issue_rows = ctx.db.q("SELECT data_json FROM issues WHERE import_id=? ORDER BY seq", (active,)) if active else []
    issues = [json.loads(r["data_json"]) for r in issue_rows]
    val = ctx.valuation()
    led = ctx.ledger()
    ledger_issues = [i for i in (led.issues if led else []) if i.severity in ("warning", "info")]
    sources = ctx.db.q("SELECT * FROM source_status ORDER BY kind, source_id")
    events = ctx.db.q("SELECT * FROM event_log ORDER BY id DESC LIMIT 150")
    jobs = ctx.job_status()
    next_runs = ctx.scheduler.next_runs() if ctx.scheduler else {}
    hist = ctx.history()
    pf = ctx.portfolio()
    meta_rows = ctx.db.q("SELECT series, history_from, history_to, history_status, history_error, last_history_fetch, "
                         "alt_series, alt_status, alt_note, alt_checked_at FROM series_meta "
                         "WHERE history_status IS NOT NULL ORDER BY history_status DESC, series")
    price_quality = sorted((q for q in (hist.quality.values() if hist else []) if q.state != "complete" or q.alt_days),
                           key=lambda q: ({"failed": 0, "estimated": 1, "gaps": 2}.get(q.state, 3),
                                          -(q.estimated_days + q.gap_days), q.asset_id))
    usage = ctx.db.q("SELECT * FROM api_usage ORDER BY period DESC, provider LIMIT 40")
    journal: dict[str, Any] | None = None
    try:
        from app.journal.service import journal_service

        js = journal_service(ctx)
        journal = {"count": ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active' AND source='manual'",
                                          default=0),
                   "csv_count": ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active' AND "
                                              "source<>'manual'", default=0),
                   "dups": js.duplicates(), "log": js.log(15)}
    except Exception as e:  # Journal-Modul optional
        log.debug("Journal nicht verfügbar: %s", e)
    return render(request, "quality.html", active="quality", imports=imports, cur=cur, journal=journal,
                  active_id=active,
                  report=report, diff=diff, check=check, issues=issues, val=val, ledger_issues=ledger_issues[:300],
                  sources=sources, events=events, jobs=jobs, next_runs=next_runs, cg=ctx.prices.cg_budget(),
                  hist=hist, meta_rows=meta_rows, usage=usage, secrets=ctx.config.secrets.status(),
                  price_quality=price_quality,
                  cash_tracked=(led.cash_tracked if led else {}),
                  asset_name=lambda aid: pf.asset(aid).name if pf else aid,
                  src_suggested=ctx.db.scalar("SELECT COUNT(*) FROM asset_source WHERE status='suggested'", default=0),
                  s_age={k: _age_label(ctx.settings.get(FB_KEYS[k], FB_DEFAULT[k])) for k in FB_KEYS})


def _age_label(v: Any) -> str:
    try:
        return f"{int(v)} Tage" if int(v) > 0 else "unbegrenzt"
    except (TypeError, ValueError):
        return "–"


# -- Einstellungen ---------------------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "") -> HTMLResponse:
    ctx = get_ctx(request)
    pf = ctx.portfolio()
    led = ctx.ledger()
    accounts = pf.all_accounts() if pf else []
    backups: list[dict[str, Any]] = []
    try:
        from app.jobs.maintenance import list_backups

        backups = list_backups(ctx)
    except Exception as e:  # Wartungsmodul optional
        log.debug("Backups nicht lesbar: %s", e)
    exports: list[dict[str, Any]] = []
    archive: list[dict[str, Any]] = []
    try:
        from app.jobs.exports import list_archive, list_exports

        exports, archive = list_exports(ctx), list_archive(ctx)
    except Exception as e:  # Exportmodul optional / Verzeichnis nicht lesbar
        log.debug("Exporte nicht lesbar: %s", e)
    job = ctx.db.q1("SELECT last_end, last_ok, last_error FROM job_status WHERE job='auto_export'")
    ds_rows = ctx.db.q("SELECT kind, status, enabled FROM data_source")
    from app.datasources.vault import Vault

    ds_summary = {"total": len(ds_rows), "vault": Vault.load().status(),
                  "exchange": sum(1 for r in ds_rows if r["kind"] == "exchange"),
                  "wallet": sum(1 for r in ds_rows if r["kind"] == "wallet"),
                  "error": sum(1 for r in ds_rows if r["status"] == "error" and r["enabled"])}
    from app.fullexport import status as restore_status
    from app.prices.budget import plan_view

    price_plan = plan_view(ctx.settings, ctx.prices.budget_inputs(pf, led), ctx.prices.cg_budget(),
                           ctx.config.coingecko_plan, bool(ctx.config.secrets.coingecko_api_key))
    next_runs = ctx.scheduler.next_runs() if ctx.scheduler else {}
    return render(request, "settings.html", active="settings", s=ctx.settings.all(), accounts=accounts,
                  detected_cash=(led.cash_tracked if led else {}), secrets=ctx.config.secrets.status(),
                  config=ctx.config, saved=saved, backups=backups, exports=exports, archive=archive,
                  export_job=job, ds_summary=ds_summary, pp=price_plan, next_runs=next_runs,
                  restore=restore_status(ctx))


def fmt_ts(ts: str | None) -> str:
    d = parse_iso(ts) if ts else None
    return d.astimezone(local_tz()).strftime("%d.%m.%Y %H:%M") if d else "–"


def now_utc() -> datetime:
    return datetime.now(UTC)
