"""Kettenbestand-Abgleich: wann weicht der Bestand der Blockchain vom Ledger ab? (Halbierung bis zum Sprung)

Für ein Konto und sein natives Asset wird die Differenz ``r(t) = Bestand laut Kette(t) − Ledger(t)`` an wenigen
Zeitpunkten gemessen (:mod:`app.datasources.chainhistory`: Archiv-RPC, kostenlos). Ändert sich ``r`` zwischen zwei
Messpunkten, wird das Zeitintervall halbiert – größte Änderung zuerst –, bis es nur noch einen Block je Netz umfasst.
Dieser Block ist der **Bestandssprung ohne passende Buchung**: ein Zugang (oder Abgang), den Anbieter oder Import nicht
geliefert haben (z. B. interne EVM-Überweisungen, Verlauf vor der Rückblick-Grenze einer kostenlosen Route).

Grenzen (bewusst, nie geraten)
    * Gemessen wird der Bestand, nicht die Transaktion: ohne Gegenadresse, Hash und Art. Mehrere Vorgänge im
      selben Block erscheinen als ein Sprung.
    * Heben sich zwei Sprünge zwischen zwei Messpunkten genau auf, bleiben sie unentdeckt (Raster: 12 Intervalle).
    * Differenzen unter ``min_step`` (Gebührenrauschen, Staub) werden nicht verfolgt; sie stehen als Rest.
    * Ein Knoten ohne Archiv liefert keinen alten Stand: das Ergebnis nennt dann den Grund und bricht ab.

Der Ledger ändert sich durch Buchungen, die Kette nicht: deshalb werden nur **Kettenwerte** zwischengespeichert
(``app_state``); nach einer Buchung zeigt ein erneuter Lauf ohne Abrufe, dass der Sprung erledigt ist.
"""

from __future__ import annotations

import heapq
import logging
import time
from bisect import bisect_right, insort
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from typing import Any

from app.datasources import connector as K
from app.datasources.chainhistory import NoArchive, Reader, Unsupported, readers_for
from app.diagnosis.engine import _effect
from app.progress import job_progress
from app.util.timeutil import iso

log = logging.getLogger(__name__)
JOB = "chain_scan"
LABEL = "Kettenbestand"
GRID = 12  # Intervalle des Anfangsrasters
MIN_STEP = Decimal("0.001")
MAX_STEPS = 20
MAX_CALLS = 400  # Abfragen je Lauf (bei 1 Anfrage/s rund 7 Minuten); danach „Weiter prüfen“
SAFE_DEPTH = 30  # Blöcke unterhalb der Spitze (Umordnungen)
CACHE_LIMIT = 6000
ZERO = Decimal(0)


class Budget(Exception):
    """Abfragebudget oder Zeitlimit des Laufs erreicht – geordnet beenden."""


def state_key(account: str, asset: str) -> str:
    return f"chainscan:{account}|{asset}"


def _ts(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


# ----------------------------------------------------------------------------------------------------
# Ledger-Seite
# ----------------------------------------------------------------------------------------------------

class LedgerSeries:
    """Bestandswirkung der Buchungen eines Kontos/Assets mit Präfixsummen (Bestand zu jedem Zeitpunkt)."""

    def __init__(self, txs: Any, account: str, asset: str) -> None:
        items: list[tuple[int, str, Decimal]] = []
        for t in txs:
            d = _effect([t]).get((account, asset))
            if d:
                items.append((int(t.ts.timestamp()), t.tx_id, d))
        items.sort(key=lambda x: (x[0], x[1]))
        self.items = items
        self.at_keys = [x[0] for x in items]
        self.cum = [ZERO]
        for it in items:
            self.cum.append(self.cum[-1] + it[2])

    def at(self, epoch: int) -> Decimal:
        return self.cum[bisect_right(self.at_keys, epoch)]

    def between(self, a: int, b: int) -> list[tuple[int, str, Decimal]]:
        """Buchungen mit ``a < Zeit ≤ b``."""
        return [it for it in self.items if a < it[0] <= b]


# ----------------------------------------------------------------------------------------------------
# Block ↔ Zeit
# ----------------------------------------------------------------------------------------------------

class Clock:
    """Letzter Block mit Zeit ≤ t (Interpolation, mit Halbierung als Rückfall); bekannte Blöcke kosten keine Abfrage."""

    def __init__(self, reader: Reader, tip: int) -> None:
        self.r, self.tip = reader, tip
        self.pts: list[tuple[int, int]] = []  # (Zeit, Block)
        for b in sorted(reader.known_times()):
            insort(self.pts, (reader.known_times()[b], b))
        self.first = self._epoch(1)
        self.last = self._epoch(tip)

    def _epoch(self, block: int) -> int:
        t = int(self.r.time(block).timestamp())
        insort(self.pts, (t, block))
        return t

    def block_at(self, t: int) -> int:
        if t <= self.first:
            return 1
        if t >= self.last:
            return self.tip
        i = bisect_right(self.pts, (t, 1 << 62))
        lo_t, lo_b = self.pts[i - 1] if i else (self.first, 1)
        hi_t, hi_b = self.pts[i] if i < len(self.pts) else (self.last, self.tip)
        n = 0
        while hi_b - lo_b > 1:
            n += 1
            if n % 3 == 0 or hi_t <= lo_t:
                est = (lo_b + hi_b) // 2
            else:
                est = lo_b + int((t - lo_t) * (hi_b - lo_b) / (hi_t - lo_t))
            est = min(max(est, lo_b + 1), hi_b - 1)
            tm = self._epoch(est)
            if tm <= t:
                lo_t, lo_b = tm, est
            else:
                hi_t, hi_b = tm, est
        return lo_b


# ----------------------------------------------------------------------------------------------------
# Ergebnis
# ----------------------------------------------------------------------------------------------------

@dataclass
class Step:
    """Sprung der Differenz zwischen zwei benachbarten Blöcken."""

    at: datetime  # Zeit des ersten Blocks mit dem neuen Stand
    delta: Decimal  # ungeklärter Teil (Kette − Ledger)
    chain_delta: Decimal
    blocks: dict[str, int]  # Netz → erster Block mit dem neuen Stand (bzw. Intervallende)
    links: dict[str, str] = field(default_factory=dict)
    ledger_tx: list[str] = field(default_factory=list)  # Buchungen im selben Zeitfenster (Ledger-Seite)
    kind: str = "chain"  # chain = Kette bewegt sich ohne Buchung · ledger = Buchung ohne Kettenbewegung

    def to_json(self) -> dict[str, Any]:
        return {"at": iso(self.at), "delta": str(self.delta), "chain_delta": str(self.chain_delta),
                "blocks": self.blocks, "links": self.links, "ledger_tx": self.ledger_tx, "kind": self.kind}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Step:
        return cls(datetime.fromisoformat(d["at"]), Decimal(d["delta"]), Decimal(d["chain_delta"]),
                   dict(d.get("blocks") or {}), dict(d.get("links") or {}), list(d.get("ledger_tx") or []),
                   d.get("kind", "chain"))


@dataclass
class Result:
    ok: bool = False
    error: str = ""  # Grund, warum nicht geprüft werden konnte (z. B. Knoten ohne Archiv)
    complete: bool = False  # alle Intervalle mit Änderung eingegrenzt
    steps: list[Step] = field(default_factory=list)
    pending: int = 0  # noch nicht eingegrenzte Intervalle (Budget)
    small: int = 0  # Intervalle unter min_step
    start: datetime | None = None
    end: datetime | None = None
    r_end: Decimal = ZERO  # Differenz am Ende (Kette − Ledger)
    chain_end: Decimal = ZERO
    asset: str = ""
    nets: list[str] = field(default_factory=list)
    calls: int = 0
    checked_at: str = ""
    min_step: Decimal = MIN_STEP
    ledger_total: Decimal = ZERO  # Ledger-Bestand beim Lauf; weicht er später ab, ist das Ergebnis veraltet

    @property
    def residual(self) -> Decimal:
        """Differenz am Ende, die kein gefundener Sprung erklärt."""
        return self.r_end - sum((s.delta for s in self.steps), ZERO)

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "error": self.error, "complete": self.complete,
                "steps": [s.to_json() for s in self.steps], "pending": self.pending, "small": self.small,
                "start": iso(self.start) if self.start else "", "end": iso(self.end) if self.end else "",
                "r_end": str(self.r_end), "chain_end": str(self.chain_end), "asset": self.asset, "nets": self.nets,
                "calls": self.calls, "checked_at": self.checked_at, "min_step": str(self.min_step),
                "ledger_total": str(self.ledger_total)}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Result:
        return cls(bool(d.get("ok")), str(d.get("error") or ""), bool(d.get("complete")),
                   [Step.from_json(s) for s in d.get("steps") or []], int(d.get("pending") or 0),
                   int(d.get("small") or 0),
                   datetime.fromisoformat(d["start"]) if d.get("start") else None,
                   datetime.fromisoformat(d["end"]) if d.get("end") else None,
                   Decimal(d.get("r_end") or "0"), Decimal(d.get("chain_end") or "0"), str(d.get("asset") or ""),
                   list(d.get("nets") or []), int(d.get("calls") or 0), str(d.get("checked_at") or ""),
                   Decimal(d.get("min_step") or str(MIN_STEP)), Decimal(d.get("ledger_total") or "0"))


# ----------------------------------------------------------------------------------------------------
# Suche
# ----------------------------------------------------------------------------------------------------

class _Eval:
    __slots__ = ("blocks", "chain", "ledger", "t")

    def __init__(self, t: int, blocks: tuple[int, ...], chain: Decimal, ledger: Decimal) -> None:
        self.t, self.blocks, self.chain, self.ledger = t, blocks, chain, ledger

    @property
    def r(self) -> Decimal:
        return self.chain - self.ledger


def scan(readers: list[Reader], ledger: LedgerSeries, *, min_step: Decimal = MIN_STEP, max_calls: int = MAX_CALLS,
         max_steps: int = MAX_STEPS, deadline: float | None = None, grid: int = GRID,
         on_progress: Callable[[int, str], None] | None = None) -> Result:
    """Sucht Sprünge von ``Kette − Ledger`` (rein lesend); Kettenwerte bleiben in den Lesern zwischengespeichert."""
    out = Result(asset=readers[0].asset if readers else "", nets=[r.label for r in readers], min_step=min_step)
    base = sum(r.calls for r in readers)
    cache: dict[int, _Eval] = {}

    def calls() -> int:
        return sum(r.calls for r in readers) - base

    def report(text: str) -> None:
        if on_progress is not None:
            on_progress(calls(), text)

    def check() -> None:
        if calls() >= max_calls or (deadline is not None and time.monotonic() > deadline):
            raise Budget

    try:
        tips = [max(r.tip() - SAFE_DEPTH, 2) for r in readers]
        clocks = [Clock(r, tp) for r, tp in zip(readers, tips, strict=True)]
    except NoArchive as e:
        out.error = str(e)
        return out
    t0 = max(c.first for c in clocks)
    t1 = min(c.last for c in clocks)
    if t1 <= t0:
        out.error = "Zu wenige Blöcke für einen Abgleich."
        return out
    b0, b1 = clocks[0].block_at(t0), clocks[0].block_at(t1)  # gemessen wird an Blöcken des ersten Netzes
    if b1 <= b0:
        out.error = "Zu wenige Blöcke für einen Abgleich."
        return out
    out.start, out.end = _ts(t0), _ts(t1)

    def evaluate(b: int) -> _Eval:
        if b in cache:
            return cache[b]
        check()
        t = int(readers[0].time(b).timestamp())
        blocks = (b, *(c.block_at(t) for c in clocks[1:]))  # weitere Netze: letzter Block bis zu dieser Zeit
        chain = sum((r.balance(bb) for r, bb in zip(readers, blocks, strict=True)), ZERO)
        ev = _Eval(t, blocks, chain, ledger.at(t))
        cache[b] = ev
        report(f"Bestand am {_ts(t):%d.%m.%Y %H:%M} UTC")
        return ev

    heap: list[tuple[Decimal, int, int, int]] = []  # (-|Δr|, Zähler, a, b) – a, b: Blöcke des ersten Netzes
    counter = 0

    def push(a: _Eval, b: _Eval) -> None:
        nonlocal counter
        d = abs(b.r - a.r)
        if d == 0:
            return
        counter += 1
        heapq.heappush(heap, (-d, counter, a.blocks[0], b.blocks[0]))

    budget_hit = False
    try:
        pts = sorted({b0 + (b1 - b0) * k // grid for k in range(grid + 1)})
        evs = [evaluate(b) for b in pts]
        out.chain_end = evs[-1].chain
        out.r_end = evs[-1].r
        for a, b in pairwise(evs):
            push(a, b)
        while heap:
            if len(out.steps) >= max_steps:
                break
            neg, _n, ba, bb = heap[0]
            if -neg < min_step:
                break
            if bb - ba <= 1:
                heapq.heappop(heap)
                out.steps.append(_step(readers, ledger, cache[ba], cache[bb]))
                continue
            m = evaluate(ba + (bb - ba) // 2)  # darf Budget auslösen – das Intervall bleibt dann auf dem Heap
            heapq.heappop(heap)
            push(cache[ba], m)
            push(m, cache[bb])
    except Budget:
        budget_hit = True
    except NoArchive as e:
        out.error = str(e)
        return out
    except K.ConnectorError as e:
        out.error = e.message
        return out
    out.pending = sum(1 for neg, *_ in heap if -neg >= min_step) or (1 if budget_hit and not heap else 0)
    out.small = sum(1 for neg, *_ in heap if -neg < min_step)
    out.steps.sort(key=lambda s: s.at)
    out.ok = True
    out.complete = out.pending == 0 and not budget_hit and len(out.steps) < max_steps
    out.calls = calls()
    out.checked_at = iso(datetime.now(UTC))
    return out


def _step(readers: list[Reader], ledger: LedgerSeries, a: _Eval, b: _Eval) -> Step:
    changed = [(r, bb) for r, ba, bb in zip(readers, a.blocks, b.blocks, strict=True) if r.balance(bb) != r.balance(ba)]
    chain_delta = b.chain - a.chain
    near = ledger.between(a.t, b.t)
    if changed:
        r0, blk0 = min(changed, key=lambda x: x[0].time(x[1]))
        at = r0.time(blk0)
        blocks = {r.key: bb for r, bb in changed}
        links = {r.key: u for r, bb in changed if (u := r.explorer_block(bb))}
        kind = "chain"
    else:  # nur der Ledger hat sich bewegt: Buchung ohne Gegenstück auf der Kette
        at = _ts(b.t)
        blocks = {r.key: bb for r, bb in zip(readers, b.blocks, strict=True)}
        links = {}
        kind = "ledger"
    return Step(at=at, delta=b.r - a.r, chain_delta=chain_delta, blocks=blocks, links=links,
                ledger_tx=[tx for _t, tx, _d in near][:5], kind=kind)


# ----------------------------------------------------------------------------------------------------
# Zwischenspeicher (nur Kettenwerte)
# ----------------------------------------------------------------------------------------------------

def _dump(readers: list[Reader]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for r in readers:
        times = sorted(r.known_times().items())[-CACHE_LIMIT:]
        keep = {b for b, _t in times}
        out[r.key] = {"t": {str(b): t for b, t in times},
                      "b": {str(b): str(q) for b, q in r.known_balances().items() if b in keep or len(keep) < 50},
                      "h": {str(b): h for b, h in r.known_hashes().items() if b in keep}}
    return out


def _load(readers: list[Reader], raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    for r in readers:
        c = raw.get(r.key)
        if not isinstance(c, dict):
            continue
        try:
            r.preload(times={int(b): int(t) for b, t in (c.get("t") or {}).items()},
                      balances={int(b): Decimal(q) for b, q in (c.get("b") or {}).items()},
                      hashes={int(b): str(h) for b, h in (c.get("h") or {}).items()})
        except (ValueError, ArithmeticError):
            log.debug("Zwischenspeicher des Kettenbestands verworfen")


# ----------------------------------------------------------------------------------------------------
# Anbindung an Konten
# ----------------------------------------------------------------------------------------------------

def sources_for(ctx: Any, account: str, asset: str) -> list[tuple[Any, list[Reader]]]:
    """Wallet-Quellen des Kontos, für die es einen kostenlosen Archivweg gibt (nativer Bestand des Assets)."""
    from app.datasources.service import datasource_service

    out: list[tuple[Any, list[Reader]]] = []
    for ds in datasource_service(ctx).list():
        if not ds.is_wallet or ds.account != account:
            continue
        for addr in ds.addresses:
            try:
                readers = readers_for(str(ds.row["provider"]), addr)
            except Unsupported:
                continue
            if readers and readers[0].asset.upper() == asset.split("@")[0].upper() and "@" not in asset:
                out.append((ds, readers))
    return out


def stored(db: Any, account: str, asset: str) -> Result | None:
    raw = db.get_state(state_key(account, asset))
    if not isinstance(raw, dict) or not isinstance(raw.get("result"), dict):
        return None
    try:
        return Result.from_json(raw["result"])
    except (KeyError, ValueError, ArithmeticError):
        return None


def all_stored(db: Any) -> dict[tuple[str, str], Result]:
    """Gespeicherte Ergebnisse je (Konto, Asset) – für die Diagnose (rein lesend)."""
    out: dict[tuple[str, str], Result] = {}
    try:
        rows = db.q("SELECT key FROM app_state WHERE key LIKE 'chainscan:%'")
    except Exception as e:  # Datenbank ohne app_state (Tests/Migration)
        log.debug("Kettenbestand nicht lesbar: %s", e)
        return out
    for r in rows:
        acc, _, asset = str(r["key"]).removeprefix("chainscan:").rpartition("|")
        res = stored(db, acc, asset)
        if res is not None and acc:
            out[(acc, asset)] = res
    return out


def refresh(ctx: Any, account: str, asset: str, *, max_calls: int = MAX_CALLS, prog: Any | None = None,
            transport: Any = None, sleep: Callable[[float], None] | None = None) -> Result:
    """Sucht neu (mit Zwischenspeicher), speichert das Ergebnis und gibt es zurück."""
    srcs = sources_for(ctx, account, asset)
    if not srcs:
        res = Result(error="Für dieses Konto gibt es keinen kostenlosen Weg zum Bestand bei einem früheren Block.")
        return res
    ds, readers = srcs[0]
    kw: dict[str, Any] = {k: v for k, v in (("transport", transport), ("sleep", sleep)) if v is not None}
    readers = readers_for(str(ds.row["provider"]), ds.addresses[0], **kw)
    pf = ctx.recorded_portfolio()
    ledger = LedgerSeries(pf.txs if pf is not None else [], account, asset)
    raw = ctx.db.get_state(state_key(account, asset))
    _load(readers, raw.get("cache") if isinstance(raw, dict) else None)
    try:
        def tick(n: int, text: str) -> None:
            if prog is not None:
                prog.update(n, max_calls, text=text)

        res = scan(readers, ledger, max_calls=max_calls, deadline=time.monotonic() + 600, on_progress=tick)
        res.ledger_total = ledger.cum[-1]
        ctx.db.set_state(state_key(account, asset), {"result": res.to_json(), "cache": _dump(readers),
                                                     "source": int(ds.id)})
    finally:
        for r in readers:
            r.close()
    return res


def job_run(ctx: Any, account: str = "", asset: str = "") -> dict[str, Any]:
    if not account or not asset:
        return {"ok": False, "error": "Konto oder Asset fehlt."}
    prog = job_progress(ctx, JOB, LABEL, unit="Abfragen", phases=["chain_scan"])
    prog.phase("chain_scan", total=MAX_CALLS, text="Bestand der Blockchain abfragen")
    try:
        res = refresh(ctx, account, asset, prog=prog)
    except Exception as e:
        log.exception("Kettenbestand fehlgeschlagen")
        prog.finish(False, result=f"{type(e).__name__}: {e}"[:200])
        return {"ok": False, "error": str(e)}
    text = res.error or (f"{len(res.steps)} Sprung/Sprünge" + ("" if res.complete else ", Suche unvollständig"))
    prog.finish(res.ok, result=text)
    return {"ok": res.ok, "error": res.error, "steps": len(res.steps)}
