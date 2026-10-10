"""Evidenz für fehlende Vorgänge (M29): was liegt vor, was belegt es, was fehlt.

Reihenfolge der Suche je Abweichungsfenster – immer zuerst lokal, nur danach gezielt beim Anbieter:

1. **Prüf-Stapel**: offene Vorgänge (neu, Dublettenverdacht, ungeklärt, unvollständig), die noch nicht gebucht sind.
2. **Recherche-Ergebnisse** (``diag_research_event``): bei einer Datenquelle *nur lesend* abgerufene Vorgänge, die weder
   im Journal noch im Prüf-Stapel stehen – bewusst getrennt von Abrufstand, Prüf-Stapel und Buchungen.
3. **Datenquelle erneut lesen** (:func:`start`): gezielter, abbrechbarer, wiederholbarer Abruf ohne Seiteneffekte auf
   den normalen Abgleich (``DataSourceService.research_fetch``). Ein normaler Sync dient nie als „Recherche“.

Ein Vorgang ist eine **Evidenz** (:class:`Evidence`), kein Buchungsvorschlag: Er wird nach seiner Beleglage eingestuft –
nie nach der Höhe der Differenz, die er erklären würde – und erst durch eine ausdrückliche Einzelfreigabe in den
Prüf-Stapel gelegt bzw. gebucht (siehe :mod:`app.diagnosis.actions`). Fehlende Angaben werden benannt, nicht ergänzt.
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

ZERO = Decimal(0)
OPEN = ("new", "invalid", "unclear", "duplicate")
SOURCE_KIND = {"sync": "Börsen-/Wallet-API", "doc": "Beleg (Dokument)", "csv": "Datei-Import"}
STATUS_TEXT = {"new": "neu – noch nicht gebucht", "duplicate": "von der Importprüfung als mögliche Dublette "
               "eingestuft", "unclear": "von der Importprüfung als ungeklärt eingestuft",
               "invalid": "unvollständig (Pflichtangaben fehlen)", "staged": "bei der Datenquelle gelesen, noch nicht "
               "im Prüf-Stapel"}


@dataclass(frozen=True)
class Evidence:
    """Ein Vorgang aus einer Quelle, der (noch) nicht gebucht ist."""

    kind: str  # row (Prüf-Stapel) | staged (Recherche-Ergebnis)
    ref: str  # row: "<stapel>:<index>", staged: Ereignis-ID
    source: str  # sync:<anbieter> | csv:<profil> | doc:<…>
    ts: datetime
    status: str  # Status der Zeile im Prüf-Stapel bzw. "staged"
    effects: tuple[tuple[str, str, Decimal], ...] = ()  # (Konto, Asset-ID, Bestandswirkung)
    label: str = ""
    flags: tuple[str, ...] = ()  # was die Beleglage einschränkt (fehlende Angaben)
    hashes: tuple[str, ...] = ()
    event_key: str | None = None
    source_id: int | None = None
    batch_id: int | None = None
    idx: int | None = None
    ts_missing: bool = False

    @property
    def id(self) -> str:
        return f"{self.kind}|{self.ref}"

    @property
    def authority(self) -> str:
        return self.source.split(":", 1)[0] if ":" in self.source else "csv"

    @property
    def authority_label(self) -> str:
        return SOURCE_KIND.get(self.authority, "Quelle")

    def effect_on(self, pos: tuple[str, str]) -> Decimal:
        return sum((q for a, b, q in self.effects if (a, b) == pos), ZERO)


def _dec(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except ArithmeticError:
        return None


def effects_of_row(row: dict[str, Any]) -> tuple[tuple[str, str, Decimal], ...]:
    """Bestandswirkung einer Zeile im einheitlichen Buchungsformat (Gebühr am Abgangskonto wie im Ledger)."""
    out: dict[tuple[str, str], Decimal] = {}

    def add(acc: Any, aid: Any, q: Decimal | None) -> None:
        if acc and aid and q:
            out[(str(acc), str(aid))] = out.get((str(acc), str(aid)), ZERO) + q

    fq, tq, fee = _dec(row.get("from_qty")), _dec(row.get("to_qty")), _dec(row.get("fee_qty"))
    add(row.get("from_account"), row.get("from_asset"), -fq if fq else None)
    add(row.get("to_account"), row.get("to_asset"), tq)
    add(row.get("from_account") or row.get("to_account"), row.get("fee_asset"), -fee if fee else None)
    return tuple((a, b, q) for (a, b), q in sorted(out.items()) if q)


def effects_of_rec(rec: Any, account: str, resolve: Any) -> tuple[tuple[str, str, Decimal], ...]:
    """Wie :func:`effects_of_row`, aber aus einer Zeile der Quelle (Symbole → Assets über ``resolve``)."""
    out: dict[tuple[str, str], Decimal] = {}
    acc = rec.account or account
    to_acc = rec.to_account or acc

    def add(a: str | None, sym: str | None, q: Decimal | None) -> None:
        if a and sym and q:
            aid = resolve(sym) or sym
            out[(a, aid)] = out.get((a, aid), ZERO) + q

    add(acc, rec.out_sym, -rec.out_qty if rec.out_qty else None)
    add(to_acc if rec.kind == "transfer" else acc, rec.in_sym, rec.in_qty)
    add(acc, rec.fee_sym, -rec.fee_qty if rec.fee_qty else None)
    return tuple((a, b, q) for (a, b), q in sorted(out.items()) if q)


def flags_of_rec(rec: Any, messages: dict[str, Any] | None = None) -> tuple[str, ...]:
    """Angaben, die dem Vorgang zum vollständigen Beleg fehlen – benannt, nie ergänzt."""
    out: list[str] = []
    if getattr(rec, "ts_missing", False):
        out.append("Zeitpunkt fehlt in der Quelle")
    if getattr(rec, "date_only", False):
        out.append("nur Datum ohne Uhrzeit")
    if getattr(rec, "review", None):
        out.append(f"Quelle verlangt Prüfung: {rec.review}")
    if getattr(rec, "fee_basis", None) == "open":
        out.append("Gebührenbelastung nicht belegt (zusätzlich oder im Betrag enthalten?)")
    if rec.kind in ("review",):
        out.append("Quelle bildet den Vorgang nicht eindeutig ab")
    for e in (messages or {}).get("errors", [])[:4]:
        out.append(str(e))
    return tuple(dict.fromkeys(out))


def _hashes(recs: list[Any]) -> tuple[str, ...]:
    from app.csvimport.events import normalize_hash

    return tuple(sorted({h for h in (normalize_hash(r.txhash) for r in recs if r.txhash) if h}))


def load_rows(db: Any, resolver: Any) -> list[Evidence]:
    """Offene Zeilen aller Prüf-Stapel als Evidenz (eine Abfrage, kein N+1)."""
    from app.csvimport.service import rec_from_json

    ph = ",".join("?" * len(OPEN))
    out: list[Evidence] = []
    for r in db.q(
            f"SELECT r.batch_id, r.idx, r.status, r.decision, r.rec_json, r.row_json, r.messages, r.event_key, "
            f"b.source, b.profile, b.account, b.datasource_id, b.kind FROM csv_row r JOIN csv_batch b ON "
            f"b.id = r.batch_id WHERE b.status IN ('preview', 'partial') AND r.status IN ({ph}) "
            f"ORDER BY r.batch_id, r.idx", OPEN):
        if r["decision"] == "skip":
            continue
        try:
            rec = rec_from_json(r["rec_json"])
            row = json.loads(r["row_json"]) if r["row_json"] else None
            msgs = json.loads(r["messages"] or "{}")
        except (ValueError, TypeError, AssertionError):
            continue
        source = r["source"] or (f"csv:{r['profile']}" if r["profile"] else "csv")
        provider = source.split(":", 1)[1] if source.startswith("sync:") else None

        def resolve(sym: str, provider: str | None = provider) -> str | None:
            if resolver is None:
                return None
            return (resolver.resolve_for(sym, provider)[0] if provider else resolver.resolve(sym)[0])

        effects = effects_of_row(row) if row else effects_of_rec(rec, r["account"] or "", resolve)
        hashes = _hashes([rec])
        out.append(Evidence(
            kind="row", ref=f"{r['batch_id']}:{r['idx']}", source=source, ts=rec.ts, status=r["status"],
            effects=effects, label=rec.label or "", flags=flags_of_rec(rec, msgs), hashes=hashes,
            event_key=r["event_key"], source_id=r["datasource_id"], batch_id=int(r["batch_id"]), idx=int(r["idx"]),
            ts_missing=bool(rec.ts_missing)))
    return out


def load_staged(db: Any, resolver: Any, accounts: dict[int, str], providers: dict[int, str]) -> list[Evidence]:
    """Recherche-Ergebnisse (noch nicht im Prüf-Stapel)."""
    from app.csvimport.service import rec_from_json

    out: list[Evidence] = []
    try:
        rows = db.q("SELECT id, source_id, event_key, ts, run_id, lines_json FROM diag_research_event "
                    "WHERE status='new' ORDER BY ts, id")
    except Exception as e:  # Datenbank vor Migration 21
        log.debug("Recherche-Ergebnisse nicht verfügbar: %s", e)
        return out
    for r in rows:
        sid = int(r["source_id"])
        provider = providers.get(sid)
        try:
            recs = [rec_from_json(json.dumps(d)) for d in json.loads(r["lines_json"])]
        except (ValueError, TypeError, AssertionError):
            continue
        if not recs:
            continue

        def resolve(sym: str, provider: str | None = provider) -> str | None:
            if resolver is None:
                return None
            return resolver.resolve_for(sym, provider)[0] if provider else resolver.resolve(sym)[0]

        eff: dict[tuple[str, str], Decimal] = {}
        flags: list[str] = []
        for rec in recs:
            for a, b, q in effects_of_rec(rec, accounts.get(sid, ""), resolve):
                eff[(a, b)] = eff.get((a, b), ZERO) + q
            flags += flags_of_rec(rec)
        out.append(Evidence(
            kind="staged", ref=r["event_key"], source=f"sync:{provider or '?'}", ts=recs[0].ts, status="staged",
            effects=tuple((a, b, q) for (a, b), q in sorted(eff.items()) if q), label=recs[0].label or "",
            flags=tuple(dict.fromkeys(flags)), hashes=_hashes(recs),
            event_key=r["event_key"], source_id=sid, ts_missing=any(x.ts_missing for x in recs)))
    return out


@dataclass
class Assessment:
    level: str  # Belegt | Stark gestützt | Plausibel | Ungeklärt | Widersprüchlich
    notes: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    similar: list[str] = field(default_factory=list)  # ähnliche vorhandene Buchungen (kein Beweis für eine Dublette)
    booked: bool = False  # bereits gebucht (gleicher Hash bzw. Ereignis) – kein Kandidat


class PositionBookings:
    """Wirksame Buchungen einer Position (Zeit, Wirkung, Kennung) – Nachschlagen in O(log n) statt Durchsuchen."""

    def __init__(self, items: list[tuple[datetime, Any, Decimal, str]]) -> None:
        rows = sorted(((ts, eff, tid) for ts, _d, eff, tid in items), key=lambda r: (r[0], r[2]))
        self.rows = rows
        self.ts = [r[0] for r in rows]
        self.eff = {tid: eff for _ts, eff, tid in rows}

    def near(self, ts: datetime, hours: int = 36) -> list[tuple[Decimal, str]]:
        from datetime import timedelta

        lo = bisect_left(self.ts, ts - timedelta(hours=hours))
        hi = bisect_right(self.ts, ts + timedelta(hours=hours))
        return [(eff, tid) for _ts, eff, tid in self.rows[lo:hi]]


def assess(ev: Evidence, pos: tuple[str, str], book: PositionBookings, booked_keys: set[str],
           by_hash: dict[str, list[str]]) -> Assessment:
    """Beleglage eines Vorgangs für eine Position. Stufen: Belegt (Originalquelle, vollständig, keine Dublette) ·
    Stark gestützt (Quelle vollständig, aber kein Originalbeleg der Börse/Wallet) · Plausibel (Quelle teilweise
    belegt oder ähnliche Buchung vorhanden) · Ungeklärt (Prüfung unklar/unvollständig) · Widersprüchlich (die
    Importprüfung hält den Vorgang für bereits gebucht)."""
    a = Assessment(level="Ungeklärt")
    if ev.event_key and ev.event_key in booked_keys:
        a.booked = True
        a.notes.append("Ereignis ist bereits gebucht (gleiche Ereignis-ID).")
        return a
    want = ev.effect_on(pos)
    same_hash = sorted({t for h in ev.hashes for t in by_hash.get(h, ()) if book.eff.get(t) == want})
    if same_hash:
        a.booked = True
        a.notes.append(f"Gleicher Transaktions-Hash und gleiche Menge wie {', '.join(same_hash[:3])} – bereits "
                       "gebucht (wirtschaftliche Dublette, keine fehlende Buchung).")
        return a
    a.missing = list(ev.flags)
    if ev.status == "duplicate":
        a.level = "Widersprüchlich"
        a.notes.append("Die Importprüfung hält den Vorgang für bereits gebucht – er erklärt die Differenz nur, "
                       "wenn die vorhandene Buchung falsch ist.")
        return a
    if ev.status in ("unclear", "invalid"):
        a.level = "Ungeklärt"
        a.notes.append(STATUS_TEXT[ev.status] + ".")
        return a
    if ev.authority == "sync" and not ev.flags:
        a.level = "Belegt"
        a.notes.append("Originalvorgang der Börsen-/Wallet-Schnittstelle, vollständig und noch nicht gebucht.")
    elif not ev.flags:
        a.level = "Stark gestützt"
        a.notes.append(f"Quelle: {ev.authority_label}, vollständig und noch nicht gebucht – kein Originalbeleg der "
                       "Börse/Wallet.")
    else:
        a.level = "Plausibel"
        a.notes.append("Nur teilweise belegt – gesichert sind nur die vorhandenen Felder; fehlende Angaben werden "
                       "nicht ergänzt.")
    sim = sorted({tid for eff, tid in book.near(ev.ts) if eff == want and want})
    if sim:
        a.similar = sim
        a.notes.append("Ähnliche Buchung(en) vorhanden (" + ", ".join(sim[:3]) + ") – gleicher Betrag in kurzem "
                       "Abstand ist kein Beweis, aber ein Dublettenverdacht; vor einer Übernahme prüfen.")
        if a.level in ("Belegt", "Stark gestützt"):
            a.level = "Plausibel"
    return a


# ----------------------------------------------------------------------------------------------------
# Untersuchungsplan je Fenster
# ----------------------------------------------------------------------------------------------------

CAUSES_MISSING_IN = ("fehlender Zugang (Einzahlung, Reward, Airdrop, Staking-Ertrag)", "fehlender Transfer-Eingang",
                     "falsch abgebildeter Tausch bzw. Token-Umstellung", "fehlende Gebührenbuchung (zu hoher Ledger)")
CAUSES_MISSING_OUT = ("Doppelbuchung desselben Vorgangs", "fehlender Abgang (Auszahlung, Verkauf, Gebühr)",
                      "fehlender Transfer-Ausgang", "falsch abgebildeter Tausch bzw. Token-Umstellung")


@dataclass
class Step:
    key: str
    title: str
    state: str  # erledigt | offen | nicht möglich
    detail: str = ""
    action: tuple[str, str, str] | None = None  # (Beschriftung, URL bzw. POST-Ziel, Art: link | post)
    items: list[str] = field(default_factory=list)


@dataclass
class InvestigationPlan:
    """Strukturierte Untersuchungsreihenfolge eines Fensters – lokal zuerst, Anbieter nur gezielt und lesend."""

    account: str
    asset: str
    start: str
    end: str
    direction: str
    size: Decimal
    platforms: list[str]
    missing_data: list[str]
    causes: tuple[str, ...]
    steps: list[Step]
    source_ids: list[int] = field(default_factory=list)
    win_from: str | None = None
    win_to: str | None = None


def plan_for(ctx: Any, report: Any, tr: Any, seg: Any) -> InvestigationPlan:
    """Untersuchungsplan für ein Fenster (rein lesend, keine Abfrage beim Anbieter)."""
    from app.datasources.service import datasource_service
    from app.diagnosis import live as L

    snap = report.snapshot
    pos = tr.position
    srcs = [s for s in snap.sources if s.account == tr.account]
    diff = seg.end.diff
    direction = ("Referenz > Ledger: es fehlt Bestand in den Buchungen" if diff > 0 else
                 "Referenz < Ledger: die Buchungen enthalten zu viel Bestand")
    start = seg.start.at.isoformat() if seg.start else None
    margin = 3
    wf = (seg.start.at - timedelta(days=margin)).isoformat() if seg.start else None
    wt = (seg.end.at + timedelta(days=margin)).isoformat()
    rows_in = [e for e in (snap.evidence or []) if e.kind == "row" and e.effect_on(pos)
               and (seg.start is None or e.ts >= seg.start.at) and e.ts <= seg.end.at]
    staged = [e for e in (snap.evidence or []) if e.kind == "staged" and e.effect_on(pos)
              and (seg.start is None or e.ts >= seg.start.at) and e.ts <= seg.end.at]
    docs = [e for e in rows_in if e.authority == "doc"]
    steps = [
        Step("rows", "1 · Offene Vorgänge im Prüf-Stapel", "erledigt",
             f"{len(rows_in)} offene Vorgang/Vorgänge dieser Position im Fenster" if rows_in else
             "keine offenen Vorgänge dieser Position im Fenster",
             items=[f"{e.authority_label} · {e.status} · {e.ts:%d.%m.%Y %H:%M} UTC · {e.label}".strip(" ·")
                    for e in rows_in[:8]]),
        Step("docs", "2 · Belege (Dokumente) im Prüf-Stapel", "erledigt",
             f"{len(docs)} Beleg-Vorgang/Vorgänge" if docs else "kein Beleg im Fenster – ein Kontoauszug bzw. eine "
             "Abrechnung kann hochgeladen werden", action=("Beleg importieren", "/documents", "link")),
        Step("staged", "3 · Frühere Recherche-Ergebnisse (lokal gespeichert)", "erledigt",
             f"{len(staged)} gespeicherte(s) Ergebnis(se) im Fenster" if staged else
             "keine gespeicherten Ergebnisse im Fenster")]
    svc = datasource_service(ctx)
    missing: list[str] = []
    ids: list[int] = []
    for s in srcs:
        ds = svc.get(s.id)
        if ds is None or not _supported(ds):
            steps.append(Step(f"src{s.id}", f"4 · {s.provider_label}: erneut lesen", "nicht möglich",
                              "Für diese Datenquelle gibt es keinen automatischen Abruf."))
            continue
        ids.append(s.id)
        last = last_run(ctx.db, s.id)
        d = (f"Letzte Recherche {last['finished_at'] or last['started_at']}: {last['status']}"
             f"{' – ' + last['message'] if last['message'] else ''}" if last else "noch keine Recherche")
        steps.append(Step(
            f"src{s.id}", f"4 · {s.provider_label} ({s.name}): Fenster bei der Datenquelle erneut lesen", "offen", d +
            ". Nur lesend: kein Abrufstand, kein Prüf-Stapel, keine Buchung; Ergebnisse werden getrennt gespeichert "
            "und erst nach Freigabe zum Importkandidaten.", action=("Recherche starten", f"research:{s.id}", "post")))
        if not s.complete:
            missing.append(f"{s.provider_label}: Abruf unvollständig bzw. mit Lücken ({s.state})")
        missing += [f"{s.provider_label}: {g}" for g in s.gaps[:3]]
    if not srcs:
        missing.append("keine Börsen-/Wallet-Anbindung für dieses Konto")
    link = L.explorer_links(ctx).get(tr.account)
    steps.append(Step("explorer", "5 · Blockchain-Explorer (öffnest du selbst)", "offen" if link else "nicht möglich",
                      "Portfolia ruft Explorer nicht ab und überträgt keine Adressen an öffentliche Dienste. "
                      "Transaktionen lassen sich anhand der bekannten Adresse bzw. Hashes selbst nachsehen."
                      if link else "Kein Wallet mit bekannter Adresse für dieses Konto.",
                      action=("Explorer ↗", link, "link") if link else None))
    return InvestigationPlan(
        account=tr.account, asset=tr.asset, start=start or "Beginn der Historie", end=seg.end.at.isoformat(),
        direction=direction, size=abs(diff), platforms=sorted({s.provider_label for s in srcs}),
        missing_data=missing, causes=CAUSES_MISSING_IN if diff > 0 else CAUSES_MISSING_OUT, steps=steps,
        source_ids=ids, win_from=wf, win_to=wt)


def _supported(ds: Any) -> bool:
    return bool(getattr(ds, "supported", False))


# ----------------------------------------------------------------------------------------------------
# Recherchelauf (lesend, abbrechbar, wiederholbar)
# ----------------------------------------------------------------------------------------------------

def last_run(db: Any, sid: int) -> Any:
    try:
        return db.q1("SELECT * FROM diag_research WHERE source_id=? ORDER BY id DESC LIMIT 1", (sid,))
    except Exception:  # Datenbank vor Migration 21
        return None


def runs(db: Any, limit: int = 20) -> list[Any]:
    try:
        return db.q("SELECT * FROM diag_research ORDER BY id DESC LIMIT ?", (limit,))
    except Exception:
        return []


def _stale(db: Any) -> None:
    """Läufe, die nach einem Neustart als „läuft“ stehen geblieben sind, als abgebrochen kennzeichnen."""
    from app.datasources.service import is_busy

    for r in db.q("SELECT id, source_id FROM diag_research WHERE status='running'"):
        if not is_busy(int(r["source_id"])):
            db.x("UPDATE diag_research SET status='cancelled', message=COALESCE(message, '') || ' (Lauf nicht mehr "
                 "aktiv)', finished_at=? WHERE id=? AND status='running'", (_now(), r["id"]))


def _now() -> str:
    from datetime import UTC

    return datetime.now(UTC).isoformat()


def _touches(recs: list[Any], asset: str | None, resolver: Any, provider: str) -> bool:
    if not asset:
        return True
    for r in recs:
        for sym in (r.in_sym, r.out_sym, r.fee_sym):
            if not sym:
                continue
            aid = resolver.resolve_for(sym, provider)[0] if resolver is not None else None
            if (aid or sym).upper() == asset.upper():
                return True
    return False


def run(ctx: Any, sid: int, *, account: str | None = None, asset: str | None = None, win_from: str | None = None,
        win_to: str | None = None, resume: int | None = None, rounds: int | None = None,
        seconds: float | None = None) -> dict[str, Any]:
    """Einen Recherchelauf ausführen (blockierend). Liest bei der Datenquelle ausschließlich, legt neue Ereignisse im
    Fenster idempotent als Recherche-Ergebnis ab (``UNIQUE(source_id, event_key)``) und gleicht nur lesend mit
    Journal und Prüf-Stapel ab. Wiederholen erzeugt keine doppelten Kandidaten; ein abgebrochener bzw. teilweiser
    Lauf lässt sich mit ``resume`` (Lauf-ID) ab dem gespeicherten Fortsetzungspunkt fortsetzen."""
    import time as _time
    from datetime import UTC

    from app.csvimport.service import SymbolResolver, csv_service, rec_to_json
    from app.datasources.service import RESEARCH_ROUNDS, RESEARCH_SECONDS, datasource_service

    db = ctx.db
    svc = datasource_service(ctx)
    ds = svc.get(sid)
    if ds is None:
        return {"error": "Datenquelle nicht gefunden."}
    cursor = None
    run_id = resume
    prev = db.q1("SELECT * FROM diag_research WHERE id=?", (resume,)) if resume else None
    if prev is not None:
        if prev["source_id"] != sid or prev["status"] not in ("partial", "cancelled"):
            return {"error": "Dieser Lauf lässt sich nicht fortsetzen."}
        cursor = json.loads(prev["cursor_json"]) if prev["cursor_json"] else None
        account, asset = account or prev["account"], asset or prev["asset"]
        win_from, win_to = win_from or prev["win_from"], win_to or prev["win_to"]
        db.x("UPDATE diag_research SET status='running', finished_at=NULL WHERE id=?", (resume,))
    else:
        cur = db.x("INSERT INTO diag_research(source_id, account, asset, win_from, win_to, status, started_at) "
                   "VALUES (?,?,?,?,?, 'running', ?)", (sid, account, asset, win_from, win_to, _now()))
        run_id = int(cur.lastrowid)
    stamp = [0.0]

    def progress(stage: str, done: int, total: int | None, text: str) -> None:
        if _time.monotonic() - stamp[0] > 1.0:
            stamp[0] = _time.monotonic()
            db.x("UPDATE diag_research SET message=? WHERE id=?",
                 (f"{stage}: {text} ({done}{'/' + str(total) if total else ''})"[:300], run_id))

    res = svc.research_fetch(sid, cursor, rounds=rounds or RESEARCH_ROUNDS, seconds=seconds or RESEARCH_SECONDS,
                             progress=progress)
    if res.get("busy"):
        db.x("DELETE FROM diag_research WHERE id=? AND status='running' AND rounds=0 AND events=0", (run_id,))
        return {"error": res["error"], "busy": True}
    recs = res.get("recs") or []
    by_event: dict[str, list[Any]] = {}
    for r in recs:
        by_event.setdefault(r.event_key or r.ext_id or "", []).append(r)
    lo = datetime.fromisoformat(win_from) if win_from else None
    hi = datetime.fromisoformat(win_to) if win_to else None
    csv = csv_service(ctx)
    resolver = SymbolResolver(csv.known_assets(), csv.saved_symbols())
    keys = [k for k in by_event if k]
    booked: set[str] = set()
    for i in range(0, len(keys), 500):
        part = keys[i:i + 500]
        ph = ",".join("?" * len(part))
        booked.update(r[0] for r in db.q(f"SELECT event_key FROM journal_tx WHERE status IN ('active', 'merged') AND "
                                         f"event_key IN ({ph})", part))
        booked.update(r[0] for r in db.q(f"SELECT a.key FROM journal_event_alias a JOIN journal_tx t ON "
                                         f"t.tx_id = a.tx_id WHERE t.status IN ('active', 'merged') AND a.key IN "
                                         f"({ph})", part))
    present = set(svc._present(sid))
    ignored = set(csv.decisions(set(keys)))
    new = already = waiting = skipped_ignored = out_of_window = 0
    stamp_now = _now()
    with db.transaction() as c:
        for key, lines in by_event.items():
            ts = min(r.ts for r in lines).astimezone(UTC)
            if (lo is not None and ts < lo) or (hi is not None and ts > hi) \
                    or not _touches(lines, asset, resolver, ds.provider):
                out_of_window += 1
                continue
            if key in booked:
                already += 1
                continue
            if key in present:
                waiting += 1
                continue
            if key in ignored:
                skipped_ignored += 1
                continue
            cur = c.execute("INSERT OR IGNORE INTO diag_research_event(source_id, event_key, ts, run_id, lines_json, "
                            "status, created_at) VALUES (?,?,?,?,?, 'new', ?)",
                            (sid, key, ts.isoformat(), run_id,
                             json.dumps([json.loads(rec_to_json(r)) for r in lines], ensure_ascii=False), stamp_now))
            new += cur.rowcount
    complete = bool(res.get("complete")) and not res.get("error")
    status = "error" if res.get("error") and not res.get("cancelled") else "cancelled" if res.get("cancelled") else \
        "done" if complete else "partial"
    parts = [f"{len(by_event)} Ereignis(se) gelesen", f"{new} neu abgelegt"]
    if already:
        parts.append(f"{already} bereits gebucht")
    if waiting:
        parts.append(f"{waiting} warten schon im Prüf-Stapel")
    if skipped_ignored:
        parts.append(f"{skipped_ignored} bewusst ignoriert (nicht aufgenommen)")
    if out_of_window:
        parts.append(f"{out_of_window} außerhalb von Fenster/Asset")
    if res.get("error"):
        parts.append(str(res["error"]))
    parts += [str(g) for g in (res.get("gaps") or [])[:3]] + [str(w) for w in (res.get("warnings") or [])[:2]]
    if status == "partial":
        parts.append("Abruf unvollständig – Fortsetzung möglich")
    db.x("UPDATE diag_research SET status=?, rounds=rounds+?, events=events+?, staged=staged+?, complete=?, "
         "message=?, cursor_json=?, finished_at=? WHERE id=?",
         (status, int(res.get("rounds", 0)), len(by_event), new, int(complete), "; ".join(parts)[:900],
          json.dumps(res["cursor"]) if res.get("cursor") and status in ("partial", "cancelled") else None, _now(),
          run_id))
    return {"run_id": run_id, "status": status, "new": new, "already": already, "events": len(by_event),
            "message": "; ".join(parts), "error": res.get("error")}


def start(ctx: Any, sid: int, **kw: Any) -> dict[str, Any]:
    """Recherchelauf im Hintergrund starten (kehrt sofort zurück; Abbrechen über die Datenquelle)."""
    import threading

    from app.datasources.service import datasource_service, is_busy

    if datasource_service(ctx).get(sid) is None:
        return {"error": "Datenquelle nicht gefunden."}
    if is_busy(sid):
        from app.datasources.service import BUSY_TEXT

        return {"error": BUSY_TEXT, "busy": True}
    _stale(ctx.db)

    def work() -> None:
        try:
            run(ctx, sid, **kw)
        except Exception:  # Absicherung: ein Lauf darf nie als „läuft“ stehen bleiben
            log.exception("Recherche für Datenquelle %s fehlgeschlagen", sid)
            ctx.db.x("UPDATE diag_research SET status='error', message='interner Fehler', finished_at=? WHERE "
                     "source_id=? AND status='running'", (_now(), sid))

    threading.Thread(target=work, name=f"diag-research-{sid}", daemon=True).start()
    return {"started": True}


def cancel(ctx: Any, sid: int) -> str:
    from app.datasources.service import datasource_service

    return datasource_service(ctx).cancel(sid)
