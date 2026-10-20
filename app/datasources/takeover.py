"""Quellenübernahme: Die API einer Börse wird für ein Konto maßgeblich, der kuratierte Import (Koinly) tritt zurück.

Pilot: Bitpanda. Die Public API liefert die Historie seitenweise bis ``has_next_page=false`` und den aktuellen Bestand
(``/portfolio``). Daraus lässt sich – anders als bei Börsen mit Lücken im Abruf (Binance: Earn, Futures, Margin …) –
prüfen, ob die API-Historie das Konto vollständig erklärt. Eine Übernahme ersetzt deshalb nicht Zeile für Zeile, sondern
ist **eine Entscheidung je Konto** mit Beweis, Vorschau und Rückgängig.

Ablauf
    1. **Vollabruf** (nur lesend, :func:`run_fetch`): alle Vorgänge ab Beginn der Historie, Abdeckung und beobachtete
       Bestände. Kein Abrufstand, kein Prüf-Stapel, keine Buchung – die Ergebnisse liegen in ``ds_takeover``.
    2. **Beweis** (:func:`assess`, rein lesend): Der Abruf ist vollständig (Seitenende laut API, keine Lücke, aktuelle
       Auswertung), alle Vorgänge sind buchbar und zugeordnet, und – das Kriterium – die Vorgänge der API **ergeben den
       beobachteten Bestand je Asset exakt**. Die Bestandswirkung wird aus denselben Zeilen berechnet, die der Import
       bucht (``CsvImportService._build``), nicht aus einer zweiten Nachbildung.
    3. **Vorschau**: Welche Buchungen des Kontos ausgeblendet werden (Import und Dateiimporte der App; nie manuelle
       Buchungen, Transfers oder Korrekturen), welche bleiben (Transfers mit einem anderen Konto – deren Gegenseite
       liegt dort), Bestand vorher/nachher/beobachtet, Zeilen ohne Gegenstück auf der anderen Seite (Warnsignale).
    4. **Übernehmen** (:func:`apply`): Buchungen ausblenden (Korrektur der Diagnose, rückgängig), API-Vorgänge in einem
       eigenen Stapel auswerten und buchen (Stapelaktion, rückgängig). EUR-Werte, Gebührenwerte und Einordnungen des
       ersetzten Imports bleiben erhalten, **wo die API sie nicht liefert** (Spender, ``CsvImportService._donate``).
       **Nachprüfung:** Stimmt der Bestand des Kontos danach nicht exakt mit dem beobachteten überein, wird alles
       zurückgenommen – es bleibt nie ein halber Zustand.
    5. **Rückgängig** (:func:`undo`): Buchungen der Quelle zurücknehmen, ausgeblendete Buchungen wiederherstellen.

Grenzen (bewusst, in der Vorschau sichtbar)
    * Der Beweis betrifft Mengen, nicht Werte: EUR-Werte stammen aus der API, aus dem ersetzten Import (Spender) oder
      aus Kursen – steuerlich relevante Unterschiede entstehen dort, nicht im Bestand.
    * Realisierte Ergebnisse je Jahr werden vor der Übernahme nicht simuliert (die Neubuchung wird erst bei der
      Übernahme ausgewertet) – nach der Übernahme unter „Steuern“ prüfen; Rückgängig stellt den Vorzustand her.
    * Ein Bestand, der exakt stimmt, schließt eine vollständige Gegenbuchung (z. B. zwei Fehler, die sich aufheben)
      nicht aus; die Zeilen ohne Gegenstück sind deshalb als Stichprobe aufgeführt.
    * Nur Anbieter mit nachweislich vollständigem Seitenende (``PILOT``); Binance und Wallets sind ausgeschlossen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

PILOT = ("bitpanda",)
MAX_AGE = timedelta(hours=24)  # der Abruf darf nicht älter sein: der beobachtete Bestand ist ein Zeitpunkt
WINDOW = timedelta(hours=36)  # Zuordnung ohne gemeinsame Kennung: Zeitabstand (Zeitzonen, Buchungszeitpunkt)
SAMPLE = 40  # Beispiele je Liste in der Vorschau
ZERO = Decimal(0)
DUST = Decimal("1e-12")  # wie der Bestand des Ledgers
SOURCE_OWN = ("csv:", "portfolia:csv:")  # Dateiimporte der App – werden wie der kuratierte Import ersetzt
_LOCK = threading.RLock()
_RUNNING: set[int] = set()  # Quellen mit laufendem Hintergrundlauf dieses Prozesses


# ----------------------------------------------------------------------------------------------------
# Zustand
# ----------------------------------------------------------------------------------------------------

def latest(db: Any, sid: int) -> Any:
    """Jüngster Eintrag der Quelle, der nicht verworfen ist."""
    return db.q1("SELECT * FROM ds_takeover WHERE source_id=? AND status <> 'discarded' ORDER BY id DESC LIMIT 1",
                 (sid,))


def _now() -> str:
    return iso(datetime.now(UTC)) or ""


def _msg(db: Any, rid: int, text: str, status: str | None = None) -> None:
    if status is None:
        db.x("UPDATE ds_takeover SET message=? WHERE id=?", (text[:500], rid))
    else:
        db.x("UPDATE ds_takeover SET message=?, status=? WHERE id=?", (text[:500], status, rid))


def supported(provider: str) -> bool:
    return provider in PILOT


def busy(sid: int) -> bool:
    """Läuft in diesem Prozess gerade ein Vollabruf bzw. eine Übernahme dieser Quelle?"""
    return sid in _RUNNING


def view(db: Any, sid: int) -> dict[str, Any] | None:
    """Jüngster Eintrag für die Anzeige. ``fetching``/``running`` ohne laufenden Prozess (Neustart) gilt als
    abgebrochen – ohne zu schreiben; der nächste Start räumt auf."""
    r = latest(db, sid)
    if r is None:
        return None
    d = dict(r)
    if d["status"] in ("fetching", "running") and sid not in _RUNNING:
        d["status"] = "failed"
        d["message"] = ("Vollabruf unterbrochen (Neustart) – bitte erneut abrufen." if r["status"] == "fetching" else
                        "Übernahme unterbrochen (Neustart) – Zustand unter Datenqualität → Entscheidungen und "
                        "Korrekturen prüfen.")
    return d


def _settle(db: Any, sid: int) -> None:
    """Unterbrochene Läufe (Neustart) abschließen, bevor ein neuer beginnt."""
    if sid in _RUNNING:
        return
    db.x("UPDATE ds_takeover SET status='failed', message='Unterbrochen (Neustart).' WHERE source_id=? AND status IN "
         "('fetching', 'running')", (sid,))


# ----------------------------------------------------------------------------------------------------
# Vollabruf
# ----------------------------------------------------------------------------------------------------

def start_fetch(ctx: Any, sid: int) -> dict[str, Any]:
    """Vollabruf im Hintergrund starten; die Seite zeigt den Fortschritt (``ds_takeover.message``)."""
    from app.datasources.service import datasource_service, is_busy

    ds = datasource_service(ctx).get(sid)
    if ds is None or not supported(ds.provider):
        return {"errors": ["Für diese Datenquelle ist keine Quellenübernahme vorgesehen."]}
    with _LOCK:
        if is_busy(sid) or busy(sid):
            return {"errors": ["Für diese Datenquelle läuft bereits ein Abruf bzw. eine Übernahme."]}
        _RUNNING.add(sid)
    try:
        _settle(ctx.db, sid)
        rid = _begin(ctx.db, sid)
    except Exception:
        _RUNNING.discard(sid)
        raise

    def work() -> None:
        try:
            run_fetch(ctx, sid, rid)
        except Exception as e:  # pragma: no cover - Absicherung: Hintergrundlauf darf nie hängen bleiben
            log.exception("Quellenübernahme: Vollabruf von %s fehlgeschlagen", sid)
            _msg(ctx.db, rid, f"Abruf fehlgeschlagen: {type(e).__name__}", "failed")
        finally:
            _RUNNING.discard(sid)
            ctx.db.close_thread_conn()

    threading.Thread(target=work, name=f"takeover-fetch-{sid}", daemon=True).start()
    return {"id": rid, "errors": []}


def _begin(db: Any, sid: int) -> int:
    with db.transaction() as c:
        c.execute("UPDATE ds_takeover SET status='discarded' WHERE source_id=? AND status IN "
                  "('staged', 'failed', 'undone', 'fetching')", (sid,))
        return int(c.execute("INSERT INTO ds_takeover(source_id, status, message, created_at) VALUES (?, 'fetching', "
                             "'Vollabruf läuft …', ?)", (sid, _now())).lastrowid)  # type: ignore[arg-type]


def run_fetch(ctx: Any, sid: int, rid: int | None = None) -> dict[str, Any]:
    """Alle Vorgänge der Quelle **nur lesend** abrufen und für den Beweis ablegen (synchron)."""
    from app.csvimport.service import rec_to_json
    from app.datasources.service import datasource_service

    db = ctx.db
    svc = datasource_service(ctx)
    ds = svc.get(sid)
    if ds is None or not supported(ds.provider):
        return {"errors": ["Für diese Datenquelle ist keine Quellenübernahme vorgesehen."]}
    rid = rid if rid is not None else _begin(db, sid)

    def progress(stage: str, done: int, total: int | None, text: str) -> None:
        _msg(db, rid, f"Vollabruf: {text or stage}" + (f" ({done}/{total})" if total else ""))

    res = svc.research_fetch(sid, None, rounds=40, seconds=3600, progress=progress)
    if res.get("error"):
        _msg(db, rid, f"Abruf fehlgeschlagen: {res['error']}", "failed")
        return {"errors": [res["error"]]}
    recs = res["recs"]
    meta = {"complete": bool(res.get("complete")), "gaps": list(res.get("gaps") or []),
            "warnings": list(res.get("warnings") or []), "coverage": res.get("coverage") or {},
            "skipped": res.get("skipped") or {}, "rounds": res.get("rounds"),
            "balances_complete": bool(res.get("balances_complete", True)),
            "balances": None if res.get("balances") is None else [
                {"key": b.asset_key, "qty": format(b.qty, "f"), "name": b.name, "note": b.note}
                for b in res["balances"]]}
    db.x("UPDATE ds_takeover SET status='staged', message=?, events_json=?, meta_json=? WHERE id=?",
         (f"{len({r.event_key or r.ext_id for r in recs})} Vorgänge abgerufen", "[" + ",".join(
             rec_to_json(r) for r in recs) + "]", json.dumps(meta, ensure_ascii=False, default=str), rid))
    return {"id": rid, "events": len(recs), "errors": []}


def discard(ctx: Any, sid: int) -> bool:
    r = latest(ctx.db, sid)
    if r is None or r["status"] not in ("staged", "failed", "undone"):
        return False
    ctx.db.x("UPDATE ds_takeover SET status='discarded', events_json=NULL WHERE id=?", (r["id"],))
    return True


# ----------------------------------------------------------------------------------------------------
# Beweis und Vorschau
# ----------------------------------------------------------------------------------------------------

@dataclass
class Check:
    key: str
    label: str
    ok: bool
    detail: str = ""
    blocking: bool = True


@dataclass
class Line:
    """Eine Zeile der Quelle samt der Zeile, die der Import daraus bucht."""

    rec: Any
    row: dict[str, str] | None
    legs: dict[str, Decimal]  # Bestandswirkung auf dem Konto je Asset
    errors: list[str]
    base: dict[str, Decimal] = field(default_factory=dict)  # dieselbe Wirkung ohne Gebühr (Zuordnung ohne Kennung)
    covered_by: str | None = None  # Transfer mit anderem Konto im Import (bleibt bestehen)


@dataclass
class Assessment:
    sid: int
    account: str
    provider: str
    rid: int
    fetched_at: str
    age: timedelta
    checks: list[Check] = field(default_factory=list)
    events: int = 0
    lines: int = 0
    first: str | None = None
    last: str | None = None
    hide: list[Any] = field(default_factory=list)  # Tx
    kept: list[tuple[Any, str]] = field(default_factory=list)
    own: int = 0
    covered: int = 0
    matched: int = 0
    donors: dict[str, dict[str, Any]] = field(default_factory=dict)
    unmatched_old: list[Any] = field(default_factory=list)
    unmatched_new: list[Any] = field(default_factory=list)
    balances: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    token: str = ""
    plan: Any = None
    finding: Any = None
    lines_by_ext: dict[str, Line] = field(default_factory=dict)
    observed: dict[str, Decimal] = field(default_factory=dict)
    ready: bool = False

    @property
    def ok(self) -> bool:
        return self.ready and all(c.ok for c in self.checks if c.blocking)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.blocking and not c.ok]


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "") else ZERO
    except ArithmeticError:
        return ZERO


def _tx_legs(t: Any, account: str, fee: bool = True) -> dict[str, Decimal]:
    """Bestandswirkung einer Buchung auf dem Konto (Gebühr am Abgangskonto wie im Ledger)."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    if t.from_account == account and t.from_asset and t.from_qty:
        out[t.from_asset] -= t.from_qty
    if t.to_account == account and t.to_asset and t.to_qty:
        out[t.to_asset] += t.to_qty
    if fee and t.fee_asset and t.fee_qty and (t.from_account or t.to_account) == account:
        out[t.fee_asset] -= t.fee_qty
    return {a: q for a, q in out.items() if q}


def _same(a: Decimal, b: Decimal) -> bool:
    """Gleiche Menge für die Zuordnung ohne Kennung: Gebühren werden je Quelle verschieden ausgewiesen (im Betrag oder
    zusätzlich) – zwei Prozent Spielraum; maßgeblich bleibt der exakte Bestandsnachweis, nicht diese Zuordnung."""
    return (a > 0) == (b > 0) and abs(a - b) <= max(abs(a), abs(b)) * Decimal("0.02") + DUST


def _fmt(q: Decimal) -> str:
    return format(q.normalize(), "f") if q else "0"


def _lines(ctx: Any, recs: list[Any], account: str, provider: str) -> tuple[list[Line], Any]:
    """Zeilen der Quelle durch dieselbe Abbildung wie beim Import schicken (keine zweite Nachbildung)."""
    from app.csvimport import model as M
    from app.csvimport.service import RowCtx, SymbolResolver, csv_service
    from app.diagnosis.research import effects_of_row

    csv = csv_service(ctx)
    resolver = SymbolResolver(csv.known_assets(), csv.saved_symbols())
    acc_map = csv.saved_accounts()
    batch = {"account": account, "profile": f"sync:{provider}"}
    out: list[Line] = []
    for i, r in enumerate(sorted(recs, key=lambda x: (x.ts, x.event_key or "", x.event_line or 0))):
        rc = RowCtx(id=0, idx=i, line=r.line, rec=r, status="new", decision=None, value_in=None, fee_in=None,
                    pair_ref=None, pair_conf=None, pair_ok=None, tx_id=None)
        row = None if r.kind == M.REVIEW else csv._build(rc, resolver, acc_map, batch, f"sync:{provider}", None)
        errors = list(rc.errors)
        if r.kind == M.REVIEW:
            errors.append(r.note or "Deutung nicht eindeutig")
        elif r.ts_missing:
            errors.append("Zeitpunkt fehlt in den Daten der Quelle")
        elif row is None and not errors:
            errors.append("nicht abbildbar")
        legs: dict[str, Decimal] = defaultdict(Decimal)
        base: dict[str, Decimal] = defaultdict(Decimal)
        if row is not None:
            for acc, aid, q in effects_of_row(row):
                if acc == account:
                    legs[aid] += q
            for acc, aid, q in effects_of_row({**row, "fee_asset": "", "fee_qty": ""}):
                if acc == account:
                    base[aid] += q
        out.append(Line(r, row, {a: q for a, q in legs.items() if q}, errors,
                        base={a: q for a, q in base.items() if q}))
    return out, resolver


def _observed(meta: dict[str, Any], resolver: Any, provider: str) -> tuple[dict[str, Decimal], list[str]]:
    obs: dict[str, Decimal] = defaultdict(Decimal)
    unmapped: list[str] = []
    for b in meta.get("balances") or []:
        q = _dec(b.get("qty"))
        aid, _how, _key = resolver.resolve_for(str(b["key"]), provider)
        if aid is None:
            if q:
                unmapped.append(str(b["key"]))
            continue
        obs[aid] += q
    return {a: q for a, q in obs.items() if q}, unmapped


def assess(ctx: Any, sid: int, *, plan: bool = True) -> Assessment | None:
    """Beweis und Vorschau aus dem abgelegten Vollabruf (rein lesend). ``None``: kein abgelegter Abruf."""
    from app.csvimport.events import identity_keys, source_ref_keys
    from app.csvimport.identity import note_identity_keys
    from app.csvimport.service import rec_from_json
    from app.datasources import connector as conn_mod
    from app.datasources.service import datasource_service

    db = ctx.db
    row = latest(db, sid)
    ds = datasource_service(ctx).get(sid)
    if row is None or ds is None or row["status"] not in ("staged", "running", "applied") or not row["events_json"]:
        return None
    meta = json.loads(row["meta_json"] or "{}")
    recs = [rec_from_json(json.dumps(d)) for d in json.loads(row["events_json"])]
    account, prov = ds.account, ds.provider
    fetched = parse_iso(row["created_at"]) or datetime.now(UTC)
    a = Assessment(sid, account, prov, int(row["id"]), row["created_at"], datetime.now(UTC) - fetched)
    cov = meta.get("coverage") or {}
    a.skipped = dict(meta.get("skipped") or {})

    lines, resolver = _lines(ctx, recs, account, prov)
    a.lines = len(lines)
    a.events = len({ln.rec.event_key or ln.rec.ext_id for ln in lines})
    times = sorted(ln.rec.ts for ln in lines if not ln.rec.ts_missing)
    a.first, a.last = (iso(times[0]), iso(times[-1])) if times else (None, None)
    observed, unmapped = _observed(meta, resolver, prov)
    a.observed = observed

    # -- Bestehende Buchungen des Kontos einteilen ------------------------------------------------------
    pf = ctx.recorded_portfolio()
    own_ids = {r["tx_id"] for r in db.q("SELECT tx_id FROM journal_tx WHERE source=? AND status='active'",
                                        (f"sync:{prov}",))}
    cand: list[Any] = []
    for t in pf.txs if pf is not None else []:
        if account not in (t.from_account, t.to_account) or t.flag == "estimated" or t.origin == "plan":
            continue
        if t.tx_id in own_ids:
            a.own += 1
        elif t.from_account and t.to_account and t.from_account != t.to_account:
            a.kept.append((t, "Transfer mit anderem Konto – die Gegenseite liegt dort"))
        elif t.origin == "journal" and not str(t.source or "").startswith(SOURCE_OWN):
            a.kept.append((t, "in der App erfasst (manuell, Transfer oder Korrektur) – bleibt"))
        else:
            cand.append(t)

    # -- Gegenstücke: Kennung zuerst, sonst Menge und Zeit -----------------------------------------------
    aliases: dict[str, set[str]] = defaultdict(set)
    for r in db.q("SELECT key, tx_id FROM journal_event_alias"):
        aliases[r["tx_id"]].add(r["key"])
    by_key: dict[str, list[int]] = defaultdict(list)
    for i, ln in enumerate(lines):
        for k in identity_keys(ln.rec.event_key, ln.rec.aliases or (), ln.rec.ext_id):
            by_key[k].append(i)
    by_event: dict[str, list[int]] = defaultdict(list)
    for i, ln in enumerate(lines):
        by_event[ln.rec.event_key or f"#{i}"].append(i)
    by_asset: dict[tuple[str, bool], list[int]] = defaultdict(list)
    for i, ln in enumerate(lines):
        for aid, q in ln.legs.items():
            by_asset[(aid, q > 0)].append(i)
    used: set[int] = set()

    def find(t: Any) -> list[int]:
        keys = source_ref_keys(t.source, t.source_ref) | aliases.get(t.tx_id, set()) | note_identity_keys(
            t.source, account, t.note)
        hit = sorted({i for k in keys for i in by_key.get(k, ())})
        if hit:
            return hit
        legs, base = _tx_legs(t, account), _tx_legs(t, account, fee=False)
        win = WINDOW + (timedelta(hours=24) if t.date_only else timedelta(0))
        best: tuple[int, timedelta, int] | None = None

        def hit(aid: str, ln: Line) -> bool:
            # Gebühren weist jede Quelle anders aus (im Betrag oder zusätzlich) – mit und ohne vergleichen
            mine = {x for x in (legs.get(aid), base.get(aid)) if x}
            theirs = {x for x in (ln.legs.get(aid), ln.base.get(aid)) if x}
            return any(_same(m, o) for m in mine for o in theirs)

        for aid, q in legs.items():
            for i in by_asset.get((aid, q > 0), ()):
                ln = lines[i]
                if i in used or ln.rec.ts_missing or ln.covered_by:
                    continue
                dt = abs(ln.rec.ts - t.ts)
                if dt > win or not hit(aid, ln):
                    continue
                shared = sum(1 for x in legs if x in ln.legs and hit(x, ln))
                score = (-shared, dt, i)
                if best is None or score < best:
                    best = score
        if best is None:
            return []
        return sorted(by_event.get(lines[best[2]].rec.event_key or f"#{best[2]}", [best[2]]))

    # Transfers mit anderem Konto: die API-Seite ist durch die bleibende Buchung gedeckt (nicht doppelt buchen)
    for t, _why in a.kept:
        if t.tx_id in own_ids or not (t.from_account and t.to_account and t.from_account != t.to_account):
            continue
        for i in find(t):
            if i not in used and lines[i].legs:
                used.add(i)
                lines[i].covered_by = t.tx_id
                a.covered += 1
    matched_new: set[int] = set()
    hide_ids: list[str] = []
    donors: dict[str, dict[str, Any]] = {}
    for t in sorted(cand, key=lambda x: (x.ts, x.tx_id)):
        hit = [i for i in find(t) if lines[i].covered_by is None]
        if not hit:
            a.unmatched_old.append(t)
            continue
        a.matched += 1
        used.update(hit)
        matched_new.update(hit)
        legs = _tx_legs(t, account)
        for i in hit:
            ln = lines[i]
            shares = any(x in ln.legs for x in legs)
            d: dict[str, Any] = {"type": t.type, "tx": t.tx_id}
            if shares and t.value_eur is not None and t.value_eur > 0:
                d["value_eur"] = format(t.value_eur, "f")
            if t.fee_eur is not None and t.fee_eur > 0 and ln.row and ln.row.get("fee_asset") == t.fee_asset:
                d["fee_eur"] = format(t.fee_eur, "f")
            if shares and t.tag:
                d["tag"] = t.tag
            if ln.rec.ext_id and len(d) > 2:
                donors.setdefault(ln.rec.ext_id, d)
    a.donors = donors
    a.unmatched_new = [ln for i, ln in enumerate(lines) if i not in matched_new and ln.covered_by is None
                       and ln.legs and (ln.rec.ext_id or "") not in own_ids]

    # -- Plan: ausblenden (Korrektur der Diagnose) ---------------------------------------------------------
    if plan and cand:
        from app.diagnosis import actions as A
        from app.diagnosis.engine import report_for
        from app.diagnosis.model import Finding
        from app.diagnosis.recommend import Option

        report = report_for(ctx)
        st = A._State(ctx, report)
        f = Finding(kind="takeover", status="belegt", priority=2, title=f"Quellenübernahme {ds.name}: API maßgeblich",
                    key=f"takeover|{sid}|{row['id']}",
                    data={"type": "takeover", "source": sid, "row": int(row["id"]), "fp": [int(row["id"]), sid]})
        opt = Option("takeover", "Buchungen des Kontos durch die API ersetzen",
                     "Import- und Dateibuchungen des Kontos ausblenden, die API-Vorgänge buchen")
        pl = A.Plan(f, opt, {}, version=A.data_version(db))
        for t in sorted(cand, key=lambda x: (x.ts, x.tx_id)):
            scratch = A.Plan(f, opt, {})
            if A._hide(st, st.tx(t.tx_id), scratch) is None:
                a.kept.append((t, "lässt sich nicht ausblenden: " + " ".join(scratch.errors)[:200]))
            else:
                pl.ops += scratch.ops
                hide_ids.append(t.tx_id)
        a.plan, a.finding = pl, f
    a.hide = [t for t in cand if t.tx_id in set(hide_ids)] if plan else list(cand)
    if plan:
        hidden = set(hide_ids)
        a.unmatched_old = [t for t in a.unmatched_old if t.tx_id in hidden]
        a.donors = {k: v for k, v in donors.items() if v.get("tx") in hidden}

    # -- Bestand: Vorgänge der Quelle + bleibende Buchungen = beobachtet --------------------------------------
    expected: dict[str, Decimal] = defaultdict(Decimal)
    for ln in lines:
        if ln.covered_by is None and not ln.errors:
            for aid, q in ln.legs.items():
                expected[aid] += q
    for t, _why in a.kept:
        if t.tx_id not in own_ids:
            for aid, q in _tx_legs(t, account).items():
                expected[aid] += q
    led = ctx.ledger()
    now = led.holdings_by_account(account) if led is not None else {}
    assets = pf.assets if pf is not None else {}
    for aid in sorted(set(expected) | set(observed) | set(now)):
        e, o, n = expected.get(aid, ZERO), observed.get(aid, ZERO), now.get(aid, ZERO)
        if not (e or o or n):
            continue
        name = getattr(assets.get(aid), "symbol", None) or aid
        a.balances.append({"asset": aid, "name": name, "now": n, "after": e, "observed": o,
                           "ok": abs(e - o) <= DUST, "was_ok": abs(n - o) <= DUST})
    a.ready = True

    # -- Prüfungen ------------------------------------------------------------------------------------------
    conn = conn_mod.connector_for(prov)
    pag = (cov.get("pagination") or {})
    stale = a.age > MAX_AGE
    bad = [ln for ln in lines if ln.errors]
    a.checks = [
        Check("pilot", "Anbieter für die Übernahme zugelassen", supported(prov),
              "Nur Anbieter mit nachweislich vollständigem Seitenende (Pilot: Bitpanda)."),
        Check("fetch", "Abruf vollständig (Seitenende laut API, keine Lücke)",
              bool(meta.get("complete")) and not meta.get("gaps") and cov.get("mode") == "vollständig"
              and pag.get("end") == "has_next_page=false",
              f"{cov.get('pages', '?')} Seiten, {cov.get('operations', '?')} Vorgänge, Seitenende: "
              f"{pag.get('end') or 'nicht belegt'}" + (f"; Lücken: {'; '.join(meta['gaps'][:3])}" if meta.get("gaps")
                                                       else "")),
        Check("parser", "Auswertung der Quelle aktuell", conn is not None and cov.get("parser") == conn.parser_version,
              f"Auswertung v{cov.get('parser')} (aktuell v{getattr(conn, 'parser_version', '?')})"),
        Check("fresh", "Abruf nicht älter als 24 Stunden", not stale,
              f"abgerufen vor {int(a.age.total_seconds() // 3600)} h – der beobachtete Bestand ist ein Zeitpunkt"),
        Check("observed", "Beobachteter Bestand vollständig und zugeordnet",
              meta.get("balances") is not None and bool(meta.get("balances_complete", True)) and not unmapped,
              ("Bestand der API fehlt (Leserecht „Balances“?)" if meta.get("balances") is None else
               "nicht zugeordnet: " + ", ".join(unmapped[:8]) if unmapped else
               f"{len(observed)} Bestände mit Menge")),
        Check("lines", "Alle Vorgänge sind eindeutig und buchbar", not bad,
              f"{len(bad)} nicht buchbar: " + "; ".join(
                  f"{(ln.rec.event_key or ln.rec.ext_id or '?')[-12:]} {ln.errors[0][:80]}" for ln in bad[:4])
              if bad else f"{a.lines} Zeilen in {a.events} Vorgängen"),
        Check("balance", "Vorgänge der API ergeben den beobachteten Bestand exakt je Asset",
              all(b["ok"] for b in a.balances),
              "; ".join(f"{b['name']}: erklärt {_fmt(b['after'])}, beobachtet {_fmt(b['observed'])}"
                         for b in a.balances if not b["ok"])[:400] or f"{len(a.balances)} Assets stimmen"),
        Check("connector", "Eigene Bestandsprüfung der Quelle (Vorgänge ↔ /portfolio)",
              bool((cov.get("balances") or {}).get("checked")) and not (cov.get("balances") or {}).get("differences"),
              "; ".join((cov.get("balances") or {}).get("examples") or [])[:300]
              or str((cov.get("balances") or {}).get("note") or "ohne Abweichung"), blocking=False),
    ]
    if plan:
        a.checks.append(Check("plan", "Alle ersetzten Buchungen lassen sich ausblenden",
                              not (a.plan and a.plan.errors), "; ".join(a.plan.errors[:3]) if a.plan else ""))
    fp = json.dumps({"s": sid, "r": a.rid, "h": sorted(hide_ids), "b": {k: _fmt(v) for k, v in sorted(
        expected.items())}, "p": a.plan.token if a.plan else "", "d": sorted(a.donors)}, sort_keys=True)
    a.token = hashlib.sha256(fp.encode()).hexdigest()[:32]
    a.lines_by_ext = {ln.rec.ext_id: ln for ln in lines if ln.rec.ext_id}
    return a


# ----------------------------------------------------------------------------------------------------
# Übernehmen
# ----------------------------------------------------------------------------------------------------

def start_apply(ctx: Any, sid: int, token: str) -> dict[str, Any]:
    """Übernahme im Hintergrund: Beweis erneut prüfen, dann übernehmen (Fortschritt in ``ds_takeover.message``)."""
    with _LOCK:
        if busy(sid):
            return {"errors": ["Es läuft bereits ein Abruf bzw. eine Übernahme."]}
        _RUNNING.add(sid)

    def work() -> None:
        try:
            apply(ctx, sid, token)
        except Exception as e:  # pragma: no cover - Absicherung
            log.exception("Quellenübernahme von %s fehlgeschlagen", sid)
            r = latest(ctx.db, sid)
            if r is not None:
                _msg(ctx.db, int(r["id"]), f"Übernahme fehlgeschlagen: {type(e).__name__} – bitte Zustand prüfen",
                     "failed")
        finally:
            _RUNNING.discard(sid)
            ctx.db.close_thread_conn()

    threading.Thread(target=work, name=f"takeover-apply-{sid}", daemon=True).start()
    return {"errors": []}


def apply(ctx: Any, sid: int, token: str) -> dict[str, Any]:
    """Quelle übernehmen – nur wenn der Beweis hält und die Vorschau noch gilt; danach Nachprüfung am Bestand, sonst
    vollständige Rücknahme. Rückgabe: ``errors`` (leer bei Erfolg), ``result``."""
    from app.datasources import service as S

    with _LOCK:
        ev = S._acquire(sid)
        if ev is None:
            return {"errors": [S.BUSY_TEXT]}
        try:
            with S._INGEST_LOCK:
                return _apply(ctx, sid, token)
        finally:
            S._release(sid)


def _apply(ctx: Any, sid: int, token: str) -> dict[str, Any]:
    from app.csvimport import batch as B
    from app.csvimport.service import csv_service, rec_to_json
    from app.diagnosis import actions as A
    from app.journal.service import journal_service

    db = ctx.db
    csv = csv_service(ctx)
    a = assess(ctx, sid)
    if a is None or db.q1("SELECT status FROM ds_takeover WHERE id=?", (a.rid,))["status"] != "staged":
        return {"errors": ["Kein abgelegter Vollabruf – bitte zuerst abrufen."]}
    def refuse(text: str) -> dict[str, Any]:
        db.x("UPDATE ds_takeover SET message=?, result_json=? WHERE id=?",
             (text[:500], json.dumps({"detail": []}), a.rid))
        return {"errors": [text]}

    if not a.ok:
        return refuse("Der Beweis ist nicht erbracht: " + "; ".join(c.label for c in a.blockers))
    if a.token != token:
        return refuse("Die Vorschau ist nicht mehr aktuell (Daten haben sich geändert) – bitte neu öffnen.")
    rid = a.rid
    ds_name = (db.q1("SELECT name FROM data_source WHERE id=?", (sid,)) or {"name": "Quelle"})["name"]
    db.x("UPDATE ds_takeover SET status='running', donors_json=?, message=? WHERE id=?",
         (json.dumps(a.donors, ensure_ascii=False), "Buchungen werden ausgeblendet …", rid))

    decision_id: int | None = None
    action_id: int | None = None
    bid: int | None = None

    def rollback(why: str, detail: list[str]) -> dict[str, Any]:
        notes: list[str] = []
        if action_id is not None:
            r = B.undo(csv, action_id)
            notes += r.get("errors") or []
        if bid is not None:
            csv.discard(bid, rewind=False)
        if decision_id is not None:
            r2 = A.undo(ctx, decision_id)
            notes += r2.errors
        text = why + (" Zurückgenommen." if not notes else " Rücknahme unvollständig: " + "; ".join(notes))
        db.x("UPDATE ds_takeover SET status='staged', message=?, result_json=?, donors_json=NULL, decision_id=NULL, "
             "action_id=NULL, batch_id=NULL WHERE id=?",
             (text[:500], json.dumps({"detail": detail[:40], "rollback_errors": notes}, ensure_ascii=False), rid))
        journal_service(ctx).after_change()
        return {"errors": [text, *detail[:12]]}

    # 1. ausblenden -----------------------------------------------------------------------------------------
    if a.plan is not None and a.plan.ops:
        res = A.apply_plan(ctx, a.finding, a.plan, a.plan.token, None)
        if res.errors:
            db.x("UPDATE ds_takeover SET status='staged', message=?, donors_json=NULL WHERE id=?",
                 (res.errors[0][:500], rid))
            return {"errors": res.errors}
        decision_id = res.decision_id
        db.x("UPDATE ds_takeover SET decision_id=? WHERE id=?", (decision_id, rid))

    # 2. Vorgänge der Quelle auswerten ----------------------------------------------------------------------------
    _msg(db, rid, "API-Vorgänge werden ausgewertet …")
    from app.csvimport.service import rec_from_json

    row = db.q1("SELECT events_json FROM ds_takeover WHERE id=?", (rid,))
    recs = [rec_from_json(json.dumps(d)) for d in json.loads(row["events_json"])]
    payload = ("[" + ",".join(rec_to_json(r) for r in recs) + "]").encode()
    prov = a.provider
    try:
        bid = csv.ingest(recs, source=f"sync:{prov}", profile=f"sync:{prov}", account=a.account,
                         label=f"{ds_name} · Quellenübernahme {datetime.now(UTC).strftime('%d.%m.%Y %H:%M')}",
                         datasource_id=sid, payload=payload, options={"cutoff": ""}, append_to=None)
    except Exception as e:
        log.exception("Quellenübernahme: Auswertung fehlgeschlagen")
        return rollback(f"Auswertung der API-Vorgänge fehlgeschlagen ({type(e).__name__}).", [])
    db.x("UPDATE ds_takeover SET batch_id=? WHERE id=?", (bid, rid))
    rows = csv.rows(bid)
    cover_ext = {e for e, ln in a.lines_by_ext.items() if ln.covered_by}
    book = [rc for rc in rows if rc.status == "new" and rc.row is not None and not rc.errors
            and (rc.rec.ext_id or "") not in cover_ext]
    rest = [rc for rc in rows if rc not in book and rc.open and (rc.rec.ext_id or "") not in cover_ext]

    # 3. buchen -------------------------------------------------------------------------------------------------
    _msg(db, rid, f"{len(book)} Vorgänge werden gebucht …")
    if book:
        idxs = {rc.idx for rc in book}
        p = B.plan(csv, bid, "include", idxs)
        if not p.todo:
            return rollback("Keine API-Zeile ist buchbar.", [f"{rc.line}: {'; '.join(rc.errors[:1])}" for rc in rows
                                                              if rc.errors][:12])
        out = B.execute(csv, bid, "include", {i.rc.idx for i in p.todo}, token=B.new_token(),
                        fingerprint=p.fingerprint(), params={"takeover": rid})
        if out.get("errors"):
            return rollback("Buchen der API-Vorgänge nicht möglich.", list(out["errors"]))
        action_id = int(out["action_id"])
        db.x("UPDATE ds_takeover SET action_id=? WHERE id=?", (action_id, rid))
    # durch Transfers gedeckte Zeilen: mit der bleibenden Buchung verknüpfen, sonst bewusst nicht übernehmen
    for act in ("link", "skip"):
        left = {rc.idx for rc in csv.rows(bid) if (rc.rec.ext_id or "") in cover_ext and rc.open}
        if not left:
            break
        todo = {i.rc.idx for i in B.plan(csv, bid, act, left).todo if act != "link" or i.target}
        if todo:
            B.execute(csv, bid, act, todo, token=B.new_token(), fingerprint=B.plan(csv, bid, act, todo).fingerprint(),
                      params={"takeover": rid})

    # 4. Nachprüfung am Bestand -----------------------------------------------------------------------------------
    journal_service(ctx).after_change()
    led = ctx.ledger()
    now = led.holdings_by_account(a.account) if led is not None else {}
    diffs = []
    for aid in sorted(set(now) | set(a.observed)):
        n, o = now.get(aid, ZERO), a.observed.get(aid, ZERO)
        if abs(n - o) > DUST:
            diffs.append(f"{aid}: gebucht {_fmt(n)}, beobachtet {_fmt(o)}")
    stuck = [rc for rc in csv.rows(bid) if rc.open and (rc.rec.ext_id or "") not in cover_ext
             and rc.status not in ("known", "ignored")]
    if diffs:
        detail = diffs[:12] + [f"Zeile {rc.line}: {rc.status} {'; '.join((rc.errors or rc.warnings)[:1])[:120]}"
                               for rc in stuck[:12]]
        return rollback("Nachprüfung fehlgeschlagen: Der Bestand des Kontos weicht vom beobachteten ab.", detail)
    csv.refresh_status(bid)
    result = {"hidden": len(a.hide), "kept": len(a.kept), "booked": len(book), "covered": len(cover_ext),
              "matched": a.matched, "donors": len(a.donors), "left": len(stuck),
              "check": {"assets": len(a.balances), "exact": True}}
    db.x("UPDATE ds_takeover SET status='applied', applied_at=?, message=?, result_json=? WHERE id=?",
         (_now(), f"Übernommen: {len(a.hide)} Buchungen ersetzt, {len(book)} API-Vorgänge gebucht, Bestand exakt "
                  f"bestätigt ({len(a.balances)} Assets).", json.dumps(result, ensure_ascii=False), rid))
    log.info("Quellenübernahme %s: %s", sid, result)
    return {"errors": [], "result": result, "unresolved": len(rest)}


# ----------------------------------------------------------------------------------------------------
# Rückgängig
# ----------------------------------------------------------------------------------------------------

def undo(ctx: Any, sid: int) -> dict[str, Any]:
    """Übernahme als Ganzes zurücknehmen: zuerst die gebuchten API-Vorgänge, dann die ausgeblendeten Buchungen."""
    from app.csvimport import batch as B
    from app.csvimport.service import csv_service
    from app.datasources import service as S
    from app.diagnosis import actions as A
    from app.journal.service import journal_service

    db = ctx.db
    with _LOCK:
        r = latest(db, sid)
        if r is None or r["status"] != "applied":
            return {"errors": ["Keine aktive Quellenübernahme."]}
        ev = S._acquire(sid)
        if ev is None:
            return {"errors": [S.BUSY_TEXT]}
        try:
            with S._INGEST_LOCK:
                csv = csv_service(ctx)
                dec = db.q1("SELECT id, status, ops_json FROM diag_decision WHERE id=?", (r["decision_id"],)) \
                    if r["decision_id"] else None
                if dec is not None and dec["status"] == "active":
                    later = A._dependents(db, int(dec["id"]), json.loads(dec["ops_json"] or "[]"))
                    if later:
                        return {"errors": ["Rückgängig nicht möglich: spätere Korrekturen betreffen dieselben "
                                           "Buchungen (" + ", ".join(f"#{i}" for i in later) + "). Bitte zuerst diese "
                                           "zurücknehmen – es wurde nichts geändert."]}
                notes: list[str] = []
                if r["action_id"]:
                    res = B.undo(csv, int(r["action_id"]))
                    if res.get("errors"):
                        return {"errors": res["errors"]}
                    if res.get("changed"):
                        notes.append(f"{res['changed']} Zeile(n) waren seit der Übernahme verändert und blieben "
                                     "unberührt.")
                if r["batch_id"] and csv.batch(int(r["batch_id"])) is not None:
                    csv.discard(int(r["batch_id"]), rewind=False)
                db.x("UPDATE ds_takeover SET status='undone', undone_at=?, donors_json=NULL, message=? WHERE id=?",
                     (_now(), "Zurückgenommen." + (" " + " ".join(notes) if notes else ""), r["id"]))
                # erst nach dem Statuswechsel: sonst verweigert die Diagnose das Zurücknehmen der Ausblendung
                if dec is not None and dec["status"] == "active":
                    out = A.undo(ctx, int(dec["id"]))
                    if out.errors:
                        db.x("UPDATE ds_takeover SET status='applied', undone_at=NULL, message=? WHERE id=?",
                             ("Teilweise zurückgenommen: " + " ".join(out.errors)[:400], r["id"]))
                        return {"errors": ["Die API-Buchungen sind zurückgenommen, die ausgeblendeten Buchungen "
                                           "ließen sich nicht wiederherstellen: " + " ".join(out.errors)]}
                journal_service(ctx).after_change()
                return {"errors": [], "notes": notes}
        finally:
            S._release(sid)
