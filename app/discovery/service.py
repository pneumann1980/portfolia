"""Discovery & Intelligence – Ablaufsteuerung: Unternehmen aufnehmen, Discovery-Läufe, Analysen (historisiert),
Vergleich, Radar.

Datenhaltung: eigene ``disc_*``-Tabellen (Migration 41). Kurse und Stammdaten nutzen die vorhandene Kursablage
(Reihe ``yahoo:<Ticker>``), SEC-Antworten liegen im Plattencache. Keine Buchung, kein Ledger, keine Steuerdaten werden
berührt; die Tabellen gehören nicht zu den Revisionstabellen (Diagnose-Datenstand und Portfolio-Zwischenspeicher
bleiben unverändert).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from app.discovery import ai as AI
from app.discovery import fit as FIT
from app.discovery import fundamentals as F
from app.discovery import nash as NASH
from app.discovery import scoring as S
from app.discovery import signals as SG
from app.discovery import valuation as V
from app.discovery.identity import Directory, Issuer, directory, is_spac, norm_name, norm_ticker
from app.discovery.sec import SecClient, SecError
from app.util.timeutil import iso, today_local

log = logging.getLogger(__name__)

STATUSES = {"neu": "Neu", "beobachten": "Beobachten", "zurueckgestellt": "Zurückgestellt", "verworfen": "Verworfen"}
SETTINGS = {"discovery.enabled": True, "discovery.news_days": 14, "discovery.max_new_per_run": 10,
            "discovery.auto_analyse_per_run": 1, "discovery.themes": True, "discovery.ai": True,
            "discovery.sec_user_agent": "", "discovery.screen_batch": 20, "discovery.min_revenue_musd": 20,
            "discovery.ki_companies": 6,
            "discovery.auto.news": 24, "discovery.auto.screening": 24, "discovery.auto.ki": 168,
            "discovery.auto.analysen": 24, "discovery.auto.beobachtet": 168}
SORTS = {"disruption": "Disruption", "nash125": "Tom Nash 125", "quality": "Quality", "garp": "Growth/GARP",
         "fundamental_v1": "Portfolia Fundamental v1", "valuation": "Bewertungsattraktivität", "fit": "Portfolio-Fit",
         "signal": "Aktualität der Signale", "prioritaet": "Discovery-Priorität"}
CATEGORIES = {
    "neu": "Neue Entdeckungen", "disruptiv": "Disruptive Geschäftsmodelle", "wendepunkt": "Fundamentale Wendepunkte",
    "fehlbewertung": "Mögliche Marktfehlbewertungen", "quality": "Quality", "garp": "Growth/GARP",
    "nash100": "Nash 100+", "beobachten": "Zu beobachten", "portfolio": "Bereits im Portfolio",
}
HIDDEN_MAX_CAP_USD = 10e9
_RUN_LOCK = threading.Lock()


def setting(ctx: Any, key: str) -> Any:
    return ctx.settings.get(key, SETTINGS.get(key))


def sec(ctx: Any) -> SecClient:
    return SecClient(ctx)


def now_iso() -> str:
    return iso(datetime.now(UTC)) or ""


# ------------------------------------------------------------------------------------------------------------
# Unternehmen
# ------------------------------------------------------------------------------------------------------------

def company(db: Any, cid: int) -> dict[str, Any] | None:
    r = db.q1("SELECT * FROM disc_company WHERE id=?", (cid,))
    if r is None:
        return None
    d = dict(r)
    d["tickers"] = [x["value"] for x in db.q("SELECT value FROM disc_alias WHERE company_id=? AND kind='ticker' "
                                             "ORDER BY value", (cid,))] or [d["ticker"]]
    return d


def by_ticker(db: Any, ticker: str) -> dict[str, Any] | None:
    t = norm_ticker(ticker)
    r = db.q1("SELECT company_id FROM disc_alias WHERE kind='ticker' AND value_norm=?", (t,))
    if r is None:
        r = db.q1("SELECT id AS company_id FROM disc_company WHERE ticker=?", (t,))
    return company(db, int(r["company_id"])) if r else None


def upsert_issuer(ctx: Any, iss: Issuer, origin: str) -> tuple[int | None, str]:
    """Emittent als Unternehmen aufnehmen (oder vorhandenes zurückgeben). SPACs werden abgelehnt."""
    db = ctx.db
    row = db.q1("SELECT id FROM disc_company WHERE cik=?", (iss.cik,))
    if row is not None:
        return int(row["id"]), ""
    try:
        sub = sec(ctx).submissions(iss.cik)
    except SecError as e:
        sub = {}
        log.info("Discovery: SEC-Stammdaten %s nicht abrufbar: %s", iss.ticker, e)
    if sub and is_spac(sub):
        return None, f"{iss.name} ({iss.ticker}) ist eine Mantelgesellschaft (SPAC, SIC 6770) – nicht aufgenommen"
    stamp = now_iso()
    addr = ((sub.get("addresses") or {}).get("business") or {}) if sub else {}
    country = addr.get("countryDescription") or ("USA" if addr.get("stateOrCountry") and
                                                len(str(addr.get("stateOrCountry"))) == 2 and
                                                not addr.get("isForeignLocation") else None)
    with db.transaction() as c:
        cur = c.execute(
            "INSERT INTO disc_company(cik, ticker, exchange, yahoo_symbol, name, sector, industry, country, sic, "
            "listed, status, origin, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,1,'neu',?,?,?)",
            (iss.cik, norm_ticker(iss.ticker), iss.exchange, iss.ticker.replace(".", "-"), iss.name, None,
             (sub.get("sicDescription") if sub else None), country, str(sub.get("sic") or "") if sub else None,
             origin, stamp, stamp))
        cid = int(cur.lastrowid)  # type: ignore[arg-type]
        for t in iss.tickers:
            c.execute("INSERT OR IGNORE INTO disc_alias(kind, value_norm, company_id, value) VALUES ('ticker',?,?,?)",
                      (norm_ticker(t), cid, t))
        names = [iss.name] + [str(f.get("name")) for f in (sub.get("formerNames") or []) if f.get("name")]
        for i, n in enumerate(names):
            c.execute("INSERT OR IGNORE INTO disc_alias(kind, value_norm, company_id, value) VALUES (?,?,?,?)",
                      ("name" if i == 0 else "former_name", norm_name(n), cid, n))
    return cid, ""


def resolve(ctx: Any, *, ticker: str = "", name: str = "") -> tuple[Any, Directory | None, str]:
    try:
        d = directory(sec(ctx))
    except SecError as e:
        return None, None, f"SEC-Verzeichnis nicht verfügbar: {e}"
    return d.resolve(ticker=ticker, name=name), d, ""


def add_manual(ctx: Any, query: str) -> dict[str, Any]:
    """Manuell eingegebenes Unternehmen (Ticker oder Name) aufnehmen."""
    q = (query or "").strip()[:120]
    if not q:
        return {"error": "Bitte Ticker oder Firmenname eingeben."}
    looks_ticker = len(q) <= 6 and q.replace(".", "").replace("-", "").isalnum() and q.upper() == q.upper()
    res, _d, err = resolve(ctx, ticker=q if looks_ticker else "", name="" if looks_ticker else q)
    if err:
        return {"error": err}
    if res.status == "not_found" and looks_ticker:
        res, _d, err = resolve(ctx, name=q)
    if res.status == "ambiguous":
        return {"error": res.reason, "choices": [{"ticker": i.ticker, "name": i.name, "exchange": i.exchange}
                                                 for i in res.candidates]}
    if res.status != "ok":
        return {"error": res.reason}
    cid, why = upsert_issuer(ctx, res.issuer, "manuell")
    if cid is None:
        return {"error": why}
    SG.add_signal(ctx.db, cid, kind="manuell", title="Manuell aufgenommen", origin="manuell", key="manuell",
                  detail=f"Eingabe: {q}")
    return {"id": cid}


def set_status(db: Any, cid: int, status: str, note: str = "") -> bool:
    if status not in STATUSES:
        return False
    return bool(db.x("UPDATE disc_company SET status=?, note=COALESCE(NULLIF(?, ''), note), updated_at=? WHERE id=?",
                     (status, note[:300], now_iso(), cid)).rowcount)


# ------------------------------------------------------------------------------------------------------------
# Discovery-Lauf
# ------------------------------------------------------------------------------------------------------------

def run_discovery(ctx: Any, *, job: Any = None, **_legacy: Any) -> dict[str, Any]:
    """Nachrichten-Discovery: News-Nennungen → Identität (SEC) → Dubletten → Signale. Begrenzt durch
    ``discovery.max_new_per_run``. KI-Themen und Analysen laufen als eigene Schritte (siehe :mod:`engine`)."""
    if not setting(ctx, "discovery.enabled"):
        return {"skipped": "Discovery ausgeschaltet"}
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "Nachrichten-Discovery läuft bereits"}
    db = ctx.db
    stats: dict[str, Any] = {"mentions": 0, "new_companies": 0, "signals": 0, "unresolved": [], "ambiguous": [],
                             "rejected": [], "touched": {}, "errors": []}
    run_id = int(db.x("INSERT INTO disc_run(kind, started_at) VALUES ('news', ?)", (now_iso(),)).lastrowid or 0)
    try:
        try:
            d = directory(sec(ctx))
        except SecError as e:
            stats["errors"].append(f"SEC-Verzeichnis: {e}")
            return stats
        max_new = int(setting(ctx, "discovery.max_new_per_run") or 0)
        touched: dict[int, float] = {}
        mentions = SG.news_mentions(db, int(setting(ctx, "discovery.news_days") or 14))
        for i, m in enumerate(mentions, 1):
            stats["mentions"] += 1
            if job is not None and i % 10 == 0:
                job.update(geprueft=i, nennungen=len(mentions))
            res = d.resolve(name=m["name"])
            if res.status == "ambiguous":
                stats["ambiguous"].append(m["name"])
                continue
            if res.status != "ok" or res.issuer is None:
                stats["unresolved"].append(m["name"])
                continue
            known = db.q1("SELECT id FROM disc_company WHERE cik=?", (res.issuer.cik,))
            if known is None and stats["new_companies"] >= max_new:
                continue
            cid, why = upsert_issuer(ctx, res.issuer, "news")
            if cid is None:
                stats["rejected"].append(why)
                continue
            if known is None:
                stats["new_companies"] += 1
                record_candidate(db, getattr(job, "id", None), "news", name=res.issuer.name,
                                 ticker=res.issuer.ticker, cik=res.issuer.cik, company_id=cid, status="neu",
                                 reason=f"Nachricht: {m['title']}", priority=None,
                                 evidence=[{"art": "Meldung", "titel": m["title"], "url": m["url"]}])
            st = SG.strength(m["importance"], m["n_sources"])
            if SG.add_signal(db, cid, kind="nachricht", title=m["title"], origin="news", key=f"news:{m['event']}",
                             detail=m["summary"], ref=m["event"] + ":" + ",".join(map(str, m["items"][:10])),
                             url=m["url"] or "", source_name=", ".join(m["sources"][:4]), n_sources=m["n_sources"],
                             direction=m["direction"], strength_=st, published_at=m["published"]):
                stats["signals"] += 1
                touched[cid] = max(touched.get(cid, 0.0), st)
        stats["touched"] = touched
        return stats
    finally:
        stats["unresolved"] = sorted(set(stats["unresolved"]))[:50]
        stats["ambiguous"] = sorted(set(stats["ambiguous"]))[:50]
        db.x("UPDATE disc_run SET finished_at=?, stats_json=? WHERE id=?",
             (now_iso(), json.dumps(stats, ensure_ascii=False, default=str), run_id))
        _RUN_LOCK.release()


def record_candidate(db: Any, job_id: int | None, path: str, *, name: str, ticker: str = "", cik: int | None = None,
                     company_id: int | None = None, status: str, reason: str, priority: float | None = None,
                     theme: str = "", evidence: list[dict[str, Any]] | None = None) -> None:
    """Kandidat eines Suchlaufs festhalten – auch verworfene (mit Begründung), für Nachvollziehbarkeit und Export."""
    db.x("INSERT INTO disc_candidate(job_id, path, theme, name, ticker, cik, company_id, status, reason, priority, "
         "evidence_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
         (job_id, path, theme or None, name[:120], ticker[:16] or None, cik, company_id, status, reason[:500],
          priority, json.dumps(evidence or [], ensure_ascii=False, default=str), now_iso()))


# ------------------------------------------------------------------------------------------------------------
# Daten für die Analyse
# ------------------------------------------------------------------------------------------------------------

def _store_facts(db: Any, cid: int, fu: F.Fundamentals) -> None:
    rows = []
    for key, by_year in fu.rows.items():
        if key not in F.CONCEPTS:
            continue
        for y, v in by_year.items():
            if v.origin == "reported" and v.v is not None and v.accn:
                rows.append((cid, key, v.tag, v.unit, None, y, None, None, v.form, v.v, v.filed, v.accn))
    with db.transaction() as c:
        c.executemany("INSERT OR REPLACE INTO disc_fact(company_id, concept, tag, unit, period_start, period_end, fy, "
                      "fp, form, value, filed, accn) VALUES (?,?,?,?,COALESCE(?, ''),?,?,?,?,?,?,?)", rows)


def _quote(ctx: Any, co: dict[str, Any], fu: F.Fundamentals) -> dict[str, Any]:
    """Kurs, Marktkapitalisierung, Yahoo-Stammdaten und Kurshistorie aus der gemeinsamen Kursablage."""
    from app.prices.market import market_data

    sym = co.get("yahoo_symbol") or co["ticker"]
    series = f"yahoo:{sym}"
    md = market_data(ctx)
    q: dict[str, Any] = {"series": series, "source": "Yahoo Finance" if ctx.prices.demo is None else "Demo-Kurse"}
    try:
        md.refresh([series])
    except Exception as e:  # Marktdaten sind Zusatz – die Analyse läuft ohne Kurs weiter (Bewertung „fehlt“)
        from app.db import fallback

        fallback(f"Discovery-Kursabruf {series}", e)
        q["error"] = f"Kursabruf: {type(e).__name__}"
    _extra_info(ctx, sym, series)
    _history(ctx, series)
    snap = md.snapshots([series]).get(series)
    info = ctx.store.info(series)[0] or {}
    if snap is not None and snap.price_native:
        q.update(price=snap.price_native, ccy=snap.ccy, at=iso(snap.ts) if snap.ts else None, stale=snap.stale)
    q["market_cap"] = float(info["marketCap"]) if info.get("marketCap") else None
    q["forward_pe"] = float(info["forwardPE"]) if info.get("forwardPE") else None
    q["inst_pct"] = float(info["heldPercentInstitutions"]) if info.get("heldPercentInstitutions") is not None \
        else None
    q["short_pct"] = float(info["shortPercentOfFloat"]) if info.get("shortPercentOfFloat") is not None else None
    q["info"] = {k: info.get(k) for k in ("sector", "industry", "country", "longBusinessSummary", "website",
                                          "fullExchangeName", "longName", "currency", "financialCurrency")}
    rep, trade = (info.get("financialCurrency") or fu.currency or "").upper(), (q.get("ccy") or "").upper()
    q["fx"] = _fx(ctx, rep, trade)
    rows = [(r["date"], float(r["close"]) / float(r["split_factor"] or 1.0))  # Handelswährung, split-bereinigt
            for r in ctx.store.daily_range(series, today_local() - timedelta(days=5 * 366), today_local())
            if r["close"]]
    q["price_cagr_5y"], q["price_cagr_period"] = V.price_cagr(rows)
    return q


def _fx(ctx: Any, rep: str, trade: str) -> float | None:
    if not rep or not trade:
        return None
    if rep == trade:
        return 1.0
    a, _ = ctx.prices.fx_to_eur(rep)
    b, _ = ctx.prices.fx_to_eur(trade)
    return a / b if a and b else None


def _extra_info(ctx: Any, sym: str, series: str) -> None:
    """Institutioneller Anteil, Short Interest, Sektor (yfinance ``info``) – einmal je Woche, in der Kursablage."""
    if ctx.prices.yahoo is None:
        return
    info, _at = ctx.store.info(series)
    stamp = (info or {}).get("_disc_info")
    if stamp and stamp >= (today_local() - timedelta(days=7)).isoformat():
        return
    keys = ("heldPercentInstitutions", "shortPercentOfFloat", "sector", "industry", "country", "longBusinessSummary",
            "website", "forwardPE", "financialCurrency")
    try:
        ctx.prices.yahoo.limiter.wait()
        raw = ctx.prices.yahoo.yf.Ticker(sym).info or {}
    except Exception as e:
        log.info("Discovery: Yahoo-Info %s nicht verfügbar: %s", sym, e)
        return
    ctx.prices._merge_info(series, {**{k: raw.get(k) for k in keys if raw.get(k) not in (None, "")},
                                    "_disc_info": today_local().isoformat()})


def _history(ctx: Any, series: str) -> None:
    first = ctx.store.coverage(series)[0]
    start = today_local() - timedelta(days=5 * 366)
    if first and first <= (start + timedelta(days=30)).isoformat():
        return
    try:
        ctx.prices._backfill_series(series, None, start, today_local(), False)
    except Exception as e:
        log.info("Discovery: Kurshistorie %s nicht geladen: %s", series, e)


def _news_sources(db: Any, cid: int, limit: int = 8) -> list[dict[str, Any]]:
    rows = db.q("SELECT title, detail, url, source_name, published_at, kind FROM disc_signal WHERE company_id=? AND "
                "kind IN ('nachricht', 'thema') ORDER BY COALESCE(published_at, observed_at) DESC LIMIT ?",
                (cid, limit))
    return [{"id": f"N{i}", "kind": "Meldung" if r["kind"] == "nachricht" else "KI-Thema (aus Meldungen)",
             "name": r["source_name"] or "News-Pipeline", "title": r["title"], "url": r["url"] or "",
             "published": str(r["published_at"] or "")[:10], "text": f"{r['title']}. {r['detail'] or ''}"}
            for i, r in enumerate(rows, 1)]


def _facts_text(fu: F.Fundamentals) -> str:
    """Kennzahlen als Text für die KI (Quelle „K“): nur berichtete bzw. berechnete Werte, mit Periode."""
    lines = [f"Berichtswährung: {fu.currency or 'unbekannt'}. Geschäftsjahre (Periodenende): {', '.join(fu.years)}."]
    for key in ("revenue", "revenue_growth", "gross_margin", "op_margin", "net_income", "fcf", "fcf_margin",
                "liquidity", "debt", "rnd_ratio", "dilution"):
        vals = []
        for y in fu.years:
            v = fu.get(key, y).v
            if v is None:
                continue
            vals.append(f"{y}: {v:.1%}" if key in F.PCT else f"{y}: {v / 1e6:,.0f} Mio.")
        if vals:
            lines.append(f"{F.LABELS.get(key, key)}: " + "; ".join(vals))
    q = (fu.quarter or {}).get("revenue")
    if q and "yoy" in q:
        lines.append(f"Letztes Quartal (bis {q['value'].get('period')}): Umsatzwachstum ggü. Vorjahresquartal "
                     f"{q['yoy']:.1%}")
    return "\n".join(lines)


def build_sources(ctx: Any, co: dict[str, Any], fu: F.Fundamentals | None, quote: dict[str, Any]
                  ) -> list[dict[str, Any]]:
    """Quellenverzeichnis mit stabilen IDs: S1 Geschäftsbericht, K Kennzahlen, Y Yahoo-Profil, N… Meldungen,
    sec:<Accession> je Einreichung (Kennzahlen), markt (Kurs/Marktkapitalisierung)."""
    out: list[dict[str, Any]] = []
    retrieved = today_local().isoformat()
    if co.get("cik"):
        try:
            got = sec(ctx).business_section(int(co["cik"]))
        except SecError as e:
            got = None
            log.info("Discovery: Geschäftsbericht %s nicht abrufbar: %s", co["ticker"], e)
        if got:
            text, filing = got
            out.append({"id": "S1", "kind": f"Geschäftsbericht ({filing['form']}), Item 1 Business",
                        "name": "SEC EDGAR", "title": f"{co['name']} {filing['form']} (Periode {filing['period']})",
                        "url": filing["url"], "published": filing["filed"], "retrieved": retrieved,
                        "locator": "Item 1. Business", "period": filing["period"], "text": text})
    if fu is not None and fu.years:
        out.append({"id": "K", "kind": "Kennzahlen (berechnet aus SEC XBRL companyfacts)", "name": "Portfolia",
                    "title": f"Kennzahlen {fu.years[0]}–{fu.years[-1]}",
                    "url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(co['cik']):010d}.json",
                    "retrieved": retrieved, "locator": "us-gaap/ifrs-full Elemente", "text": _facts_text(fu)})
        for accn, f in sorted((fu.filings or {}).items(), key=lambda kv: kv[1].get("filed", "")):
            out.append({"id": f"sec:{accn}", "kind": f"SEC-Einreichung {f.get('form', '')}", "name": "SEC EDGAR",
                        "title": f"{co['name']} {f.get('form', '')}, eingereicht {f.get('filed', '')}",
                        "url": f"https://www.sec.gov/Archives/edgar/data/{int(co['cik'])}/{accn.replace('-', '')}/",
                        "published": f.get("filed", ""), "retrieved": retrieved, "locator": f"Accession {accn}"})
    summary = (quote.get("info") or {}).get("longBusinessSummary")
    if summary:
        out.append({"id": "Y", "kind": "Unternehmensprofil", "name": quote.get("source", "Yahoo Finance"),
                    "title": f"Profil {co['ticker']}", "url": f"https://finance.yahoo.com/quote/{co['ticker']}/profile",
                    "retrieved": retrieved, "text": str(summary)[:3000]})
    if quote.get("price"):
        out.append({"id": "markt", "kind": "Kurs und Marktdaten", "name": quote.get("source", "Yahoo Finance"),
                    "title": f"Kurs {co['ticker']}", "url": f"https://finance.yahoo.com/quote/{co['ticker']}",
                    "published": str(quote.get("at") or "")[:16], "retrieved": retrieved,
                    "locator": "Kurs, Marktkapitalisierung, Institutionelle, Short Interest"})
    out.extend(_news_sources(ctx.db, int(co["id"])))
    return out


# ------------------------------------------------------------------------------------------------------------
# Analyse
# ------------------------------------------------------------------------------------------------------------

def _hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


MODES = {
    "auto": "Automatisch (Daten aus dem Zwischenspeicher, fehlende KI-Teile im Rahmen des Budgets)",
    "daten": "Fundamentaldaten aktualisieren (SEC und Kurse neu laden, keine KI)",
    "ergaenzen": "Fehlende KI-Teile ergänzen (nur Teile ohne gültiges Ergebnis)",
    "ki": "KI-Analyse starten (alle drei Teile neu erstellen)",
    "voll": "Vollanalyse aktualisieren (Daten neu laden, veraltete und fehlende KI-Teile neu)",
}
PART_STATES = {"ok": "vollständig", "veraltet": "veraltet (neue Daten seit Erstellung)", "fehlt": "noch nicht erstellt",
               "ungueltig": "KI-Antwort ungültig", "fehler": "Anbieterfehler", "budget": "Kontingent erschöpft",
               "pausiert": "Anbieter pausiert", "aus": "KI nicht konfiguriert", "keine_quellen": "keine Quellen",
               "uebersprungen": "nicht versucht (letzter Fehlversuch < 24 h)"}
AI_STATES = {"vollstaendig": "KI-Analyse vollständig", "veraltet": "KI-Teile vorhanden, aber teils veraltet",
             "teilweise": "KI teilweise erfolgreich", "fehlt": "Nur SEC-/Marktdaten – KI-Teil fehlt",
             "ungueltig": "KI-Antwort ungültig", "budget": "KI wegen Budget nicht ausgeführt",
             "keine_quellen": "KI mangels Quellen nicht sinnvoll ausführbar", "aus": "KI nicht konfiguriert",
             "fehler": "KI-Anbieter fehlerhaft", "pausiert": "KI-Anbieter pausiert", "historisch": "historische Sicht"}
RETRY_AFTER_H = 24  # automatische Läufe wiederholen fehlgeschlagene Teile frühestens nach 24 h


def ai_parts(db: Any, cid: int) -> dict[str, dict[str, Any]]:
    """Je Teilanalyse: jüngster Versuch und jüngstes gültiges Ergebnis."""
    out: dict[str, dict[str, Any]] = {}
    for part in AI.PARTS:
        last = db.q1("SELECT * FROM disc_ai_part WHERE company_id=? AND part=? ORDER BY id DESC LIMIT 1", (cid, part))
        ok = db.q1("SELECT * FROM disc_ai_part WHERE company_id=? AND part=? AND status='ok' ORDER BY id DESC LIMIT 1",
                   (cid, part))
        out[part] = {"last": dict(last) if last else None, "ok": dict(ok) if ok else None}
    return out


def _store_part(db: Any, cid: int, part: str, res: dict[str, Any] | None, meta: dict[str, Any],
                job_id: int | None) -> int:
    return int(db.x("INSERT INTO disc_ai_part(company_id, part, status, input_fp, prompt_version, provider, model, "
                    "result_json, error, call_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, part, meta.get("status") or "fehler", meta.get("input_fp"), AI.PART_VERSIONS[part],
                     meta.get("provider"), meta.get("model"),
                     json.dumps(res, ensure_ascii=False) if res is not None else None,
                     (meta.get("error") or "")[:300] or None, meta.get("call_id"), now_iso())).lastrowid or 0)


def _recent_fail(row: dict[str, Any] | None) -> bool:
    if not row or row["status"] == "ok":
        return False
    return row["created_at"] >= iso(datetime.now(UTC) - timedelta(hours=RETRY_AFTER_H))  # type: ignore[operator]


def plan_parts(db: Any, cid: int, mode: str, fps: dict[str, str]) -> list[str]:
    """Welche KI-Teile im Modus neu erstellt werden (gültige Ergebnisse werden nicht unnötig wiederholt)."""
    if mode == "daten":
        return []
    st = ai_parts(db, cid)
    out = []
    for part in AI.PARTS:
        ok, last = st[part]["ok"], st[part]["last"]
        if mode == "ki":
            out.append(part)
        elif mode == "voll":
            if ok is None or ok["input_fp"] != fps.get(part) or ok["prompt_version"] != AI.PART_VERSIONS[part]:
                out.append(part)
        elif ok is None and not (mode == "auto" and _recent_fail(last)):
            out.append(part)
    return out


def parts_state(db: Any, cid: int, fps: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Zustand je KI-Teil (für Anzeige und Gesamtzustand); ``fps`` = aktuelle Eingabe-Fingerabdrücke."""
    out = {}
    for part, st in ai_parts(db, cid).items():
        ok, last = st["ok"], st["last"]
        if ok is not None:
            stale = bool(fps) and (ok["input_fp"] != (fps or {}).get(part) or
                                   ok["prompt_version"] != AI.PART_VERSIONS[part])
            status = "veraltet" if stale else "ok"
        else:
            status = last["status"] if last else "fehlt"
        failed_after = last if (last and ok and last["id"] > ok["id"] and last["status"] != "ok") else None
        out[part] = {"part": part, "label": AI.PARTS[part], "status": status,
                     "status_label": PART_STATES.get(status, status), "ok_id": ok["id"] if ok else None,
                     "at": (ok or last or {}).get("created_at"), "provider": (ok or last or {}).get("provider"),
                     "model": (ok or last or {}).get("model"),
                     "error": (last or {}).get("error") if (not ok or failed_after) else "",
                     "last_attempt": (last or {}).get("created_at"), "last_status": (last or {}).get("status")}
    return out


def overall_ai_state(parts: dict[str, dict[str, Any]]) -> str:
    sts = [p["status"] for p in parts.values()]
    if all(s == "ok" for s in sts):
        return "vollstaendig"
    if all(s in ("ok", "veraltet") for s in sts):
        return "veraltet"
    if any(s in ("ok", "veraltet") for s in sts):
        return "teilweise"
    for s in ("aus", "budget", "pausiert", "keine_quellen", "ungueltig", "fehler"):
        if s in sts:
            return s
    return "fehlt"


def _total_return(ctx: Any, co: dict[str, Any], quote: dict[str, Any]) -> None:
    """Gesamtrendite (dividenden- und splitbereinigt) der Aktie und des S&P 500 über 5 Jahre – für Tom Nash 125."""
    if (quote.get("ccy") or "").upper() != "USD":
        quote["tr_missing"] = "Vergleich mit dem S&P 500 nur für in US-Dollar gehandelte Aktien"
        return
    sym = co.get("yahoo_symbol") or co["ticker"]
    day = today_local().isoformat()
    key = f"disc.tr.{sym}"
    cached = ctx.db.get_state(key)
    if isinstance(cached, dict) and cached.get("day") == day:
        quote.update(cached.get("q") or {})
        return
    q: dict[str, Any] = {}
    stock = _adj_history(ctx, sym)
    bench, bname = [], ""
    for b in BENCHMARKS:
        bench = _adj_history(ctx, b)
        if bench:
            bname = b
            break
    if not stock or not bench:
        q["tr_missing"] = "Kurshistorie (bereinigt) beim Datenanbieter nicht verfügbar"
    else:
        r = V.total_return(stock, bench)
        if r.get("missing"):
            q["tr_missing"] = r["missing"]
        else:
            q.update(tr_5y=r["tr"], spx_tr_5y=r["bench"], tr_period=r["period"],
                     tr_note="Yahoo Finance, Schlusskurse dividenden- und splitbereinigt (auto_adjust)",
                     tr_bench_note=BENCHMARKS[bname])
    ctx.db.set_state(key, {"day": day, "q": q})
    quote.update(q)


BENCHMARKS = {"^SP500TR": "S&P 500 Total Return Index (Dividenden reinvestiert)",
              "SPY": "SPDR S&P 500 ETF, dividendenbereinigt (laufende Kosten 0,09 % p. a. nicht herausgerechnet)"}
_ADJ_CACHE: dict[tuple[str, str], list[tuple[str, float]]] = {}


def _adj_history(ctx: Any, sym: str) -> list[tuple[str, float]]:
    """Bereinigte Tagesschlusskurse über gut 5 Jahre (yfinance ``auto_adjust``) – je Tag und Symbol einmal."""
    yahoo = getattr(ctx.prices, "yahoo", None)
    if yahoo is None:
        return []
    k = (sym, today_local().isoformat())
    if k in _ADJ_CACHE:
        return _ADJ_CACHE[k]
    try:
        yahoo.limiter.wait()
        df = yahoo.yf.Ticker(sym).history(period="6y", interval="1d", auto_adjust=True)
        rows = [(idx.date().isoformat(), float(v)) for idx, v in df["Close"].items() if v == v and v > 0]
    except Exception as e:  # Zusatzdaten: ohne sie bleibt das Kriterium „nicht bewertet“ (Grund sichtbar)
        from app.db import fallback

        fallback(f"Discovery-Gesamtrendite {sym}", e, quiet=True)
        rows = []
    _ADJ_CACHE[k] = rows
    return rows


def analyse_company(ctx: Any, cid: int, *, trigger: str = "manuell", force: bool = False,
                    as_of: str | None = None, mode: str = "auto", job: Any = None) -> dict[str, Any]:
    """Neue Analyseversion erstellen (überschreibt nie eine frühere).

    Ablauf: Daten (SEC, Kurs) → Quellen → KI-Teile nach ``mode`` (siehe :data:`MODES`) → Scores → Version. Eine
    Version gilt als „unverändert“, wenn Daten, Quellen, Kurs-Tag, Berechnungs-/Promptversion **und die verwendeten
    KI-Teile** gleich sind – ein nachgeholter KI-Teil erzeugt also immer eine neue Version."""
    db = ctx.db
    co = company(db, cid)
    if co is None:
        return {"status": "error", "error": "Unternehmen unbekannt"}
    mode = mode if mode in MODES else "auto"
    fresh = mode in ("daten", "voll")
    step = job.step if job is not None else (lambda *a, **k: None)
    fu: F.Fundamentals | None = None
    errors: list[str] = []
    step("SEC-Kennzahlen laden")
    if co.get("cik"):
        try:
            fu = F.parse(sec(ctx).facts(int(co["cik"]), fresh=fresh), as_of=as_of)
        except SecError as e:
            errors.append(f"SEC-Kennzahlen: {e}")
    if fu is not None:
        _store_facts(db, cid, fu)
    fu = fu or F.Fundamentals(currency=None)
    step("Kurs- und Marktdaten laden")
    quote = _quote(ctx, co, fu) if as_of is None else {}
    if as_of is None:
        _total_return(ctx, co, quote)
    info = quote.get("info") or {}
    if info.get("sector") or info.get("industry") or info.get("country"):
        db.x("UPDATE disc_company SET sector=COALESCE(?, sector), industry=COALESCE(?, industry), "
             "country=COALESCE(?, country), updated_at=? WHERE id=?",
             (info.get("sector"), info.get("industry"), info.get("country"), now_iso(), cid))
        co = company(db, cid) or co
    step("Quellen zusammenstellen")
    sources = build_sources(ctx, co, fu, quote) if as_of is None else []
    fps = {p: AI.part_fingerprint(p, co, sources) for p in AI.PARTS}
    ai_errors: list[str] = []
    if as_of is None:
        todo = plan_parts(db, cid, mode, fps)
        for i, part in enumerate(todo, 1):
            step(f"KI-Teil {i}/{len(todo)}: {AI.PARTS[part]}", ai_part=part)
            res, meta = AI.run_part(ctx, part, co, sources, job_id=getattr(job, "id", None))
            _store_part(db, cid, part, res, meta, getattr(job, "id", None))
            if meta.get("status") != "ok":
                ai_errors.append(f"{AI.PARTS[part]}: {meta.get('error') or meta.get('status')}")
                if meta.get("status") in ("aus", "budget", "pausiert"):  # weitere Teile wären ebenso chancenlos
                    for rest in todo[i:]:
                        _store_part(db, cid, rest, None, {"status": meta["status"], "error": meta.get("error"),
                                                          "input_fp": fps[rest]}, getattr(job, "id", None))
                    break
    step("Scores berechnen")
    pst = parts_state(db, cid, fps) if as_of is None else {}
    used = {p: json.loads(r["ok"]["result_json"]) for p, r in ai_parts(db, cid).items()
            if as_of is None and r["ok"] is not None}
    ai_state = overall_ai_state(pst) if as_of is None else "historisch"
    ihash = _hash(S.CALC_VERSION, NASH.METHOD_VERSION, fu.to_dict(), str(quote.get("at") or "")[:10],
                  [(s["id"], s.get("published"), hashlib.sha256(str(s.get("text", "")).encode()).hexdigest()[:16])
                   for s in sources], as_of, sorted((p, st.get("ok_id")) for p, st in pst.items()),
                  {k: quote.get(k) for k in ("tr_5y", "spx_tr_5y", "inst_pct", "short_pct")})
    prev = db.q1("SELECT id, input_hash FROM disc_analysis WHERE company_id=? ORDER BY id DESC LIMIT 1", (cid,))
    if prev is not None and prev["input_hash"] == ihash and not force:
        return {"status": "unchanged", "id": int(prev["id"]), "ai_error": "; ".join(ai_errors), "ai_state": ai_state}
    rep = used.get("bericht") or {}
    crit = (used.get("disruption") or {}).get("kriterien")
    ncrit = (used.get("nash") or {}).get("kriterien")
    market = V.market_metrics(fu, quote)
    scores = {
        "disruption": S.disruption(fu, crit), "quality": S.quality(fu, crit), "garp": S.garp(fu, crit, market),
        "nash125": NASH.nash125(fu, ncrit, market), "fundamental_v1": S.fundamental_v1(fu, crit, market),
    }
    data_cov = _data_coverage(fu)
    scen = V.scenarios(fu, market)
    scores["valuation"] = V.valuation_score(fu, market, scen, data_cov)
    for s in SG.fundamental_signals(fu):
        SG.add_signal(db, cid, kind="fundamental", title=s["title"], origin="sec", key=s["key"],
                      detail=s["detail"], ref=s["accn"], strength_=0.5, published_at=s["period"])
    sig_rows = db.q("SELECT kind, title, detail, origin, ref, url, source_name, n_sources, direction, strength, "
                    "published_at, observed_at FROM disc_signal WHERE company_id=? ORDER BY observed_at DESC LIMIT 30",
                    (cid,))
    business = rep.get("geschaeftsmodell") or {}
    if not business.get("was") and info.get("longBusinessSummary"):
        business = {"was": _sentences(str(info["longBusinessSummary"]), 1200), "belege": ["Y"], "origin": "reported",
                    "hinweis": "Unternehmensbeschreibung des Datenanbieters (keine KI-Analyse verfügbar)"}
    sc_dicts = {k: v.to_dict() for k, v in scores.items()}
    last_meta = next((st for st in pst.values() if st.get("ok_id")), {})
    ai_meta = {"prompt_version": AI.PROMPT_VERSION, "provider": last_meta.get("provider"),
               "model": last_meta.get("model"), "error": "; ".join(ai_errors), "state": ai_state}
    result: dict[str, Any] = {
        "company": {k: co.get(k) for k in ("id", "cik", "ticker", "tickers", "exchange", "name", "sector", "industry",
                                           "country", "sic")},
        "as_of": as_of, "trigger": trigger, "mode": mode, "calc_version": S.CALC_VERSION, "ai_meta": ai_meta,
        "ai_state": ai_state, "ai_parts": pst, "ai_fps": fps if as_of is None else {}, "errors": errors,
        "fundamentals": fu.to_dict(), "market": market,
        "scenarios": scen, "scores": sc_dicts, "nash_method": NASH.METHOD,
        "nash_band": NASH.band(sc_dicts["nash125"]),
        "business": business, "thesis": rep.get("these"), "counter": rep.get("gegenhypothese"),
        "core_risk": rep.get("kernrisiko") or "",
        "uncertainties": rep.get("unsicherheiten") or [], "ai_check": {p: (u or {}).get("pruefung")
                                                                       for p, u in used.items()},
        "milestones": rep.get("meilensteine") or [],
        "signals": [dict(r) for r in sig_rows],
        "sources": [{k: v for k, v in s.items() if k != "text"} | {"excerpt": str(s.get("text", ""))[:400]}
                    for s in sources],
        "data_coverage": data_cov,
    }
    conf = _confidence(scores, used)
    new_ms = rep.get("meilensteine") or []
    if used.get("bericht") is not None:
        for r in db.q("SELECT parts_json FROM disc_analysis WHERE company_id=? AND parts_json IS NOT NULL", (cid,)):
            if (json.loads(r["parts_json"]).get("bericht") or {}).get("ok_id") == pst["bericht"]["ok_id"]:
                new_ms = []  # derselbe Berichtsteil wie in einer früheren Version – Meilensteine nicht doppelt
                break
    with db.transaction() as c:
        cur = c.execute(
            "INSERT INTO disc_analysis(company_id, created_at, calc_version, prompt_version, provider, model, trigger, "
            "disruption, quality, garp, nash, valuation, coverage, confidence, input_hash, result_json, nash125, "
            "fundamental_v1, ai_state, parts_json, job_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, now_iso(), S.CALC_VERSION, AI.PROMPT_VERSION if used else None, ai_meta.get("provider"),
             ai_meta.get("model"), trigger, scores["disruption"].total, scores["quality"].total, scores["garp"].total,
             scores["fundamental_v1"].total, scores["valuation"].total, data_cov, conf, ihash,
             json.dumps(result, ensure_ascii=False, default=str), scores["nash125"].total,
             scores["fundamental_v1"].total, ai_state, json.dumps(pst, ensure_ascii=False, default=str),
             getattr(job, "id", None)))
        aid = int(cur.lastrowid)  # type: ignore[arg-type]
        for m in new_ms:
            c.execute("INSERT INTO disc_milestone(company_id, analysis_id, text, kind, metric, op, threshold, due) "
                      "VALUES (?,?,?,?,?,?,?,?)", (cid, aid, m["text"], m["art"], m["kennzahl"] or None,
                                                   m["operator"] or None, m["schwelle"], m["bis"] or None))
    check_milestones(db, cid, fu)
    return {"status": "ok", "id": aid, "ai_error": "; ".join(ai_errors), "ai_state": ai_state}


def _sentences(text: str, limit: int) -> str:
    """Auf ganze Sätze kürzen (statt mitten im Wort abzuschneiden)."""
    t = " ".join(text.split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + " …").strip()


def normalize(res: dict[str, Any]) -> dict[str, Any]:
    """Ältere Berichte (bis 0.108) lesbar machen – ohne Neuberechnung: Die bisherige 125-Punkte-Methode heißt jetzt
    „Portfolia Fundamental v1“; ein KI-Gesamtteil wird als Teilzustand ausgewiesen."""
    sc = res.get("scores") or {}
    if "nash" in sc and "fundamental_v1" not in sc:
        old = sc.pop("nash")
        sc["fundamental_v1"] = {**old, "key": "fundamental_v1", "label": S.FUNDAMENTAL_V1_LABEL,
                                "legacy_label": old.get("label")}
    if "ai_state" not in res:
        meta = res.get("ai_meta") or {}
        res["ai_state"] = "historisch" if res.get("as_of") else ("vollstaendig" if res.get("thesis") and
                                                                  not meta.get("error") else
                                                                  "fehlt" if not meta.get("provider") else "teilweise")
        res["legacy_ai"] = True
    return res


def _data_coverage(fu: F.Fundamentals) -> float:
    """Anteil vorhandener Werte der Kernkennzahlen über die verfügbaren Jahre (höchstens 5)."""
    keys = ("revenue", "gross_profit", "operating_income", "net_income", "ocf", "capex", "cash", "equity",
            "shares_diluted")
    ys = fu.years[-5:]
    if not ys:
        return 0.0
    have = sum(1 for k in keys for y in ys if fu.get(k, y).v is not None)
    return round(have / (len(keys) * 5), 3)


def _confidence(scores: dict[str, S.Score], used: dict[str, Any]) -> float:
    covs = [s.coverage for s in scores.values()]
    ai_conf = [float(v.get("konfidenz", 0)) for part in ("disruption", "nash")
               for v in ((used.get(part) or {}).get("kriterien") or {}).values()]
    base = sum(covs) / len(covs) if covs else 0.0
    return round(base * (0.5 + 0.5 * (sum(ai_conf) / len(ai_conf))) if ai_conf else base * 0.5, 3)


def check_milestones(db: Any, cid: int, fu: F.Fundamentals) -> None:
    """Maschinell prüfbare Meilensteine gegen die jüngsten Kennzahlen: erfüllt sofort; verfehlt nach Fälligkeit."""
    today = today_local().isoformat()[:7]
    for m in db.q("SELECT * FROM disc_milestone WHERE company_id=? AND status='offen' AND metric IS NOT NULL", (cid,)):
        v = fu.latest(m["metric"]).v
        if v is None or m["threshold"] is None:
            continue
        hit = v >= m["threshold"] if m["op"] == ">=" else v <= m["threshold"]
        if hit:
            db.x("UPDATE disc_milestone SET status='erfuellt', checked_at=? WHERE id=?", (now_iso(), m["id"]))
        elif m["due"] and m["due"] < today:
            db.x("UPDATE disc_milestone SET status='verfehlt', checked_at=? WHERE id=?", (now_iso(), m["id"]))


def analyses(db: Any, cid: int) -> list[dict[str, Any]]:
    return [dict(r) for r in db.q("SELECT id, created_at, trigger, provider, model, disruption, quality, garp, nash, "
                                  "nash125, fundamental_v1, valuation, coverage, confidence, ai_state, job_id "
                                  "FROM disc_analysis WHERE company_id=? ORDER BY id DESC", (cid,))]


def analysis(db: Any, cid: int, aid: int | None = None) -> dict[str, Any] | None:
    r = db.q1("SELECT * FROM disc_analysis WHERE company_id=? AND (? IS NULL OR id=?) ORDER BY id DESC LIMIT 1",
              (cid, aid, aid))
    if r is None:
        return None
    d = dict(r)
    d["result"] = normalize(json.loads(r["result_json"]))
    return d


def compare(db: Any, cid: int, a_id: int, b_id: int) -> dict[str, Any] | None:
    """Vergleich zweier Analyseversionen (a = älter, b = neuer)."""
    a, b = analysis(db, cid, a_id), analysis(db, cid, b_id)
    if a is None or b is None:
        return None
    if a["id"] > b["id"]:
        a, b = b, a
    ra, rb = a["result"], b["result"]
    scores = []
    for k, sb in rb["scores"].items():
        sa = ra["scores"].get(k) or {"total": None, "criteria": []}
        crit = []
        pa = {c["key"]: c.get("points") for c in sa.get("criteria", [])}
        for c in sb["criteria"]:
            if pa.get(c["key"]) != c.get("points"):
                crit.append({"label": c["label"], "max": c["max"], "a": pa.get(c["key"]), "b": c.get("points"),
                             "reason": c.get("reason", "")})
        scores.append({"key": k, "label": sb["label"], "a": sa.get("total"), "b": sb["total"], "max": sb["max"],
                       "criteria": crit})
    metrics = []
    fa, fb = ra["fundamentals"], rb["fundamentals"]
    for key in ("revenue", "revenue_growth", "gross_margin", "op_margin", "fcf", "fcf_margin", "net_cash",
                "dilution"):
        la = _latest(fa, key)
        lb = _latest(fb, key)
        if la != lb:
            metrics.append({"key": key, "label": F.LABELS.get(key, key), "a": la, "b": lb, "pct": key in F.PCT})
    corrected = []
    for y in set(fa.get("years", [])) & set(fb.get("years", [])):
        for key in F.CONCEPTS:
            va = ((fa.get("rows") or {}).get(key) or {}).get(y) or {}
            vb = ((fb.get("rows") or {}).get(key) or {}).get(y) or {}
            if va.get("v") is not None and vb.get("v") is not None and abs(va["v"] - vb["v"]) > \
                    max(1.0, abs(va["v"]) * 0.005):
                corrected.append({"label": F.LABELS.get(key, key), "period": y, "a": va["v"], "b": vb["v"],
                                  "filed": vb.get("filed")})
    new_signals = [s for s in rb.get("signals", []) if s["observed_at"] > a["created_at"]]
    risks_a = {p.get("risiko") for p in ((ra.get("counter") or {}).get("punkte") or [])}
    new_risks = [p for p in ((rb.get("counter") or {}).get("punkte") or []) if p.get("risiko") not in risks_a]
    ms = [dict(r) for r in db.q("SELECT text, kind, status, checked_at FROM disc_milestone WHERE analysis_id=? ",
                                (a["id"],))]
    return {"a": a, "b": b, "scores": scores, "metrics": metrics, "corrected": corrected, "signals": new_signals,
            "new_risks": new_risks, "milestones": ms,
            "price": ((ra.get("market") or {}).get("price"), (rb.get("market") or {}).get("price"))}


def _latest(fd: dict[str, Any], key: str) -> float | None:
    rows = (fd.get("rows") or {}).get(key) or {}
    for y in reversed(fd.get("years") or []):
        v = (rows.get(y) or {}).get("v")
        if v is not None:
            return float(v)
    return None


# ------------------------------------------------------------------------------------------------------------
# Radar
# ------------------------------------------------------------------------------------------------------------

def _cats(db: Any, r: Any, res: dict[str, Any], held: bool, since_new: str) -> set[str]:
    sc = res.get("scores") or {}
    cats = set()
    if r["created_at"] >= since_new and r["status"] == "neu" and r["origin"] != "manuell":
        cats.add("neu")
    dis, qual, garp = sc.get("disruption") or {}, sc.get("quality") or {}, sc.get("garp") or {}
    if dis.get("complete") and (dis.get("total") or 0) >= 55:
        cats.add("disruptiv")
    half_year = (datetime.now(UTC) - timedelta(days=180)).isoformat()
    kinds = {x["kind"] for x in db.q("SELECT DISTINCT kind FROM disc_signal WHERE company_id=? AND observed_at>=? "
                                     "AND COALESCE(direction, '') != 'negativ'", (r["id"], half_year))}
    if kinds & {"wendepunkt", "fundamental"}:
        cats.add("wendepunkt")
    if "fehlbewertung" in kinds:
        cats.add("fehlbewertung")
    if qual.get("complete") and (qual.get("total") or 0) >= 70:
        cats.add("quality")
    if garp.get("complete") and (garp.get("total") or 0) >= 65:
        cats.add("garp")
    band = res.get("nash_band") or {}
    if band.get("key") == "exceptional" and band.get("sure"):
        cats.add("nash100")  # nur gesichert ≥ 100 Punkte (fehlende Kriterien können nur Punkte hinzufügen)
    if r["status"] == "beobachten":
        cats.add("beobachten")
    if held:
        cats.add("portfolio")
    return cats


def radar(ctx: Any, *, cat: str = "", sort: str = "disruption", q: str = "", show_all: bool = False
          ) -> dict[str, Any]:
    db = ctx.db
    hs = FIT.holdings(ctx)
    rows = db.q("SELECT c.*, a.id AS aid, a.created_at AS analysed_at, a.coverage, a.confidence, a.ai_state, "
                "a.result_json FROM disc_company c LEFT JOIN disc_analysis a "
                "ON a.id = (SELECT MAX(id) FROM disc_analysis WHERE company_id = c.id)")
    cards = []
    since_new = (datetime.now(UTC) - timedelta(days=14)).isoformat()
    for r in rows:
        res = normalize(json.loads(r["result_json"])) if r["result_json"] else {}
        co = company(db, int(r["id"])) or dict(r)
        held = bool(FIT.held_match(co, hs))
        sig = db.q("SELECT title, kind, strength, observed_at, n_sources FROM disc_signal WHERE company_id=? "
                   "ORDER BY strength DESC, observed_at DESC LIMIT 1", (r["id"],))
        last_sig = db.scalar("SELECT MAX(COALESCE(published_at, observed_at)) FROM disc_signal WHERE company_id=?",
                             (r["id"],))
        prio = db.scalar("SELECT MAX(priority) FROM disc_candidate WHERE company_id=?", (r["id"],))
        sc = res.get("scores") or {}
        ccy = (res.get("market") or {}).get("ccy")
        counter = res.get("core_risk") or (((res.get("counter") or {}).get("punkte") or [{}])[0].get("risiko")
                                           if res.get("counter") else None)
        if not counter:
            counter = next((c["counter"][0] for s in sc.values() for c in s.get("criteria", []) if c.get("counter")),
                           None)
        biz = (res.get("business") or {}).get("was") or ""
        cards.append({
            "id": r["id"], "ticker": r["ticker"], "name": r["name"], "sector": r["sector"] or r["industry"],
            "status": r["status"], "origin": r["origin"], "held": held,
            "cats": sorted(_cats(db, r, res, held, since_new)), "analysed_at": r["analysed_at"],
            "business": _sentences(biz, 220) if biz else "",
            "scores": {k: {"total": v.get("total"), "max": v.get("max"), "complete": v.get("complete")}
                       for k, v in sc.items()},
            "nash_band": res.get("nash_band"), "ai_state": r["ai_state"] or res.get("ai_state"),
            "signal": dict(sig[0]) if sig else None, "last_signal": last_sig, "counter": counter,
            "coverage": r["coverage"], "fit": None, "created_at": r["created_at"], "ccy": ccy, "priority": prio,
        })
    if cat:
        cards = [c for c in cards if cat in c["cats"]]
    elif not show_all:
        cards = [c for c in cards if c["status"] not in ("verworfen", "zurueckgestellt") and not c["held"]]
    if q:
        ql = q.lower()
        cards = [c for c in cards if ql in c["name"].lower() or ql in c["ticker"].lower()]
    if sort == "fit":
        for c in cards:
            fco = company(db, int(c["id"]))
            if fco is not None and c["analysed_at"]:
                fco["ccy"] = c["ccy"]
                sim = FIT.simulate(ctx, fco)
                c["fit"] = FIT.score(ctx, fco, sim, (None, 0)).total
    key = {"signal": lambda c: c["last_signal"] or "", "fit": lambda c: c["fit"] if c["fit"] is not None else -1,
           "prioritaet": lambda c: c["priority"] if c["priority"] is not None else -1}
    if sort in key:
        cards.sort(key=key[sort], reverse=True)
    else:
        s = sort if sort in SORTS else "disruption"
        # vollständige Bewertungen vor unvollständigen (ein Teil-Score ist kein vergleichbarer Rang)
        cards.sort(key=lambda c: ((c["scores"].get(s) or {}).get("total") is not None,
                                  bool((c["scores"].get(s) or {}).get("complete")),
                                  (c["scores"].get(s) or {}).get("total") or 0, c["name"]), reverse=True)
    last_run = db.q1("SELECT * FROM disc_run ORDER BY id DESC LIMIT 1")
    return {"cards": cards, "last_run": dict(last_run) if last_run else None}


def category_counts(cards: list[dict[str, Any]]) -> dict[str, int]:
    out = dict.fromkeys(CATEGORIES, 0)
    for c in cards:
        for k in c["cats"]:
            out[k] += 1
    return out


def detail_context(ctx: Any, cid: int, aid: int | None = None, amount: float = FIT.DEFAULT_AMOUNT
                   ) -> dict[str, Any] | None:
    from app.discovery import jobs as JOBS

    db = ctx.db
    co = company(db, cid)
    if co is None:
        return None
    an = analysis(db, cid, aid)
    res = an["result"] if an else None
    fitd = None
    if res is not None:
        co_fit = {**co, "ccy": (res.get("market") or {}).get("ccy")}
        sim = FIT.simulate(ctx, co_fit, amount)
        corr = FIT.correlation(ctx, f"yahoo:{co.get('yahoo_symbol') or co['ticker']}")
        fitd = {"sim": sim, "score": FIT.score(ctx, co_fit, sim, corr).to_dict(), "corr": corr}
    ms = [dict(r) for r in db.q("SELECT * FROM disc_milestone WHERE company_id=? ORDER BY id DESC LIMIT 30", (cid,))]
    signals = [dict(r) for r in db.q("SELECT * FROM disc_signal WHERE company_id=? ORDER BY COALESCE(published_at, "
                                     "observed_at) DESC LIMIT 40", (cid,))]
    cands = [dict(r) for r in db.q("SELECT * FROM disc_candidate WHERE company_id=? ORDER BY id DESC LIMIT 10",
                                   (cid,))]
    JOBS.expire_stale(ctx)
    return {"co": co, "an": an, "res": res, "versions": analyses(db, cid), "fit": fitd, "milestones": ms,
            "signals": signals, "amount": amount, "today": today_local().isoformat(), "candidates": cands,
            "parts": parts_state(db, cid, _current_fps(res) if res else None),
            "jobs": JOBS.recent(db, 6, company_id=cid), "active_jobs": JOBS.active(db, cid),
            "calls": JOBS.calls(db, company_id=cid, limit=12), "modes": MODES, "ai_states": AI_STATES}


def _current_fps(res: dict[str, Any]) -> dict[str, str] | None:
    """Eingabe-Fingerabdrücke der angezeigten Version (erkennt KI-Teile, die zu älteren Daten gehören)."""
    return res.get("ai_fps") or None
