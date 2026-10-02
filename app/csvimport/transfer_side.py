"""Zu- bzw. Abgang ↔ Seite eines erfassten Transfers – auch verzögert und unter anderem Kontonamen.

Muster
    Der kuratierte Import (z. B. aus einem Steuertool) führt eine Auszahlung als Transfer „Börse → eigenes Wallet“ –
    mit dem Zeitpunkt der Auszahlung und dem Wallet-Namen des Steuertools. Die Wallet-Datenquelle liefert denselben
    Vorgang als Zugang: Stunden bis Tage später (die Börse zahlt verzögert aus, die Bestätigung dauert) und unter
    ihrem eigenen Kontonamen. Als zusätzlicher Zugang gebucht, zählt die Menge doppelt – und der Zugang begänne einen
    neuen Einstand, statt Einstand und Haltedauer der Börse fortzuführen.

Regeln (je Transferseite höchstens ein Treffer; gleiches Konto vor anderem Konto, exakte Menge vor ungefährer, dann
der nächste Zeitpunkt)
    * Zu- bzw. Abgang ohne Einordnung (Erträge, Verluste … nie); der Transfer bewegt ein Asset (Abgang = Zugang).
      Transfers, die Portfolia selbst aus Abgang und Zugang gebildet hat (``PF-T``), zählen nicht – ihre Seiten sind
      über ihre Kennungen bekannt.
    * Zugangsseite: höchstens 2 h vor und 72 h nach dem Transfer; bei exakt gleicher, unverwechselbarer Menge
      (mindestens sechs signifikante Stellen) bis 7 Tage – verzögerte Auszahlung. Menge = empfangene bzw. gesendete
      Menge (auch abzüglich einer Gebühr im selben Asset).
    * Abgangsseite: ± 2 h; gleicher Abgang auch bei anders dargestellter Gebühr (Gesamtabgang gleich).
    * Nie bei verschiedenen Transaktions-Hashes.
    * Gleiches Konto: Menge ± 0,5 % (wie die Dublettenprüfung). Anderes Konto: nur exakt gleiche Menge, kein Fiat,
      nicht das Gegenkonto des Transfers und nur, wenn das Konto des Transfers nicht von einer *anderen* Datenquelle
      geführt wird – sonst wäre der Vorgang dort zu erwarten. (Führt es dieselbe Datenquelle, wurde ihr Konto
      inzwischen umgestellt: ältere Buchungen liegen noch auf dem früheren Konto.)

Belege, die die Zuordnung stützen (nie Voraussetzung): ein Zeitpunkt in der Notiz der Transfer-Buchung, der auf
± 2 min dem Zu- bzw. Abgang entspricht (z. B. „Zugang … am 2024-05-02T10:15:00Z“ eines Steuertools).

Ein Treffer ist ein begründeter Verdacht, kein Beweis: Im Prüf-Stapel verhindert er das stille Buchen (mögliche
Dublette, nie automatisch übernommen); bei bereits gebuchten App-Buchungen schlägt er eine Entscheidung vor
(Journal → Abgleich, Datenqualität). Portfolia ändert nie selbst eine vorhandene Buchung.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.importer import contract as C
from app.ledger.models import Tx
from app.util.timeutil import parse_iso

BEFORE = timedelta(hours=2)  # Zugang höchstens so lange vor dem Transfer (Uhren, Zeitzonen)
AFTER = timedelta(hours=72)  # … und höchstens so lange danach
DELAYED = timedelta(days=7)  # verzögerte Auszahlung: nur bei exakt gleicher, unverwechselbarer Menge
OUT_WINDOW = timedelta(hours=2)  # Abgangsseite: gleicher Zeitpunkt ± Uhren/Zeitzonen
QTY_TOL = Decimal("0.005")  # gleiches Konto: Menge ± 0,5 % (wie die Dublettenprüfung)
EXACT_REL = Decimal("0.000001")  # „exakt“: nur Rundung der Quellen (z. B. 8 statt 18 Nachkommastellen)
EXACT_ABS = Decimal("1e-8")
DISTINCT_DIGITS = 6  # ab so vielen signifikanten Stellen gilt eine Menge als unverwechselbar
NOTE_TIME_S = 120  # Zeitpunkt laut Notiz der Transfer-Buchung: höchstens so viele Sekunden Abweichung
_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})")


def exact(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs(b) * EXACT_REL, EXACT_ABS)


def close(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs(b) * QTY_TOL, EXACT_ABS)


def distinct(q: Decimal) -> bool:
    """Unverwechselbare Menge (z. B. 0,38228216 BTC) – runde Beträge wiederholen sich häufiger zufällig."""
    t = q.normalize().as_tuple()
    return isinstance(t.exponent, int) and len(t.digits) >= DISTINCT_DIGITS


def note_times(note: str | None) -> list[datetime]:
    """Zeitpunkte (ISO 8601 mit Zeitzone) im Freitext einer Buchung."""
    out = []
    for m in _ISO_RE.findall(note or ""):
        try:
            ts = parse_iso(m)
        except ValueError:
            continue
        if ts is not None:
            out.append(ts)
    return out


def is_transfer(t: Tx) -> bool:
    """Erfasster Transfer eines Assets (Import, Journal) – nicht die aus abgeglichenen Paaren entstandenen (PF-T)."""
    return t.type == "transfer" and bool(t.from_asset) and t.from_asset == t.to_asset and \
        not (t.origin == "journal" and t.source == "transfer")


def span_text(delay: timedelta) -> str:
    """Abstand des Zu- bzw. Abgangs zum Transfer als Text („27,5 h nach dem Transfer“)."""
    s = delay.total_seconds()
    when = "nach dem Transfer" if s >= 0 else "vor dem Transfer"
    a = abs(s)
    if a < 120:
        return "zeitgleich mit dem Transfer"
    if a < 7200:
        return f"{int(a // 60)} min {when}"
    if a < 48 * 3600:
        return f"{a / 3600:.1f} h {when}".replace(".", ",")
    return f"{a / 86400:.1f} Tage {when}".replace(".", ",")


# ----------------------------------------------------------------------------------------------------
# Zu-/Abgang und Treffer
# ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Probe:
    """Ein Zu- bzw. Abgang, der geprüft wird (neue Zeile oder App-Buchung)."""

    side: str  # in (Zugang) | out (Abgang)
    account: str
    asset: str
    qty: Decimal
    ts: datetime
    fee: Decimal | None = None  # Gebühr im selben Asset (Abgang)
    hash: str | None = None
    source_id: int | None = None  # Datenquelle, aus der der Vorgang stammt (falls bekannt)


@dataclass
class Hit:
    tx: Tx
    role: str  # in | out – Seite des Transfers
    same_account: bool
    exact: bool
    delay: timedelta  # Zeitpunkt des Zu-/Abgangs − Zeitpunkt des Transfers
    note_time: datetime | None = None  # Zeitpunkt laut Notiz der Transfer-Buchung (Beleg)

    @property
    def account(self) -> str:
        """Konto der Transferseite."""
        return (self.tx.to_account if self.role == "in" else self.tx.from_account) or ""

    @property
    def basis(self) -> str:
        return "transfer_leg" if self.same_account else "transfer_leg_acc"

    @property
    def side_label(self) -> str:
        return "Zugangsseite" if self.role == "in" else "Abgangsseite"

    @property
    def delayed(self) -> bool:
        """Deutlich später als der Transfer (verzögerte Auszahlung bzw. Gutschrift)."""
        return self.role == "in" and self.delay > BEFORE

    def info(self) -> dict[str, Any]:
        """Kurzfassung für Anzeige und Bewertung (JSON-fähig)."""
        return {"tx": self.tx.tx_id, "role": self.role, "same": self.same_account, "exact": self.exact,
                "delay_s": int(self.delay.total_seconds()), "account": self.account,
                "route": f"{self.tx.from_account or '?'} → {self.tx.to_account or '?'}",
                **({"note_time": self.note_time.isoformat()} if self.note_time else {})}

    def text(self, account: str) -> str:
        """Begründung (eine Zeile) aus Sicht des Zu- bzw. Abgangs auf ``account``."""
        t = self.tx
        parts = [f"{self.side_label} des Transfers {t.tx_id} ({t.from_account or '?'} → {t.to_account or '?'})",
                 span_text(self.delay), "gleiche Menge" if self.exact else "nahezu gleiche Menge"]
        if not self.same_account:
            parts.append(f"Konto „{account}“ statt „{self.account}“")
        if self.note_time is not None:
            parts.append("Zeitpunkt laut Notiz der Transfer-Buchung bestätigt")
        return ", ".join(parts)


def _d(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return abs(v if isinstance(v, Decimal) else Decimal(str(v)))
    except (InvalidOperation, ValueError):
        return None


def probe_of_row(row: Mapping[str, Any], ts: datetime, h: str | None, source_id: int | None = None) -> Probe | None:
    """Zu- bzw. Abgang einer aufbereiteten Prüfzeile (``transactions.csv``-Format) – ohne Einordnung."""
    if row.get("tag"):
        return None
    if row.get("type") == "deposit" and row.get("to_asset"):
        q = _d(row.get("to_qty"))
        return Probe("in", str(row.get("to_account") or ""), str(row["to_asset"]), q, ts, None, h,
                     source_id) if q else None
    if row.get("type") == "withdrawal" and row.get("from_asset"):
        q = _d(row.get("from_qty"))
        asset = str(row["from_asset"])
        fee = _d(row.get("fee_qty")) if row.get("fee_asset") == asset else None
        return Probe("out", str(row.get("from_account") or ""), asset, q, ts, fee, h, source_id) if q else None
    return None


def probe_of_tx(t: Tx, h: str | None = None, source_id: int | None = None) -> Probe | None:
    """Zu- bzw. Abgang einer Buchung (z. B. App-Buchung einer Datenquelle) – ohne Einordnung."""
    if t.tag:
        return None
    if t.type == "deposit" and t.to_asset and t.to_qty:
        return Probe("in", t.to_account or "", t.to_asset, abs(t.to_qty), t.ts, None, h, source_id)
    if t.type == "withdrawal" and t.from_asset and t.from_qty:
        fee = abs(t.fee_qty) if t.fee_asset == t.from_asset and t.fee_qty else None
        return Probe("out", t.from_account or "", t.from_asset, abs(t.from_qty), t.ts, fee, h, source_id)
    return None


# ----------------------------------------------------------------------------------------------------
# Index der erfassten Transfers
# ----------------------------------------------------------------------------------------------------

class TransferIndex:
    """Erfasste Transfers je Asset; :meth:`find` sucht die passende Seite, :meth:`claim` vergibt sie (je Seite
    höchstens ein Zu- bzw. Abgang)."""

    def __init__(self, txs: Iterable[Tx], hashes: Callable[[Tx], set[str]],
                 managed: Mapping[str, Iterable[int]] | Iterable[str] = (),
                 keep: Callable[[Tx], bool] | None = None) -> None:
        self.by_asset: dict[str, list[Tx]] = defaultdict(list)
        for t in txs:
            if is_transfer(t) and (keep is None or keep(t)):
                self.by_asset[t.from_asset or ""].append(t)
        self.hashes = hashes
        # Konten mit eigener Datenquelle → deren IDs (ohne ID: von einer unbekannten Datenquelle geführt)
        self.managed: dict[str, frozenset[int]] = (
            {a: frozenset(ids) for a, ids in managed.items() if a} if isinstance(managed, Mapping)
            else {a: frozenset() for a in managed if a})
        self.used: set[tuple[str, str]] = set()

    def __bool__(self) -> bool:
        return bool(self.by_asset)

    def claim(self, hit: Hit) -> None:
        self.used.add((hit.tx.tx_id, hit.role))

    def claim_side(self, tx_id: str, role: str) -> None:
        """Seite als vergeben markieren (z. B. bereits entschieden: „Import-Buchung gilt“)."""
        self.used.add((tx_id, role))

    def find(self, p: Probe, exclude: Iterable[str] = ()) -> Hit | None:
        """Beste passende Transferseite (``exclude``: Transfers, die nicht in Frage kommen – z. B. entschieden
        „keine Dublette“)."""
        skip = set(exclude)
        hits = [h for t in self.by_asset.get(p.asset, ()) if t.tx_id not in skip
                and (h := self._match(p, t)) is not None]
        if not hits:
            return None
        return min(hits, key=lambda h: (not h.same_account, not h.exact, abs(h.delay), h.tx.tx_id))

    def _other_source(self, account: str, source_id: int | None) -> bool:
        """Führt eine *andere* Datenquelle dieses Konto? Dann wäre der Vorgang dort zu erwarten. Dieselbe
        Datenquelle zählt nicht (ihr Konto wurde umgestellt, ältere Buchungen liegen noch auf dem früheren)."""
        if account not in self.managed:
            return False
        owners = self.managed[account]
        return source_id is None or not owners or bool(owners - {source_id})

    def _match(self, p: Probe, t: Tx) -> Hit | None:
        if (t.tx_id, p.side) in self.used:
            return None
        t_fee = abs(t.fee_qty) if t.fee_asset and t.fee_asset == t.from_asset and t.fee_qty else None
        t_out = abs(t.from_qty) if t.from_qty else None
        if p.side == "in":
            acc, other = t.to_account or "", t.from_account or ""
            theirs = [q for q in (abs(t.to_qty) if t.to_qty else None, t_out,
                                  (t_out - t_fee) if t_out and t_fee and t_out > t_fee else None) if q]
            mine = [p.qty]
        else:
            acc, other = t.from_account or "", t.to_account or ""
            theirs = [q for q in (t_out, (t_out + t_fee) if t_out and t_fee else None) if q]
            mine = [p.qty, *([p.qty + p.fee] if p.fee else [])]
        same = bool(acc) and acc == p.account
        if not same and (not acc or not p.account or p.account == other or p.asset in C.ISO_CURRENCIES
                         or self._other_source(acc, p.source_id)):
            return None
        is_exact = any(exact(a, b) for a in mine for b in theirs)
        if not is_exact and not (same and any(close(a, b) for a in mine for b in theirs)):
            return None
        dt = p.ts - t.ts
        if p.side == "in":
            limit = DELAYED if is_exact and distinct(p.qty) else AFTER
            if not (-BEFORE <= dt <= limit):
                return None
        elif abs(dt) > OUT_WINDOW:
            return None
        hs = self.hashes(t)
        if p.hash and hs and p.hash not in hs:
            return None  # gleiche Menge, aber eine andere Blockchain-Transaktion
        nt = next((x for x in note_times(t.note) if abs((x - p.ts).total_seconds()) <= NOTE_TIME_S), None)
        return Hit(t, p.side, same, is_exact, dt, nt)


def hash_lookup(db: Any) -> Callable[[Tx], set[str]]:
    """Transaktions-Hashes einer Buchung: Import aus Notiz/Quellkennung, App-Buchungen aus ``tx_hash``."""
    from app.csvimport import reconcile as R
    from app.csvimport.events import normalize_hash

    journal: dict[str, str] = {r["tx_id"]: h for r in db.q(
        "SELECT tx_id, tx_hash FROM journal_tx WHERE tx_hash IS NOT NULL AND tx_hash <> ''")
        if (h := normalize_hash(r["tx_hash"]))}

    def get(t: Tx) -> set[str]:
        out = R.hashes_in(t.note, t.source_ref)
        if t.tx_id in journal:
            out.add(journal[t.tx_id])
        return out

    return get


def managed_accounts(db: Any) -> dict[str, set[int]]:
    """Konten, die eine Datenquelle führt (dort kommen deren Vorgänge an) → IDs der Datenquellen."""
    out: dict[str, set[int]] = defaultdict(set)
    try:
        rows = db.q("SELECT id, account FROM data_source")
    except Exception:  # ältere Datenbank ohne Datenquellen
        return {}
    for r in rows:
        if r["account"]:
            out[r["account"]].add(int(r["id"]))
    return dict(out)


__all__ = ["AFTER", "BEFORE", "DELAYED", "Hit", "Probe", "TransferIndex", "close", "distinct", "exact",
           "hash_lookup", "is_transfer", "managed_accounts", "note_times", "probe_of_row", "probe_of_tx",
           "span_text"]
