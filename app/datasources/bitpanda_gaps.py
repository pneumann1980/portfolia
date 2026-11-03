"""Bitpanda: Hat die Public API Lücken? – Prüfung **nur lesend**, ohne Buchung, auf Basis des gespeicherten Vollabrufs
der Quellenübernahme.

* **Saldoverlauf:** ``asset_balance_after`` je Vorgang und Wallet. Springt der Saldo stärker, als die gelieferten
  Vorgänge erklären, fehlt dort eine Bewegung (``bitpanda.chain_gaps``) – der dauerhafte, unabhängige Beleg.
* **Import ↔ API:** Beginn und Ende beider Historien, Buchungen ohne Gegenstück auf jeder Seite (gruppiert nach Asset,
  Art und Jahr und als Einzelzeilen) sowie Sparplan-Einzahlungen, deren Gutschrift die API Tage nach dem Datum des
  Imports führt (``takeover.lag_pairs``).
* **Urteil:** „vollständig“, wenn der Saldoverlauf lückenlos ist und der gemeldete Bestand erreicht wird.

0.84.0 enthielt zusätzlich eine Gegenprüfung mit der alten API (``api.bitpanda.com``). Sie hat gezeigt, dass die neue
API vollständig ist (Unterschiede: Zeitversatz der Sparplan-Einzahlungen, abweichendes Kürzel, Staking-Umbuchung) und
wurde in 0.85.0 entfernt; die alte API wird laut Bitpanda am 31.01.2027 abgeschaltet.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.util.timeutil import iso, parse_iso

ZERO = Decimal(0)
MAX_ROWS = 2000  # Einzelzeilen je Abschnitt im CSV-Bericht


def _fmt(q: Decimal) -> str:
    s = format(q.normalize(), "f")
    return "0" if s in ("-0", "") else s


def _iso(ts: datetime | None) -> str:
    return (iso(ts) or "") if ts else ""


def _year(ts: datetime | None) -> str:
    return str(ts.year) if ts else "?"


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return parse_iso(str(v)) if v else None


def _staged(ctx: Any, sid: int) -> Any:
    from app.datasources import takeover as TK

    row = TK.view(ctx.db, sid)
    if row is None or row["status"] != "staged":
        return None
    return TK.assess(ctx, sid, plan=False)


_Item = tuple[str, str, datetime | None, Decimal, bool]  # Asset, Art, Zeit, Menge, füllt Lücke


def _groups(items: list[_Item]) -> list[dict[str, Any]]:
    """Einzelposten → Gruppen je Asset, Art und Jahr mit Anzahl und Summe."""
    acc: dict[tuple[str, str, str], dict[str, Any]] = {}
    for asset, kind, ts, q, filler in items:
        g = acc.setdefault((asset, kind, _year(ts)), {"asset": asset, "kind": kind, "year": _year(ts), "count": 0,
                                                     "sum": ZERO, "fillers": 0})
        g["count"] += 1
        g["sum"] += q
        g["fillers"] += int(filler)
    return sorted(acc.values(), key=lambda g: (g["asset"], g["year"], -abs(g["sum"])))


def _rows(items: list[_Item]) -> list[dict[str, Any]]:
    return [{"ts": ts, "asset": asset, "kind": kind, "qty": q, "filler": filler}
            for asset, kind, ts, q, filler in sorted(items, key=lambda x: (x[2] or datetime.min.replace(tzinfo=UTC),
                                                                          x[0]))]


def analyse(ctx: Any, sid: int) -> dict[str, Any] | None:
    """Befund aus dem gespeicherten Vollabruf; ``None`` ohne gespeicherten Vollabruf."""
    from app.datasources import takeover as TK

    a = _staged(ctx, sid)
    if a is None:
        return None
    fill_ids = {t.tx_id for t, _g in a.fillers}
    old = list(a.unmatched_old) + [t for t, _g in a.fillers if t not in a.unmatched_old]
    imp: list[_Item] = []
    for t in old:
        kind = t.type + (f" · {t.tag}" if t.tag else "")
        for asset, q in TK._tx_legs(t, a.account).items():
            imp.append((asset, kind, _ts(t.ts), q, t.tx_id in fill_ids))
    api: list[_Item] = []
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
        adj = a.adjustable.get(aid) or {}
        assets.append({"asset": aid, "name": b.get("name"), "after": b.get("after"), "observed": b.get("observed"),
                       "gaps": [{"from": g.get("from"), "to": g.get("to"), "amount": g["amount"],
                                 "left": g.get("left", g["amount"]), "window": TK._window(g)} for g in gaps],
                       "gaps_total": sum((g["amount"] for g in gaps), ZERO), "filled": filled.get(aid, ZERO),
                       "rest": adj.get("need"),
                       "state": ("lückenlos" if not gaps and not adj else "Lücken belegt" if gaps else
                                 "Abweichung ohne Beleg")})
    # Zeitraum des Imports (Buchungen des Kontos, die die Übernahme ersetzen würde) und der API
    imported: list[datetime] = [x for x in [_ts(t.ts) for t in a.hide]
                                + [_ts(t.ts) for t, _w in a.kept if t.tx_id in fill_ids] if x]
    imp_last = max(imported, default=None)
    api_ts = [x for x in (_ts(ln.rec.ts) for ln in a.lines_by_ext.values()) if x]
    after = sum(1 for x in api_ts if imp_last and x > imp_last)
    lagged = [{"import_ts": _ts(t.ts), "api_ts": _ts(ln.rec.ts), "asset": next(iter(ln.legs)),
               "qty": next(iter(ln.legs.values())), "lag": TK._days(lag)} for t, ln, lag in a.lagged]
    gap_count = sum(len(x["gaps"]) for x in assets)
    rest = [x["asset"] for x in assets if x["rest"]]

    def kept_rows(txs: list[Any], note: str = "") -> list[dict[str, Any]]:
        return [{"ts": _ts(t.ts), "asset": asset, "kind": t.type, "qty": q, "tx": t.tx_id, "note": note}
                for t in txs for asset, q in TK._tx_legs(t, a.account).items()]

    kept = {"covered": a.covered, "lagged": len(a.lagged_kept),
            "free": kept_rows(a.kept_free), "app": kept_rows(a.app_free),
            "dups": [r for d in a.dups for r in kept_rows([d["tx"]], d["why"] or ("ausblendbar" if d["hideable"]
                                                                                     else ""))]}
    off = sorted({b["asset"] for b in a.balances if not b["ok"]})
    if not gap_count and off:
        verdict = ("warn", "API vollständig (Saldoverlauf lückenlos) – die Abweichung bei " + ", ".join(off)
                   + " stammt aus Buchungen, die bei der Übernahme stehen bleiben bzw. doppelt zählen (unten).")
    elif not gap_count and not rest:
        verdict = ("good", "API vollständig: Der Saldoverlauf ist lückenlos und ergibt den gemeldeten Bestand.")
    elif gap_count and not rest:
        verdict = ("warn", f"Die API lässt {gap_count} Bewegung(en) aus (Saldoverlauf belegt sie); der Import füllt "
                           "sie vollständig.")
    else:
        verdict = ("crit", f"Die API lässt Bewegungen aus; für {', '.join(rest)} bleibt ein Rest ohne Beleg.")
    return {"account": a.account, "fetched_at": a.fetched_at, "first": a.first, "last": a.last,
            "window_from": a.window_from, "before": len(a.before),
            "before_first": min((t.ts for t in a.before), default=None), "events": a.events,
            "import_first": min(imported, default=None), "import_last": imp_last, "after_import": after,
            "verdict": verdict, "assets": assets, "gap_count": gap_count,
            "import_only": _groups(imp), "import_only_n": len(old), "fillers_n": len(fill_ids),
            "import_rows": _rows(imp), "api_only": _groups(api), "api_only_n": len(a.unmatched_new),
            "api_rows": _rows(api), "lagged": lagged, "lag_range": a.lag_range, "kept": kept,
            "causes": a.causes}


def csv_rows(rep: dict[str, Any] | None) -> list[list[str]]:
    """Bericht zum Weitergeben: nur Datum, Asset, Art, Mengen – keine Kennungen, keine Schlüssel."""
    rows: list[list[str]] = [["Abschnitt", "Datum", "Asset", "Art", "Menge", "Hinweis"]]
    if not rep:
        return rows
    rows.append(["Urteil", "", "", "", "", rep["verdict"][1]])
    rows.append(["API", rep["first"] or "", "", "Historie", str(rep["events"]),
                 f"bis {rep['last'] or '?'}; Import davor: {rep['before']} Buchungen"])
    if rep["import_last"]:
        rows.append(["Import", _iso(rep["import_first"]), "", "Historie", "",
                     f"bis {_iso(rep['import_last'])}; danach {rep['after_import']} Vorgänge nur in der API"])
    for x in rep["assets"]:
        for g in x["gaps"]:
            rows.append(["Lücke laut Saldo", "", x["asset"], g["window"], _fmt(g["amount"]),
                         f"offen nach Import: {_fmt(g['left'])}"])
    for p in rep["lagged"]:
        rows.append(["Sparplan mit Versatz", _iso(p["import_ts"]), p["asset"], "deposit",
                     _fmt(p["qty"]), f"API: {_iso(p['api_ts']) or '?'} (+{p['lag']} Tage)"])
    for section, items in (("bleibt, nicht in der API", rep["kept"]["free"] + rep["kept"]["app"]),
                           ("bleibt, auch in der API (doppelt)", rep["kept"]["dups"])):
        for r in items[:MAX_ROWS]:
            rows.append([section, _iso(r["ts"]), r["asset"], r["kind"], _fmt(r["qty"]), r["note"]])
    for section, items in (("Import ohne API", rep["import_rows"]), ("API ohne Import", rep["api_rows"])):
        for r in items[:MAX_ROWS]:
            rows.append([section, _iso(r["ts"]), r["asset"], r["kind"], _fmt(r["qty"]),
                         "füllt Lücke der API" if r["filler"] else ""])
    return rows
