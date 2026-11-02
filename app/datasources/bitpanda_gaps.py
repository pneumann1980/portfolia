"""Bitpanda: Hat die Public API Lücken? – Prüfung **nur lesend**, ohne Buchung.

Zwei unabhängige Belege, beide auf Basis des gespeicherten Vollabrufs der Quellenübernahme:

1. **Saldoverlauf** (ohne weiteren Abruf): ``asset_balance_after`` je Vorgang und Wallet. Springt der Saldo stärker,
   als die gelieferten Vorgänge erklären, fehlt dort eine Bewegung (``bitpanda.chain_gaps``). Dazu: Beginn der
   Historie der API gegenüber dem Import und die Import-Buchungen ohne Gegenstück, gruppiert nach Asset, Art und Jahr.
2. **Gegenprüfung mit der alten API** (``api.bitpanda.com``, laut Bitpanda bis 31.01.2027): Fiat- und
   Krypto-Transaktionen der alten Schnittstelle werden gegen die Vorgänge der neuen API gelegt (Asset, Richtung,
   Betrag mit/ohne Gebühr, Zeit ±36 h). Was nur die alte API kennt, fehlt in ``/operations``. Interne Umbuchungen
   (Gegenbuchung im selben Asset) heben sich auf und zählen nicht.

Grenzen: Die alte API ist laut Doku nicht für alle Produkte gleich (Aktien, Metalle, Indizes nur teilweise); geprüft
werden Fiat- und Krypto-Transaktionen mit Status ``finished``. Ob ein Schlüssel der neuen API für die alte gilt, ist
nicht dokumentiert – lehnt sie ihn ab, kann für die Prüfung einmalig ein eigener Schlüssel eingegeben werden (wird
nicht gespeichert). Die Zuordnung über Betrag und Zeit ist heuristisch; das Ergebnis ist ein Befund, keine Buchung.
"""

from __future__ import annotations

import bisect
import logging
import threading
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app import __version__
from app.datasources import bitpanda as B
from app.datasources import connector as K
from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

LEGACY_URL = "https://api.bitpanda.com/v1"
LEGACY_UNTIL = date(2027, 1, 31)  # laut „Migrating from the legacy API“
LEGACY_PAGE = 100
LEGACY_MAX_PAGES = 400
MATCH_WINDOW = timedelta(hours=36)
PAIR_WINDOW = timedelta(minutes=5)
MAX_STORED = 400  # Einzelzeilen im gespeicherten Ergebnis (Gruppen sind immer vollständig)
ZERO = Decimal(0)
FIAT_TOL = Decimal("0.01")

_LOCK = threading.Lock()
_RUNNING: set[int] = set()


def state_key(sid: int) -> str:
    return f"bp_gapcheck:{sid}"


def _sym(aid: Any) -> str:
    return str(aid or "").split("@", 1)[0].upper()


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _tol(sym: str, q: Decimal) -> Decimal:
    from app.importer import contract as C

    return FIAT_TOL if sym in C.ISO_CURRENCIES else max(Decimal("0.00000001"), abs(q) * Decimal("0.000000001"))


def _fmt(q: Decimal) -> str:
    s = format(q.normalize(), "f")
    return "0" if s in ("-0", "") else s


def _year(ts: datetime | None) -> str:
    return str(ts.year) if ts else "?"


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return parse_iso(str(v)) if v else None


# ----------------------------------------------------------------------------------------------------
# 1 · Saldoverlauf und Import (gespeicherter Vollabruf)
# ----------------------------------------------------------------------------------------------------

def _staged(ctx: Any, sid: int) -> Any:
    from app.datasources import takeover as TK

    row = TK.view(ctx.db, sid)
    if row is None or row["status"] != "staged":
        return None
    return TK.assess(ctx, sid, plan=False)


def _groups(items: list[tuple[str, str, datetime | None, Decimal, bool]]) -> list[dict[str, Any]]:
    """(Asset, Art, Zeit, Menge, füllt Lücke) → Gruppen je Asset, Art und Jahr mit Anzahl und Summe."""
    acc: dict[tuple[str, str, str], dict[str, Any]] = {}
    for asset, kind, ts, q, filler in items:
        g = acc.setdefault((asset, kind, _year(ts)), {"asset": asset, "kind": kind, "year": _year(ts), "count": 0,
                                                     "sum": ZERO, "fillers": 0, "first": ts, "last": ts})
        g["count"] += 1
        g["sum"] += q
        g["fillers"] += int(filler)
        if ts and (g["first"] is None or ts < g["first"]):
            g["first"] = ts
        if ts and (g["last"] is None or ts > g["last"]):
            g["last"] = ts
    return sorted(acc.values(), key=lambda g: (g["asset"], g["year"], -abs(g["sum"])))


def analyse(ctx: Any, sid: int) -> dict[str, Any] | None:
    """Befund aus dem gespeicherten Vollabruf: Lücken laut Saldoverlauf je Asset, Beginn der Historie, Import-Buchungen
    ohne Gegenstück und API-Vorgänge ohne Gegenstück (gruppiert). ``None`` ohne gespeicherten Vollabruf."""
    from app.datasources import takeover as TK

    a = _staged(ctx, sid)
    if a is None:
        return None
    fill_ids = {t.tx_id for t, _g in a.fillers}
    old = list(a.unmatched_old) + [t for t, _g in a.fillers if t not in a.unmatched_old]
    imp: list[tuple[str, str, datetime | None, Decimal, bool]] = []
    for t in old:
        kind = t.type + (f" · {t.tag}" if t.tag else "")
        for asset, q in TK._tx_legs(t, a.account).items():
            imp.append((asset, kind, _ts(t.ts), q, t.tx_id in fill_ids))
    api: list[tuple[str, str, datetime | None, Decimal, bool]] = []
    for ln in a.unmatched_new:
        for asset, q in ln.legs.items():
            api.append((asset, str(ln.rec.label or ln.rec.kind), _ts(ln.rec.ts), q, False))
    filled: dict[str, Decimal] = defaultdict(Decimal)
    for t, _g in a.fillers:
        for asset, q in TK._tx_legs(t, a.account).items():
            filled[asset] += q
    assets = []
    for b in a.balances:
        aid = b["asset"]
        gaps = a.gaps.get(aid, [])
        total = sum((g["amount"] for g in gaps), ZERO)
        adj = a.adjustable.get(aid) or {}
        assets.append({"asset": aid, "name": b.get("name"), "after": b.get("after"), "observed": b.get("observed"),
                       "gaps": [{"from": g.get("from"), "to": g.get("to"), "amount": g["amount"],
                                 "left": g.get("left", g["amount"]), "window": TK._window(g)} for g in gaps],
                       "gaps_total": total, "filled": filled.get(aid, ZERO), "rest": adj.get("need"),
                       "state": ("lückenlos" if not gaps and not adj else "Lücken belegt" if gaps else
                                 "Abweichung ohne Beleg")})
    ib = list(a.before)
    return {"account": a.account, "fetched_at": a.fetched_at, "first": a.first, "last": a.last,
            "window_from": a.window_from, "before": len(ib),
            "before_first": min((t.ts for t in ib), default=None), "events": a.events,
            "assets": assets, "with_gaps": [x for x in assets if x["gaps"]],
            "gap_count": sum(len(x["gaps"]) for x in assets),
            "import_only": _groups(imp), "import_only_n": len(old), "fillers_n": len(fill_ids),
            "api_only": _groups(api), "api_only_n": len(a.unmatched_new)}


# ----------------------------------------------------------------------------------------------------
# 2 · Gegenprüfung mit der alten API
# ----------------------------------------------------------------------------------------------------

def legacy_available(today: date | None = None) -> bool:
    return (today or datetime.now(UTC).date()) <= LEGACY_UNTIL


def _pages(api: Any, path: str, what: str, progress: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Cursor-Seiten der alten API (``meta.next_cursor``); Ende ohne Folgecursor, bei leerer Seite oder wiederholtem
    Cursor. ``total_count`` (falls geliefert) dient als Vollständigkeitsbeleg."""
    out: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    cursor: str | None = None
    size: int | None = LEGACY_PAGE
    total = None
    capped = False
    for page in range(LEGACY_MAX_PAGES):
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if size:
            params["page_size"] = size
        try:
            body = api.get(path, params or None, what)
        except K.ConnectorError as e:
            if page == 0 and size and "HTTP 400" in e.message:  # Seitengröße nicht akzeptiert → Standard
                size = None
                continue
            raise
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise K.ConnectorError("data", f"Antwort von {what} ohne Liste „data“.")
        meta = body["meta"] if isinstance(body.get("meta"), dict) else {}
        if isinstance(meta.get("total_count"), int):
            total = meta["total_count"]
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                out.setdefault(str(item["id"]), item)
        progress(f"{what}: {len(out)}" + (f" von {total}" if total else ""))
        nxt = meta.get("next_cursor")
        nxt = nxt if isinstance(nxt, str) and nxt.strip() else None
        if not data or nxt is None or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    else:
        capped = True
    complete = not capped and (total is None or len(out) >= total)
    return list(out.values()), {"count": len(out), "total": total, "complete": complete}


def _ids(items: list[dict[str, Any]], key: str, sym: str) -> dict[str, str]:
    out = {}
    for w in items:
        at: dict[str, Any] = w["attributes"] if isinstance(w.get("attributes"), dict) else {}
        if at.get(key) is not None and at.get(sym):
            out[str(at[key])] = str(at[sym]).upper()
    return out


def normalize(items: list[dict[str, Any]], kind: str, names: dict[str, str]) -> list[dict[str, Any]]:
    """Transaktionen der alten API → einheitliche Einträge (signierte Menge, Gebühr, Zeit, Art, Status)."""
    out = []
    id_key = "fiat_id" if kind == "fiat" else "cryptocoin_id"
    for it in items:
        at: dict[str, Any] = it["attributes"] if isinstance(it.get("attributes"), dict) else {}
        amount = _dec(at.get("amount"))
        tm: dict[str, Any] = at["time"] if isinstance(at.get("time"), dict) else {}
        ts = _ts(tm.get("date_iso8601"))
        if ts is None and _dec(tm.get("unix")) is not None:
            ts = datetime.fromtimestamp(int(_dec(tm.get("unix")) or 0), UTC)
        direction = str(at.get("in_or_out") or "").lower()
        if amount is None or ts is None or direction not in ("incoming", "outgoing"):
            continue
        ref = str(at.get(id_key) or "")
        sym = names.get(ref) or f"{kind.upper()}#{ref}"
        out.append({"id": str(it.get("id")), "src": kind, "asset": sym, "ts": ts.astimezone(UTC),
                    "qty": amount if direction == "incoming" else -amount, "fee": abs(_dec(at.get("fee")) or ZERO),
                    "type": str(at.get("type") or "?").lower(), "status": str(at.get("status") or "?").lower(),
                    "related": str(at.get("related_wallet_transaction_id") or "") or None,
                    "internal": str(at.get("tx_id") or "").lower() == "internal"})
    return out


def _cancel_internal(entries: list[dict[str, Any]]) -> set[str]:
    """Interne Umbuchungen: verknüpfte Gegenbuchung (``related_wallet_transaction_id``) oder Zu-/Abgang gleichen
    Betrags im selben Asset innerhalb weniger Minuten (Art ``transfer``) – heben sich im Konto auf."""
    by_id = {e["id"]: e for e in entries}
    out: set[str] = set()
    for e in entries:
        r = e.get("related")
        if r and r in by_id and by_id[r]["asset"] == e["asset"] and by_id[r]["qty"] == -e["qty"]:
            out |= {e["id"], r}
    rest = sorted((e for e in entries if e["id"] not in out and e["type"] == "transfer"),
                  key=lambda e: (e["asset"], e["ts"]))
    for i, e in enumerate(rest):
        if e["id"] in out:
            continue
        for f in rest[i + 1:]:
            if f["asset"] != e["asset"] or f["ts"] - e["ts"] > PAIR_WINDOW:
                break
            if f["id"] not in out and f["qty"] == -e["qty"]:
                out |= {e["id"], f["id"]}
                break
    return out


def compare(entries: list[dict[str, Any]], legs: list[tuple[datetime, str, Decimal, Decimal]]
            ) -> tuple[list[dict[str, Any]], int, set[int]]:
    """Einträge der alten API gegen Teile der neuen (Zeit, Asset, Menge mit Gebühr, Menge ohne Gebühr). Jeder Teil
    wird höchstens einmal verwendet; bei mehreren Kandidaten gewinnt der zeitlich nächste. Ergebnis: Einträge ohne
    Gegenstück, Anzahl Treffer, verwendete Teile."""
    by_sym: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for i, (ts, sym, _q, _b) in enumerate(legs):
        by_sym[sym].append((ts, i))
    for v in by_sym.values():
        v.sort()
    used: set[int] = set()
    missing, hits = [], 0
    for e in sorted(entries, key=lambda x: x["ts"]):
        cands = by_sym.get(e["asset"], [])
        lo = bisect.bisect_left(cands, (e["ts"] - MATCH_WINDOW, -1))
        hi = bisect.bisect_right(cands, (e["ts"] + MATCH_WINDOW, len(legs)))
        sign = 1 if e["qty"] > 0 else -1
        wanted = {e["qty"], e["qty"] - sign * e["fee"], e["qty"] + sign * e["fee"]}
        tol = _tol(e["asset"], e["qty"])
        best: tuple[timedelta, int] | None = None
        for ts, i in cands[lo:hi]:
            if i in used:
                continue
            _t, _s, q, base = legs[i]
            if any(abs(q - w) <= tol or abs(base - w) <= tol for w in wanted):
                d = abs(ts - e["ts"])
                if best is None or d < best[0]:
                    best = (d, i)
        if best is None:
            missing.append(e)
        else:
            used.add(best[1])
            hits += 1
    return missing, hits, used


def _api_legs(a: Any) -> list[tuple[datetime, str, Decimal, Decimal]]:
    out = []
    for ln in a.lines_by_ext.values():
        ts = _ts(ln.rec.ts)
        if ts is None:
            continue
        for asset, q in ln.legs.items():
            out.append((ts, _sym(asset), q, ln.base.get(asset, q)))
    return out


def legacy_result(entries: list[dict[str, Any]], a: Any, meta: dict[str, Any]) -> dict[str, Any]:
    """Abgleich und Befund (rein, testbar): fehlende Einträge je Asset/Art/Jahr, Bezug zu den Lücken laut Saldo."""
    finished = [e for e in entries if e["status"] == "finished"]
    internal = _cancel_internal(finished)
    relevant = [e for e in finished if e["id"] not in internal]
    legs = _api_legs(a)
    missing, hits, used = compare(relevant, legs)
    first = parse_iso(a.first) if a.first else None
    groups = _groups([(e["asset"], f"{e['src']} · {e['type']}", e["ts"], e["qty"], False) for e in missing])
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for e in missing:
        sums[e["asset"]] += e["qty"]
    gaps: dict[str, Decimal] = defaultdict(Decimal)
    for aid, gs in a.gaps.items():
        gaps[_sym(aid)] += sum((g["amount"] for g in gs), ZERO)
    syms = {e["asset"] for e in relevant}
    extra = sum(1 for i, (_t, s, _q, _b) in enumerate(legs) if s in syms and i not in used)
    per_asset: list[dict[str, Any]] = []
    for s in sorted(set(sums) | {k for k, v in gaps.items() if v}):
        m, g = sums.get(s, ZERO), gaps.get(s, ZERO)
        tol = _tol(s, g or m)
        per_asset.append({"asset": s, "missing_sum": m, "gap_total": g,
                          "explains": ("vollständig" if g and abs(m - g) <= tol else
                                       "teilweise" if g and m and (m > 0) == (g > 0) else
                                       "keine Lücke laut Saldo" if not g else "nein")})
    rows = [{"ts": iso(e["ts"]), "asset": e["asset"], "src": e["src"], "type": e["type"], "qty": _fmt(e["qty"]),
             "fee": _fmt(e["fee"]), "before_api": bool(first and e["ts"] < first)} for e in missing]
    return {"status": "done", "at": iso(datetime.now(UTC)), "meta": meta,
            "entries": len(entries), "finished": len(finished), "other_status": len(entries) - len(finished),
            "internal": len(internal), "matched": hits, "missing_n": len(missing),
            "before_api": sum(1 for r in rows if r["before_api"]), "api_extra": extra,
            "groups": [{**g, "sum": _fmt(g["sum"]), "first": iso(g["first"]) if g["first"] else None,
                        "last": iso(g["last"]) if g["last"] else None} for g in groups],
            "per_asset": [{**x, "missing_sum": _fmt(x["missing_sum"]), "gap_total": _fmt(x["gap_total"])}
                          for x in per_asset],
            "missing": rows[:MAX_STORED], "truncated": max(0, len(rows) - MAX_STORED)}


def run_legacy(ctx: Any, sid: int, key: str | None = None) -> dict[str, Any]:
    """Alte API vollständig lesen und abgleichen; Ergebnis in ``app_state`` (``state_key``). Der eingegebene Schlüssel
    wird nur für diesen Lauf verwendet und nie gespeichert oder protokolliert."""
    from app.datasources.service import datasource_service
    from app.logging_setup import get_redactor

    db = ctx.db
    k = state_key(sid)

    def progress(text: str) -> None:
        db.set_state(k, {"status": "running", "message": text, "at": iso(datetime.now(UTC))})

    def fail(text: str) -> dict[str, Any]:
        res = {"status": "failed", "message": text, "at": iso(datetime.now(UTC))}
        db.set_state(k, res)
        return res

    a = _staged(ctx, sid)
    if a is None:
        return fail("Kein gespeicherter Vollabruf – zuerst auf der Übernahmeseite „Vollabruf starten“.")
    svc = datasource_service(ctx)
    ds = svc.get(sid)
    if ds is None or ds.provider != "bitpanda":
        return fail("Nur für Bitpanda.")
    try:
        conn = K.connector_for("bitpanda")
        secret = K.Secret(value=key) if key else svc._secret(ds, conn)  # type: ignore[arg-type]
    except K.ConnectorError as e:
        return fail(e.message)
    if not secret.present:
        return fail("Kein API-Key vorhanden.")
    progress("Alte API wird gelesen …")
    try:
        with get_redactor().temporary(secret.values()), httpx.Client(
                base_url=LEGACY_URL, timeout=B.TIMEOUT, follow_redirects=False,
                transport=B.BitpandaConnector.transport,
                headers={"Accept": "application/json",
                         "User-Agent": f"Portfolia/{__version__} (read-only)"}) as client:
            api = B._Api(secret.reveal() or "", client, B.BitpandaConnector.sleep)
            fw, _m = _pages(api, "/fiatwallets", "Fiat-Wallets (alte API)", progress)
            cw, _m = _pages(api, "/wallets", "Krypto-Wallets (alte API)", progress)
            fiat, fm = _pages(api, "/fiatwallets/transactions", "Fiat-Transaktionen (alte API)", progress)
            crypto, cm = _pages(api, "/wallets/transactions", "Krypto-Transaktionen (alte API)", progress)
    except K.ConnectorError as e:
        if e.kind in ("auth", "scope") or "HTTP 401" in e.message or "HTTP 403" in e.message:
            return fail("Die alte API (api.bitpanda.com) lehnt diesen Schlüssel ab. Ob Schlüssel der neuen API dort "
                        "gelten, ist nicht dokumentiert – für die Prüfung kann einmalig ein Schlüssel der alten API "
                        "eingegeben werden (Leserechte; wird nicht gespeichert).")
        return fail(f"Alte API: {e.message}")
    entries = (normalize(fiat, "fiat", _ids(fw, "fiat_id", "fiat_symbol"))
               + normalize(crypto, "crypto", _ids(cw, "cryptocoin_id", "cryptocoin_symbol")))
    res = legacy_result(entries, a, {"fiat": fm, "crypto": cm, "fetched_at": a.fetched_at})
    db.set_state(k, res)
    log.info("Bitpanda-Lückenprüfung %s: %s Einträge, %s ohne Gegenstück, %s intern", sid, res["entries"],
             res["missing_n"], res["internal"])
    return res


def start_legacy(ctx: Any, sid: int, key: str | None = None) -> dict[str, Any]:
    if not legacy_available():
        return {"errors": [f"Die alte API ist laut Bitpanda nur bis {LEGACY_UNTIL:%d.%m.%Y} verfügbar."]}
    with _LOCK:
        if sid in _RUNNING:
            return {"errors": ["Die Gegenprüfung läuft bereits."]}
        _RUNNING.add(sid)
    ctx.db.set_state(state_key(sid), {"status": "running", "message": "Gegenprüfung startet …",
                                      "at": iso(datetime.now(UTC))})

    def work() -> None:
        try:
            run_legacy(ctx, sid, key)
        except Exception as e:  # pragma: no cover - Absicherung: Hintergrundlauf darf nie hängen bleiben
            log.exception("Bitpanda-Lückenprüfung %s fehlgeschlagen", sid)
            ctx.db.set_state(state_key(sid), {"status": "failed", "message": f"Fehler: {type(e).__name__}",
                                              "at": iso(datetime.now(UTC))})
        finally:
            _RUNNING.discard(sid)
            ctx.db.close_thread_conn()

    threading.Thread(target=work, name=f"bp-gapcheck-{sid}", daemon=True).start()
    return {"errors": []}


def legacy_state(db: Any, sid: int) -> dict[str, Any] | None:
    st = db.get_state(state_key(sid))
    if isinstance(st, dict) and st.get("status") == "running" and sid not in _RUNNING:
        return {**st, "status": "failed", "message": "Abgebrochen (Neustart) – bitte erneut starten."}
    return st if isinstance(st, dict) else None


def csv_rows(rep: dict[str, Any] | None, leg: dict[str, Any] | None) -> list[list[str]]:
    """Bericht zum Weitergeben: nur Datum, Asset, Art, Mengen – keine Kennungen, keine Schlüssel."""
    rows: list[list[str]] = [["Abschnitt", "Asset", "Art", "Jahr/Zeitraum", "Anzahl", "Summe", "Hinweis"]]
    if rep:
        rows.append(["API", "", "Historie", f"{rep['first'] or '?'} bis {rep['last'] or '?'}", str(rep["events"]),
                     "", f"Import davor: {rep['before']} Buchungen"])
        for x in rep["assets"]:
            for g in x["gaps"]:
                rows.append(["Lücke laut Saldo", x["asset"], "", g["window"], "1", _fmt(g["amount"]),
                             f"offen nach Import: {_fmt(g['left'])}"])
        for g in rep["import_only"]:
            rows.append(["Import ohne API", g["asset"], g["kind"], g["year"], str(g["count"]), _fmt(g["sum"]),
                         f"davon füllen Lücke: {g['fillers']}" if g["fillers"] else ""])
        for g in rep["api_only"]:
            rows.append(["API ohne Import", g["asset"], g["kind"], g["year"], str(g["count"]), _fmt(g["sum"]), ""])
    if leg and leg.get("status") == "done":
        for g in leg["groups"]:
            rows.append(["nur alte API", g["asset"], g["kind"], g["year"], str(g["count"]), g["sum"], ""])
        for x in leg["per_asset"]:
            rows.append(["alte API vs. Saldo", x["asset"], "", "", "", x["missing_sum"],
                         f"Lücke laut Saldo {x['gap_total']} – erklärt: {x['explains']}"])
    return rows
