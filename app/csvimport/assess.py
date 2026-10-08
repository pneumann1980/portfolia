"""Abgleich je Prüfzeile: Ergebnis, Sicherheit, Belege, Abweichungen, Ergänzungen, Gebührenprüfung, Quellenvorrang.

Die Erkennungsregeln der Prüfung (:meth:`CsvImportService.evaluate`) finden Kandidaten im Bestand und halten fest,
worauf ein Treffer beruht (``RowCtx.basis``) und welche Seite einer vorhandenen Buchung die Zeile betrifft
(``RowCtx.roles``: ganze Buchung, Abgangs- bzw. Zugangsseite eines Transfers, Teil). Hier wird jede Zeile gegen ihre
beste vorhandene Buchung Feld für Feld bewertet – mehrdimensional: gleiche Menge oder gleicher Zeitpunkt allein
beweisen keine Identität, eine Kennung bzw. dieselbe Blockchain-Transaktion schon.

Ergebnis (``cat``):

* ``dublette`` – derselbe Vorgang, die neue Quelle bringt nichts hinzu → verknüpfen (nicht buchen)
* ``ergaenzung`` – derselbe Vorgang, die neue Quelle ergänzt Angaben (Hash, genaue Uhrzeit, Zeitpunkt bzw. EUR-Wert
  der Originalquelle …) → verknüpfen: die Angaben bleiben mit Herkunft erhalten, die Buchung bleibt unverändert
* ``neu`` – kein Gegenstück im Bestand → übernehmen
* ``widerspruch`` – Gegenstück gefunden, aber Werte widersprechen sich (Menge, Asset, Konto, Gebühr, Zeit, EUR-Wert)
  oder nur schwacher Hinweis (gleiche Menge) → einzeln prüfen
* ``komplex`` – 1:n bzw. n:1 (Teil eines Vorgangs, gleiche Blockchain-Transaktion mit fehlenden Teilen, rekonstruierte
  Buchung, Gegenbuchung nur im kuratierten Import, mehrere Kandidaten) → einzeln prüfen

Sicherheit (``conf``) bewusst qualitativ statt mit Scheingenauigkeit: ``sicher`` (gleiche Kennung bzw.
Blockchain-Transaktion, Werte gleich), ``hoch`` (alle Merkmale stimmen, nur geringe Abweichungen), ``mittel``
(deutliche Abweichungen, mehrdeutig, größerer Zeitabstand), ``niedrig`` (nur gleiche Menge).

Quellenvorrang (Phase 3) gilt **je Feld** und ist nie absolut: Originalquelle vor abgeleiteter Quelle (Börse für
Börsenbeine, Gebühren und Ausführungswerte; Blockchain für Hash, Netzwerkgebühr und Wallet-Seite; Steuertool für
Verknüpfungen und Einordnung), manuelle Korrekturen haben immer Vorrang. Der Vorrang wird nur angezeigt und begründet
– vorhandene Buchungen werden nie automatisch überschrieben.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.csvimport import reconcile as R
from app.csvimport.events import derive_tx_hash, normalize_hash
from app.importer import contract as C
from app.util.timeutil import fmt_de_date, fmt_de_datetime, to_local_date

CATS = ("dublette", "ergaenzung", "neu", "widerspruch", "komplex")
CAT_LABEL = {"dublette": "Eindeutige Dublette", "ergaenzung": "Ergänzende Informationen", "neu": "Neue Transaktion",
             "widerspruch": "Widersprüchlich / unklar", "komplex": "Komplexe Zuordnung"}
CAT_HINT = {"dublette": "Identisch mit einer vorhandenen Buchung, eindeutig zugeordnet",
            "ergaenzung": "Vorhandene Buchung, die neue Quelle ergänzt Angaben",
            "neu": "Kein Gegenstück im Bestand",
            "widerspruch": "Gegenstück gefunden, aber abweichende Werte – einzeln prüfen",
            "komplex": "Zusammengefasst bzw. aufgeteilt (1:n, n:1) – einzeln prüfen"}
CAT_BADGE = {"dublette": "", "ergaenzung": "info", "neu": "good", "widerspruch": "warn", "komplex": "warn"}
CONF = ("sicher", "hoch", "mittel", "niedrig")
CONF_BADGE = {"sicher": "good", "hoch": "good", "mittel": "info", "niedrig": "warn"}
ACTION_LABEL = {"link": "verknüpfen", "include": "übernehmen", "review": "einzeln prüfen", None: "–"}
SAFE_CONF = ("sicher", "hoch")  # Vorauswahl der Stapelaktionen

# Erkennungsregeln (``RowCtx.basis``)
BASIS_LABEL = {
    "ext": "gleiche Kennung derselben Quelle (bereits übernommen)",
    "event": "Vorgang derselben Quelle bereits übernommen",
    "link": "mit „verknüpfen“ entschieden",
    "ref": "gleiche Kennung im Import",
    "id": "gleiche Anbieter-ID",
    "hash": "gleiche Blockchain-Transaktion",
    "hash_full": "gleiche Blockchain-Transaktion, alle Teile gefunden",
    "hash_partial": "gleiche Blockchain-Transaktion, nicht alle Teile gefunden",
    "file_dup": "doppelte Zeile in der Datei",
    "sig": "gleiche Assets, Mengen und Zeitpunkt",
    "transfer_leg": "Seite eines erfassten Transfers",
    "transfer_leg_acc": "Seite eines erfassten Transfers unter anderem Kontonamen",
    "same_qty": "gleiche Menge auf demselben Konto",
    "reconstructed": "nahe einer rekonstruierten Buchung",
    "event_part": "Teil eines bereits vorhandenen Vorgangs",
    "counterpart": "Gegenbuchung im kuratierten Import",
    "jpair": "Transfer mit bereits übernommener Buchung",
    "asset_mismatch": "gleiche Menge, Konto und Zeit, aber anderes Asset",
    "conversion_twin": "derselbe Umtausch aus einem anders benannten Ausgangs-Asset (Ticker-Umbenennung?)",
}
IDENTITY = frozenset({"ext", "event", "link", "ref", "id", "hash", "hash_full", "file_dup"})
SAME_SOURCE = frozenset({"ext", "event", "file_dup", "link"})  # nichts zu verknüpfen: bereits entschieden/übernommen
COMPLEX = frozenset({"hash_partial", "reconstructed", "event_part", "counterpart", "jpair", "conversion_twin"})

# Quellarten und feldbezogener Vorrang
KIND_LABEL = {"exchange": "Börse", "chain": "Blockchain/Wallet", "taxtool": "Steuertool", "manual": "manuell",
              "app": "Portfolia", "plan": "Sparplan", "reconstructed": "rekonstruiert", "import": "kuratierter Import"}
TAXTOOLS = frozenset({"koinly", "blockpit", "cointracking", "accointing", "cointracker", "coinpanda", "divly",
                      "koinly_universal"})
_ORIGINAL = ("manual", "exchange", "chain", "app", "taxtool", "import", "plan", "reconstructed")
PRIORITY: dict[str, tuple[str, ...]] = {
    # Feld → Rangfolge der Quellarten (vorne = Vorrang); manuelle Korrekturen gehen immer vor
    "hash": ("manual", "chain", "exchange", "app", "taxtool", "import", "plan", "reconstructed"),
    "ts": ("manual", "exchange", "chain", "app", "taxtool", "import", "plan", "reconstructed"),
    "value": ("manual", "exchange", "app", "taxtool", "import", "chain", "plan", "reconstructed"),
    "fee": ("manual", "exchange", "chain", "app", "taxtool", "import", "plan", "reconstructed"),
    "net_fee": ("manual", "chain", "exchange", "app", "taxtool", "import", "plan", "reconstructed"),
    "type": ("manual", "taxtool", "import", "app", "exchange", "chain", "plan", "reconstructed"),
    "counter": ("manual", "taxtool", "import", "chain", "app", "exchange", "plan", "reconstructed"),
    "qty": _ORIGINAL,
}
PRIORITY_REASON = {
    "hash": "Blockchain-Daten sind die Originalquelle des Transaktions-Hashes",
    "ts": "Zeitpunkt der Originalquelle (Ausführung bei der Börse bzw. Bestätigung auf der Chain)",
    "value": "EUR-Wert der Ausführung bei der Börse vor abgeleiteten Werten",
    "fee": "Börsengebühr laut Börse; Netzwerkgebühr laut Blockchain",
    "net_fee": "Netzwerkgebühr laut Blockchain",
    "type": "Einordnung und Verknüpfungen (z. B. Transfer zwischen eigenen Wallets) laut Steuertool bzw. Nutzer",
    "counter": "Gegenkonto laut Steuertool bzw. Blockchain",
    "qty": "Menge laut Originalquelle",
}
TIME_MINOR_S = 15 * 60  # geringe Zeitabweichung (Erfassung vs. Ausführung)
TRANSFER_MINOR_S = 6 * 3600  # Transferseiten: Auslösung und Gutschrift liegen auseinander
TRANSFER_BASES = frozenset({"transfer_leg", "transfer_leg_acc"})  # Treffer auf eine Transferseite (transfer_side.py)
TRANSFER_VALUE_REL = Decimal("0.15")  # Transferseite: Kursbewegung zwischen Auslösung und Gutschrift (bis 7 Tage)
ROUND_REL = Decimal("0.0001")  # Rundungsdifferenz (0,01 %)


def source_kind(source: str | None) -> str:
    """Quellart einer Buchung bzw. eines Prüf-Stapels: ``sync:<anbieter>`` (Börse/Chain laut Anbieterkatalog),
    ``csv:<profil>`` (Gruppe des Profils), Journal (``manual``, ``transfer``) bzw. Quelle des kuratierten Imports."""
    s = (source or "").strip().lower().removeprefix("portfolia:")
    if not s:
        return "import"
    if s in ("manual", "manuell", "writeoff"):
        return "manual"
    if s in ("transfer", "plan"):
        return "app" if s == "transfer" else "plan"
    if s == "reconstructed" or "reconstruct" in s:
        return "reconstructed"
    if s.startswith("sync:"):
        from app.datasources.providers import PROVIDERS, WALLET

        p = PROVIDERS.get(s.split(":", 1)[1])
        return "chain" if p is not None and p.kind == WALLET else "exchange"
    if s.startswith("csv:"):
        pid = s.split(":", 1)[1]
        if pid.startswith("mapping:"):
            return "import"
        from app.csvimport.profiles import PROFILES

        prof = PROFILES.get(pid)
        group = getattr(prof, "group", "") if prof is not None else ""
        return {"Börse": "exchange", "Wallet": "chain", "Steuertool": "taxtool", "Portfolia": "app"}.get(group,
                                                                                                         "import")
    base = s.split(":", 1)[0].split()[0] if s else s
    if base in TAXTOOLS:
        return "taxtool"
    from app.datasources.providers import EXCHANGE, PROVIDERS

    p = PROVIDERS.get(base)
    if p is not None:
        return "exchange" if p.kind == EXCHANGE else "chain"
    return "import"


def preferred(fld: str, new_kind: str, old_kind: str, old_manual: bool = False) -> str | None:
    """Welche Seite hat für das Feld Vorrang? ``old`` | ``new`` | None (gleichrangig). Eine manuell korrigierte
    vorhandene Buchung geht immer vor."""
    if old_manual:
        return "old"
    order = PRIORITY.get(fld, _ORIGINAL)
    rank = {k: i for i, k in enumerate(order)}
    a, b = rank.get(new_kind, len(order)), rank.get(old_kind, len(order))
    if a == b:
        return None
    return "new" if a < b else "old"


# ----------------------------------------------------------------------------------------------------
# Seiten des Vergleichs
# ----------------------------------------------------------------------------------------------------

Leg = tuple[str, str, Decimal]  # (Konto, Asset, Menge)


def _d(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _leg(acc: Any, asset: Any, qty: Any) -> Leg | None:
    q = _d(qty)
    return (str(acc or ""), str(asset), abs(q)) if asset and q else None


@dataclass
class Book:
    """Eine Seite: neue Zeile oder vorhandene Buchung, normalisiert."""

    ref: str
    ts: datetime | None
    date_only: bool = False
    type: str | None = None
    tag: str | None = None
    out: Leg | None = None
    inn: Leg | None = None
    fee: tuple[str, Decimal] | None = None
    fee_eur: Decimal | None = None
    value_eur: Decimal | None = None
    value_src: str | None = None  # Herkunft des EUR-Werts (neue Zeile)
    hashes: set[str] = field(default_factory=set)
    kind: str = "import"  # Quellart
    source: str | None = None
    manual: bool = False  # in der App korrigiert bzw. manuell erfasst
    status: str | None = None  # Journal: deleted | merged | …
    fee_basis: str | None = None
    found: bool = True

    @property
    def total_out(self) -> Decimal | None:
        """Gesamtabgang im Asset des Abgangs (Abgang + Gebühr im selben Asset) – so bucht der Ledger."""
        if self.out is None:
            return None
        fee = self.fee[1] if self.fee and self.fee[0] == self.out[1] else Decimal(0)
        return self.out[2] + fee


def new_book(rc: Any, kind: str, source: str) -> Book:
    rec = rc.rec
    h = normalize_hash(rec.txhash or derive_tx_hash(rec.ext_id))
    b = Book(ref=f"Zeile {rc.line}", ts=None if rec.ts_missing else rec.ts, date_only=bool(rec.date_only),
             hashes={h} if h else set(), kind=kind, source=source, fee_basis=fee_basis_of(rec))
    row = rc.row
    if row is not None:
        b.type, b.tag = row.get("type") or None, row.get("tag") or None
        b.out = _leg(row.get("from_account"), row.get("from_asset"), row.get("from_qty"))
        b.inn = _leg(row.get("to_account"), row.get("to_asset"), row.get("to_qty"))
        fq = _d(row.get("fee_qty"))
        b.fee = (str(row["fee_asset"]), abs(fq)) if row.get("fee_asset") and fq else None
        b.fee_eur, b.value_eur = _d(row.get("fee_eur")), _d(row.get("value_eur"))
        b.value_src = rc.value_src
    else:
        b.tag = rec.tag
        b.out = _leg(rec.account, rec.out_sym, rec.out_qty)
        b.inn = _leg(rec.to_account or rec.account, rec.in_sym, rec.in_qty)
        b.fee = (rec.fee_sym, abs(rec.fee_qty)) if rec.fee_sym and rec.fee_qty else None
    return b


def fee_basis_of(rec: Any) -> str | None:
    """Gebührenbeleg der Quelle (``Rec.fee_basis``); ältere Bitpanda-Zeilen (vor 0.17.0) tragen ihn nur im Text."""
    if getattr(rec, "fee_basis", None):
        return str(rec.fee_basis)
    text = f"{rec.note or ''} {rec.review or ''}"
    if "zusätzlich abgezogen (laut Saldoverlauf)" in text or "zusätzlich zum Betrag (laut Kurs und Saldo" in text:
        return "extra"
    if "enthält die Gebühr" in text or "ohne Wirkung auf den Bestand (laut Saldoverlauf)" in text:
        return "inside"
    if "ob der Betrag sie bereits enthält" in text or "ob der Betrag sie enthält" in text:
        return "open"
    return None


class Books:
    """Vorhandene Buchungen nach Kennung: erfasste Buchungen (inkl. Änderungen der App), Import, Journal (jeder
    Status) – mit Quellart und Kennzeichen „manuell korrigiert“."""

    def __init__(self, ctx: Any, pf: Any, ids: set[str]) -> None:
        self.by_id: dict[str, Book] = {}
        if not ids:
            return
        db = ctx.db
        recorded = {t.tx_id: t for t in pf.txs} if pf is not None else {}
        base = None
        missing = [i for i in ids if i not in recorded]
        if missing:
            b, _ = ctx.effective_base()
            base = {t.tx_id: t for t in b.txs} if b is not None else {}
        journal: dict[str, Any] = {}
        want = sorted(ids)
        for i in range(0, len(want), 500):
            part = want[i:i + 500]
            for r in db.q(f"SELECT tx_id, source, status, tx_hash, external_id, ts_utc, type, tag, from_account, "
                          f"from_asset, from_qty, to_account, to_asset, to_qty, fee_asset, fee_qty, fee_eur, "
                          f"value_eur, date_only FROM journal_tx WHERE tx_id IN ({','.join('?' * len(part))})", part):
                journal[r["tx_id"]] = r
        edited_import = {r["tx_id"] for r in db.q("SELECT tx_id FROM tx_override WHERE action='edit'")}
        edited_journal = {r["ref"] for r in db.q("SELECT DISTINCT ref FROM journal_log WHERE action='update'")}
        for tid in ids:
            t = recorded.get(tid) or (base or {}).get(tid)
            j = journal.get(tid)
            if t is not None:
                self.by_id[tid] = self._from_tx(t, j, tid in edited_import or tid in edited_journal)
            elif j is not None:
                self.by_id[tid] = self._from_journal(j, tid in edited_journal)
            else:
                self.by_id[tid] = Book(ref=tid, ts=None, found=False)

    @staticmethod
    def _from_tx(t: Any, j: Any, edited: bool) -> Book:
        if t.origin == "journal":
            src = j["source"] if j is not None else t.source
            kind = source_kind(src)
            hashes = {h for h in [normalize_hash(j["tx_hash"]) if j is not None else None] if h}
        elif t.origin == "plan":
            src, kind, hashes = t.source, "plan", set()
        else:
            src, kind = t.source, source_kind(t.source)
            hashes = R.hashes_in(t.note, t.source_ref)
        return Book(ref=t.tx_id, ts=t.ts, date_only=bool(t.date_only), type=t.type, tag=t.tag,
                    out=_leg(t.from_account, t.from_asset, t.from_qty), inn=_leg(t.to_account, t.to_asset, t.to_qty),
                    fee=(t.fee_asset, abs(t.fee_qty)) if t.fee_asset and t.fee_qty else None, fee_eur=t.fee_eur,
                    value_eur=t.value_eur, hashes=hashes, kind=kind, source=src,
                    manual=edited or kind == "manual",
                    status=j["status"] if j is not None and j["status"] != "active" else None)

    @staticmethod
    def _from_journal(j: Any, edited: bool) -> Book:
        from app.util.timeutil import parse_iso

        fq = _d(j["fee_qty"])
        h = normalize_hash(j["tx_hash"])
        kind = source_kind(j["source"])
        return Book(ref=j["tx_id"], ts=parse_iso(j["ts_utc"]), date_only=bool(j["date_only"]), type=j["type"],
                    tag=j["tag"], out=_leg(j["from_account"], j["from_asset"], j["from_qty"]),
                    inn=_leg(j["to_account"], j["to_asset"], j["to_qty"]),
                    fee=(j["fee_asset"], abs(fq)) if j["fee_asset"] and fq else None, fee_eur=_d(j["fee_eur"]),
                    value_eur=_d(j["value_eur"]), hashes={h} if h else set(), kind=kind, source=j["source"],
                    manual=edited or kind == "manual", status=j["status"] if j["status"] != "active" else None)

    def get(self, tid: str) -> Book:
        return self.by_id.get(tid) or Book(ref=tid, ts=None, found=False)


# ----------------------------------------------------------------------------------------------------
# Bewertung
# ----------------------------------------------------------------------------------------------------

def _q(v: Decimal | None) -> str:
    from app.web.fmt import qty_exact

    return qty_exact(v)


def _e(v: Decimal | None) -> str:
    from app.web.fmt import eur

    return eur(v)


def _pct(a: Decimal, b: Decimal) -> str:
    base = max(abs(a), abs(b))
    if not base:
        return "0 %"
    p = abs(a - b) / base * 100
    return f"{p:.2f} %".replace(".", ",") if p < 10 else f"{p:.0f} %"


def _span_text(seconds: float) -> str:
    s = int(abs(seconds))
    if s < 3600:
        return f"{max(1, round(s / 60))} min"
    h, rest = divmod(s, 3600)
    if h < 48:
        m = rest // 60
        return f"{h} h" + (f" {m} min" if m and h < 6 else "")
    return f"{s // 86400} Tage"


def _round(b: Book) -> bool:
    """Runde Menge (höchstens drei signifikante Stellen, z. B. 0,5 ETH oder 250 EUR)."""
    legs = [x for x in (b.out, b.inn) if x is not None]
    return bool(legs) and all(len(x[2].normalize().as_tuple().digits) <= 3 for x in legs)


def _qty_same(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(abs(b) * Decimal("1e-9"), Decimal("1e-10"))


def _value_src_kind(src: str | None) -> str:
    """Herkunft des EUR-Werts der neuen Zeile: source (Datei/API bzw. Fiat-Betrag), manual (Eingabe), derived
    (Portfolia-Kurs × Menge)."""
    if not src:
        return "source"
    if src == "Eingabe":
        return "manual"
    if "× Menge" in src or "Kurs des Vorgangs" in src:
        return "derived"
    return "source"


@dataclass
class _Acc:
    ok: list[str] = field(default_factory=list)
    diff: list[dict[str, str]] = field(default_factory=list)
    add: list[dict[str, str]] = field(default_factory=list)
    prio: list[dict[str, str]] = field(default_factory=list)
    fix: list[str] = field(default_factory=list)
    fee: dict[str, str] | None = None

    def d(self, f: str, sev: str, text: str) -> None:
        self.diff.append({"f": f, "sev": sev, "t": text})

    def a(self, f: str, text: str, pref: str | None = None) -> None:
        self.add.append({"f": f, "t": text, **({"pref": pref} if pref else {})})

    def p(self, f: str, side: str | None, new_kind: str, old: Book) -> None:
        if side is None:
            return
        who = KIND_LABEL.get(new_kind, new_kind) if side == "new" else (
            "vorhandene Buchung (manuell korrigiert)" if old.manual else KIND_LABEL.get(old.kind, old.kind))
        self.prio.append({"f": f, "side": side, "t": f"{who}: {PRIORITY_REASON.get(f, '')}"})

    @property
    def relevant(self) -> list[dict[str, str]]:
        return [x for x in self.diff if x["sev"] == "relevant"]


def _cmp_time(acc: _Acc, new: Book, old: Book, role: str, delayed_ok: bool = False) -> float | None:
    """Zeitpunkt vergleichen. ``delayed_ok``: Zugangsseite eines erfassten Transfers – eine spätere Gutschrift
    (verzögerte Auszahlung, Fenster laut :mod:`app.csvimport.transfer_side`) ist kein Widerspruch, und ihr Zeitpunkt
    ergänzt den Transfer, statt dessen Zeitpunkt (Auslösung) zu ersetzen."""
    if new.ts is None or old.ts is None:
        acc.d("ts", "relevant", "Zeitpunkt fehlt auf einer Seite")
        return None
    if new.date_only or old.date_only:
        dn, do = to_local_date(new.ts), to_local_date(old.ts)
        if dn == do:
            acc.ok.append("gleicher Tag")
            if old.date_only and not new.date_only:
                acc.a("ts", f"genaue Uhrzeit {fmt_de_datetime(new.ts)}", "new")
        else:
            acc.d("ts", "relevant", f"anderer Tag: vorhanden {fmt_de_date(do)}, neu {fmt_de_date(dn)}")
        return None
    sec = (old.ts - new.ts).total_seconds()
    a = abs(sec)
    when = f"vorhanden {fmt_de_datetime(old.ts)[-5:]}, neu {fmt_de_datetime(new.ts)[-5:]}" if a < 86400 else \
        f"vorhanden {fmt_de_datetime(old.ts)}, neu {fmt_de_datetime(new.ts)}"
    if a < 60:
        acc.ok.append("gleicher Zeitpunkt")
    elif a <= TIME_MINOR_S:
        acc.d("ts", "minor", f"Zeitpunkt {_span_text(a)} abweichend ({when})")
    elif (a % 3600 >= 3600 - 120 or a % 3600 <= 120) and a <= 14 * 3600:
        acc.d("ts", "minor", f"Zeitpunkt um {round(a / 3600)} h versetzt – vermutlich Zeitzone ({when})")
    elif role in ("out", "in") and a <= TRANSFER_MINOR_S:
        acc.d("ts", "minor", f"Zeitpunkt {_span_text(a)} abweichend – bei Transfers liegen Auslösung und "
                             f"Gutschrift auseinander ({when})")
    elif role == "in" and delayed_ok and sec < 0:
        acc.d("ts", "minor", f"Gutschrift {_span_text(a)} nach der Auslösung des Transfers – verzögerte Auszahlung "
                             f"bzw. Bestätigung ({when})")
    else:
        acc.d("ts", "relevant", f"Zeitpunkt {_span_text(a)} abweichend ({when})")
    if a >= 60 and role == "in" and delayed_ok and sec < 0:
        acc.a("ts", f"Zeitpunkt der Gutschrift {fmt_de_datetime(new.ts)} (laut {KIND_LABEL.get(new.kind, new.kind)};"
                    " der Transfer behält den Zeitpunkt der Auslösung)")
    elif a >= 60:
        side = preferred("ts", new.kind, old.kind, old.manual)
        if side == "new":
            acc.a("ts", f"Zeitpunkt der {KIND_LABEL.get(new.kind, new.kind)} {fmt_de_datetime(new.ts)}", "new")
        acc.p("ts", side, new.kind, old)
    return a


def _cmp_leg(acc: _Acc, label: str, side: str, a: Leg | None, b: Leg | None, *, skip_qty: bool = False) -> None:
    if a is None and b is None:
        return
    if a is None or b is None:
        acc.d(side, "relevant", f"{label} nur {'in der neuen Zeile' if a else 'in der vorhandenen Buchung'}")
        return
    acc_a, asset_a, qa = a
    acc_b, asset_b, qb = b
    if asset_a != asset_b:
        acc.d(side, "relevant", f"{label}: anderes Asset ({asset_a} statt {asset_b}) – Asset-Zuordnung prüfen")
    elif _qty_same(qa, qb):
        acc.ok.append(f"{label} {_q(qa)} {asset_a}")
    elif skip_qty:
        pass
    elif abs(qa - qb) <= max(abs(qa), abs(qb)) * ROUND_REL:
        acc.d(side, "minor", f"{label}: Rundungsdifferenz ({_q(qa)} statt {_q(qb)} {asset_a})")
    else:
        acc.d(side, "relevant", f"{label}: {_q(qa)} statt {_q(qb)} {asset_a} ({_pct(qa, qb)} Abweichung)")
    if acc_a and acc_b:
        if acc_a == acc_b:
            acc.ok.append(f"Konto {acc_a}")
        else:
            acc.d("acct", "relevant", f"{label}: Konto {acc_a} statt {acc_b}")


def _cmp_fee(acc: _Acc, new: Book, old: Book) -> bool:
    """Gebührenprüfung für Abgänge (ganze Buchung bzw. Abgangsseite eines Transfers). Vergleicht den Gesamtabgang,
    wie ihn der Ledger bucht (Abgang + Gebühr im selben Asset), und den Gebührenbeleg der Quelle. Rückgabe: True,
    wenn eine Mengenabweichung des Abgangs vollständig durch die Darstellung der Gebühr erklärt ist."""
    nf, of = new.fee, old.fee
    if nf is None and of is None:
        return False
    nt, ot = new.total_out, old.total_out
    unit = new.out[1] if new.out else (nf or of or ("", 0))[0]
    explained = False
    if nf and of and nf[0] == of[0] and _qty_same(nf[1], of[1]):
        same_totals = nt is not None and ot is not None and _qty_same(nt, ot)
        if same_totals and new.fee_basis == "extra":
            acc.fee = {"state": "ok", "t": f"Gebühr {_q(nf[1])} {nf[0]} laut Quelle zusätzlich zum Betrag belastet "
                                           f"(belegt durch den Saldoverlauf) – die vorhandene Buchung bucht sie ebenso "
                                           f"zusätzlich: Abgang gesamt {_q(ot)} {unit}"}
        elif same_totals and new.fee_basis == "inside":
            acc.fee = {"state": "ok", "t": f"Gebühr {_q(nf[1])} {nf[0]} im Betrag enthalten (belegt) – die vorhandene "
                                           f"Buchung ebenso: Abgang gesamt {_q(ot)} {unit}"}
        elif same_totals and new.fee_basis == "open":
            acc.fee = {"state": "open", "t": f"Gebühr {_q(nf[1])} {nf[0]} in beiden Quellen gleich. Ob die Quelle sie "
                                             "zusätzlich zum Betrag belastet hat, belegen ihre Daten nicht (kein "
                                             f"Saldoverlauf) – die vorhandene Buchung zieht Betrag und Gebühr ab: "
                                             f"gesamt {_q(ot)} {unit}. Mit dem Beleg der Quelle vergleichen."}
        elif same_totals:
            acc.fee = {"state": "ok", "t": f"gleiche Gebühr {_q(nf[1])} {nf[0]} (gesamt {_q(ot)} {unit})"}
        else:
            acc.fee = {"state": "ok", "t": f"gleiche Gebühr {_q(nf[1])} {nf[0]}"}
            if nt is not None and old.out is not None and new.out is not None and _qty_same(nt, old.out[2]):
                # Quelle: Gebühr im Betrag (netto + Gebühr = vorhandener Abgang), vorhanden: Gebühr zusätzlich
                acc.fee = {"state": "conflict", "t": (
                    f"Gebühr unterschiedlich gebucht: laut {KIND_LABEL.get(new.kind, new.kind)}"
                    f"{' (belegt durch den Saldoverlauf)' if new.fee_basis == 'inside' else ''} ist sie im Betrag "
                    f"{_q(old.out[2])} enthalten (Abgang gesamt {_q(nt)} {unit}); die vorhandene Buchung zieht sie "
                    f"zusätzlich ab (gesamt {_q(ot)} {unit}) – Differenz {_q(nf[1])} {unit}")}
                acc.fix.append(f"Vorhandene Buchung {old.ref} prüfen: Abgang {_q(new.out[2])} + Gebühr {_q(nf[1])} "
                               f"{unit} (= {_q(nt)} gesamt), sofern der Beleg der Quelle das bestätigt")
                explained = True
            elif ot is not None and new.out is not None and old.out is not None and _qty_same(ot, new.out[2]):
                acc.fee = {"state": "conflict", "t": (
                    f"Gebühr unterschiedlich gebucht: laut vorhandener Buchung im Betrag {_q(new.out[2])} enthalten "
                    f"(gesamt {_q(ot)} {unit}), laut {KIND_LABEL.get(new.kind, new.kind)} zusätzlich (gesamt "
                    f"{_q(nt)} {unit}) – Differenz {_q(nf[1])} {unit}")}
                explained = True
    elif nf and of:
        acc.fee = {"state": "conflict", "t": f"Gebühr weicht ab: neu {_q(nf[1])} {nf[0]}, vorhanden {_q(of[1])} "
                                             f"{of[0]}"}
        side = preferred("fee", new.kind, old.kind, old.manual)
        acc.p("fee", side, new.kind, old)
    else:
        only_new = nf is not None
        f = nf or of
        assert f is not None
        if nt is not None and ot is not None and _qty_same(nt, ot):
            acc.fee = {"state": "ok", "t": (
                f"Gebühr {_q(f[1])} {f[0]} nur {'in der neuen Quelle' if only_new else 'in der vorhandenen Buchung'} "
                f"ausgewiesen, die andere führt sie im Abgang – Gesamtabgang gleich ({_q(nt)} {unit})")}
            explained = True
        else:
            acc.fee = {"state": "conflict", "t": (
                f"Gebühr {_q(f[1])} {f[0]} nur {'in der neuen Quelle' if only_new else 'in der vorhandenen Buchung'}"
                " – der Bestand unterscheidet sich um die Gebühr")}
            if only_new:
                acc.fix.append(f"Gebühr {_q(f[1])} {f[0]} in der vorhandenen Buchung {old.ref} ergänzen, sofern sie "
                               "dort fehlt")
    return explained


def _cmp_value(acc: _Acc, new: Book, old: Book, role: str = "same") -> None:
    nv, ov = new.value_eur, old.value_eur
    vkind = _value_src_kind(new.value_src)
    origin = {"source": f"laut {KIND_LABEL.get(new.kind, new.kind)}", "manual": "Eingabe",
              "derived": f"Portfolia: {new.value_src}"}.get(vkind, "")
    if nv is None or nv == 0:
        return
    if ov is None or ov == 0:
        if vkind == "derived":  # aus Portfolia-Kursen berechnet – keine Angabe der Quelle
            acc.ok.append(f"EUR-Wert nur in der neuen Zeile ({origin})")
        else:
            acc.a("value", f"EUR-Wert {_e(nv)} ({origin})", "new")
        return
    dv = abs(nv - ov)
    if dv <= Decimal("0.01"):
        acc.ok.append(f"EUR-Wert {_e(ov)}")
        return
    trade_like = new.type in ("buy", "sell", "trade") or (new.tag or "") in C.INCOME_TAGS
    tol_abs, tol_rel = (Decimal(1), Decimal("0.01")) if trade_like else (Decimal(2), Decimal("0.03"))
    text = f"EUR-Wert neu {_e(nv)} ({origin}), vorhanden {_e(ov)} – {_pct(nv, ov)}"
    if dv <= tol_abs or dv / max(nv, ov) <= tol_rel:
        acc.d("value", "minor", text)
    elif role in ("in", "out") and dv / max(nv, ov) <= TRANSFER_VALUE_REL:
        # Seite eines Transfers: Wert nur informativ (der Einstand wandert mit), oft zu anderen Zeitpunkten bewertet
        # (verzögerte Gutschrift) – grobe Abweichungen deuten dagegen auf eine falsche Kurs-/Asset-Zuordnung
        acc.d("value", "minor", text + " – Transferseite, zu verschiedenen Zeitpunkten bewertet (nur informativ)")
    else:
        acc.d("value", "relevant", text + " – Kurs- bzw. Asset-Zuordnung prüfen")
    if vkind == "source":
        side = preferred("value", new.kind, old.kind, old.manual)
        if side == "new":
            acc.a("value", f"EUR-Wert {origin} {_e(nv)}", "new")
        acc.p("value", side, new.kind, old)


def _cmp_hash(acc: _Acc, new: Book, old: Book) -> None:
    if new.hashes and old.hashes:
        if new.hashes & old.hashes:
            acc.ok.append("gleiche Blockchain-Transaktion")
        else:
            acc.d("hash", "relevant", "verschiedene Transaktions-Hashes – vermutlich zwei Vorgänge")
    elif new.hashes:
        acc.a("hash", "Transaktions-Hash", "new" if preferred("hash", new.kind, old.kind) != "old" else None)


def _cmp_type(acc: _Acc, new: Book, old: Book, role: str) -> None:
    from app.journal.forms import TAG_LABEL, TYPE_LABEL

    if role == "out" and old.type == "transfer":
        acc.ok.append(f"Abgangsseite des Transfers {old.out[0] if old.out else '?'} → {old.inn[0] if old.inn else '?'}")
        return
    if role == "in" and old.type == "transfer":
        acc.ok.append(f"Zugangsseite des Transfers {old.out[0] if old.out else '?'} → {old.inn[0] if old.inn else '?'}")
        return
    if not new.type or not old.type:
        return
    if new.type != old.type:
        acc.d("type", "relevant", f"Art: {TYPE_LABEL.get(new.type, new.type)} statt "
                                  f"{TYPE_LABEL.get(old.type, old.type)}")
        return
    if (new.tag or None) != (old.tag or None):
        side = preferred("type", new.kind, old.kind, old.manual)
        acc.d("type", "minor", f"Einordnung: {TAG_LABEL.get(new.tag or '', new.tag or 'ohne')} statt "
                               f"{TAG_LABEL.get(old.tag or '', old.tag or 'ohne')}"
                               + (" – maßgeblich bleibt die vorhandene Buchung" if side != "new" else ""))
        acc.p("type", side, new.kind, old)
    else:
        acc.ok.append(f"gleiche Art ({TYPE_LABEL.get(new.type, new.type)})")


def _neu(rc: Any) -> dict[str, Any]:
    hints: list[str] = []
    if rc.rec.review:
        hints.append("Quelle markiert den Vorgang als prüfbedürftig")
    if rc.counterpart:
        return {"cat": "komplex", "conf": "mittel", "basis": "counterpart", "target": rc.counterpart, "role": "part",
                "action": "review", "ok": [], "diff": [{"f": "struct", "sev": "relevant", "t": (
                    f"passende Gegenbuchung {rc.counterpart} nur im kuratierten Import – als Transfer dort erfassen")}],
                "add": [], "why": BASIS_LABEL["counterpart"]}
    if rc.pair_ref and rc.pair_ref.startswith("j:"):
        return {"cat": "komplex", "conf": "hoch" if rc.pair_conf == "hoch" else "mittel", "basis": "jpair",
                "target": rc.pair_ref[2:], "role": "part", "action": "review", "ok": [], "diff": [
                    {"f": "struct", "sev": "relevant", "t": "Transfer mit einer bereits übernommenen Buchung – deren "
                                                            "Lots werden fortgeführt; nur einzeln bestätigen"}],
                "add": [], "why": BASIS_LABEL["jpair"]}
    if rc.transfer_unclear:
        hints.append("möglicher Transfer mit mittlerer Sicherheit")
    if rc.errors:
        hints.append("unvollständig: " + rc.errors[0])
    if rc.status == "unclear":
        hints.append("ungeklärte Vorgangsart")
    action = "include" if rc.status == "new" and not rc.errors and not rc.rec.review and not rc.transfer_unclear \
        and rc.row is not None else "review"
    return {"cat": "neu", "conf": "hoch" if not hints else "mittel", "basis": None, "target": None, "role": None,
            "action": action, "ok": [], "diff": [], "add": [], "hints": hints,
            "why": "kein Gegenstück im Bestand gefunden (Kennung, Hash, Menge/Zeit, Transfers)"}


def _transfer_side(acc: _Acc, rc: Any, new: Book, old: Book, target: str, role: str, basis: str) -> None:
    """Zu- bzw. Abgang als Seite eines erfassten Transfers: Belege (Zeitpunkt laut Notiz) und – unter anderem
    Kontonamen – die Korrekturvorschläge (nie automatisch)."""
    side = getattr(rc, "side", None) or {}
    if side.get("note_time"):
        from app.util.timeutil import parse_iso

        nt = parse_iso(side["note_time"])
        if nt is not None:
            acc.ok.append(f"Zeitpunkt der Gutschrift laut Notiz von {target}: {fmt_de_datetime(nt)}")
    if basis != "transfer_leg_acc":
        return
    mine = (new.inn if role == "in" else new.out) or ("", "", Decimal(0))
    theirs = (old.inn if role == "in" else old.out) or ("", "", Decimal(0))
    what = "Zugangsseite" if role == "in" else "Abgangsseite"
    acc.fix.append(f"Nicht zusätzlich buchen: mit {target} verknüpfen ({what} des Transfers) – sonst zählt die Menge "
                   "doppelt und der Einstand der Gegenseite ginge verloren")
    if mine[0] and theirs[0]:
        acc.fix.append(f"Konten angleichen: „{mine[0]}“ und „{theirs[0]}“ bezeichnen vermutlich dasselbe Wallet – "
                       f"Konto der Datenquelle auf „{theirs[0]}“ umstellen bzw. im kuratierten Import vereinheitlichen")


def assess(rc: Any, books: Books, new_kind: str, source: str) -> dict[str, Any] | None:
    """Bewertung einer offenen Prüfzeile (oder None, wenn nichts zu bewerten ist)."""
    if rc.status == "ignored" or not rc.open:
        return None
    ids = list(dict.fromkeys(rc.dup_of))
    if not ids:
        if rc.status == "known":
            return None
        if rc.status == "before":  # vor dem Stichtag nicht im Bestand gefunden: mögliche Lücke im kuratierten Import
            if rc.row is None:
                return None  # nicht abgleichbar (z. B. Asset unbekannt)
            return {"cat": "neu", "conf": "mittel", "basis": None, "target": None, "role": None, "action": "review",
                    "ok": [], "diff": [], "add": [], "hints": ["vor dem Stichtag nicht im Bestand gefunden – mögliche "
                                                              "Lücke im kuratierten Import"],
                    "why": "kein Gegenstück im Bestand gefunden (Kennung, Hash, Menge/Zeit, Transfers)"}
        return _neu(rc)
    basis = rc.basis or ("sig" if rc.status == "duplicate" else "id")
    target = ids[0]
    role = rc.roles.get(target) or ("part" if basis in ("event_part", "hash_partial") else "same")
    old = books.get(target)
    new = new_book(rc, new_kind, source)
    acc = _Acc()
    if not old.found:
        return {"cat": "widerspruch", "conf": "niedrig", "basis": basis, "target": target, "role": role,
                "action": "review", "ok": [], "diff": [{"f": "struct", "sev": "relevant",
                                                        "t": f"vorhandene Buchung {target} nicht mehr auffindbar"}],
                "add": [], "why": BASIS_LABEL.get(basis, basis)}
    if old.status in ("deleted", "reverted", "replaced"):
        state = {"deleted": "gelöscht", "reverted": "rückgängig gemacht", "replaced": "ersetzt"}[old.status]
        acc.d("struct", "relevant", f"vorhandene Buchung {target} ist {state}")
    # Felder
    seconds = _cmp_time(acc, new, old, role, delayed_ok=basis in TRANSFER_BASES)
    if role == "part":
        acc.ok.append("Teil desselben Vorgangs")
    else:
        _cmp_type(acc, new, old, role)
        explained = _cmp_fee(acc, new, old) if role in ("same", "out") and (new.out or old.out) else False
        if role == "in":
            inn_old = old.inn
            if new.inn and old.inn and old.out and not _qty_same(new.inn[2], old.inn[2]) and \
                    _qty_same(new.inn[2], old.out[2]):
                inn_old = (old.inn[0], old.inn[1], old.out[2])  # Gutschrift = gesendete Menge
            _cmp_leg(acc, "Zugang", "inn", new.inn, inn_old)
            if old.fee:
                acc.ok.append("Gebühr gehört zur Abgangsseite des Transfers")
        elif role == "out":
            _cmp_leg(acc, "Abgang", "out", new.out, old.out, skip_qty=explained)
            if old.inn and not new.inn:
                acc.ok.append(f"Empfänger {old.inn[0] or '?'} nur in der vorhandenen Buchung (bleibt erhalten)")
        else:
            _cmp_leg(acc, "Abgang", "out", new.out, old.out, skip_qty=explained)
            _cmp_leg(acc, "Zugang", "inn", new.inn, old.inn)
            if new.inn and old.inn and not old.inn[0] and new.inn[0]:
                acc.a("counter", f"Gegenkonto {new.inn[0]}")
        _cmp_value(acc, new, old, role)
    _cmp_hash(acc, new, old)
    if basis in TRANSFER_BASES:
        _transfer_side(acc, rc, new, old, target, role, basis)
    if basis not in SAME_SOURCE and new.kind != old.kind:
        acc.a("id", f"Kennung der {KIND_LABEL.get(new.kind, new.kind)}", None)
    # Ergebnis
    relevant = acc.relevant
    fee_conflict = bool(acc.fee and acc.fee.get("state") == "conflict")
    multi = len(ids) > 1 and basis not in IDENTITY
    if basis in COMPLEX or multi:
        cat = "komplex"
    elif relevant or fee_conflict:
        cat = "widerspruch"
    else:
        meaningful = [x for x in acc.add if x["f"] != "id"]
        cat = "ergaenzung" if meaningful else "dublette"
    # Sicherheit
    # „hoch“ nur bei kleinem Zeitabstand; ein Versatz um ganze Stunden (Zeitzone) bleibt „mittel“ – ebenso runde
    # Mengen ohne Kennung (zwei gleiche runde Beträge sind häufiger Zufall als krumme)
    close = seconds is None or seconds <= TIME_MINOR_S or (role in ("out", "in") and seconds <= 2 * 3600)
    if basis in ("sig", "same_qty", *TRANSFER_BASES) and seconds is not None and seconds > 60 and _round(new):
        close = False
    if basis in IDENTITY:
        conf = "sicher" if not relevant and not fee_conflict else "hoch"
    elif basis == "hash_partial":
        conf = "hoch"
    elif basis == "same_qty":
        conf = "mittel" if not relevant else "niedrig"
    elif cat in ("dublette", "ergaenzung"):
        conf = "hoch" if close and not multi else "mittel"
    elif cat == "komplex":
        conf = "mittel"
    else:
        conf = "mittel" if len(relevant) <= 1 and not fee_conflict else "niedrig"
    action: str | None
    if basis in SAME_SOURCE:
        action = None  # bereits übernommen bzw. entschieden
    elif cat in ("dublette", "ergaenzung"):
        action = "link"
    else:
        action = "review"
    why = BASIS_LABEL.get(basis, basis)
    if multi:
        acc.diff.insert(0, {"f": "struct", "sev": "relevant", "t": f"mehrere Kandidaten: {', '.join(ids[:3])}"})
    out: dict[str, Any] = {"cat": cat, "conf": conf, "basis": basis, "target": target, "role": role,
                           "action": action, "ok": acc.ok[:8], "diff": acc.diff[:8], "add": acc.add[:6],
                           "why": why, "src": {"new": new.kind, "old": old.kind, "old_manual": old.manual}}
    if acc.fee:
        out["fee"] = acc.fee
    if acc.prio:
        out["prio"] = acc.prio[:4]
    if acc.fix:
        out["fix"] = acc.fix[:3]
    return out


def assess_rows(ctx: Any, rows: list[Any], source: str, pf: Any) -> None:
    """Alle offenen Zeilen eines Stapels bewerten (``RowCtx.match``)."""
    ids = {t for rc in rows if rc.open for t in rc.dup_of[:5]}
    books = Books(ctx, pf, ids)
    kind = source_kind(source)
    for rc in rows:
        try:
            rc.match = assess(rc, books, kind, source)
        except (ArithmeticError, TypeError, ValueError, KeyError):  # nie die Auswertung blockieren
            rc.match = {"cat": "widerspruch", "conf": "niedrig", "basis": rc.basis, "target": (rc.dup_of or
                                                                                              [None])[0],
                        "role": None, "action": "review", "ok": [], "add": [],
                        "diff": [{"f": "struct", "sev": "relevant", "t": "Abgleich nicht auswertbar – bitte prüfen"}],
                        "why": "Fehler im Abgleich"}
    _competing(rows)


def _competing(rows: list[Any]) -> None:
    """Mehrere offene Zeilen passen nur unscharf (gleiche Menge/Zeit, ohne Kennung oder Hash) auf *dieselbe*
    vorhandene Buchung: höchstens eine kann sie sein. Keine davon gilt dann als sichere Dublette – sonst würden zwei
    echte, gleich große Vorgänge mit einer Buchung verknüpft und einer ginge verloren. → einzeln prüfen."""
    by_target: dict[str, list[Any]] = {}
    for rc in rows:
        m = getattr(rc, "match", None)
        if not m or not rc.open or m.get("basis") in IDENTITY or not m.get("target"):
            continue
        by_target.setdefault(m["target"], []).append(rc)
    for target, rcs in by_target.items():
        if len(rcs) < 2:
            continue
        for rc in rcs:
            m = rc.match
            m.update(cat="komplex", conf="mittel", action="review")
            m["diff"] = [{"f": "struct", "sev": "relevant",
                          "t": f"{len(rcs) - 1} weitere Zeile(n) dieses Stapels passen auf dieselbe Buchung {target} "
                               "– höchstens eine kann diese sein; die anderen sind eigene Vorgänge"}, *m["diff"]][:8]


def counts(rows: list[Any]) -> dict[str, int]:
    """Ergebnisse der offenen, noch zu entscheidenden Zeilen (für die Übersicht)."""
    out = dict.fromkeys(CATS, 0)
    for rc in rows:
        m = getattr(rc, "match", None)
        if m and rc.open and rc.status not in ("known", "ignored", "before"):
            out[m["cat"]] = out.get(m["cat"], 0) + 1
    return out


def is_safe(rc: Any) -> bool:
    """Vorauswahl der Stapelaktion: Dublette bzw. Ergänzung mit hoher Sicherheit (verknüpfen) oder neu ohne
    Prüfhinweis (übernehmen)."""
    m = getattr(rc, "match", None)
    if not m or not rc.open or rc.status in ("known", "ignored", "before"):
        return False
    if m["cat"] in ("dublette", "ergaenzung"):
        return m.get("action") == "link" and m["conf"] in SAFE_CONF
    return m["cat"] == "neu" and m.get("action") == "include" and m["conf"] in SAFE_CONF


def kind_label(kind: str | None) -> str:
    return KIND_LABEL.get(kind or "", kind or "")


__all__ = ["ACTION_LABEL", "BASIS_LABEL", "CATS", "CAT_BADGE", "CAT_HINT", "CAT_LABEL", "CONF", "CONF_BADGE",
           "KIND_LABEL", "assess", "assess_rows", "counts", "fee_basis_of", "is_safe", "kind_label",
           "preferred", "source_kind"]
