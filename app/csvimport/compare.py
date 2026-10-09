"""Gegenüberstellung im Prüf-Stapel: neue Zeile ↔ vorhandene Buchung(en) im Bestand.

Für Zeilen mit Verweis auf vorhandene Buchungen (mögliche Dublette, bereits vorhanden, Abgleich über den Hash) zeigt
der Prüf-Stapel beide Seiten Feld für Feld – Zeitpunkt, Art, Abgang, Zugang, Gebühr, EUR-Wert, Herkunft, Kennung,
Transaktions-Hash, Notiz – und markiert, worin sie sich unterscheiden. Nur Anzeige: nichts wird verändert.

Gesucht wird in den erfassten Buchungen (Import mit Änderungen der App, App-Buchungen, freigegebene Sparplan-
Ausführungen), dann im Import selbst (z. B. durch eine App-Buchung ersetzte Import-Buchung) und zuletzt im Journal
in jedem Status (gelöscht, zusammengeführt) – so bleibt auch ein Verweis auf eine gelöschte Buchung nachvollziehbar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from app.csvimport import model as M
from app.csvimport import reconcile as R
from app.csvimport.events import derive_tx_hash, normalize_hash
from app.util.timeutil import parse_iso

MAX_SIDES = 2  # vorhandene Buchungen nebeneinander; weitere als Verweis
FIELDS = ("ts", "type", "out", "inn", "fee", "value", "hash")
_JOURNAL_STATUS = {"deleted": "gelöscht", "merged": "in einem Transfer zusammengeführt", "replaced": "ersetzt",
                   "reverted": "rückgängig gemacht"}
_KIND_TYPE = {M.TRADE: "trade", M.DEPOSIT: "deposit", M.WITHDRAWAL: "withdrawal", M.TRANSFER: "transfer",
              M.FEE: "withdrawal", M.CONVERSION: "corporate_action"}


@dataclass
class Side:
    """Eine Seite der Gegenüberstellung (neue Zeile oder vorhandene Buchung)."""

    label: str
    ts: datetime | None
    type: str | None = None
    tag: str | None = None
    out: tuple[str, str, Decimal] | None = None  # (Konto, Asset, Menge)
    inn: tuple[str, str, Decimal] | None = None
    fee: tuple[str, Decimal] | None = None
    value_eur: Decimal | None = None
    origin: str = ""
    ref: str | None = None
    hashes: list[str] = field(default_factory=list)
    note: str | None = None
    status: str | None = None  # z. B. „gelöscht“ (nur Journal)
    url: str | None = None
    edit_url: str | None = None
    ts_missing: bool = False
    found: bool = True


@dataclass
class Compare:
    new: Side
    old: list[Side]
    diffs: list[set[str]]  # je vorhandener Buchung: Felder, die von der neuen Zeile abweichen
    deltas: list[str | None]  # je vorhandener Buchung: zeitlicher Abstand als Text
    more: list[str] = field(default_factory=list)  # weitere Verweise (ohne Spalte)
    new_title: str = "Neu"  # Spaltenkopf der verglichenen Seite („Neu“ im Prüf-Stapel, „Diese Buchung“ im Journal)

    @property
    def has_hash(self) -> bool:
        return bool(self.new.hashes or any(s.hashes for s in self.old))

    @property
    def has_note(self) -> bool:
        return bool(self.new.note or any(s.note for s in self.old))


def _d(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _leg(acc: Any, asset: Any, qty: Any) -> tuple[str, str, Decimal] | None:
    q = _d(qty)
    return (str(acc or ""), str(asset), q) if asset and q else None


def _span(d: timedelta) -> str:
    s = int(abs(d.total_seconds()))
    sign = "+" if d.total_seconds() >= 0 else "−"
    if s < 60:
        return "gleich" if s == 0 else f"{sign}{s} s"
    days, rest = divmod(s, 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    parts = ([f"{days} Tag{'e' if days != 1 else ''}"] if days else []) + ([f"{h} h"] if h else []) + \
        ([f"{m} min"] if m and not days else [])
    return sign + " ".join(parts)


def new_side(rc: Any, line_label: str, origin: str) -> Side:
    """Die neue Zeile – aus der aufbereiteten Buchungszeile, ohne sie (ungeklärt) aus den Rohangaben."""
    rec = rc.rec
    h = normalize_hash(rec.txhash or derive_tx_hash(rec.ext_id))
    side = Side(label=line_label, ts=None if rec.ts_missing else rec.ts, ts_missing=bool(rec.ts_missing),
                origin=origin, ref=rec.ext_id, hashes=[h] if h else [], note=rec.note)
    row = rc.row
    if row is not None:
        side.type, side.tag = row.get("type") or None, row.get("tag") or None
        side.out = _leg(row.get("from_account"), row.get("from_asset"), row.get("from_qty"))
        side.inn = _leg(row.get("to_account"), row.get("to_asset"), row.get("to_qty"))
        fq = _d(row.get("fee_qty"))
        side.fee = (str(row["fee_asset"]), fq) if row.get("fee_asset") and fq else None
        side.value_eur = _d(row.get("value_eur"))
    else:  # ohne aufbereitete Zeile (ungeklärt, unvollständig): Angaben der Quelle
        side.type = _KIND_TYPE.get(rec.kind) if rec.kind != M.REVIEW else None
        side.tag = rec.tag
        side.out = _leg(rec.account, rec.out_sym, rec.out_qty)
        side.inn = _leg(rec.to_account or rec.account, rec.in_sym, rec.in_qty)
        side.fee = (rec.fee_sym, rec.fee_qty) if rec.fee_sym and rec.fee_qty else None
    return side


class Lookup:
    """Vorhandene Buchungen nach Kennung – erfasste Buchungen, Import, Journal (jeder Status)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        pf = ctx.recorded_portfolio()
        self.recorded = {t.tx_id: t for t in pf.txs} if pf is not None else {}
        base, _ = ctx.effective_base()
        self.imports = {t.tx_id: t for t in base.txs} if base is not None else {}
        self._journal: dict[str, Any] = {}

    def _journal_rows(self, ids: list[str]) -> None:
        want = [i for i in ids if i not in self._journal]
        for i in range(0, len(want), 500):
            part = want[i:i + 500]
            for r in self.ctx.db.q(f"SELECT * FROM journal_tx WHERE tx_id IN ({','.join('?' * len(part))})", part):
                self._journal[r["tx_id"]] = r
        for i in want:
            self._journal.setdefault(i, None)

    def side_of(self, t: Any) -> Side:
        """Eine erfasste Buchung als Seite (z. B. die App-Buchung in der Buchungsliste)."""
        self._journal_rows([t.tx_id])
        return self._from_tx(t, self._journal.get(t.tx_id))

    def sides(self, ids: list[str]) -> list[Side]:
        self._journal_rows(ids)
        out = []
        for tid in ids:
            j = self._journal.get(tid)
            t = self.recorded.get(tid) or self.imports.get(tid)
            if t is not None:
                out.append(self._from_tx(t, j))
            elif j is not None:
                out.append(self._from_journal(j))
            else:
                out.append(Side(label=tid, ts=None, origin="nicht mehr vorhanden", found=False))
        return out

    @staticmethod
    def _url(tid: str) -> str:
        return f"/journal?q={quote(tid, safe='')}"

    def _from_tx(self, t: Any, j: Any) -> Side:
        from app.journal.service import source_label

        if t.origin == "journal":
            origin = "App · " + source_label(j["source"] if j is not None else t.source)
            hashes = [h for h in [normalize_hash(j["tx_hash"]) if j is not None else None] if h]
            ref = j["external_id"] if j is not None else t.source_ref
        elif t.origin == "plan":
            origin, hashes, ref = "Sparplan-Ausführung", [], t.source_ref
        else:
            origin = "Import" + (f" · {t.source}" if t.source else "")
            hashes, ref = sorted(R.hashes_in(t.note, t.source_ref)), t.source_ref
        status = _JOURNAL_STATUS.get(j["status"]) if j is not None and j["status"] != "active" else None
        editable = t.origin == "import" or (j is not None and j["status"] == "active" and (
            j["source"] in ("manual", "transfer") or str(j["source"]).startswith(("csv:", "sync:", "doc:"))))
        return Side(label=t.tx_id, ts=t.ts, type=t.type, tag=t.tag,
                    out=_leg(t.from_account, t.from_asset, t.from_qty), inn=_leg(t.to_account, t.to_asset, t.to_qty),
                    fee=(t.fee_asset, t.fee_qty) if t.fee_asset and t.fee_qty else None, value_eur=t.value_eur,
                    origin=origin, ref=ref, hashes=hashes, note=t.note, status=status, url=self._url(t.tx_id),
                    edit_url=f"/journal/{quote(t.tx_id, safe='')}/edit" if editable else None)

    def _from_journal(self, j: Any) -> Side:
        from app.journal.service import source_label

        fq = _d(j["fee_qty"])
        h = normalize_hash(j["tx_hash"])
        return Side(label=j["tx_id"], ts=parse_iso(j["ts_utc"]), type=j["type"], tag=j["tag"],
                    out=_leg(j["from_account"], j["from_asset"], j["from_qty"]),
                    inn=_leg(j["to_account"], j["to_asset"], j["to_qty"]),
                    fee=(j["fee_asset"], fq) if j["fee_asset"] and fq else None, value_eur=_d(j["value_eur"]),
                    origin="App · " + source_label(j["source"]), ref=j["external_id"], hashes=[h] if h else [],
                    note=j["note"], status=_JOURNAL_STATUS.get(j["status"], j["status"]), url=self._url(j["tx_id"]))


def differences(new: Side, old: Side) -> set[str]:
    """Felder, in denen die vorhandene Buchung von der neuen Zeile abweicht (nur, was beide Seiten angeben)."""
    out: set[str] = set()
    if not old.found:
        return out
    if new.ts is None or old.ts is None or abs((new.ts - old.ts).total_seconds()) >= 60:
        out.add("ts")
    if new.type and (new.type, new.tag or None) != (old.type, old.tag or None):
        out.add("type")
    for f in ("out", "inn"):
        a, b = getattr(new, f), getattr(old, f)
        if a is None and b is None:
            continue
        if a is None or b is None or a[1:] != b[1:] or (a[0] and b[0] and a[0] != b[0]):
            out.add(f)
    if (new.fee or old.fee) and (new.fee is None or old.fee is None or new.fee != old.fee):
        out.add("fee")
    if new.value_eur is not None and old.value_eur is not None and abs(new.value_eur - old.value_eur) > Decimal("0.01"):
        out.add("value")
    if new.hashes and old.hashes and not set(new.hashes) & set(old.hashes):
        out.add("hash")
    return out


def _compare(new: Side, old: list[Side], more: list[str], title: str) -> Compare:
    deltas = [(_span(s.ts - new.ts) if s.ts is not None and new.ts is not None else None) for s in old]
    return Compare(new=new, old=old, diffs=[differences(new, s) for s in old], deltas=deltas, more=more,
                   new_title=title)


def for_journal(ctx: Any, rows: list[dict[str, Any]]) -> dict[str, Compare]:
    """Buchungsliste: App-Buchung mit Verdacht auf Dublette (``dups``) ↔ ähnliche Import-Buchung(en)."""
    wanted = [r for r in rows if r.get("dups")]
    if not wanted:
        return {}
    look = Lookup(ctx)
    out: dict[str, Compare] = {}
    for r in wanted:
        t = r["t"]
        ids = list(dict.fromkeys(r["dups"]))
        out[t.tx_id] = _compare(look.side_of(t), look.sides(ids[:MAX_SIDES]), ids[MAX_SIDES:], "Diese Buchung")
    return out


def build(ctx: Any, rows: list[Any], origin: str) -> dict[int, Compare]:
    """Gegenüberstellungen für die angezeigten Zeilen mit Verweisen auf vorhandene Buchungen (Zeilenindex →
    Vergleich); die ersten ``MAX_SIDES`` Verweise nebeneinander, weitere als Liste."""
    wanted = [rc for rc in rows if rc.dup_of]
    if not wanted:
        return {}
    look = Lookup(ctx)
    out: dict[int, Compare] = {}
    for rc in wanted:
        ids = list(dict.fromkeys(rc.dup_of))
        out[rc.idx] = _compare(new_side(rc, f"Zeile {rc.line}", origin), look.sides(ids[:MAX_SIDES]),
                               ids[MAX_SIDES:], "Neu")
    return out
