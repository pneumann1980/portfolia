"""Abgleich eines Prüf-Stapels mit bereits erfassten Buchungen über den Transaktions-Hash: was existiert bereits,
was ist neu – ohne Zutun des Nutzers.

Wallet-Vorgänge (Datenquellen, Wallet-Exporte) tragen den Hash ihrer Blockchain-Transaktion. Der kuratierte Import
führt ihn in ``note`` bzw. ``source_ref`` (z. B. aus Koinly), App-Buchungen in ``journal_tx.tx_hash``. Je Hash werden
die Beine beider Seiten verglichen – Zugang, Abgang, Gebühr –, Mengen je Seite und Asset summiert (eine Seite kann
einen Vorgang aufteilen, die andere zusammenfassen), Toleranz wie bei Dubletten (0,5 %). Gebühren führt der
kuratierte Import oft als eigene Buchung (Abgang mit Tag ``cost``): Sie zählen dort als Gebühr, nicht als Abgang.

Ergebnis je Zeile: ``full`` (alle Hauptbeine gefunden → „bereits vorhanden“), ``partial`` (gleicher Hash, aber nicht
alle Beine) oder kein Treffer. Abgeleitet wird nur bei eindeutiger Evidenz: das Asset unbekannter Symbole (alle
Treffer zeigen auf dasselbe Asset) und die Konten der Gegenbuchungen (Verteilung, für die Konto-Zuordnung).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.csvimport import model as M
from app.csvimport.events import derive_tx_hash, normalize_hash
from app.csvimport.model import Rec

QTY_TOL = Decimal("0.005")
FEE_TAGS = frozenset({"cost", "fee"})
_HASH_RES = (
    re.compile(r"0x[0-9a-fA-F]{64}(?![0-9a-fA-F])"),  # EVM
    re.compile(r"(?<![0-9a-fA-Fx])[0-9a-fA-F]{64}(?![0-9a-fA-F])"),  # Bitcoin, Kaspa
    re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{86,88}(?![1-9A-HJ-NP-Za-km-z])"),  # Solana-Signatur
)
SIDE_LABEL = {"in": "+", "out": "−", "fee": "Gebühr "}


def hashes_in(*texts: str | None) -> set[str]:
    """Transaktions-Hashes in freiem Text (Notiz, Quellkennung) – normalisiert wie ``journal_tx.tx_hash``."""
    out: set[str] = set()
    for t in texts:
        if t:
            for rx in _HASH_RES:
                out.update(h for m in rx.findall(t) if (h := normalize_hash(m)))
    return out


def row_hash(r: Rec) -> str | None:
    return normalize_hash(r.txhash or derive_tx_hash(r.ext_id))


def qty_eq(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(abs(b) * QTY_TOL, Decimal("1e-8"))


def rec_legs(r: Rec) -> list[tuple[str, str, Decimal]]:
    """Beine einer Zeile in Symbolen der Quelle: (Seite, Symbol, Menge > 0)."""
    if r.row is not None:
        pairs = (("in", r.row.get("to_asset"), r.row.get("to_qty")),
                 ("out", r.row.get("from_asset"), r.row.get("from_qty")),
                 ("fee", r.row.get("fee_asset"), r.row.get("fee_qty")))
    else:
        pairs = (("in", r.in_sym, r.in_qty), ("out", r.out_sym, r.out_qty), ("fee", r.fee_sym, r.fee_qty))
    out = []
    for side, sym, q in pairs:
        try:
            qty = abs(Decimal(str(q))) if q not in (None, "") else Decimal(0)
        except ArithmeticError:
            continue
        if sym and qty > 0:
            out.append((side, str(sym), qty))
    return out


def tx_legs(t: Any) -> list[tuple[str, str, Decimal, str]]:
    """Beine einer Buchung (Import oder Journal): (Seite, Asset, Menge, Konto). Gebühren-Abgänge (Tag ``cost``/
    ``fee``) zählen als Gebühr."""
    g = t.get if isinstance(t, Mapping) else lambda k: getattr(t, k, None)

    def dec(v: Any) -> Decimal:
        try:
            return abs(Decimal(str(v))) if v not in (None, "") else Decimal(0)
        except ArithmeticError:
            return Decimal(0)

    out: list[tuple[str, str, Decimal, str]] = []
    fq, tq, feeq = dec(g("from_qty")), dec(g("to_qty")), dec(g("fee_qty"))
    if g("type") == "withdrawal" and (g("tag") or "") in FEE_TAGS:
        if g("from_asset") and fq:
            out.append(("fee", g("from_asset"), fq, g("from_account") or ""))
        return out
    if g("from_asset") and fq:
        out.append(("out", g("from_asset"), fq, g("from_account") or ""))
    if g("to_asset") and tq:
        out.append(("in", g("to_asset"), tq, g("to_account") or ""))
    if g("fee_asset") and feeq:
        out.append(("fee", g("fee_asset"), feeq, g("from_account") or g("to_account") or ""))
    return out


@dataclass
class Booking:
    tx_id: str
    origin: str  # import | journal
    legs: list[tuple[str, str, Decimal, str]]


@dataclass
class RowMatch:
    state: str  # full | partial
    origin: str  # import | journal
    txs: list[str]
    accounts: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # Beine ohne Gegenstück (Anzeige)
    other_asset: dict[str, str] = field(default_factory=dict)  # Symbol → Asset laut Gegenbuchung (Konflikt)

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "origin": self.origin, "txs": self.txs[:5], "accounts": self.accounts[:3],
                "missing": self.missing[:3], "other_asset": self.other_asset}


@dataclass
class Result:
    rows: dict[int, RowMatch] = field(default_factory=dict)
    learned: dict[str, str] = field(default_factory=dict)  # Symbol (Großschreibung) → Asset, eindeutig
    accounts: Counter[str] = field(default_factory=Counter)  # Konten der Gegenbuchungen (Seite der Wallet)
    hashes: int = 0  # Hashes dieses Stapels, die in Buchungen vorkommen


class HashIndex:
    """Buchungen mit Transaktions-Hash: kuratierter Import (Notiz/Quellkennung) und App-Buchungen."""

    def __init__(self, import_txs: Iterable[Any], journal_rows: Iterable[Mapping[str, Any]]) -> None:
        self.by_hash: dict[str, list[Booking]] = defaultdict(list)
        for t in import_txs:
            hs = hashes_in(getattr(t, "note", None), getattr(t, "source_ref", None))
            if hs:
                b = Booking(t.tx_id, "import", tx_legs(t))
                for h in hs:
                    self.by_hash[h].append(b)
        for r in journal_rows:
            h = normalize_hash(r["tx_hash"])
            if h:
                self.by_hash[h].append(Booking(r["tx_id"], "journal", tx_legs(r)))

    def __len__(self) -> int:
        return len(self.by_hash)


def reconcile(rows: Iterable[Any], index: HashIndex, resolve: Callable[[str], str | None],
              known_assets: Iterable[str]) -> Result:
    """Zeilen (``RowCtx``) eines Stapels gegen die Buchungen gleichen Hashs abgleichen.

    ``resolve``: Symbol der Quelle → Asset (bereits zugeordnet) oder None. Unbekannte Symbole werden nur über die
    Menge abgeglichen; zeigen alle Treffer auf dasselbe Asset, wird es gelernt."""
    res = Result()
    assets = set(known_assets)
    by_hash: dict[str, list[Any]] = defaultdict(list)
    for rc in rows:
        h = row_hash(rc.rec)
        if h and h in index.by_hash:
            by_hash[h].append(rc)
    votes: dict[str, Counter[str]] = defaultdict(Counter)
    res.hashes = len(by_hash)
    for h, rcs in by_hash.items():
        bookings = index.by_hash[h]
        for origin in ("import", "journal"):
            cands = [b for b in bookings if b.origin == origin]
            open_rcs = [rc for rc in rcs if rc.idx not in res.rows or res.rows[rc.idx].state != "full"]
            if not cands or not open_rcs:
                continue
            _match_hash(open_rcs, cands, origin, resolve, res, votes)
    for sym, c in votes.items():
        if len(c) == 1:
            aid = next(iter(c))
            if aid in assets:
                res.learned[sym.upper()] = aid
    return res


def _match_hash(rcs: list[Any], cands: list[Booking], origin: str, resolve: Callable[[str], str | None],
                res: Result, votes: dict[str, Counter[str]]) -> None:
    other: dict[tuple[str, str, str], list[Any]] = {}  # (Seite, Asset, Konto) → [Menge, tx_ids]
    for b in cands:
        for side, asset, qty, acc in b.legs:
            slot = other.setdefault((side, asset, acc), [Decimal(0), []])
            slot[0] += qty
            slot[1].append(b.tx_id)
    mine: dict[tuple[str, str], list[Any]] = {}  # (Seite, Symbol) → [Menge, Zeilen]
    for rc in rcs:
        for side, sym, qty in rec_legs(rc.rec):
            slot = mine.setdefault((side, sym), [Decimal(0), []])
            slot[0] += qty
            slot[1].append(rc)
    found: dict[tuple[str, str], tuple[list[str], set[str]]] = {}  # Bein → (tx_ids, Konten)
    conflicts: dict[str, str] = {}
    used: set[tuple[str, str, str]] = set()  # jede Gegenbuchung deckt höchstens ein Bein (keine Doppelnutzung)
    used_known: set[tuple[str, str, str]] = set()  # … davon durch Beine mit bekanntem Asset (eindeutig)
    # bekannte Assets zuerst – sie haben genau ein mögliches Gegenstück; unbekannte nehmen, was übrig bleibt
    for (side, sym), (qty, lines) in sorted(mine.items(), key=lambda kv: resolve(kv[0][1]) is None):
        aid = resolve(sym)

        def pool_for(q: Decimal, taken: set[tuple[str, str, str]], s: str = side,
                     a: str | None = aid) -> list[tuple[str, str, str]]:
            return [k for k, v in other.items() if k not in taken and k[0] == s and (a is None or k[1] == a)
                    and qty_eq(q, v[0])]

        want = qty
        pool = pool_for(want, used)
        if not pool and side == "out" and ("fee", sym) in mine:  # Gebühr im Abgang enthalten
            want = qty + mine[("fee", sym)][0]
            pool = pool_for(want, used)
        if not pool:
            if aid is not None:  # gleiche Menge unter anderem Asset → Zuordnung prüfen
                alt = {k[1] for k, v in other.items() if k[0] == side and qty_eq(qty, v[0])}
                if len(alt) == 1:
                    conflicts[sym] = next(iter(alt))
            continue
        # eindeutig nur, wenn auch ohne die Wahl anderer unbekannter Beine genau ein Asset bzw. Konto in Frage kommt
        options = pool_for(want, used_known)
        kinds = {k[1] for k in options}
        accs = {k[2] for k in options if k[2]}
        pick = sorted(pool)[0]
        used.add(pick)
        if aid is not None:
            used_known.add(pick)
        found[(side, sym)] = (list(other[pick][1]), {pick[2]} if pick[2] and len(accs) == 1 else set())
        if aid is None and len(kinds) == 1:
            votes[sym][next(iter(kinds))] += len(lines)
        if side != "fee" and len(accs) == 1:
            res.accounts[next(iter(accs))] += 1
    all_tx = list(dict.fromkeys(b.tx_id for b in cands))
    for rc in rcs:
        legs = rec_legs(rc.rec)
        main = [(s, sym) for s, sym, _ in legs if s != "fee"] or [(s, sym) for s, sym, _ in legs]
        hit = [found[k] for k in main if k in found]
        txs = list(dict.fromkeys(t for tx_ids, _ in hit for t in tx_ids)) or all_tx
        accs = sorted({a for _, a_set in hit for a in a_set})
        if not legs and rc.rec.kind == M.REVIEW:  # mehrteiliger Vorgang ohne Beine: Hash genügt
            res.rows[rc.idx] = RowMatch("full", origin, all_tx)
            continue
        missing = [f"{SIDE_LABEL[s]}{q.normalize():f} {sym}" for s, sym, q in legs
                   if (s, sym) in main and (s, sym) not in found]
        state = "full" if main and not missing else "partial"
        prev = res.rows.get(rc.idx)
        if prev is None or state == "full":
            res.rows[rc.idx] = RowMatch(state, origin, txs, accs, missing,
                                        {s: conflicts[s] for _, s in main if s in conflicts})
