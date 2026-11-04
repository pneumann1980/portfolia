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
    * Zwei Stufen: **belegt** (``PROOF``: Bitpanda – Seitenende laut API und exakter Bestand) und **bestätigt**
      (``CONFIRM`` und Blockchain-Wallets: Der Nutzer erklärt die Historie der Quelle für vollständig; die Übernahme
      gilt nur, wenn der Bestand je Asset danach **nicht schlechter** vom beobachteten abweicht als vorher – sonst
      vollständige Rücknahme). Ohne Bestätigung bleibt es bei Verknüpfen im Prüfstapel.
    * Zeitraum: Buchungen **vor dem ersten Ereignis der Quelle** bleiben (die Quelle belegt nur, was sie selbst kennt).
    * Steuerjahre: Realisierte Ergebnisse und Erträge je Jahr werden vor und nach der Übernahme **gemessen** und im
      Ergebnis gezeigt (nicht vorab simuliert); Rückgängig stellt den Vorzustand her.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.util.timeutil import iso, parse_iso
from app.web.fmt import asset_label, qty_exact

log = logging.getLogger(__name__)

PROOF = ("bitpanda",)  # Vollständigkeit belegt: Seitenende laut API + exakter Bestand je Asset
CONFIRM = ("binance",)  # Vollständigkeit ohne Beleg: der Nutzer bestätigt sie; der Bestand darf nicht schlechter werden
PILOT = PROOF  # Name bis 0.67
MAX_AGE = timedelta(hours=24)  # der Abruf darf nicht älter sein: der beobachtete Bestand ist ein Zeitpunkt
WINDOW = timedelta(hours=36)  # Zuordnung ohne gemeinsame Kennung: Zeitabstand (Zeitzonen, Buchungszeitpunkt)
LAG_MAX = timedelta(days=7)  # Sparplan-Einzahlung: Gutschrift der API bis zu 7 Tage nach dem Datum des Imports
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


def mode(provider: str) -> str:
    """``proof`` (Anbieter belegt die Vollständigkeit), ``confirm`` (Nutzer bestätigt; Börsen mit Lücken im Abruf und
    alle Blockchain-Wallets) oder leer (keine Übernahme vorgesehen)."""
    from app.datasources import connector as conn_mod
    from app.datasources.wallet import WalletConnector

    if provider in PROOF:
        return "proof"
    if provider in CONFIRM:
        return "confirm"
    conn = conn_mod.connector_for(provider)
    return "confirm" if isinstance(conn, WalletConnector) else ""


def supported(provider: str) -> bool:
    return bool(mode(provider))


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
                        "Übernahme unterbrochen (Neustart) – bitte „Unterbrochene Übernahme zurücknehmen“: der Stand "
                        "vor der Übernahme wird wiederhergestellt.")
    d["recover"] = needs_recovery(db, sid) is not None
    d["busy"] = sid in _RUNNING  # auch zwischen Start und Laufbeginn automatisch aktualisieren
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
        if needs_recovery(ctx.db, sid) is not None:
            return {"errors": [RECOVER_FIRST]}
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


def to_review(ctx: Any, sid: int, *, confirm: bool = False, adjust: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Vorgänge, die nur mit Einzelentscheidung buchbar sind, aus dem **gespeicherten** Vollabruf in einen Prüf-Stapel
    der Quelle legen – ohne neuen Abruf beim Anbieter. Dort entscheiden (z. B. Transfer bestätigen, dauerhaft
    ignorieren); danach bewertet die Vorschau den Plan neu. Bereits vorgelegte Vorgänge werden nicht doppelt abgelegt.

    Ergebnis: ``bid`` (Stapel mit den Vorgängen), ``event`` (erster betroffener Vorgang), ``errors``."""
    from app.csvimport.service import csv_service, rec_from_json, rec_to_json
    from app.datasources import service as DSM

    db = ctx.db
    row = latest(db, sid)
    ds = DSM.datasource_service(ctx).get(sid)
    if row is None or ds is None or row["status"] != "staged" or not row["events_json"]:
        return {"errors": ["Kein gespeicherter Vollabruf vorhanden."]}
    a = assess(ctx, sid, confirm=confirm, adjust=adjust)
    keys = [str(e.get("event") or "") for p in (a.problems if a is not None else []) if p.get("kind") == "row"
            for e in p.get("events") or []]
    keys = list(dict.fromkeys(k for k in keys if k))
    if not keys:
        return {"errors": ["Kein Vorgang braucht eine Einzelentscheidung."]}
    recs = [r for r in (rec_from_json(json.dumps(d)) for d in json.loads(row["events_json"]))
            if (r.event_key or "") in set(keys)]
    svc = DSM.datasource_service(ctx)
    csv = csv_service(ctx)
    with DSM._INGEST_LOCK:
        present = svc._present(sid, recs)
        fresh = [r for r in recs if (r.event_key or "") not in present]
        if fresh:
            payload = ("[" + ",".join(rec_to_json(r) for r in fresh) + "]").encode()
            bid = csv.ingest(fresh, source=f"sync:{ds.provider}", profile=f"sync:{ds.provider}", account=ds.account,
                             label=f"{ds.name} · Einzelentscheidungen aus dem Vollabruf "
                                   f"{datetime.now(UTC).strftime('%d.%m.%Y %H:%M')}",
                             datasource_id=sid, payload=payload, append_to=svc._appendable(sid))
        else:  # schon vorgelegt: den offenen Stapel mit dem ersten Vorgang öffnen
            hit = db.q1("SELECT r.batch_id FROM csv_row r JOIN csv_batch b ON b.id=r.batch_id WHERE b.kind='sync' AND "
                        "b.datasource_id=? AND b.status IN ('preview','partial') AND r.event_key=? ORDER BY r.batch_id "
                        "DESC LIMIT 1", (sid, keys[0]))
            if hit is None:
                return {"errors": ["Die Vorgänge sind bereits entschieden – bitte die Vorschau neu laden."]}
            bid = int(hit["batch_id"])
    return {"bid": int(bid), "event": keys[0], "n": len(keys), "errors": []}


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
    mode: str = ""  # proof | confirm
    confirm: bool = False  # Vollständigkeit vom Nutzer bestätigt (nur bei mode=confirm wirksam)
    window_from: str | None = None  # Beginn der Quelle: davor bleibt der Import maßgeblich
    before: list[Any] = field(default_factory=list)  # Buchungen vor dem Beginn der Quelle (bleiben)
    delivered: set[str] = field(default_factory=set)  # Assets, für die die Quelle einen Bestand meldet (auch 0)
    counts: dict[str, Any] = field(default_factory=dict)  # API-Vorgänge → ausgewertete Vorgänge → Zeilen
    problems: list[dict[str, Any]] = field(default_factory=list)  # einzeln bearbeitbare Probleme (stabile Kennung)
    breakdown: dict[str, list[tuple[str, Decimal]]] = field(default_factory=dict)  # Zusammensetzung je Asset
    raw_check: dict[str, list[dict[str, str]]] = field(default_factory=dict)  # Bestandsprüfung der Quelle je Kürzel
    gaps: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # Lücken laut Saldoverlauf je Asset
    fillers: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)  # Import-Buchung füllt Lücke (bleibt)
    adjust: frozenset[str] = frozenset()  # Assets, für die der Nutzer den Ausgleich bestätigt hat
    adjustable: dict[str, dict[str, Any]] = field(default_factory=dict)  # möglicher Ausgleich je Asset
    adjustments: list[dict[str, Any]] = field(default_factory=list)  # zu buchende Ausgleichsbuchungen (bestätigt)
    lagged: list[tuple[Any, Any, timedelta]] = field(default_factory=list)  # Sparplan-Einzahlung mit Versatz
    lagged_kept: list[tuple[Any, Any, timedelta]] = field(default_factory=list)  # Transfer ↔ API mit Versatz
    dups: list[dict[str, Any]] = field(default_factory=list)  # in der App erfasst und zugleich in der API
    kept_free: list[Any] = field(default_factory=list)  # bleibende Transfers ohne Gegenstück in der API
    causes: dict[str, dict[str, Any]] = field(default_factory=dict)  # Ursachen einer Bestandsabweichung je Asset
    chain: bool = False  # die Quelle liefert einen Saldoverlauf (Lücken belegbar)
    app_free: list[Any] = field(default_factory=list)  # in der App erfasst, ohne Gegenstück in der Quelle

    @property
    def tier(self) -> str:
        """belegt = Beweis erbracht · bestätigt = vom Nutzer bestätigt, Bestand wird nicht schlechter; mit Ausgleich:
        Restabweichung mit Zustimmung als gekennzeichnete Ausgleichsbuchung."""
        base = "belegt" if self.mode == "proof" else "bestätigt"
        return base + (" mit Ausgleich" if self.adjustments else "")

    @property
    def lag_range(self) -> str:
        """Versatz der zugeordneten Sparplan-Einzahlungen, z. B. „3–5 Tage“."""
        return lag_text([lag for _t, _ln, lag in self.lagged])

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


def _observed(meta: dict[str, Any], resolver: Any, provider: str
              ) -> tuple[dict[str, Decimal], list[str], set[str]]:
    """Beobachteter Bestand je Asset, nicht zugeordnete Kennungen und – getrennt davon – die Assets, für die die Quelle
    überhaupt eine Position meldet (auch mit 0). „Nicht geliefert“ ist nicht dasselbe wie „0 gemeldet“."""
    obs: dict[str, Decimal] = defaultdict(Decimal)
    unmapped: list[str] = []
    delivered: set[str] = set()
    for b in meta.get("balances") or []:
        q = _dec(b.get("qty"))
        aid, how, _key = resolver.resolve_for(str(b["key"]), provider)
        if aid is None:
            if q and how != "ignored":  # als Spam/ignoriert markierte Kennungen zählen nicht
                unmapped.append(str(b["key"]))
            continue
        obs[aid] += q
        delivered.add(aid)
    return {a: q for a, q in obs.items() if q}, unmapped, delivered


def _obs(b: dict[str, Any]) -> str:
    """Beobachteter Bestand für die Anzeige: gemeldete Menge (auch 0) oder „nicht geliefert“."""
    return qty_exact(b["observed"]) if b.get("known", True) else "nicht geliefert"


def _last_refusal(db: Any, row: Any) -> dict[str, Any] | None:
    """Gründe des letzten abgelehnten Übernahmeversuchs je Zeile (``result_json.rows``) samt Datenstand ``dv``."""
    try:
        res = json.loads(row["result_json"] or "{}")
    except ValueError:
        return None
    return res if isinstance(res, dict) and res.get("rows") else None


# Zähleinheit je Anbieter. Nur wo ``coverage.operations`` die Rohobjekte der API zählt und jedes Objekt genau ein
# Ereignis (auch mehrzeilig) oder genau eine begründete Auslassung ergibt, ist die Zählung ein unabhängiger Beleg der
# Vollständigkeit. Bei anderen Quellen ist die Zahl aus den Ereignissen selbst abgeleitet (kein Beleg) – sie wird nur
# angezeigt. Wirtschaftliches Ereignis, technische Operation und Buchungszeile werden nie gegeneinander gerechnet.
COUNT_UNIT = {"bitpanda": "Einträge von /operations (je Eintrag ein Vorgang oder eine begründete Auslassung)"}


def _counts(cov: dict[str, Any], a: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """API-Vorgänge → ausgewertete Vorgänge → Buchungszeilen, mit den bewusst nicht gebuchten dazwischen.

    ``state``: ``ok`` (Zählung erklärt), ``gap`` (unerklärte Differenz), ``unknown`` (Zählung fehlt bzw. gilt nur für
    eine Etappe), ``na`` (Quelle ohne unabhängige Zählung)."""
    ops = cov.get("operations")
    skipped = sum(int(v or 0) for v in (a.skipped or {}).values())
    out: dict[str, Any] = {"api": ops, "events": a.events, "lines": a.lines, "skipped": skipped,
                           "unit": COUNT_UNIT.get(a.provider, ""), "state": "na"}
    if isinstance(ops, int):
        out["explained"] = ops == a.events + skipped
        out["gap"] = ops - a.events - skipped
    if a.provider in COUNT_UNIT:
        rounds = (meta or {}).get("rounds")
        if not isinstance(ops, int) or (isinstance(rounds, int) and rounds > 1):
            out["state"] = "unknown"  # mehrere Etappen: die Zählung beschreibt nur die letzte
        else:
            out["state"] = "ok" if out["gap"] == 0 else "gap"
    return out


def _count_check(c: dict[str, Any]) -> Check | None:
    """Vollständigkeit über die Vorgangszählung der Quelle (nur bei belastbarer Zähleinheit)."""
    label = "Jeder von der API gemeldete Vorgang ist ausgewertet oder begründet ausgelassen"
    if c["state"] == "ok":
        return Check("count", label, True, f"{c['api']} {c['unit']}: {c['events']} ausgewertet, {c['skipped']} "
                                           "begründet ausgelassen")
    if c["state"] == "gap":
        g = c["gap"]
        return Check("count", label, False,
                     f"Die API meldet {c['api']} Vorgänge ({c['unit']}); ausgewertet {c['events']}, begründet "
                     f"ausgelassen {c['skipped']} – " + (f"{g} Vorgang/Vorgänge fehlen ohne Erklärung."
                                                         if g > 0 else f"{-g} mehr ausgewertet als gemeldet.")
                     + " Bitte erneut vollständig abrufen; besteht die Differenz weiter, ist die Auswertung der "
                     "Quelle unvollständig.")
    if c["state"] == "unknown":
        return Check("count", label, False, "Zählung der Quelle unbekannt (nicht geliefert bzw. nur für einen Teil "
                                            "des Abrufs) – nicht als vollständig belegt gewertet.", blocking=False)
    return None


_MAP = re.compile(r"Asset für „([^“]+)“ zuordnen|„([^“]+)“ bei .+ Asset zuordnen")


_FIAT_TOL = Decimal("0.01")


def _tol(aid: str, q: Decimal) -> Decimal:
    """Vergleichsspielraum für Lücke ↔ Import-Buchung: Fiat auf den Cent, sonst sehr eng (relativ)."""
    from app.importer import contract as C

    return _FIAT_TOL if aid.upper() in C.ISO_CURRENCIES else max(DUST, abs(q) * Decimal("1e-9"))


def _window(g: dict[str, Any]) -> str:
    f, t = parse_iso(g.get("from")), parse_iso(g.get("to"))
    fmt = "%d.%m.%Y"
    return (f"{f.strftime(fmt) if f else 'Beginn'} bis {t.strftime(fmt) if t else 'zum Abruf'}")


def _gaps_by_asset(cov: dict[str, Any], resolver: Any, provider: str) -> dict[str, list[dict[str, Any]]]:
    """Lücken laut Saldoverlauf (``coverage.balances.gaps`` der Quelle, je Kürzel) → je Portfolia-Asset."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sym, items in ((cov.get("balances") or {}).get("gaps") or {}).items():
        aid, _how, _key = resolver.resolve_for(str(sym), provider)
        if aid is None:
            continue
        for g in items:
            out[aid].append({"from": g.get("from"), "to": g.get("to"), "amount": _dec(g.get("amount")),
                             "wallet": g.get("wallet") or ""})
    return {k: sorted(v, key=lambda g: (g["to"] or "9999", g["from"] or "")) for k, v in out.items()}


def _fill(gaps: dict[str, list[dict[str, Any]]], unmatched: list[Any], account: str, fetched_at: str
          ) -> list[tuple[Any, dict[str, Any]]]:
    """Import-Buchungen ohne Gegenstück in der API, die eine Lücke im Saldoverlauf füllen: gleiches Asset, gleiche
    Richtung, Zeitpunkt im Fenster der Lücke (± Zuordnungsspielraum) und zusammen nicht mehr als die Lücke. Sie bleiben
    bei der Übernahme bestehen – die Bewegung ist von beiden Seiten belegt (Import: Vorgang, Quelle: Saldosprung)."""
    out: list[tuple[Any, dict[str, Any]]] = []
    used: set[str] = set()
    end = parse_iso(fetched_at) or datetime.now(UTC)
    for aid, gs in gaps.items():
        for g in gs:
            start = parse_iso(g["from"])
            lo = start - WINDOW if start is not None else datetime(1970, 1, 1, tzinfo=UTC)
            hi = (parse_iso(g["to"]) or end) + WINDOW
            rest = g["amount"]
            cands = []
            for t in unmatched:
                q = _tx_legs(t, account).get(aid, ZERO)
                if t.tx_id in used or not q or (q > 0) != (rest > 0) or not (lo <= t.ts <= hi):
                    continue
                cands.append((t, q))
            for t, q in sorted(cands, key=lambda x: (x[0].ts, x[0].tx_id)):
                if abs(q) <= abs(rest) + _tol(aid, rest):
                    out.append((t, g))
                    used.add(t.tx_id)
                    rest -= q
            g["left"] = rest if abs(rest) > _tol(aid, g["amount"]) else ZERO
    return out


def _donate(donors: dict[str, dict[str, Any]], t: Any, ln: Line, account: str) -> None:
    """EUR-Wert, Gebührenwert und Einordnung der Import-Buchung für die API-Zeile, wo die API sie nicht liefert."""
    legs = _tx_legs(t, account)
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


def _plain_deposit(t: Any, account: str) -> bool:
    legs = _tx_legs(t, account)
    return t.type == "deposit" and len(legs) == 1 and next(iter(legs.values())) > 0


def _plan_deposit(ln: Line) -> bool:
    """Sparplan-Einzahlung der API (Zugang ohne Gegenbuchung, Vorgangsart Sparplan)."""
    from app.csvimport import model as M

    plan = "saving" in str(ln.rec.label or "").lower() or "sparplan" in str(ln.rec.note or "").lower()
    return (plan and ln.rec.kind == M.DEPOSIT and not ln.errors and not ln.rec.ts_missing and len(ln.legs) == 1
            and next(iter(ln.legs.values())) > 0)


def _days(td: timedelta) -> str:
    d = td.total_seconds() / 86400
    return f"{d:.1f}".replace(".", ",").removesuffix(",0")


def lag_text(lags: list[timedelta]) -> str:
    if not lags:
        return ""
    lo, hi = _days(min(lags)), _days(max(lags))
    return (lo if lo == hi else f"{lo}–{hi}") + (" Tag" if lo == hi == "1" else " Tage")


_Cand = tuple[datetime, tuple[str, Decimal], Any]  # Zeit, (Asset, Menge), Bezug


def lag_pairs(olds: list[_Cand], news: list[_Cand], max_lag: timedelta = LAG_MAX) -> list[tuple[Any, Any, timedelta]]:
    """Zuordnung mit Zeitversatz (nur vorwärts: Datum der Quelle ≥ Datum des Imports): gleiches Asset, exakt gleicher
    Betrag; ein Paar nur, wenn beide gegenseitig nächste freie Kandidaten sind – die früheste freie API-Zeile nach der
    Import-Buchung und keine weitere freie Import-Buchung bis zu ihr. Mehrdeutiges bleibt ohne Zuordnung."""
    def key(x: tuple[str, Decimal]) -> tuple[str, str]:
        return x[0], format(x[1].normalize(), "f")

    groups_new: dict[tuple[str, str], list[tuple[datetime, Any]]] = defaultdict(list)
    for ts, k, ref in news:
        groups_new[key(k)].append((ts, ref))
    groups_old: dict[tuple[str, str], list[tuple[datetime, Any]]] = defaultdict(list)
    for ts, k, ref in olds:
        groups_old[key(k)].append((ts, ref))
    out = []
    for g, olist in groups_old.items():
        nlist = sorted(groups_new.get(g, []), key=lambda x: x[0])
        olist = sorted(olist, key=lambda x: x[0])
        taken: set[int] = set()
        for j, (ots, oref) in enumerate(olist):
            n = next((k for k, (nts, _r) in enumerate(nlist) if k not in taken and nts >= ots), None)
            if n is None or nlist[n][0] - ots > max_lag:
                continue
            if any(ots < o2 <= nlist[n][0] for o2, _r in olist[j + 1:]):
                continue  # eine spätere Import-Buchung liegt näher – mehrdeutig
            taken.add(n)
            out.append((oref, nlist[n][1], nlist[n][0] - ots))
    return out


def _move_line(ln: Line) -> tuple[str, Decimal] | None:
    """Ein- bzw. Auszahlung der Quelle mit genau einem Asset (Betrag ohne Gebühr) – Kandidat für eine Zuordnung mit
    Zeitversatz."""
    from app.csvimport import model as M

    if ln.errors or ln.rec.ts_missing or ln.rec.kind not in (M.DEPOSIT, M.WITHDRAWAL):
        return None
    legs = ln.base or ln.legs
    return next(iter(legs.items())) if len(legs) == 1 else None


def _move_tx(t: Any, account: str) -> tuple[str, Decimal] | None:
    legs = _tx_legs(t, account, fee=False)
    return next(iter(legs.items())) if len(legs) == 1 else None


def _lag_moves(txs: list[Any], lines: list[Line], taken: set[int], account: str) -> list[tuple[Any, int, timedelta]]:
    """Buchungen ↔ freie Ein-/Auszahlungen der Quelle mit Zeitversatz bis ``LAG_MAX``: erst Quelle später (Gutschrift
    nach dem Datum des Imports), dann Quelle früher – jeweils nur gegenseitig nächste Kandidaten."""
    olds = [(t.ts, k, t) for t in txs if (k := _move_tx(t, account))]
    news = [(ln.rec.ts, k, i) for i, ln in enumerate(lines)
            if i not in taken and ln.covered_by is None and (k := _move_line(ln))]
    fwd = lag_pairs(olds, news)
    done_t, done_i = {id(t) for t, _i, _l in fwd}, {i for _t, i, _l in fwd}
    back = lag_pairs([x for x in news if x[2] not in done_i], [x for x in olds if id(x[2]) not in done_t])
    return fwd + [(t, i, -lag) for i, t, lag in back]


def _dup(db: Any, t: Any, lns: list[Line], lag: timedelta | None) -> dict[str, Any]:
    """In der App erfasste Buchung mit Gegenstück in der Quelle: ausblendbar nur, wenn sie keine Korrektur der
    Diagnose, kein abgeglichener Transfer und kein Teil einer Gruppe ist (sonst dort zurücknehmen)."""
    row = db.q1("SELECT source, group_ref FROM journal_tx WHERE tx_id=?", (t.tx_id,))
    source = str(row["source"]) if row is not None else ""
    hideable = row is not None and not row["group_ref"] and source not in ("diagnose", "transfer")
    why = ("Korrektur aus der Datenqualität – dort zurücknehmen" if source == "diagnose" else
           "abgeglichener Transfer – im Journal auflösen" if source == "transfer" else
           "Teil einer zusammengesetzten Buchung – im Journal bearbeiten" if row is not None and row["group_ref"]
           else "")
    return {"tx": t, "lines": lns, "lag": lag, "hideable": hideable, "why": why, "source": source}


def _causes(a: Any, lines: list[Line], account: str) -> dict[str, dict[str, Any]]:
    """Ursachen einer Bestandsabweichung je Asset (nach ihrer Wirkung auf „danach“): bleibende Transfers ohne
    Gegenstück in der Quelle, in der App erfasste Buchungen ohne bzw. mit Gegenstück (doppelt), nicht buchbare
    Vorgänge der Quelle (fehlen in „danach“) und offene Lücken des Saldoverlaufs (fehlen ebenfalls)."""
    out: dict[str, dict[str, Any]] = {}
    for b in a.balances:
        if b["ok"]:
            continue
        aid = b["asset"]

        def rows(txs: list[Any], aid: str = aid) -> list[dict[str, Any]]:
            return [{"tx": t, "qty": q} for t in txs if (q := _tx_legs(t, account).get(aid))]

        c: dict[str, Any] = {
            "kept_free": rows(a.kept_free),
            "app": rows(a.app_free),
            "dups": [{**d, "qty": q} for d in a.dups if not d["drop"] and (q := _tx_legs(d["tx"], account).get(aid))],
            "unbookable": sum((ln.legs.get(aid, ZERO) for ln in lines if ln.errors), ZERO),
            "gaps": sum((g.get("left", g["amount"]) for g in a.gaps.get(aid, [])), ZERO)}
        explained = (sum((r["qty"] for k in ("kept_free", "app", "dups") for r in c[k]), ZERO)
                     - c["unbookable"] - c["gaps"])
        c["deviation"] = b["after"] - b["observed"]
        c["explained"] = explained
        c["complete"] = bool(explained) and abs(explained - c["deviation"]) <= _tol(aid, c["deviation"])
        if any(c[k] for k in ("kept_free", "app", "dups", "unbookable", "gaps")):
            out[aid] = c
    return out


def _adjustable(a: Any, expected: dict[str, Decimal], observed: dict[str, Decimal],
                parts: dict[str, dict[str, Decimal]]) -> None:
    """Rest je Asset nach Lückenfüllern: Ausgleich auf den beobachteten Bestand anbieten – „belegt“, wenn die
    verbleibenden Lücken des Saldoverlaufs genau die Differenz erklären (Buchung je Lücke zu deren Zeitpunkt), sonst
    „nicht belegt“ (eine Buchung zum Abrufzeitpunkt). Nur mit gemeldetem Bestand der Quelle; gebucht wird nur, wenn
    der Nutzer den Ausgleich für dieses Asset ausdrücklich bestätigt (``a.adjust``)."""
    end = a.fetched_at
    for aid in sorted(set(expected) | set(observed)):
        need = observed.get(aid, ZERO) - expected.get(aid, ZERO)
        if abs(need) <= DUST or (aid not in a.delivered and not observed.get(aid)):
            continue
        left = [g for g in a.gaps.get(aid, []) if g.get("left", g["amount"])]
        total = sum((g.get("left", g["amount"]) for g in left), ZERO)
        if getattr(a, "chain", False):
            # Mit Saldoverlauf: nur der belegte Teil (Lücken) ist ausgleichbar. Ist der Verlauf lückenlos, enthält die
            # API alle Bewegungen – die Abweichung stammt aus bleibenden Buchungen bzw. der Abbildung (a.causes)
            if not left or (total > 0) != (need > 0) or abs(total) > abs(need) + _tol(aid, need):
                continue
            dated, amount = True, total
        else:
            dated = bool(left) and abs(total - need) <= _tol(aid, need)
            amount = need
        items = ([{"asset": aid, "ts": (g["to"] or end), "amount": g.get("left", g["amount"]), "window": _window(g)}
                  for g in left] if dated else
                 [{"asset": aid, "ts": end, "amount": need, "window": "zum Abrufzeitpunkt"}])
        a.adjustable[aid] = {"need": need, "dated": dated, "gaps_total": total, "items": items, "amount": amount,
                             "rest": need - amount}
        if aid in a.adjust:
            a.adjustments += items
            expected[aid] += amount
            key = "Ausgleich: " + ("Lücken laut Saldoverlauf der Quelle" if dated else "nicht belegte Restabweichung")
            parts[aid][key] = parts[aid].get(key, ZERO) + amount


def _problems(a: Any, lines: list[Line], unmapped: list[str], provider: str,
              refusal: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Einzeln bearbeitbare Probleme mit stabiler Kennung (nie gekürzter Meldungstext): fehlende Asset-Zuordnung je
    Anbieter-Kennung, übrige nicht buchbare Vorgänge, Bestandsabweichung je Asset, gleiche Kürzel mit getrennter
    Identität, im letzten Versuch nur mit Einzelentscheidung buchbare Vorgänge, fehlende Bestätigung."""
    from app.csvimport.identity import provider_key

    out: list[dict[str, Any]] = []
    maps: dict[str, dict[str, Any]] = {}
    other: list[Line] = []
    for ln in lines:
        if not ln.errors:
            continue
        m = _MAP.search(ln.errors[0])
        if m is None:
            other.append(ln)
            continue
        raw = (m.group(1) or m.group(2) or "").strip()
        p = maps.setdefault(raw.upper(), {"id": "map:" + provider_key(raw, provider), "kind": "map", "symbol": raw,
                                          "key": provider_key(raw, provider), "events": []})
        p["events"].append({"event": ln.rec.event_key or "", "ext": ln.rec.ext_id or "", "ts": ln.rec.ts,
                            "what": str(ln.rec.label or ln.rec.kind or ""), "line": ln.rec.line})
    for key in unmapped:  # Bestand der Quelle ohne Zuordnung (gleiche Kennung wie bei den Vorgängen)
        raw = key.split(";", 1)[0]
        maps.setdefault(raw.upper(), {"id": "map:" + provider_key(raw, provider), "kind": "map", "symbol": raw,
                                      "key": provider_key(raw, provider), "events": []})["in_balance"] = True
    for p in maps.values():
        n = len(p["events"])
        p["title"] = f"Asset für „{p['symbol']}“ zuordnen"
        p["detail"] = (f"{n} Vorgang{'' if n == 1 else 'e'} der Quelle" if n else "") + (
            " und der gemeldete Bestand" if p.get("in_balance") else "") + " – Portfolia kennt dieses Asset noch nicht."
        out.append(p)
    for ln in other:
        ident = ln.rec.ext_id or ln.rec.event_key or f"zeile:{ln.rec.line}"
        out.append({"id": f"line:{ident}", "kind": "line", "title": "Vorgang nicht buchbar",
                    "detail": ln.errors[0][:200], "events": [{"event": ln.rec.event_key or "", "ext": ln.rec.ext_id
                                                              or "", "ts": ln.rec.ts, "line": ln.rec.line,
                                                              "what": str(ln.rec.label or ln.rec.kind or "")}]})
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for b in a.balances:
        by_name[str(b["name"]).split(" (", 1)[0].upper()].append(b)
    split = {sym: bs for sym, bs in by_name.items() if len(bs) > 1 and any(not b["ok"] for b in bs)}
    for sym, bs in sorted(split.items()):
        total = sum((b["after"] for b in bs), ZERO)
        out.append({"id": f"ident:{sym}", "kind": "ident", "title": f"„{sym}“ unter {len(bs)} Asset-Kennungen",
                    "detail": "Nach der Übernahme zusammen " + qty_exact(total) + " " + sym + " (" + ", ".join(
                        f"{b['asset']}: {qty_exact(b['after'])}" for b in bs) + "). Möglicherweise dieselbe Position "
                    "unter zwei Kennungen – nur bei belegter gleicher Identität zusammenführen.",
                    "assets": [b["asset"] for b in bs]})
    for b in a.balances:
        if b["ok"] or (a.mode == "confirm" and b["not_worse"]):
            continue
        out.append({"id": f"bal:{b['asset']}", "kind": "balance", "asset": b["asset"],
                    "symbol": str(b["name"]).split(" (", 1)[0],
                    "adjust": (getattr(a, "adjustable", None) or {}).get(b["asset"]),
                    "causes": (getattr(a, "causes", None) or {}).get(b["asset"]),
                    "title": f"Bestand {b['name']} passt nicht",
                    "detail": f"jetzt {qty_exact(b['now'])}, nach Übernahme {qty_exact(b['after'])}, laut Quelle "
                              f"{_obs(b)}" + (" – würde negativ" if b["negative"] else ""),
                    "parts": a.breakdown.get(b["asset"], []) if a.breakdown else []})
    out += [_row_problem(x) for x in (refusal or {}).get("rows") or []]
    if a.mode == "confirm" and not a.confirm:
        out.append({"id": "confirm", "kind": "confirm", "title": "Deine Bestätigung fehlt",
                    "detail": "Bestätige in Schritt 2, dass die Quelle die vollständige Historie des Kontos kennt."})
    return out


def _row_problem(x: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"row:{x.get('key')}", "kind": "row", "title": "Vorgang braucht eine Einzelentscheidung",
            "detail": f"{x.get('when', '')} {x.get('what', '')}: {x.get('why', '')}".strip(),
            "events": [{"event": x.get("event", ""), "ext": x.get("key", ""), "line": x.get("line")}]}


def assess(ctx: Any, sid: int, *, plan: bool = True, confirm: bool = False,
           adjust: frozenset[str] | set[str] = frozenset()) -> Assessment | None:
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
    a.mode, a.confirm = mode(prov), bool(confirm)
    cov = meta.get("coverage") or {}
    a.skipped = dict(meta.get("skipped") or {})

    lines, resolver = _lines(ctx, recs, account, prov)
    a.lines = len(lines)
    a.events = len({ln.rec.event_key or ln.rec.ext_id for ln in lines})
    times = sorted(ln.rec.ts for ln in lines if not ln.rec.ts_missing)
    a.first, a.last = (iso(times[0]), iso(times[-1])) if times else (None, None)
    # Zeitraum der Quelle: Buchungen davor (Import, Dateien) bleiben – die Quelle belegt nur, was sie selbst kennt
    cut = times[0] - WINDOW if times else None
    a.window_from = iso(cut) if cut is not None else None
    observed, unmapped, a.delivered = _observed(meta, resolver, prov)
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
        elif cut is not None and t.ts < cut - (timedelta(hours=24) if t.date_only else timedelta(0)):
            a.kept.append((t, "vor dem ersten Ereignis der Quelle – der Import bleibt maßgeblich"))
            a.before.append(t)
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

    def find(t: Any, taken: set[int] | None = None) -> list[int]:
        taken = used if taken is None else taken
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
                if i in taken or ln.rec.ts_missing or ln.covered_by:
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
        hits = [i for i in find(t) if i not in used and lines[i].legs]
        for i in hits:
            used.add(i)
            lines[i].covered_by = t.tx_id
            a.covered += 1
        if not hits:
            a.kept_free.append(t)
    # … auch mit Zeitversatz (SEPA/Sparplan: Gutschrift Tage nach dem Datum des Imports bzw. umgekehrt)
    for t, i, lag in _lag_moves(a.kept_free, lines, used, account):
        used.add(i)
        lines[i].covered_by = t.tx_id
        a.covered += 1
        a.kept_free.remove(t)
        a.lagged_kept.append((t, lines[i], lag))

    # In der App erfasste Buchungen, die die (vollständige) Quelle ebenfalls enthält, zählten doppelt: anzeigen und nur
    # mit Zustimmung je Buchung („drop:<tx_id>“) bei der Übernahme ausblenden
    drops = {x[5:] for x in adjust if x.startswith("drop:")}
    dup_taken: set[int] = set()
    app_free: list[Any] = []
    for t, why in list(a.kept):
        if not why.startswith("in der App erfasst"):
            continue
        hit = [i for i in find(t, dup_taken) if lines[i].legs and not lines[i].errors]
        if not hit:
            app_free.append(t)
            continue
        dup_taken.update(hit)
        a.dups.append(_dup(db, t, [lines[i] for i in hit], None))
    for t, i, lag in _lag_moves(app_free, lines, dup_taken | {j for j, ln in enumerate(lines) if ln.covered_by},
                                account):
        dup_taken.add(i)
        app_free.remove(t)
        a.dups.append(_dup(db, t, [lines[i]], lag))
    a.app_free = app_free
    for d in a.dups:
        d["drop"] = d["tx"].tx_id in drops and d["hideable"]
        if d["drop"]:
            a.kept = [(t, w) for t, w in a.kept if t.tx_id != d["tx"].tx_id]
            cand.append(d["tx"])
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
        for i in hit:
            _donate(donors, t, lines[i], account)
    # Sparplan-Einzahlungen: Import (Datum des Einzugs) ↔ API (Gutschrift, Tage später) – eindeutig, ohne Überkreuzung
    olds = [(t.ts, t) for t in a.unmatched_old if _plain_deposit(t, account)]
    news = [(lines[i].rec.ts, i) for i in range(len(lines))
            if i not in used and lines[i].covered_by is None and _plan_deposit(lines[i])]
    for t, i, lag in lag_pairs([(ts, next(iter(_tx_legs(t, account).items())), t) for ts, t in olds],
                               [(ts, next(iter(lines[i].legs.items())), i) for ts, i in news]):
        a.unmatched_old.remove(t)
        a.matched += 1
        used.add(i)
        matched_new.add(i)
        a.lagged.append((t, lines[i], lag))
        _donate(donors, t, lines[i], account)
    a.donors = donors
    a.unmatched_new = [ln for i, ln in enumerate(lines) if i not in matched_new and ln.covered_by is None
                       and ln.legs and (ln.rec.ext_id or "") not in own_ids]

    # -- Lücken der API (Saldoverlauf der Quelle): passende Import-Buchungen ohne Gegenstück bleiben ------------
    a.gaps = _gaps_by_asset(cov, resolver, prov)
    a.chain = "gaps" in (cov.get("balances") or {})
    a.fillers = _fill(a.gaps, a.unmatched_old, account, a.fetched_at)
    if a.fillers:
        keep_ids = {t.tx_id for t, _g in a.fillers}
        cand = [t for t in cand if t.tx_id not in keep_ids]
        a.unmatched_old = [t for t in a.unmatched_old if t.tx_id not in keep_ids]
        for t, g in a.fillers:
            a.kept.append((t, f"füllt eine Lücke der API ({_window(g)}) – Saldoverlauf der Quelle belegt die "
                              "fehlende Bewegung"))

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
    parts: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for ln in lines:
        what = str(ln.rec.label or ln.rec.kind or "Vorgang")
        for aid, q in ln.legs.items():
            fee = q - ln.base.get(aid, ZERO)
            if ln.errors:
                parts[aid][f"nicht buchbar (zählt nicht): {what}"] += q
            elif ln.covered_by is not None:
                parts[aid][f"durch bleibenden Transfer gedeckt (zählt nicht): {what}"] += q
            else:
                expected[aid] += q
                parts[aid][f"API: {what}" + (" (ohne Gebühr)" if fee else "")] += q - fee
                if fee:
                    parts[aid]["API: Gebühren"] += fee
    for t, why in a.kept:
        if t.tx_id not in own_ids:
            for aid, q in _tx_legs(t, account).items():
                expected[aid] += q
                parts[aid][f"bleibt: {why.split(' – ')[0]}"] += q
    a.adjust = frozenset(adjust)
    _adjustable(a, expected, observed, parts)
    led = ctx.ledger()
    now = led.holdings_by_account(account) if led is not None else {}
    assets = pf.assets if pf is not None else {}
    for aid in sorted(set(expected) | set(observed) | set(now)):
        e, o, n = expected.get(aid, ZERO), observed.get(aid, ZERO), now.get(aid, ZERO)
        if not (e or o or n):
            continue
        name = getattr(assets.get(aid), "symbol", None) or asset_label(aid)
        known = aid in a.delivered or bool(o)
        # Bestätigt: ohne gemeldete Position ist „nicht schlechter“ nur belegt, wenn sich nichts ändert
        not_worse = abs(e - o) <= abs(n - o) + DUST if known or a.mode == "proof" else abs(e - n) <= DUST
        adj = sum((x["amount"] for x in a.adjustments if x["asset"] == aid), ZERO)
        a.balances.append({"asset": aid, "name": name, "now": n, "after": e, "observed": o, "negative": e < -DUST,
                           "ok": abs(e - o) <= DUST, "was_ok": abs(n - o) <= DUST, "known": known,
                           "not_worse": not_worse, "adjusted": adj,
                           "filled": sum((_tx_legs(t, account).get(aid, ZERO) for t, _g in a.fillers), ZERO)})
    a.causes = _causes(a, lines, account)
    names = [b["name"] for b in a.balances]
    for b in a.balances:  # gleiches Kürzel, verschiedene Assets (z. B. PEAQ und PEAQ-Token auf Ethereum)
        if names.count(b["name"]) > 1 and b["name"] != b["asset"]:
            b["name"] = f"{b['name']} ({asset_label(b['asset'])})"
    a.ready = True

    # -- Prüfungen ------------------------------------------------------------------------------------------
    conn = conn_mod.connector_for(prov)
    pag = (cov.get("pagination") or {})
    stale = a.age > MAX_AGE
    bad = [ln for ln in lines if ln.errors]
    proof = a.mode == "proof"
    reported = ("; ".join(meta["gaps"][:3]) or "keine gemeldet") if meta.get("gaps") else "keine gemeldet"
    a.checks = [
        Check("pilot", "Anbieter für die Übernahme zugelassen", supported(prov),
              "Belegt (Seitenende laut API): Bitpanda. Mit Bestätigung des Nutzers: Binance und Blockchain-Wallets."),
        Check("fetch", "Abruf vollständig (Seitenende laut API, keine Lücke)",
              bool(meta.get("complete")) and not meta.get("gaps") and cov.get("mode") == "vollständig"
              and pag.get("end") == "has_next_page=false",
              f"{cov.get('pages', '?')} Seiten, {cov.get('operations', '?')} Vorgänge, Seitenende: "
              f"{pag.get('end') or 'nicht belegt'}" + (f"; Lücken: {'; '.join(meta['gaps'][:3])}" if meta.get("gaps")
                                                       else ""))
        if proof else
        Check("fetch", "Vollständigkeit der Historie bestätigt", a.confirm and not meta.get("error"),
              f"Die Quelle belegt sie nicht selbst (gemeldete Lücken: {reported}; Abruf "
              f"{'vollständig' if meta.get('complete') else 'nicht als vollständig gemeldet'}). Mit der Bestätigung "
              "erklärst du, dass diese Quelle für das Konto die vollständige Historie kennt."),
        Check("parser", "Auswertung der Quelle aktuell", conn is not None and cov.get("parser") == conn.parser_version,
              f"Auswertung v{cov.get('parser')} (aktuell v{getattr(conn, 'parser_version', '?')})"),
        Check("fresh", "Abruf nicht älter als 24 Stunden", not stale,
              f"abgerufen vor {int(a.age.total_seconds() // 3600)} h – der beobachtete Bestand ist ein Zeitpunkt"),
        Check("observed", "Beobachteter Bestand vollständig und zugeordnet",
              meta.get("balances") is not None and bool(meta.get("balances_complete", True)) and not unmapped,
              ("Bestand der API fehlt (Leserecht „Balances“?)" if meta.get("balances") is None else
               "Portfolia kennt diese Tokens der Quelle nicht (noch keinem Asset zugeordnet): "
               + ", ".join(asset_label(k) for k in unmapped[:8]) if unmapped else
               f"{len(observed)} Bestände mit Menge")),
        Check("lines", "Alle Vorgänge sind eindeutig und buchbar", not bad,
              f"{len(bad)} nicht buchbar: " + "; ".join(
                  f"{(ln.rec.event_key or ln.rec.ext_id or '?')[-12:]} {ln.errors[0][:80]}" for ln in bad[:4])
              if bad else f"{a.lines} Zeilen in {a.events} Vorgängen"),
        Check("balance", "Vorgänge der API ergeben den beobachteten Bestand exakt je Asset",
              all(b["ok"] for b in a.balances),
              "; ".join(f"{b['name']}: erklärt {qty_exact(b['after'])}, laut Quelle {_obs(b)}"
                         for b in a.balances if not b["ok"])[:400] or f"{len(a.balances)} Assets stimmen" + (
                  " – davon mit bestätigtem Ausgleich: " + ", ".join(sorted({x["asset"] for x in a.adjustments}))
                  if a.adjustments else ""))
        if proof else
        Check("balance", "Der Bestand weicht danach je Asset nicht stärker vom beobachteten ab als jetzt",
              all(b["not_worse"] for b in a.balances),
              "; ".join(f"{b['name']}: jetzt {qty_exact(b['now'])}, danach {qty_exact(b['after'])}, laut Quelle "
                         f"{_obs(b)}" + (" – würde negativ" if b["negative"] else "")
                         for b in a.balances if not b["not_worse"])[:600]
              or (f"{len(a.balances)} Assets: {sum(1 for b in a.balances if b['ok'])} exakt, "
                  f"{sum(1 for b in a.balances if not b['ok'])} mit unveränderter oder kleinerer Abweichung")),
        Check("connector", "Eigene Bestandsprüfung der Quelle (Vorgänge ↔ /portfolio)",
              bool((cov.get("balances") or {}).get("checked")) and not (cov.get("balances") or {}).get("differences"),
              "; ".join((cov.get("balances") or {}).get("examples") or [])[:300]
              or str((cov.get("balances") or {}).get("note") or "ohne Abweichung"), blocking=False),
    ]
    if plan:
        a.checks.append(Check("plan", "Alle ersetzten Buchungen lassen sich ausblenden",
                              not (a.plan and a.plan.errors), "; ".join(a.plan.errors[:3]) if a.plan else ""))
    # Letzter Übernahmeversuch: Vorgänge, die nur mit Einzelentscheidung buchbar sind. Solange sich der Datenstand nicht
    # geändert hat, scheitert ein neuer Versuch genauso – erst entscheiden (Prüfstapel), dann erneut übernehmen.
    last = _last_refusal(db, row)
    same = False
    if last is not None and plan:
        from app.diagnosis.actions import data_version

        same = last.get("dv") == data_version(db)
        if same:
            why = "; ".join(f"{x.get('when', '')} {x.get('what', '')}: {x['why']}" for x in last["rows"][:4])
            a.checks.append(Check("rows", "Alle Vorgänge ohne Einzelentscheidung buchbar", False,
                                  f"{len(last['rows'])} Vorgänge brauchen eine Entscheidung im Prüfstapel: {why}"))
    a.counts = _counts(cov, a, meta)
    if (cc := _count_check(a.counts)) is not None:
        a.checks.append(cc)
    a.raw_check = dict((cov.get("balances") or {}).get("detail") or {})
    a.breakdown = {aid: sorted(((k, v) for k, v in d.items() if v), key=lambda x: x[0]) for aid, d in parts.items()}
    a.problems = _problems(a, lines, unmapped, prov, last if same else None)
    fp = json.dumps({"s": sid, "r": a.rid, "h": sorted(hide_ids), "b": {k: _fmt(v) for k, v in sorted(
        expected.items())}, "p": a.plan.token if a.plan else "", "d": sorted(a.donors), "m": a.mode,
        "c": a.confirm, "f": sorted(t.tx_id for t, _g in a.fillers),
        "a": [[x["asset"], x["ts"], _fmt(x["amount"])] for x in a.adjustments]}, sort_keys=True)
    a.token = hashlib.sha256(fp.encode()).hexdigest()[:32]
    a.lines_by_ext = {ln.rec.ext_id: ln for ln in lines if ln.rec.ext_id}
    # Probelauf: dieselben Schreibphasen wie die Übernahme auf einer Kopie der Datenbank – erst wenn alle übrigen
    # Bedingungen erfüllt sind (sonst entscheidet ohnehin ein anderer Blocker) und nie während eines Laufs
    if plan and not same and row["status"] == "staged" and a.ok:
        pr = probe(ctx, sid, a, run=sid not in _RUNNING)  # während eines Laufs nur das gespeicherte Ergebnis
        if pr is not None:
            _probe_checks(a, pr)
    return a


# ----------------------------------------------------------------------------------------------------
# Probelauf (rein lesend für die echten Daten)
# ----------------------------------------------------------------------------------------------------

_SANDBOX = threading.local()


def _probe_key(ctx: Any, a: Assessment) -> dict[str, Any]:
    from app.diagnosis.actions import data_version

    return {"rid": a.rid, "token": a.token, "dv": data_version(ctx.db)}


def probe(ctx: Any, sid: int, a: Assessment, run: bool = True) -> dict[str, Any] | None:
    """Probelauf der Übernahme: Ausblenden, Auswerten, vollständiger Buchungsplan, Buchen und Nachprüfung – mit
    denselben Funktionen wie ``apply`` (``_apply_steps``), aber auf einer Kopie der Datenbank, die danach verworfen
    wird. Ergebnis je Vorschau (Token) und Datenstand zwischengespeichert. Kurse kommen aus dem gemeinsamen Kursbestand.

    Ergebnis: ``ok``, ``errors``, ``detail``, ``rows`` (Vorgänge, die nur mit Einzelentscheidung buchbar sind) bzw.
    ``failed`` (Probelauf selbst nicht möglich)."""
    import dataclasses
    import shutil
    import tempfile
    from pathlib import Path

    from app.context import AppContext

    key = _probe_key(ctx, a)
    cache = ctx.db.get_state(f"takeover.probe.{sid}") or {}
    if isinstance(cache, dict) and cache.get("key") == key:
        return dict(cache.get("result") or {})
    if not run:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="probe-", dir=ctx.config.data_dir))
    sb: Any = None
    out: dict[str, Any] = {}
    try:
        ctx.db.backup_to(tmp / "app.sqlite")
        sb = AppContext(dataclasses.replace(ctx.config, data_dir=tmp, scheduler_enabled=False, startup_jobs=False))
        sb.prices, sb.store, sb.valuer = ctx.prices, ctx.store, ctx.valuer  # Kurse: kein zweiter Abruf

        def rollback(why: str, detail: list[str], rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
            out.update(errors=[why], detail=list(detail)[:40], rows=list(rows or [])[:200])
            return {"errors": [why]}

        _SANDBOX.on = True
        st: dict[str, Any] = {"decision_id": None, "action_id": None, "bid": None}
        res = _apply_steps(sb, sid, a, a.rid, _years(sb), "Probelauf", rollback, st)
        if res.get("errors") and not out:
            out.update(errors=list(res["errors"]), detail=[], rows=[])
        out["ok"] = not out.get("errors")
    except Exception as e:
        log.exception("Quellenübernahme %s: Probelauf nicht möglich", sid)
        out = {"ok": False, "failed": True, "errors": [f"Probelauf nicht möglich ({type(e).__name__})"]}
    finally:
        _SANDBOX.on = False
        if sb is not None:
            sb.db.close_thread_conn()
            sb.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    if not out.get("failed"):
        ctx.db.set_state(f"takeover.probe.{sid}", {"key": key, "result": out})
    return out


def _probe_checks(a: Assessment, pr: dict[str, Any]) -> None:
    if pr.get("failed"):
        a.checks.append(Check("probe", "Probelauf der Übernahme", False,
                              "; ".join(pr.get("errors") or []) + " – beim Übernehmen wird erneut geprüft; bei einer "
                              "Ablehnung bleibt nichts zurück.", blocking=False))
        return
    if pr.get("rows"):
        rows = pr["rows"]
        why = "; ".join(f"{x.get('when', '')} {x.get('what', '')}: {x['why']}" for x in rows[:4])
        a.checks.append(Check("rows", "Alle Vorgänge ohne Einzelentscheidung buchbar (Probelauf)", False,
                              f"{len(rows)} Vorgänge brauchen eine Entscheidung: {why}"))
        a.problems += [_row_problem(x) for x in rows]
        return
    a.checks.append(Check("probe", "Probelauf: Ausblenden, Buchen und Nachprüfung gelingen vollständig",
                          bool(pr.get("ok")), "; ".join([*(pr.get("errors") or []), *(pr.get("detail") or [])][:6])
                          or "ohne Befund"))


# ----------------------------------------------------------------------------------------------------
# Wirkung auf realisierte Ergebnisse und Erträge je Steuerjahr (gemessen, nicht simuliert)
# ----------------------------------------------------------------------------------------------------

def _years(ctx: Any) -> dict[int, tuple[Decimal, Decimal]]:
    from app.diagnosis.actions import _per_year

    led = ctx.ledger()
    return _per_year(led) if led is not None else {}


def _year_diff(before: dict[int, tuple[Decimal, Decimal]], after: dict[int, tuple[Decimal, Decimal]]
               ) -> list[dict[str, Any]]:
    out = []
    cent = Decimal("0.01")
    for y in sorted(set(before) | set(after)):
        b, a = before.get(y, (ZERO, ZERO)), after.get(y, (ZERO, ZERO))
        if abs(a[0] - b[0]) >= cent or abs(a[1] - b[1]) >= cent:
            out.append({"year": y, "realized": [format(b[0].quantize(cent), "f"), format(a[0].quantize(cent), "f")],
                        "income": [format(b[1].quantize(cent), "f"), format(a[1].quantize(cent), "f")]})
    return out


# ----------------------------------------------------------------------------------------------------
# Übernehmen
# ----------------------------------------------------------------------------------------------------

STARTED = "Übernahme gestartet – der Beweis wird unmittelbar vor dem Buchen erneut geprüft …"


def note_refusal(db: Any, rid: int, text: str) -> None:
    """Abgelehnten Versuch melden, ohne die fachliche Ablehnungsdiagnose (``rows``/``dv`` des letzten Laufs) zu
    überschreiben: wiederholtes Absenden ändert den fachlichen Stand nicht."""
    row = db.q1("SELECT result_json FROM ds_takeover WHERE id=?", (rid,))
    try:
        old = json.loads(row["result_json"] or "{}") if row is not None else {}
    except ValueError:
        old = {}
    doc = {**(old if isinstance(old, dict) else {}), "refused": text}
    doc.setdefault("detail", [])
    db.x("UPDATE ds_takeover SET message=?, result_json=? WHERE id=?",
         (text[:500], json.dumps(doc, ensure_ascii=False), rid))


def start_apply(ctx: Any, sid: int, token: str, confirm: bool = False,
                adjust: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Übernahme im Hintergrund: Beweis erneut prüfen, dann übernehmen (Fortschritt in ``ds_takeover.message``).

    „Gestartet“ ist kein Erfolg: vor dem Start steht der Laufzustand im Übernahmedatensatz, Ablehnungen (beschäftigte
    Quelle, veraltete Vorschau, fehlender Vollabruf, Prüfungsfehler) werden dort mit nächstem Schritt gespeichert."""
    from app.datasources.service import is_busy

    db = ctx.db
    with _LOCK:
        if busy(sid) or is_busy(sid):
            return {"errors": ["Für diese Datenquelle läuft bereits ein Abruf bzw. eine Übernahme – bitte warten, bis "
                               "er abgeschlossen ist, dann erneut übernehmen."]}
        if needs_recovery(db, sid) is not None:
            return {"errors": [RECOVER_FIRST]}
        r = latest(db, sid)
        if r is None or r["status"] != "staged" or not r["events_json"]:
            return {"errors": ["Kein gespeicherter Vollabruf – bitte zuerst den Vollabruf starten."]}
        rid = int(r["id"])
        _RUNNING.add(sid)
        _msg(db, rid, STARTED)

    def work() -> None:
        try:
            res = apply(ctx, sid, token, confirm, adjust)
            cur = db.q1("SELECT message FROM ds_takeover WHERE id=?", (rid,))
            if res.get("errors") and cur is not None and cur["message"] == STARTED:
                # zurückgegebener Fehler ohne eigene Meldung (z. B. Quelle beschäftigt): sichtbar machen
                note_refusal(db, rid, "Übernahme nicht ausgeführt: " + " ".join(res["errors"])[:400])
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


def apply(ctx: Any, sid: int, token: str, confirm: bool = False,
          adjust: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Quelle übernehmen – nur wenn der Beweis hält und die Vorschau noch gilt; danach Nachprüfung am Bestand, sonst
    vollständige Rücknahme. Rückgabe: ``errors`` (leer bei Erfolg), ``result``."""
    from app.datasources import service as S

    with _LOCK:
        ev = S._acquire(sid)
        if ev is None:
            return {"errors": [S.BUSY_TEXT]}
        try:
            with S._INGEST_LOCK:
                return _apply(ctx, sid, token, confirm, adjust)
        finally:
            S._release(sid)


def _apply(ctx: Any, sid: int, token: str, confirm: bool = False,
           adjust: frozenset[str] = frozenset()) -> dict[str, Any]:
    from app.diagnosis import actions as A

    db = ctx.db
    a = assess(ctx, sid, confirm=confirm, adjust=adjust)
    if a is None or db.q1("SELECT status FROM ds_takeover WHERE id=?", (a.rid,))["status"] != "staged":
        return {"errors": ["Kein abgelegter Vollabruf – bitte zuerst abrufen."]}

    def refuse(text: str) -> dict[str, Any]:
        note_refusal(db, a.rid, text)  # Ablehnungsdiagnose (Zeilenblocker) bleibt erhalten
        return {"errors": [text]}

    if not a.ok:
        return refuse("Der Beweis ist nicht erbracht: " + "; ".join(c.label for c in a.blockers))
    if a.token != token:
        return refuse("Die Vorschau ist nicht mehr aktuell (Daten haben sich geändert) – bitte neu öffnen.")
    rid = a.rid
    years_before = _years(ctx)
    ds_name = (db.q1("SELECT name FROM data_source WHERE id=?", (sid,)) or {"name": "Quelle"})["name"]
    db.x("UPDATE ds_takeover SET status='running', donors_json=?, message=? WHERE id=?",
         (json.dumps(a.donors, ensure_ascii=False), "Buchungen werden ausgeblendet …", rid))
    st: dict[str, Any] = {"decision_id": None, "action_id": None, "bid": None}

    def rollback(why: str, detail: list[str], rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        # maßgeblich sind die in derselben Transaktion wie die Änderung gespeicherten Bezüge (auch wenn eine Methode
        # nach ihrem Commit scheiterte und ihre Kennung nie zurückgab)
        saved = db.q1("SELECT decision_id, action_id, batch_id FROM ds_takeover WHERE id=?", (rid,))
        for key, col in (("decision_id", "decision_id"), ("action_id", "action_id"), ("bid", "batch_id")):
            if st[key] is None and saved is not None and saved[col] is not None:
                st[key] = int(saved[col])
        written = st["decision_id"] is not None or st["action_id"] is not None
        notes = _compensate(ctx, rid, st["decision_id"], st["action_id"], st["bid"])
        res_doc: dict[str, Any] = {"detail": detail[:40], "rollback_errors": notes,
                                   "rolled_back": written and not notes}
        if rows:  # Gründe je Zeile + Datenstand: bei unverändertem Stand sperrt die Vorschau einen neuen Versuch
            res_doc["rows"] = rows[:200]
            res_doc["dv"] = A.data_version(db)
        if notes:  # Teilzustand: Bezüge bleiben gespeichert, die Seite bietet die Wiederherstellung an (recover)
            text = (why + " Rücknahme unvollständig – bitte „Unterbrochene Übernahme zurücknehmen“ ausführen: "
                    + "; ".join(notes))
            db.x("UPDATE ds_takeover SET status='failed', message=?, result_json=? WHERE id=?",
                 (text[:500], json.dumps(res_doc, ensure_ascii=False), rid))
            return {"errors": [text, *detail[:12]]}
        text = why + (" Zurückgenommen – es bleibt nichts davon." if written else " Es wurde nichts gebucht.")
        db.x("UPDATE ds_takeover SET status='staged', message=?, result_json=?, donors_json=NULL, decision_id=NULL, "
             "action_id=NULL, batch_id=NULL WHERE id=?", (text[:500], json.dumps(res_doc, ensure_ascii=False), rid))
        return {"errors": [text, *detail[:12]]}

    try:
        return _apply_steps(ctx, sid, a, rid, years_before, ds_name, rollback, st)
    except Exception as e:  # unerwarteter Fehler nach dem ersten Schreiben: den gespeicherten Stand kompensieren
        log.exception("Quellenübernahme %s: unerwarteter Fehler – Rücknahme", sid)
        return rollback(f"Unerwarteter Fehler bei der Übernahme ({type(e).__name__}).", [])


def _fault(phase: str) -> None:
    """Testhaken: Fehler nach einer Schreibphase einspeisen (``hidden``, ``batch``, ``booked``, ``check``)."""
    if phase in _FAULTS and not getattr(_SANDBOX, "on", False):
        raise RuntimeError(f"eingespeister Fehler nach Phase {phase}")


_FAULTS: set[str] = set()


def _run_actions(db: Any, action_id: int | None, bid: int | None) -> list[int]:
    """Aktive Stapelaktionen eines Übernahmelaufs, neueste zuerst: die gespeicherte Buchungsaktion und alle weiteren
    Aktionen auf dem eigenen Stapel des Laufs (Verknüpfen, Nicht übernehmen, Entscheidungen im Übernahmekontext)."""
    ids = {int(action_id)} if action_id is not None else set()
    if bid is not None:
        ids |= {int(r["id"]) for r in db.q("SELECT id FROM import_action WHERE batch_id=?", (bid,))}
    return sorted((i for i in ids if (db.q1("SELECT status FROM import_action WHERE id=?", (i,)) or
                                      {"status": ""})["status"] == "active"), reverse=True)


def _compensate(ctx: Any, rid: int, decision_id: int | None, action_id: int | None, bid: int | None,
                keep: bool = False) -> list[str]:
    """Geschriebenes in umgekehrter Reihenfolge zurücknehmen: Stapelaktionen des Laufs, Stapel, Ausblendung.

    Idempotent und fortsetzbar: maßgeblich ist der dauerhafte Zustand der Teile (Aktion ``undone``, Stapel gelöscht,
    Entscheidung nicht mehr aktiv) – bereits Zurückgenommenes wird übersprungen, ein erneuter Aufruf setzt beim
    ersten offenen Teil fort. Ohne ``keep`` wird jede erfolgreich zurückgenommene Kennung sofort aus ``ds_takeover``
    gelöscht (Abbruch der Übernahme); mit ``keep`` bleiben die Bezüge als Nachweis (Rückgängig einer angewendeten
    Übernahme). Was nicht gelingt, steht in der Rückgabe."""
    from app.csvimport import batch as B
    from app.csvimport.service import csv_service
    from app.diagnosis import actions as A
    from app.journal.service import journal_service

    db = ctx.db
    csv = csv_service(ctx)
    notes: list[str] = []
    def state(table: str, key: int) -> str:
        return str((db.q1(f"SELECT status FROM {table} WHERE id=?", (key,)) or {"status": ""})["status"])

    for aid in _run_actions(db, action_id, bid):
        try:
            errs = list(B.undo(csv, aid).get("errors") or [])
        except Exception as e:
            log.exception("Quellenübernahme %s: Rücknahme der Stapelaktion %s fehlgeschlagen", rid, aid)
            # nach dem Commit gescheitert (z. B. Nachberechnung)? Dann ist der Teil zurückgenommen
            errs = [] if state("import_action", aid) != "active" else [
                f"Buchungen der Stapelaktion #{aid} nicht zurückgenommen ({type(e).__name__})"]
        if errs:
            notes += errs
            break
    if not notes and action_id is not None and not keep:
        db.x("UPDATE ds_takeover SET action_id=NULL WHERE id=?", (rid,))
    if bid is not None and not notes:
        try:
            if csv.batch(bid) is not None and not csv.discard(bid, rewind=False):
                notes.append(f"Prüf-Stapel #{bid} enthält noch Buchungen und wurde nicht verworfen")
            elif not keep:
                db.x("UPDATE ds_takeover SET batch_id=NULL WHERE id=?", (rid,))
        except Exception as e:
            log.exception("Quellenübernahme %s: Verwerfen des Stapels %s fehlgeschlagen", rid, bid)
            notes.append(f"Prüf-Stapel #{bid} nicht verworfen ({type(e).__name__})")
    if decision_id is not None and not notes:  # Ausblendung erst, wenn die API-Buchungen sicher weg sind
        try:
            done = (db.q1("SELECT status FROM diag_decision WHERE id=?", (decision_id,)) or {"status": ""})["status"]
            errs = [] if done != "active" else list(A.undo(ctx, decision_id).errors)
        except Exception as e:
            log.exception("Quellenübernahme %s: Rücknahme der Ausblendung %s fehlgeschlagen", rid, decision_id)
            errs = [] if state("diag_decision", decision_id) != "active" else [
                f"Ausgeblendete Buchungen (Entscheidung #{decision_id}) nicht wiederhergestellt ({type(e).__name__})"]
        if errs:
            notes += errs
        elif not keep:
            db.x("UPDATE ds_takeover SET decision_id=NULL WHERE id=?", (rid,))
    elif decision_id is not None:
        notes.append(f"Ausgeblendete Buchungen (Entscheidung #{decision_id}) noch nicht wiederhergestellt")
    try:
        journal_service(ctx).after_change()
    except Exception as e:  # pragma: no cover - Nachberechnung holt der nächste Lauf nach
        log.exception("Quellenübernahme %s: Nachberechnung nach Rücknahme fehlgeschlagen", rid)
        notes.append(f"Nachberechnung fehlgeschlagen ({type(e).__name__})")
    return notes


RECOVER_FIRST = ("Eine frühere Übernahme wurde unterbrochen und ist noch nicht zurückgenommen – bitte zuerst "
                 "„Unterbrochene Übernahme zurücknehmen“.")


def needs_recovery(db: Any, sid: int) -> Any:
    """Übernahme mit gespeichertem Teilzustand: abgebrochen (``running`` ohne laufenden Prozess), nach
    unvollständiger Rücknahme (``failed``) bzw. begonnenes, nicht abgeschlossenes Rückgängig (``undoing``) – mit
    mindestens einer Kennung des Geschriebenen."""
    r = db.q1("SELECT * FROM ds_takeover WHERE source_id=? AND status IN ('running', 'failed', 'undoing') AND "
              "(decision_id IS NOT NULL OR action_id IS NOT NULL OR batch_id IS NOT NULL) ORDER BY id DESC LIMIT 1",
              (sid,))
    if r is None or (r["status"] == "running" and sid in _RUNNING):
        return None
    return r


def recover(ctx: Any, sid: int) -> dict[str, Any]:
    """Unterbrochene bzw. unvollständig zurückgenommene Übernahme kompensieren (Wiederanlauf nach Neustart oder
    Fehler): exakt der Stand vor der Übernahme; der Vollabruf bleibt für einen neuen Versuch erhalten."""
    from app.datasources import service as S

    with _LOCK:
        if busy(sid):
            return {"errors": ["Es läuft bereits ein Abruf bzw. eine Übernahme."]}
        r = needs_recovery(ctx.db, sid)
        if r is None:
            return {"errors": ["Keine unterbrochene Übernahme gespeichert."]}
        if r["status"] == "undoing":  # begonnenes Rückgängig einer angewendeten Übernahme: dort fortsetzen
            return undo(ctx, sid)
        ev = S._acquire(sid)
        if ev is None:
            return {"errors": [S.BUSY_TEXT]}
        try:
            with S._INGEST_LOCK:
                notes = _compensate(ctx, int(r["id"]), r["decision_id"], r["action_id"], r["batch_id"])
                if notes:
                    ctx.db.x("UPDATE ds_takeover SET status='failed', message=? WHERE id=?",
                             (("Rücknahme weiterhin unvollständig: " + "; ".join(notes))[:500], r["id"]))
                    return {"errors": notes}
                ctx.db.x("UPDATE ds_takeover SET status='staged', donors_json=NULL, message=? WHERE id=?",
                         ("Unterbrochene Übernahme zurückgenommen – es bleibt nichts davon. Der Vollabruf ist "
                          "erhalten; die Übernahme kann neu geprüft werden.", r["id"]))
                log.info("Quellenübernahme %s: unterbrochener Lauf %s zurückgenommen", sid, r["id"])
                return {"errors": []}
        finally:
            S._release(sid)


def _apply_steps(ctx: Any, sid: int, a: Any, rid: int, years_before: Any, ds_name: str, rollback: Any,
                 st: dict[str, Any]) -> dict[str, Any]:
    """Schreibphasen der Übernahme. Jede Kennung des Geschriebenen (Entscheidung, Stapel, Stapelaktion) steht sofort
    in ``st`` und in ``ds_takeover`` – für die Rücknahme bei Fehlern und nach einer Unterbrechung (``recover``)."""
    from app.csvimport import batch as B
    from app.csvimport.service import csv_service, rec_from_json, rec_to_json
    from app.diagnosis import actions as A
    from app.journal.service import journal_service

    db = ctx.db
    csv = csv_service(ctx)

    # 1. ausblenden -----------------------------------------------------------------------------------------
    if a.plan is not None and a.plan.ops:
        res = A.apply_plan(ctx, a.finding, a.plan, a.plan.token, None, in_tx=lambda c, did: c.execute(
            "UPDATE ds_takeover SET decision_id=? WHERE id=?", (did, rid)))
        if res.errors:
            db.x("UPDATE ds_takeover SET status='staged', message=?, donors_json=NULL WHERE id=?",
                 (res.errors[0][:500], rid))
            return {"errors": res.errors}
        st["decision_id"] = res.decision_id
        db.x("UPDATE ds_takeover SET decision_id=? WHERE id=?", (res.decision_id, rid))
        _fault("hidden")

    # 2. Vorgänge der Quelle auswerten ----------------------------------------------------------------------------
    _msg(db, rid, "API-Vorgänge werden ausgewertet …")
    row = db.q1("SELECT events_json FROM ds_takeover WHERE id=?", (rid,))
    recs = [rec_from_json(json.dumps(d)) for d in json.loads(row["events_json"])]
    recs += _adjust_recs(a)  # bestätigter Ausgleich: gekennzeichnete Zeilen im selben Stapel (gemeinsam rückgängig)
    payload = ("[" + ",".join(rec_to_json(r) for r in recs) + "]").encode()
    prov = a.provider
    try:
        bid = st["bid"] = csv.ingest(recs, source=f"sync:{prov}", profile=f"sync:{prov}", account=a.account,
                         label=f"{ds_name} · Quellenübernahme {datetime.now(UTC).strftime('%d.%m.%Y %H:%M')}",
                         datasource_id=sid, payload=payload, options={"cutoff": ""}, append_to=None,
                         in_tx=lambda c, b: c.execute("UPDATE ds_takeover SET batch_id=? WHERE id=?", (b, rid)))
    except Exception as e:
        log.exception("Quellenübernahme: Auswertung fehlgeschlagen")
        return rollback(f"Auswertung der API-Vorgänge fehlgeschlagen ({type(e).__name__}).", [])
    db.x("UPDATE ds_takeover SET batch_id=? WHERE id=?", (bid, rid))
    _fault("batch")
    rows = csv.rows(bid)
    cover_ext = {e for e, ln in a.lines_by_ext.items() if ln.covered_by}
    book = [rc for rc in rows if rc.status == "new" and rc.row is not None and not rc.errors
            and (rc.rec.ext_id or "") not in cover_ext]
    rest = [rc for rc in rows if rc not in book and rc.open and (rc.rec.ext_id or "") not in cover_ext]

    # 3. buchen -------------------------------------------------------------------------------------------------
    # Die Übernahme ist eine Entscheidung für alle Vorgänge: Zeilen, die der Prüfstapel nur mit Einzelentscheidung bucht
    # (möglicher Transfer, Dublette/Ergänzung, prüfbedürftig …), werden nicht still ausgelassen – vor jeder Buchung
    # abbrechen, mit Grund je Zeile (stabile Kennung), damit sie einzeln entschieden werden können.
    _msg(db, rid, f"{len(book)} Vorgänge werden gebucht …")
    if book:
        idxs = {rc.idx for rc in book}
        p = B.plan(csv, bid, "include", idxs)
        refused = [it for it in p.items if not it.act]
        if refused or not p.todo:
            info = [{"key": it.rc.rec.ext_id or it.rc.rec.event_key or f"zeile:{it.rc.line}",
                     "event": it.rc.rec.event_key or "", "line": it.rc.line,
                     "when": it.rc.rec.ts.astimezone(UTC).strftime("%d.%m.%Y") if not it.rc.rec.ts_missing else "",
                     "what": str(it.rc.rec.label or it.rc.rec.kind or ""), "why": it.note or "nicht buchbar"}
                    for it in refused]
            head = ("Keine API-Zeile ist ohne Einzelentscheidung buchbar" if not p.todo else
                    f"{len(refused)} von {len(book)} API-Vorgängen sind nur mit Einzelentscheidung buchbar")
            return rollback(f"{head} – bitte zuerst im Prüfstapel entscheiden.",
                            [f"Zeile {x['line']} ({x['when']} {x['what']}): {x['why']}" for x in info][:12], info)
        out = B.execute(csv, bid, "include", {i.rc.idx for i in p.todo}, token=B.new_token(),
                        fingerprint=p.fingerprint(), params={"takeover": rid},
                        in_tx=lambda c, aid: c.execute("UPDATE ds_takeover SET action_id=? WHERE id=?", (aid, rid)))
        if out.get("errors"):
            return rollback("Buchen der API-Vorgänge nicht möglich.", list(out["errors"]))
        st["action_id"] = int(out["action_id"])
        db.x("UPDATE ds_takeover SET action_id=? WHERE id=?", (st["action_id"], rid))
        _fault("booked")
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
    _fault("check")
    led = ctx.ledger()
    now = led.holdings_by_account(a.account) if led is not None else {}
    diffs = []
    before = {b["asset"]: b["now"] for b in a.balances}
    for aid in sorted(set(now) | set(a.observed)):
        n, o = now.get(aid, ZERO), a.observed.get(aid, ZERO)
        limit = DUST if a.mode == "proof" else abs(before.get(aid, ZERO) - o) + DUST  # bestätigt: nicht schlechter
        if abs(n - o) > limit:
            diffs.append(f"{asset_label(aid)}: gebucht {qty_exact(n)}, laut Quelle {qty_exact(o)}"
                         + ("" if a.mode == "proof" else f" (vorher {qty_exact(before.get(aid, ZERO))})"))
    stuck = [rc for rc in csv.rows(bid) if rc.open and (rc.rec.ext_id or "") not in cover_ext
             and rc.status not in ("known", "ignored")]
    if diffs:
        detail = diffs[:12] + [f"Zeile {rc.line}: {rc.status} {'; '.join((rc.errors or rc.warnings)[:1])[:120]}"
                               for rc in stuck[:12]]
        return rollback("Nachprüfung fehlgeschlagen: Der Bestand des Kontos weicht "
                        + ("vom beobachteten ab." if a.mode == "proof" else "stärker vom beobachteten ab als vorher."),
                        detail)
    csv.refresh_status(bid)
    exact = all(abs(now.get(aid, ZERO) - a.observed.get(aid, ZERO)) <= DUST for aid in set(now) | set(a.observed))
    result = {"hidden": len(a.hide), "kept": len(a.kept), "booked": len(book), "covered": len(cover_ext),
              "fillers": len(a.fillers),
              "adjusted": [{"asset": x["asset"], "amount": _fmt(x["amount"]), "window": x["window"]}
                           for x in a.adjustments],
              "matched": a.matched, "donors": len(a.donors), "left": len(stuck), "tier": a.tier,
              "before": len(a.before), "window_from": a.window_from,
              "check": {"assets": len(a.balances), "exact": exact},
              "years": _year_diff(years_before, _years(ctx))}
    db.x("UPDATE ds_takeover SET status='applied', applied_at=?, message=?, result_json=? WHERE id=?",
         (_now(), f"Übernommen ({a.tier}): {len(a.hide)} Buchungen ersetzt, {len(book)} API-Vorgänge gebucht"
                  + (f" (davon {len(a.adjustments)} Ausgleichsbuchungen)" if a.adjustments else "")
                  + (f", {len(a.fillers)} Import-Buchungen füllen Lücken der API und bleiben" if a.fillers else "")
                  + ", "
                  + (f"Bestand exakt bestätigt ({len(a.balances)} Assets)." if exact else
                     "Bestand nicht schlechter als vorher (nicht exakt – Abweichung bleibt als Befund sichtbar)."),
          json.dumps(result, ensure_ascii=False), rid))
    log.info("Quellenübernahme %s: %s", sid, result)
    return {"errors": [], "result": result, "unresolved": len(rest)}


def _adjust_recs(a: Any) -> list[Any]:
    """Ausgleichsbuchungen als Vorgänge der Übernahme: Zugang bzw. Abgang ohne Gegenbuchung, zum Zeitpunkt der Lücke
    (belegt) bzw. des Abrufs (nicht belegt), deutlich gekennzeichnet; stabile Kennung je Übernahme, Asset und Lücke."""
    from app.csvimport import model as M
    from app.importer import contract as C

    out = []
    for i, x in enumerate(a.adjustments):
        q, aid = x["amount"], x["asset"]
        ts = parse_iso(x["ts"]) or datetime.now(UTC)
        ts = ts - timedelta(seconds=1) if x["window"] != "zum Abrufzeitpunkt" else ts  # vor dem Vorgang danach
        fiat_eur = aid.upper() == "EUR"
        why = "nicht belegte Restabweichung" if x["window"] == "zum Abrufzeitpunkt" else \
            "Lücke laut Saldoverlauf der Quelle"
        r = M.Rec(line=900_000 + i, ts=ts, kind=M.DEPOSIT if q > 0 else M.WITHDRAWAL,
                  ext_id=f"ausgleich:{a.rid}:{aid}:{i}", label="Ausgleich: Bewegung fehlt in der API",
                  note=f"Quellenübernahme – {why} ({x['window']}); vom Nutzer bestätigt",
                  value=abs(q) if fiat_eur else None, value_ccy="EUR" if fiat_eur else None,
                  class_hint={aid: "fiat"} if aid.upper() in C.ISO_CURRENCIES else {})
        if q > 0:
            r.in_sym, r.in_qty = aid, q
        else:
            r.out_sym, r.out_qty = aid, -q
        out.append(r)
    return out


# ----------------------------------------------------------------------------------------------------
# Rückgängig
# ----------------------------------------------------------------------------------------------------

def undo(ctx: Any, sid: int) -> dict[str, Any]:
    """Übernahme als Ganzes zurücknehmen: zuerst die Stapelaktionen des Laufs (gebuchte API-Vorgänge, Verknüpfungen),
    dann der Stapel, zuletzt die ausgeblendeten Buchungen.

    Fortsetzbar: vor dem ersten Schritt steht der Lauf dauerhaft auf ``undoing``; bereits zurückgenommene Teile
    erkennt die Kompensation an ihrem Zustand. Scheitert ein Schritt (Fehlerergebnis, Ausnahme, Prozessabbruch),
    bleibt ``undoing`` mit allen Bezügen – ein erneuter Aufruf setzt fort. ``undone`` erst, wenn alles zurück ist."""
    from app.datasources import service as S
    from app.diagnosis import actions as A

    db = ctx.db
    with _LOCK:
        r = latest(db, sid)
        if r is None or r["status"] not in ("applied", "undoing"):
            return {"errors": ["Keine aktive Quellenübernahme."]}
        ev = S._acquire(sid)
        if ev is None:
            return {"errors": [S.BUSY_TEXT]}
        try:
            with S._INGEST_LOCK:
                dec = db.q1("SELECT id, status, ops_json FROM diag_decision WHERE id=?", (r["decision_id"],)) \
                    if r["decision_id"] else None
                if dec is not None and dec["status"] == "active":
                    later = A._dependents(db, int(dec["id"]), json.loads(dec["ops_json"] or "[]"))
                    if later:
                        return {"errors": ["Rückgängig nicht möglich: spätere Korrekturen betreffen dieselben "
                                           "Buchungen (" + ", ".join(f"#{i}" for i in later) + "). Bitte zuerst diese "
                                           "zurücknehmen – es wurde nichts geändert."]}
                db.x("UPDATE ds_takeover SET status='undoing', message=? WHERE id=?",
                     ("Rücknahme läuft …", r["id"]))
                notes = _compensate(ctx, int(r["id"]), r["decision_id"], r["action_id"], r["batch_id"], keep=True)
                if notes:
                    text = ("Rücknahme unvollständig – bereits Zurückgenommenes bleibt zurückgenommen; bitte "
                            "„Übernahme zurücknehmen“ erneut ausführen: " + "; ".join(notes))
                    db.x("UPDATE ds_takeover SET message=? WHERE id=?", (text[:500], r["id"]))
                    return {"errors": [text]}
                db.x("UPDATE ds_takeover SET status='undone', undone_at=?, donors_json=NULL, message=? WHERE id=?",
                     (_now(), "Zurückgenommen.", r["id"]))
                return {"errors": [], "notes": []}
        finally:
            S._release(sid)
