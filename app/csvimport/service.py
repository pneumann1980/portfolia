"""CSV-Import: Datei → Vorschau → Übernahme ins Journal → bei Bedarf rückgängig.

Ablauf
------
1. **Hochladen**: Datei (≤ 25 MB) wird komprimiert gespeichert, das Profil erkannt (oder gewählt) und jede Zeile in
   das Zwischenformat :class:`~app.csvimport.model.Rec` übersetzt. Nichts wird online abgefragt.
2. **Auswerten** (bei jeder Änderung von Zuordnungen/Optionen erneut): Symbole → Assets, Konten der Datei →
   Konten in Portfolia, Umwandlung in das einheitliche Buchungsformat (``transactions.csv``), EUR-Werte, Prüfung mit
   demselben Validator wie der Import, Erkennung bereits importierter Zeilen (Quellkennung), möglicher Dubletten
   (gleicher Zeitpunkt ± Zeitzonenversatz, gleiche Mengen) und Transfer-Paare (Abgang hier, Zugang dort).
3. **Übernehmen**: gültige, ausgewählte Zeilen werden Journal-Buchungen (``PF-C-…``); bestätigte Transfer-Paare
   werden zu einer Transfer-Buchung (``PF-T-…``) zusammengeführt, die Einzelbuchungen bleiben als „merged“ erhalten.
   Die Übernahme ist inkrementell: offene Zeilen können später ergänzt und nachgeschoben werden.
4. **Rückgängig**: alle Buchungen des Stapels werden zurückgenommen; Transfers mit Buchungen anderer Stapel werden
   aufgelöst, deren Einzelbuchungen gelten wieder.

Datenquellen (Connectoren) nutzen denselben Weg: :meth:`CsvImportService.ingest` legt ihre normalisierten Vorgänge als
Stapel der Art „sync“ an (Quelle ``sync:<anbieter>``, Kennung ``<ereignis>#<zeile>``). Gleiche Ereignisse aus
anderen Quellen – etwa ein früherer CSV-Import derselben Börse – werden über Ereignis-ID bzw. Transaktions-Hash exakt
erkannt (:mod:`app.csvimport.events`), sonst über die unscharfe Dublettenprüfung, und vor dem Übernehmen angezeigt.

EUR-Werte (Reihenfolge): Eingabe → Wert aus der Datei (Journal-Format) → Fiat-Seite des Handels (Devisenkurs der EZB
bzw. Yahoo für Fremdwährungen) → Gegenwert laut Datei → Stablecoin-Seite (Marktkurs, sonst 1 USD bzw. 1 EUR) →
gespeicherter Tageskurs des erhaltenen bzw. abgegebenen Assets → Transaktionskurs aus Import/Journal oder aus der
Datei (± 31 Tage). Fehlt der Wert bei Handel oder Ertrag, bleibt die Zeile offen, bis er eingegeben oder nach
„Kurse laden“ verfügbar ist.
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import gzip
import hashlib
import json
import logging
import threading
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.csvimport import model as M
from app.csvimport import reconcile as R
from app.csvimport.events import derive_event_key, derive_tx_hash, identity_keys, normalize_hash, source_ref_keys
from app.csvimport.identity import (
    PROVIDER_LABEL,
    confirms,
    identity,
    note_identity_keys,
    provider_key,
    provider_of,
    split_provider_key,
)
from app.csvimport.model import ParseOptions, Rec
from app.csvimport.profiles import BUILTIN, PROFILES, MappingProfile, Profile, detect, header_matcher
from app.csvimport.reader import CsvError, read_table, zone
from app.importer import contract as C
from app.importer.validate import validate_tx_rows
from app.journal.service import JournalService, _now, journal_service, source_label
from app.ledger.engine import run_ledger
from app.ledger.models import AssetInfo, Portfolio, Tx
from app.util.numbers import parse_number
from app.util.timeutil import fmt_de_date, iso, parse_iso, to_local_date, today_local

log = logging.getLogger(__name__)

MAX_UPLOAD = 25 * 1024 * 1024
PAGE = 100
MAX_STALE_DAYS = 5
MAX_TX_PRICE_DAYS = 31
TRANSFER_BEFORE = timedelta(hours=2)  # Zugang höchstens so lange vor dem Abgang (Uhren, Zeitzonen)
TRANSFER_AFTER = timedelta(hours=72)  # … und höchstens so lange danach
TRANSFER_MIN_RATIO = Decimal("0.5")  # Zugang ≥ 50 % des Abgangs (Netzwerkgebühren bei kleinen Beträgen)
DUP_QTY_TOL = Decimal("0.005")
SAME_QTY_WINDOW = timedelta(hours=36)  # gleiche exakte Menge auf demselben Konto: mögliche Doppelerfassung
RECONSTRUCTED_WINDOW = timedelta(days=7)  # Vorgang nahe einer rekonstruierten Buchung: ersetzt er sie?
REF_ID_SOURCES = ("koinly",)  # Import-Quellen, deren source_ref die ID des CSV-Exports derselben Quelle ist
_LOCK = threading.RLock()  # Auswerten/Übernehmen/Rückgängig nacheinander (Doppelklick, parallele Tabs)


def _locked(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _LOCK:
            return fn(*args, **kwargs)

    return wrapper

STATUS_LABEL = {"new": "neu", "known": "bereits vorhanden", "duplicate": "mögliche Dublette", "before": "vor Stichtag",
                "ignored": "ignoriert", "invalid": "unvollständig", "unclear": "ungeklärt", "committed": "übernommen",
                "merged": "als Transfer übernommen"}
STATUS_BADGE = {"new": "good", "known": "", "duplicate": "warn", "before": "", "ignored": "", "invalid": "crit",
                "unclear": "warn", "committed": "info", "merged": "info"}
DONE = ("known", "ignored", "committed", "merged")  # Zeilen ohne offene Entscheidung
EVAL_VERSION = 4  # erhöhen, wenn sich die Auswertung ändert – offene Stapel werden beim Öffnen neu bewertet
RELEVANT = ("new", "invalid", "unclear", "duplicate")  # zu übernehmen bzw. zu entscheiden
BATCH_STATUS = {"mapping": "Zuordnung nötig", "preview": "Vorschau", "partial": "teilweise übernommen",
                "committed": "übernommen", "reverted": "rückgängig gemacht"}
NEED_VALUE_TAGS = frozenset(C.INCOME_TAGS) | frozenset(C.LOSS_TAGS) | frozenset(C.GIFT_OUT_TAGS) | \
    frozenset(C.GIFT_IN_TAGS)


ROW_COLS = ("tx_id", "datetime", "type", "tag", "from_account", "from_asset", "from_qty", "to_account", "to_asset",
            "to_qty", "fee_asset", "fee_qty", "fee_eur", "value_eur", "orig_price", "orig_ccy", "source", "source_ref",
            "flag", "note", "related_asset")


def _dec(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def s(v: Decimal | None) -> str:
    if v is None:
        return ""
    return "0" if v == 0 else format(v.normalize(), "f")


def money(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01")) if abs(v) >= 1 else v.quantize(Decimal("0.00000001")).normalize()


# ----------------------------------------------------------------------------------------------------
# (De-)Serialisierung des Zwischenformats
# ----------------------------------------------------------------------------------------------------

def rec_to_json(r: Rec) -> str:
    d = dataclasses.asdict(r)
    d["ts"] = iso(r.ts)
    for k, v in list(d.items()):
        if isinstance(v, Decimal):
            d[k] = s(v)
    return json.dumps({k: v for k, v in d.items() if v not in (None, {}, "", []) and not (k == "ts_missing" and not v)},
                      ensure_ascii=False)


def rec_from_json(raw: str) -> Rec:
    d = json.loads(raw)
    ts = parse_iso(d.pop("ts"))
    assert ts is not None
    for k in ("out_qty", "in_qty", "fee_qty", "value", "fee_value"):
        if k in d:
            d[k] = Decimal(d[k])
    return Rec(ts=ts, **d)


# ----------------------------------------------------------------------------------------------------
# Symbole → Assets
# ----------------------------------------------------------------------------------------------------

class SymbolResolver:
    def __init__(self, assets: Mapping[str, AssetInfo], saved: Mapping[str, str | None]) -> None:
        self.assets = assets
        self.saved = dict(saved)
        self.by_id = {aid.upper(): aid for aid in assets}
        self.by_koinly = {str(a.koinly_id).strip().upper(): aid for aid, a in assets.items() if a.koinly_id}
        self.by_sym: dict[str, set[str]] = defaultdict(set)
        self.by_alias: dict[str, set[str]] = defaultdict(set)
        for aid, a in assets.items():
            self.by_sym[a.symbol.upper()].add(aid)
            for al in a.aliases:
                self.by_alias[al.strip().upper()].add(aid)
        self._cache: dict[str, tuple[str | None, str]] = {}

    def learn(self, mapping: Mapping[str, str]) -> None:
        """Zuordnungen aus dem Abgleich ergänzen (gespeicherte Zuordnungen des Nutzers haben Vorrang)."""
        for k, v in mapping.items():
            self.saved.setdefault(k.strip().upper(), v)
        self._cache.clear()

    def resolve(self, raw: str) -> tuple[str | None, str]:
        key = raw.strip().upper()
        hit = self._cache.get(key)
        if hit is None:
            hit = self._resolve(key)
            self._cache[key] = hit
        return hit

    def resolve_for(self, raw: str, provider: str | None) -> tuple[str | None, str, str]:
        """Wie :meth:`resolve`, beachtet aber die Anbieter-Identität von Kürzeln (``identity.PROVIDER_SYMBOLS``):
        Zuordnung nur über eine Zuordnung genau für diesen Anbieter (``TH@BITPANDA``) oder ein Asset, dessen
        Kursquelle den Anbieter-Coin bestätigt – sonst „mehrdeutig“, nie still über das Symbol.
        Rückgabe: (Asset, Art, Schlüssel der Zuordnung)."""
        pa = identity(provider, raw)
        if pa is None or provider is None:
            aid, how = self.resolve(raw)
            return aid, how, raw.strip().upper()
        key = provider_key(raw, provider)
        if key in self.saved:
            aid = self.saved[key]
            return (aid, "saved", key) if aid else (None, "ignored", key)
        aid, how = self.resolve(raw)
        if aid is not None and how != "ignored" and confirms(pa, self.assets.get(aid)):
            return aid, how, key
        confirmed = sorted(x for x, a in self.assets.items() if confirms(pa, a))
        if len(confirmed) == 1:
            return confirmed[0], "provider", key
        return None, "ambiguous", key

    def _resolve(self, key: str) -> tuple[str | None, str]:
        if key in self.saved:
            aid = self.saved[key]
            return (aid, "saved") if aid else (None, "ignored")
        base, _, kid = key.partition(";")
        base = base.strip()
        if kid and kid.strip() in self.by_koinly:
            return self.by_koinly[kid.strip()], "koinly"
        if key in self.by_koinly:
            return self.by_koinly[key], "koinly"
        if base in self.by_id:
            return self.by_id[base], "id"
        cands = self.by_sym.get(base, set())
        if len(cands) == 1:
            return next(iter(cands)), "symbol"
        al = self.by_alias.get(base, set())
        if len(al) == 1 and not cands:
            return next(iter(al)), "alias"
        if base in C.ISO_CURRENCIES:
            return base, "fiat"
        if len(cands) > 1 or len(al) > 1:
            return None, "ambiguous"
        return None, "unknown"


def _direction(r: Rec, raw: str) -> str:
    """Rolle eines Symbols im Vorgang: in (Zugang), out (Abgang), fee (Gebühr) oder other."""
    if r.row is not None:
        cols = (("to_asset", "in"), ("from_asset", "out"), ("fee_asset", "fee"))
        return next((d for col, d in cols if r.row.get(col) == raw), "other")
    return "in" if raw == r.in_sym else "out" if raw == r.out_sym else "fee" if raw == r.fee_sym else "other"


# ----------------------------------------------------------------------------------------------------
# Bewertung in EUR (nur gespeicherte Kurse; keine Online-Abfrage)
# ----------------------------------------------------------------------------------------------------

class Valuer:
    def __init__(self, ctx: Any, assets: Mapping[str, AssetInfo], pf: Portfolio | None, today: date) -> None:
        self.ctx = ctx
        self.assets = assets
        self.today = today
        self.manual = pf.manual_prices if pf is not None else {}
        self._fx: dict[tuple[str, date], tuple[Decimal, str] | None] = {}
        self._px: dict[tuple[str, date], tuple[Decimal, str] | None] = {}
        self.tx_prices: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
        self.implied: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
        for t in (pf.txs if pf is not None else []):
            if t.type not in ("buy", "sell", "trade") or not t.value_eur or t.flag == "estimated":
                continue
            for aid, q in ((t.to_asset, t.to_qty), (t.from_asset, t.from_qty)):
                if aid and q and aid in assets and not assets[aid].is_fiat:
                    self.tx_prices[aid].append((t.date, t.value_eur / q))
        for v in self.tx_prices.values():
            v.sort()

    def is_fiat(self, aid: str | None) -> bool:
        if not aid:
            return False
        a = self.assets.get(aid)
        return a.is_fiat if a is not None else aid in C.ISO_CURRENCIES

    def fx(self, amount: Decimal, ccy: str | None, d: date) -> tuple[Decimal, str] | None:
        if not ccy:
            return None
        c = ccy.strip().upper()
        if c == "EUR":
            return amount, "EUR"
        if c in M.EUR_STABLE:
            return amount, f"{c} ≈ 1 EUR"
        base = "USD" if c in M.USD_STABLE else c
        if base not in C.ISO_CURRENCIES:
            return None
        key = (base, d)
        if key not in self._fx:
            row = self.ctx.store.fx_on_or_before(base, d)
            if row and row[0] and (d - date.fromisoformat(row[1])).days <= MAX_STALE_DAYS:
                self._fx[key] = (Decimal(str(row[0])), row[1])
            else:
                self._fx[key] = None
        hit = self._fx[key]
        if hit is None:
            return None
        rate, on = hit
        label = f"Devisenkurs {base} {fmt_de_date(on)}"
        if c != base:
            label = f"{c} ≈ 1 USD, {label}"
        return amount / rate, label

    def price(self, aid: str, d: date) -> tuple[Decimal, str] | None:
        key = (aid, d)
        if key in self._px:
            return self._px[key]
        self._px[key] = hit = self._price(aid, d)
        return hit

    def _price(self, aid: str, d: date) -> tuple[Decimal, str] | None:
        a = self.assets.get(aid)
        if a is None:
            return None
        if a.is_fiat:
            return self.fx(Decimal(1), aid, d)
        series = self.ctx.prices.series_for(a)
        if series:
            row = self.ctx.store.close_on_or_before(series, d)
            if row is not None and row["close"]:
                age = (d - date.fromisoformat(row["date"])).days
                if age == 0 or (age <= MAX_STALE_DAYS and d < self.today - timedelta(days=1)):
                    conv = self.fx(Decimal(str(row["close"])), row["ccy"] or "EUR", d)
                    if conv:
                        label = f"Schlusskurs {fmt_de_date(row['date'])}"
                        return conv[0], label if age == 0 else f"{label} (letzter verfügbarer)"
            if d >= self.today - timedelta(days=1):
                q = self.ctx.store.latest(series)
                if q is not None and q["price"]:
                    conv = self.fx(Decimal(str(q["price"])), q["ccy"] or "EUR", d)
                    if conv:
                        return conv[0], "aktueller Kurs"
        manual = [x for x in self.manual.get(aid, []) if x[0] <= d]
        if manual:
            md, mp = max(manual)
            return Decimal(str(mp)), f"manueller Kurs {fmt_de_date(md)}"
        sym = a.symbol.upper()
        if sym in M.USD_STABLE or sym in M.EUR_STABLE:
            conv = self.fx(Decimal(1), sym, d)
            if conv:
                return conv[0], f"Stablecoin: {conv[1]}"
        for src, label in ((self.tx_prices, "Transaktionskurs"), (self.implied, "Kurs aus der Datei")):
            hit = self._nearest(src.get(aid), d)
            if hit is not None:
                return hit[1], f"{label} {fmt_de_date(hit[0])} (ersatzweise)"
        return None

    @staticmethod
    def _nearest(vals: list[tuple[date, Decimal]] | None, d: date) -> tuple[date, Decimal] | None:
        if not vals:
            return None
        i = bisect.bisect_left(vals, (d, Decimal(0)))
        best = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(vals):
                dist = abs((vals[j][0] - d).days)
                if dist <= MAX_TX_PRICE_DAYS and (best is None or dist < best[0]):
                    best = (dist, vals[j])
        return best[1] if best else None

    def add_implied(self, aid: str | None, d: date, value: Decimal, qty: Decimal | None) -> None:
        if aid and qty and qty > 0 and value > 0 and not self.is_fiat(aid):
            bisect.insort(self.implied[aid], (d, value / qty))
            self._px = {k: v for k, v in self._px.items() if k[0] != aid}


# ----------------------------------------------------------------------------------------------------
# Zeilenkontext
# ----------------------------------------------------------------------------------------------------

@dataclass
class RowCtx:
    id: int
    idx: int
    line: int | None
    rec: Rec
    status: str
    decision: str | None
    value_in: str | None
    fee_in: str | None
    pair_ref: str | None
    pair_conf: str | None
    pair_ok: int | None
    tx_id: str | None
    row: dict[str, str] | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    value_src: str | None = None
    fee_src: str | None = None
    symbols: dict[str, str | None] = field(default_factory=dict)
    dup_of: list[str] = field(default_factory=list)
    dup_same_account: bool = False
    prev_ref: str | None = None
    pair_why: str | None = None  # Begründung des Transfer-Vorschlags (Anzeige)
    recon: dict[str, Any] | None = None  # Abgleich über den Transaktions-Hash (siehe reconcile.py)
    counterpart: str | None = None  # passende Gegenbuchung im kuratierten Import (möglicher Transfer)

    @property
    def ts(self) -> datetime:
        return self.rec.ts

    @property
    def d(self) -> date:
        return to_local_date(self.rec.ts)

    @property
    def open(self) -> bool:
        return self.status not in ("committed", "merged")

    @property
    def missing_value(self) -> bool:
        return any(e.startswith("EUR-Wert fehlt") for e in self.errors)

    @property
    def default_include(self) -> bool:
        """Vorschlag ohne Wahl des Nutzers: neu → ja; Dublette auf anderem Konto → ja; sonst nein."""
        return self.status == "new" or (self.status == "duplicate" and not self.dup_same_account)

    @property
    def transfer_unclear(self) -> bool:
        """Möglicher Transfer, über den noch niemand entschieden hat: Vorschlag mittlerer Sicherheit ohne Bestätigung
        oder passende Gegenbuchung im kuratierten Import. Solche Zeilen gehen nie automatisch in die Buchungen –
        als einfacher Zu-/Abgang verbucht, gingen Einstand und Haltedauer verloren."""
        if self.pair_ref and self.pair_ok is None and self.pair_conf != "hoch":
            return True
        return self.counterpart is not None and not (self.pair_ref and self.pair_ok == 1)

    def include(self) -> bool:
        if self.status == "new":
            return self.decision != "skip"
        if self.status == "duplicate":
            return self.decision == "include" or (self.decision is None and not self.dup_same_account)
        if self.status == "before":
            return self.decision == "include"
        return False


def _row_d(v: str | None) -> Decimal | None:
    return Decimal(v) if v not in (None, "") else None


# ----------------------------------------------------------------------------------------------------
# Dienst
# ----------------------------------------------------------------------------------------------------

class CsvImportService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.db = ctx.db

    @property
    def journal(self) -> JournalService:
        return journal_service(self.ctx)

    # -- Profile & eigene Formate -------------------------------------------------------------------------
    def mapping_profiles(self) -> list[MappingProfile]:
        out = []
        for r in self.db.q("SELECT * FROM csv_mapping ORDER BY name"):
            try:
                out.append(MappingProfile(r["id"], r["name"], json.loads(r["spec_json"])))
            except (ValueError, TypeError) as e:
                log.warning("Zuordnung %s unlesbar: %s", r["id"], e)
        return out

    def profile(self, pid: str) -> Profile | None:
        if pid in PROFILES:
            return PROFILES[pid]
        if pid.startswith("mapping:"):
            return next((p for p in self.mapping_profiles() if p.id == pid), None)
        return None

    def profiles(self) -> list[Profile]:
        return [*BUILTIN, *self.mapping_profiles()]

    def save_mapping(self, name: str, spec: dict[str, Any], mid: int | None = None) -> int:
        stamp = _now()
        raw = json.dumps(spec, ensure_ascii=False)
        if mid:
            self.db.x("UPDATE csv_mapping SET name=?, spec_json=?, updated_at=? WHERE id=?", (name, raw, stamp, mid))
            return mid
        cur = self.db.x("INSERT INTO csv_mapping(name, spec_json, created_at, updated_at) VALUES (?,?,?,?)",
                        (name, raw, stamp, stamp))
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def delete_mapping(self, mid: int) -> None:
        self.db.x("DELETE FROM csv_mapping WHERE id=?", (mid,))

    # -- Stapel -------------------------------------------------------------------------------------------
    @staticmethod
    def source_of(batch: Any) -> str:
        """Journal-Quelle der Buchungen eines Stapels: ``csv:<profil>`` bzw. ``sync:<anbieter>``."""
        return batch["source"] or f"csv:{batch['profile']}"

    def batches(self, limit: int = 50) -> list[Any]:
        return self.db.q("SELECT id, filename, file_size, profile, account, status, summary_json, created_at, "
                         "committed_at, reverted_at, kind, source, datasource_id FROM csv_batch ORDER BY id DESC "
                         "LIMIT ?", (limit,))

    def batch(self, bid: int) -> Any:
        return self.db.q1("SELECT * FROM csv_batch WHERE id=?", (bid,))

    def options(self, batch: Any) -> dict[str, Any]:
        return json.loads(batch["options_json"] or "{}")

    def raw(self, batch: Any) -> bytes:
        return gzip.decompress(batch["raw_gz"])

    def upload(self, data: bytes, filename: str, profile_id: str, account: str,
               options: Mapping[str, Any]) -> tuple[int | None, list[str]]:
        if len(data) > MAX_UPLOAD:
            return None, [f"Datei zu groß ({len(data) // 1024 // 1024} MB, höchstens 25 MB)."]
        name = (filename or "upload.csv").replace("\\", "/").rsplit("/", 1)[-1][:120] or "upload.csv"
        extra = self.mapping_profiles()
        try:
            table = read_table(data, header_matcher(extra))
        except CsvError as e:
            return None, [str(e)]
        prof = self.profile(profile_id) if profile_id and profile_id != "auto" else detect(table.keys, extra)
        status = "preview" if prof is not None else "mapping"
        acc = (account or "").strip()[:80] or (prof.account if prof else "") or name.rsplit(".", 1)[0][:40]
        opts = {k: str(v).strip() for k, v in options.items() if v is not None}
        base = self.ctx.base_portfolio()
        if "cutoff" not in opts and base is not None and base.valuation_date is not None:
            opts["cutoff"] = base.valuation_date.isoformat()
        stamp = _now()
        cur = self.db.x(
            "INSERT INTO csv_batch(filename, file_sha256, file_size, raw_gz, profile, account, options_json, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, hashlib.sha256(data).hexdigest(), len(data), gzip.compress(data, 6),
             prof.id if prof else "unknown", acc, json.dumps(opts, ensure_ascii=False), status, stamp, stamp))
        bid = int(cur.lastrowid)  # type: ignore[arg-type]
        if prof is not None:
            self.process(bid)
        log.info("CSV-Datei hochgeladen: %s (Stapel %s, Profil %s)", name, bid, prof.id if prof else "unbekannt")
        return bid, []

    def untouched(self, bid: int) -> bool:
        """Sync-Stapel ohne Eingriff des Nutzers (Vorschau, keine Entscheidung, keine Eingabe, keine Übernahme) –
        neue Vorgänge eines späteren Laufs dürfen angehängt werden, statt einen weiteren Stapel anzulegen."""
        b = self.batch(bid)
        if b is None or b["kind"] != "sync" or b["status"] != "preview":
            return False
        return not self.db.scalar("SELECT COUNT(*) FROM csv_row WHERE batch_id=? AND (decision IS NOT NULL OR "
                                  "value_in IS NOT NULL OR fee_in IS NOT NULL OR pair_ok IS NOT NULL OR tx_id IS NOT "
                                  "NULL)", (bid,), default=0)

    @_locked
    def ingest(self, recs: list[Rec], *, source: str, profile: str, account: str, label: str,
               datasource_id: int | None, payload: bytes, options: Mapping[str, Any] | None = None,
               append_to: int | None = None, skipped: Mapping[str, int] | None = None) -> int:
        """Normalisierte Vorgänge einer Datenquelle als Stapel (Art „sync“) anlegen und auswerten.

        ``payload`` (normalisierte Rohdaten) wird wie eine CSV-Datei komprimiert aufbewahrt – nachvollziehbar, ohne
        Zugangsdaten. Ohne Stichtag gilt wie beim CSV-Import das ``valuation_date`` des kuratierten Imports.
        ``append_to``: an einen unberührten Prüf-Stapel derselben Quelle anhängen (siehe :meth:`untouched`)."""
        if append_to is not None and self.untouched(append_to):
            return self._append(append_to, recs, payload, skipped or {})
        opts = {k: str(v).strip() for k, v in (options or {}).items() if v is not None}
        base = self.ctx.base_portfolio()
        if "cutoff" not in opts and base is not None and base.valuation_date is not None:
            opts["cutoff"] = base.valuation_date.isoformat()
        recs = sorted(recs, key=lambda r: (r.ts, r.event_key or "", r.event_line or 0))
        stamp = _now()
        summary = {"rows_read": len(recs), "recs": len(recs), "events": len({r.event_key or r.ext_id for r in recs}),
                   "skipped": dict(skipped or {}), "errors": [], "error_count": 0, "notes": []}
        with self.db.transaction() as c:
            cur = c.execute(
                "INSERT INTO csv_batch(filename, file_sha256, file_size, raw_gz, profile, account, options_json, "
                "status, summary_json, created_at, updated_at, kind, source, datasource_id) "
                "VALUES (?,?,?,?,?,?,?, 'preview', ?,?,?, 'sync', ?,?)",
                (label[:120], hashlib.sha256(payload).hexdigest(), len(payload), gzip.compress(payload, 6), profile,
                 account, json.dumps(opts, ensure_ascii=False), json.dumps(summary), stamp, stamp, source,
                 datasource_id))
            bid = int(cur.lastrowid)  # type: ignore[arg-type]
            c.executemany("INSERT INTO csv_row(batch_id, idx, line, rec_json, status, event_key, event_line) "
                          "VALUES (?,?,?,?, 'new', ?,?)",
                          [(bid, i, r.line, rec_to_json(r), r.event_key, r.event_line) for i, r in enumerate(recs)])
        self.evaluate(bid)
        return bid

    def _append(self, bid: int, recs: list[Rec], payload: bytes, skipped: Mapping[str, int]) -> int:
        batch = self.batch(bid)
        assert batch is not None
        old = json.loads(self.raw(batch) or b"[]")
        new = json.loads(payload or b"[]")
        merged = json.dumps([*old, *new], ensure_ascii=False).encode()
        summary = json.loads(batch["summary_json"] or "{}")
        first = int(self.db.scalar("SELECT COALESCE(MAX(idx), -1) + 1 FROM csv_row WHERE batch_id=?", (bid,),
                                   default=0))
        line0 = int(self.db.scalar("SELECT COALESCE(MAX(line), 0) FROM csv_row WHERE batch_id=?", (bid,), default=0))
        recs = sorted(recs, key=lambda r: (r.ts, r.event_key or "", r.event_line or 0))
        for r in recs:
            r.line = (r.line or 0) + line0
        summary["rows_read"] = int(summary.get("rows_read", 0)) + len(recs)
        summary["recs"] = int(summary.get("recs", 0)) + len(recs)
        summary["events"] = int(summary.get("events", 0)) + len({r.event_key or r.ext_id for r in recs})
        sk = dict(summary.get("skipped") or {})
        for k, n in skipped.items():
            sk[k] = int(sk.get(k, 0)) + int(n)
        summary["skipped"] = sk
        with self.db.transaction() as c:
            c.execute("UPDATE csv_batch SET raw_gz=?, file_size=?, file_sha256=?, summary_json=?, updated_at=? "
                      "WHERE id=?", (gzip.compress(merged, 6), len(merged), hashlib.sha256(merged).hexdigest(),
                                     json.dumps(summary), _now(), bid))
            c.executemany("INSERT INTO csv_row(batch_id, idx, line, rec_json, status, event_key, event_line) "
                          "VALUES (?,?,?,?, 'new', ?,?)",
                          [(bid, first + i, r.line, rec_to_json(r), r.event_key, r.event_line)
                           for i, r in enumerate(recs)])
        self.evaluate(bid)
        return bid

    def table(self, batch: Any) -> Any:
        return read_table(self.raw(batch), header_matcher(self.mapping_profiles()))

    def parse_options(self, batch: Any, prof: Profile) -> ParseOptions:
        o = self.options(batch)
        return ParseOptions(tz=zone(o.get("tz") or prof.tz), decimal=o.get("decimal") or prof.decimal,
                            dayfirst={"1": True, "0": False}.get(o.get("dayfirst", "")), account=batch["account"],
                            default_asset=o.get("default_asset", ""), filename=batch["filename"],
                            mapping={"accounts_from_file": o.get("accounts_from_file") == "1",
                                     **(getattr(prof, "spec", None) or {})})

    @_locked
    def set_profile(self, bid: int, pid: str) -> list[str]:
        batch = self.batch(bid)
        if batch is None or batch["status"] not in ("mapping", "preview"):
            return ["Stapel nicht gefunden oder bereits übernommen."]
        if batch["kind"] == "sync":
            return ["Stapel einer Datenquelle haben kein Dateiformat."]
        prof = self.profile(pid)
        if prof is None:
            return ["Unbekanntes Format."]
        acc = batch["account"] or prof.account
        self.db.x("UPDATE csv_batch SET profile=?, account=?, status='preview', updated_at=? WHERE id=?",
                  (prof.id, acc, _now(), bid))
        self.process(bid)
        return []

    @_locked
    def set_options(self, bid: int, form: Mapping[str, Any]) -> list[str]:
        batch = self.batch(bid)
        if batch is None or batch["status"] in ("reverted",):
            return ["Stapel nicht gefunden."]
        opts = self.options(batch)
        if batch["kind"] == "sync":  # Datenquelle: nur der Stichtag ist einstellbar (Konto kommt aus der Quelle)
            form = {k: v for k, v in form.items() if k == "cutoff"}
        for k in ("tz", "decimal", "dayfirst", "default_asset", "cutoff", "accounts_from_file"):
            if k in form:
                v = str(form.get(k) or "").strip()
                if k == "cutoff" and v:
                    try:
                        date.fromisoformat(v)
                    except ValueError:
                        return ["Stichtag ungültig (JJJJ-MM-TT)."]
                if k == "default_asset":
                    v = v.upper()[:20]
                opts[k] = v
        account = (str(form.get("account") or batch["account"]).strip()[:80] or batch["account"]) \
            if batch["kind"] != "sync" else batch["account"]
        committed = self.db.scalar("SELECT COUNT(*) FROM csv_row WHERE batch_id=? AND status IN ('committed','merged')",
                                   (bid,), default=0)
        reparse = any(opts.get(k) != self.options(batch).get(k) for k in ("tz", "decimal", "dayfirst",
                                                                           "default_asset", "accounts_from_file")) \
            or account != batch["account"]
        if reparse and committed:
            return ["Zeitzone, Zahlenformat, Standardwährung und Konto lassen sich nach der ersten Übernahme nicht "
                    "mehr ändern – Stapel zuerst rückgängig machen."]
        self.db.x("UPDATE csv_batch SET options_json=?, account=?, updated_at=? WHERE id=?",
                  (json.dumps(opts, ensure_ascii=False), account, _now(), bid))
        if reparse:
            self.process(bid)
        else:
            self.evaluate(bid)
        return []

    @_locked
    def discard(self, bid: int, rewind: bool = True) -> bool:
        """Stapel ohne Übernahmen verwerfen. Bei Datenquellen wird der Abrufstand vor den ältesten offenen Vorgang
        zurückgesetzt – verworfene Vorgänge kommen beim nächsten Lauf wieder (bekannte werden erkannt).
        ``rewind=False``: Abrufstand bleibt (der laufende Abruf liefert die Vorgänge ohnehin neu)."""
        batch = self.batch(bid)
        if batch is None:
            return False
        n = self.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status <> 'reverted'", (bid,),
                           default=0)
        if n:
            return False
        open_ts = [rc.ts for rc in self._load(bid) if rc.status not in DONE] if batch["kind"] == "sync" and rewind \
            else []
        self.db.x("DELETE FROM csv_batch WHERE id=?", (bid,))
        if open_ts and batch["datasource_id"]:
            from app.datasources.service import datasource_service

            datasource_service(self.ctx).rewind(int(batch["datasource_id"]), min(open_ts))
        return True

    # -- Entscheidungen je Ereignis (Datenquellen) ------------------------------------------------------
    def decisions(self, keys: set[str]) -> dict[str, Any]:
        if not keys:
            return {}
        out: dict[str, Any] = {}
        ks = sorted(keys)
        for i in range(0, len(ks), 500):
            part = ks[i:i + 500]
            for r in self.db.q(f"SELECT event_key, decision, reason FROM event_decision WHERE decision='ignore' AND "
                               f"event_key IN ({','.join('?' * len(part))})", part):
                out[r["event_key"]] = r
        return out

    def set_ignored(self, bid: int, event_key: str, ignore: bool, reason: str = "") -> bool:
        """Vorgang dauerhaft ignorieren (bzw. wieder freigeben) – gespeichert je Anbieter-Ereignis, gilt für alle
        künftigen Abrufe und Stapel; die Herkunfts-ID bleibt nachvollziehbar."""
        batch = self.batch(bid)
        if batch is None or not event_key or not self.db.scalar(
                "SELECT 1 FROM csv_row WHERE batch_id=? AND event_key=?", (bid, event_key)):
            return False
        if ignore:
            self.db.x("INSERT INTO event_decision(event_key, decision, reason, batch_id, decided_at) "
                      "VALUES (?, 'ignore', ?, ?, ?) ON CONFLICT(event_key) DO UPDATE SET decision='ignore', "
                      "reason=excluded.reason, batch_id=excluded.batch_id, decided_at=excluded.decided_at",
                      (event_key, reason.strip()[:200] or None, bid, _now()))
        else:
            self.db.x("DELETE FROM event_decision WHERE event_key=?", (event_key,))
        self.evaluate(bid)
        self.refresh_status(bid)
        return True

    def refresh_status(self, bid: int) -> None:
        """Sync-Stapel: „teilweise übernommen“ → „übernommen“, sobald nichts mehr zu entscheiden ist."""
        batch = self.batch(bid)
        if batch is None or batch["kind"] != "sync" or batch["status"] != "partial":
            return
        if not self.db.scalar("SELECT COUNT(*) FROM csv_row WHERE batch_id=? AND (status IN ('invalid', 'unclear') OR "
                              "(status IN ('new', 'duplicate', 'before') AND decision IS NULL))", (bid,), default=0):
            self.db.x("UPDATE csv_batch SET status='committed', updated_at=? WHERE id=?", (_now(), bid))

    # -- Einlesen -----------------------------------------------------------------------------------------
    @_locked
    def process(self, bid: int) -> None:
        """Datei mit dem Profil des Stapels (neu) lesen und die Vorschauzeilen ersetzen."""
        batch = self.batch(bid)
        if batch is None:
            return
        if batch["kind"] == "sync":  # keine Datei – Vorgänge stehen bereits im Stapel
            self.evaluate(bid)
            return
        prof = self.profile(batch["profile"])
        if prof is None:
            return
        summary: dict[str, Any]
        recs: list[Rec] = []
        try:
            table = self.table(batch)
            res = prof.parse(table, self.parse_options(batch, prof))
            recs = sorted(res.recs, key=lambda r: (r.ts, r.line))
            summary = {"rows_read": res.rows_read, "recs": len(recs), "skipped": dict(res.skipped),
                       "errors": [{"line": ln, "message": m} for ln, m in res.errors[:500]],
                       "error_count": len(res.errors), "notes": res.notes, "header": table.header[:60],
                       "delimiter": table.delimiter, "encoding": table.encoding, "header_line": table.header_line}
        except CsvError as e:
            summary = {"rows_read": 0, "recs": 0, "skipped": {}, "errors": [{"line": 0, "message": str(e)}],
                       "error_count": 1, "notes": []}
        with self.db.transaction() as c:
            c.execute("DELETE FROM csv_row WHERE batch_id=? AND status NOT IN ('committed','merged')", (bid,))
            done = {r["idx"] for r in c.execute("SELECT idx FROM csv_row WHERE batch_id=?", (bid,))}
            c.executemany("INSERT INTO csv_row(batch_id, idx, line, rec_json, status) VALUES (?,?,?,?, 'new')",
                          [(bid, i, r.line, rec_to_json(r)) for i, r in enumerate(recs) if i not in done])
            c.execute("UPDATE csv_batch SET summary_json=?, updated_at=? WHERE id=?",
                      (json.dumps(summary, ensure_ascii=False, default=str), _now(), bid))
        self.evaluate(bid)

    # -- Zuordnungen --------------------------------------------------------------------------------------
    def saved_symbols(self) -> dict[str, str | None]:
        return {r["symbol"]: r["asset_id"] for r in self.db.q("SELECT symbol, asset_id FROM csv_symbol")}

    def saved_accounts(self) -> dict[str, str]:
        return {r["name"]: r["account"] for r in self.db.q("SELECT name, account FROM csv_account")}

    def set_symbol(self, symbol: str, asset_id: str | None, origin: str | None = None) -> None:
        """Zuordnung speichern; ``origin`` = 'abgleich' für automatisch abgeleitete (sonst vom Nutzer)."""
        self.db.x("INSERT INTO csv_symbol(symbol, asset_id, origin, updated_at) VALUES (?,?,?,?) ON CONFLICT(symbol) "
                  "DO UPDATE SET asset_id=excluded.asset_id, origin=excluded.origin, updated_at=excluded.updated_at",
                  (symbol.strip().upper()[:80], asset_id, origin, _now()))

    def delete_symbol(self, symbol: str) -> None:
        self.db.x("DELETE FROM csv_symbol WHERE symbol=?", (symbol,))

    def set_account(self, name: str, account: str) -> None:
        if not account.strip():
            self.db.x("DELETE FROM csv_account WHERE name=?", (name,))
            return
        self.db.x("INSERT INTO csv_account(name, account, updated_at) VALUES (?,?,?) ON CONFLICT(name) DO "
                  "UPDATE SET account=excluded.account, updated_at=excluded.updated_at",
                  (name[:120], account.strip()[:80], _now()))

    def known_assets(self) -> dict[str, AssetInfo]:
        return self.journal.known_assets()

    @_locked
    def rebook(self, bid: int, old: str, new: str) -> int:
        """Offene Zeilen eines Stapels vom Konto ``old`` auf ``new`` umstellen (Konto-Umstellung einer Datenquelle –
        deren Zeilen tragen das Konto seit dem Abruf); übernommene Zeilen bleiben unverändert."""
        data = []
        for r in self.db.q("SELECT id, rec_json, status FROM csv_row WHERE batch_id=?", (bid,)):
            if r["status"] in ("committed", "merged"):
                continue
            rec = rec_from_json(r["rec_json"])
            if old not in (rec.account, rec.to_account):
                continue
            rec.account = new if rec.account == old else rec.account
            rec.to_account = new if rec.to_account == old else rec.to_account
            data.append((rec_to_json(rec), r["id"]))
        if data:
            self.db.xmany("UPDATE csv_row SET rec_json=? WHERE id=?", data)
        self.db.x("UPDATE csv_batch SET account=?, updated_at=? WHERE id=? AND account=?", (new, _now(), bid, old))
        return len(data)

    # -- Auswerten ----------------------------------------------------------------------------------------
    def _load(self, bid: int) -> list[RowCtx]:
        out = []
        for r in self.db.q("SELECT * FROM csv_row WHERE batch_id=? ORDER BY idx", (bid,)):
            rc = RowCtx(id=r["id"], idx=r["idx"], line=r["line"], rec=rec_from_json(r["rec_json"]), status=r["status"],
                        decision=r["decision"], value_in=r["value_in"], fee_in=r["fee_in"], pair_ref=r["pair_ref"],
                        pair_conf=r["pair_conf"], pair_ok=r["pair_ok"], tx_id=r["tx_id"])
            if r["row_json"]:
                rc.row = json.loads(r["row_json"])
            msgs = json.loads(r["messages"] or "{}")
            rc.errors, rc.warnings = msgs.get("errors", []), msgs.get("warnings", [])
            rc.value_src, rc.fee_src = msgs.get("value_src"), msgs.get("fee_src")
            rc.dup_of, rc.dup_same_account = msgs.get("dup_of", []), bool(msgs.get("dup_same"))
            rc.symbols = msgs.get("symbols", {})
            rc.pair_why = msgs.get("pair_why")
            rc.recon = msgs.get("recon")
            rc.counterpart = msgs.get("counterpart")
            out.append(rc)
        return out

    @_locked
    def evaluate(self, bid: int) -> dict[str, Any]:
        batch = self.batch(bid)
        if batch is None or batch["status"] == "mapping":
            return {}
        prof = self.profile(batch["profile"])
        source = self.source_of(batch)
        opts = self.options(batch)
        rows = self._load(bid)
        assets = self.known_assets()
        resolver = SymbolResolver(assets, self.saved_symbols())
        acc_map = self.saved_accounts()
        pf = self.ctx.recorded_portfolio()
        valuer = Valuer(self.ctx, {**assets, **{c: AssetInfo(asset_id=c, name=c, asset_class="fiat")
                                                 for c in C.ISO_CURRENCIES if c not in assets}},
                        pf, today_local())
        cutoff = date.fromisoformat(opts["cutoff"]) if opts.get("cutoff") else None
        known = {r["external_id"]: r for r in self.db.q(
            "SELECT external_id, status, tx_id, batch_id FROM journal_tx WHERE source=? AND external_id IS NOT NULL "
            "AND status <> 'reverted'", (source,))}
        # Datenquellen mit versionierter Auswertung (Bitpanda): ein Ereignis, das aus einem anderen Stapel bereits
        # übernommen ist, bleibt bekannt – auch wenn eine neuere Auswertung es in andere Zeilen teilt (sonst entstünden
        # Doppelbuchungen). Nicht bei Wallets: dort sind Zeilen eines Ereignisses eigene Bewegungen (Unterkennung).
        known_events: dict[str, Any] = {}
        if batch["kind"] == "sync" and _versioned(source):
            for r in self.db.q("SELECT event_key, tx_id, status FROM journal_tx WHERE source=? AND event_key IS NOT "
                               "NULL AND status <> 'reverted' AND (batch_id IS NULL OR batch_id <> ?) ORDER BY tx_id",
                               (source, bid)):
                known_events.setdefault(r["event_key"], r)
        open_rows = [rc for rc in rows if rc.open]
        # Abgleich über den Transaktions-Hash: vorhandene Vorgänge erkennen, Zuordnungen lernen (vor dem Aufbau)
        recon = self._reconcile(bid, open_rows, resolver, assets)
        seen_ext: dict[str, int] = {}
        for rc in open_rows:
            rc.errors, rc.warnings, rc.dup_of, rc.dup_same_account = [], [], [], False
            rc.value_src = rc.fee_src = None
            m = recon.rows.get(rc.idx)
            rc.recon = m.as_dict() if m is not None else None
            # ungeklärte Vorgänge einer Datenquelle werden nie zu Buchungen – nur angezeigt und entschieden
            rc.row = None if rc.rec.kind == M.REVIEW else self._build(rc, resolver, acc_map, batch, source, prof)
        # Werte: zuerst Zeilen mit Fiat-Seite/Dateiwert (liefern Kurse für die übrigen), dann Kursabfragen
        for rc in open_rows:
            if rc.row is not None and not rc.errors:
                self._value(rc, valuer, first_pass=True)
        for rc in open_rows:
            if rc.row is not None and not rc.errors:
                self._value(rc, valuer, first_pass=False)
        # Prüfung mit dem Import-Validator
        classes = {aid: {"asset_class": a.asset_class} for aid, a in valuer.assets.items()}
        checkable = [rc for rc in open_rows if rc.row is not None and not rc.errors]
        rep, _parsed = validate_tx_rows([{**rc.row, "tx_id": f"Z{rc.idx}"} for rc in checkable  # type: ignore[dict-item]
                                         ], classes)
        by_line = {i + 1: rc for i, rc in enumerate(checkable)}
        for m in rep.errors:
            rc = by_line.get(m.line or 0)
            if rc is not None:
                rc.errors.append(_strip_prefix(m.message))
        for m in rep.warnings:
            rc = by_line.get(m.line or 0)
            if rc is not None and m.code not in ("fee_eur", "value_eur"):
                rc.warnings.append(_strip_prefix(m.message))
        # Status
        decisions = self.decisions({rc.rec.event_key for rc in open_rows if rc.rec.event_key})
        for rc in open_rows:
            ext = rc.rec.ext_id or ""
            dec = decisions.get(rc.rec.event_key or "")
            m = recon.rows.get(rc.idx)
            if rc.symbols and any(v == "ignored" for v in rc.symbols.values()):
                rc.status = "ignored"
            elif ext and ext in known:
                k = known[ext]
                rc.status = "known"
                rc.warnings.insert(0, f"bereits importiert als {k['tx_id']}" + (" (gelöscht)" if k["status"] ==
                                                                                 "deleted" else ""))
            elif rc.rec.event_key and rc.rec.event_key in known_events:
                k = known_events[rc.rec.event_key]
                rc.status = "known"
                rc.warnings.insert(0, f"Vorgang bereits übernommen als {k['tx_id']}" + (" (gelöscht)" if k["status"] ==
                                                                                         "deleted" else "")
                                   + " – Zeile einer neueren Auswertung, wird nicht zusätzlich gebucht")
            elif dec is not None:
                rc.status = "ignored"
                rc.warnings.insert(0, "dauerhaft ignoriert" + (f": {dec['reason']}" if dec["reason"] else ""))
            elif rc.rec.ts_missing:  # ohne Zeitpunkt weder Stichtag noch Buchung – nur Prüfung
                rc.status = "unclear"
                rc.warnings.insert(0, rc.rec.note or "Zeitpunkt fehlt in den Daten der Quelle")
            elif m is not None and m.state == "full":  # gleiche Blockchain-Transaktion, alle Beine gefunden
                rc.status = "known"
                rc.dup_of = m.txs[:5]
                rc.warnings.insert(0, _recon_text(m))
            elif ext and ext in seen_ext:
                rc.status = "known"
                rc.warnings.insert(0, f"doppelte Zeile in der Datei (wie Zeile {seen_ext[ext]})")
            elif cutoff is not None and rc.d <= cutoff:
                # bis zum Stichtag maßgeblich ist der kuratierte Import – keine Zuordnung oder Entscheidung nötig
                rc.status = "before"
                if m is not None:
                    rc.warnings.insert(0, _recon_text(m))
                elif recon.rows and R.row_hash(rc.rec):
                    rc.recon = {"state": "missing"}
                    rc.warnings.insert(0, "nicht im kuratierten Import gefunden (z. B. Spam, Freigabe oder Lücke im "
                                          "Import) – bei Bedarf übernehmen")
            elif rc.rec.kind == M.REVIEW:
                rc.status = "unclear"
                rc.warnings.insert(0, rc.rec.note or "Deutung nicht eindeutig – bitte prüfen")
            elif rc.errors or rc.row is None:
                rc.status = "invalid"
            elif m is not None:  # gleicher Hash, nicht alle Beine gefunden → Entscheidung, nie still doppelt
                rc.status = "duplicate"
                rc.dup_of = m.txs[:5]
                rc.dup_same_account = True
                rc.warnings.insert(0, _recon_text(m))
            else:
                rc.status = "new"
            if rc.rec.review and rc.status == "new":
                rc.warnings.insert(0, f"Bitte prüfen: {rc.rec.review}")
            if ext:
                seen_ext.setdefault(ext, rc.line or 0)
        self._same_events([rc for rc in open_rows if rc.status in ("new", "invalid", "unclear", "before")], source)
        self._duplicates([rc for rc in open_rows if rc.status == "new"], pf, source)
        self._same_qty([rc for rc in open_rows if rc.status == "new"], pf, source)
        self._reconstructed([rc for rc in open_rows if rc.status == "new"], pf)
        self._transfers(bid, rows, pf, valuer)
        self._save(rows)
        self._save_recon(batch, rows, recon)
        counts = defaultdict(int)
        for rc in rows:
            counts[rc.status] += 1
        return dict(counts)

    def _reconcile(self, bid: int, rows: list[RowCtx], resolver: SymbolResolver,
                   assets: Mapping[str, AssetInfo]) -> R.Result:
        """Abgleich mit dem kuratierten Import und App-Buchungen über den Transaktions-Hash. Gelernte Token-Zuordnungen
        werden gespeichert (Herkunft „abgleich“, gelten für künftige Abrufe ohne Gegenbuchung), gelernte Symbole ohne
        Contract nur für diese Auswertung."""
        if not any(R.row_hash(rc.rec) for rc in rows):
            return R.Result()
        base, _ = self.ctx.effective_base()
        # Buchungen derselben Quelle nicht: dort gilt die Ereigniskennung (zwei Bewegungen derselben Transaktion mit
        # verschiedenem Ereignisindex sind zwei Vorgänge – der Hash allein darf sie nicht zusammenlegen)
        source = self.source_of(self.batch(bid))
        journal = self.db.q(
            "SELECT tx_id, type, tag, from_account, from_asset, from_qty, to_account, to_asset, to_qty, fee_asset, "
            "fee_qty, tx_hash FROM journal_tx WHERE status IN ('active', 'merged') AND tx_hash IS NOT NULL AND "
            "tx_hash <> '' AND source <> 'transfer' AND source <> ? AND (batch_id IS NULL OR batch_id <> ?)",
            (source, bid))
        index = R.HashIndex(base.txs if base is not None else [], journal)
        if not len(index):
            return R.Result()
        res = R.reconcile(rows, index, lambda sym: resolver.resolve(sym)[0], assets)
        for sym, aid in res.learned.items():
            if "@" in sym:
                self.set_symbol(sym, aid, origin="abgleich")
                log.info("Token-Zuordnung aus dem Abgleich: %s → %s", sym, aid)
        if res.learned:
            resolver.learn(res.learned)
        return res

    def _save_recon(self, batch: Any, rows: list[RowCtx], recon: R.Result) -> None:
        """Ergebnis des Abgleichs im Stapel (Anzeige; Grundlage der Konto-Zuordnung einer Datenquelle) und Stand der
        Auswertung (ältere Stapel werden beim Öffnen neu bewertet)."""
        summ = json.loads(self.db.scalar("SELECT summary_json FROM csv_batch WHERE id=?", (batch["id"],)) or "{}")
        summ["eval_v"] = EVAL_VERSION
        if recon.rows:
            found = [rc for rc in rows if rc.status == "known" and (rc.recon or {}).get("state") == "full"]
            summ["recon"] = {
                "hashes": recon.hashes,
                "found_import": sum(1 for rc in found if rc.recon and rc.recon.get("origin") == "import"),
                "found_app": sum(1 for rc in found if rc.recon and rc.recon.get("origin") == "journal"),
                # nach dem Stichtag: gleiche Transaktion, aber nicht alle Teile → mögliche Dublette (Entscheidung)
                "partial": sum(1 for rc in rows if rc.open and rc.status == "duplicate"
                               and (rc.recon or {}).get("state") == "partial"),
                # vor dem Stichtag: nicht bzw. nicht vollständig im Import (Information, z. B. Spam oder Lücke)
                "missing": sum(1 for rc in rows if rc.open and rc.status == "before"
                               and (rc.recon or {}).get("state") in ("missing", "partial")),
                "accounts": dict(recon.accounts.most_common(5)),
                "learned_plain": {k: v for k, v in recon.learned.items() if "@" not in k},
            }
        else:
            summ.pop("recon", None)
        self.db.x("UPDATE csv_batch SET summary_json=? WHERE id=?", (json.dumps(summ, ensure_ascii=False),
                                                                     batch["id"]))

    def _build(self, rc: RowCtx, resolver: SymbolResolver, acc_map: Mapping[str, str], batch: Any, source: str,
               prof: Profile | None) -> dict[str, str] | None:
        r = rc.rec
        syms: dict[str, str | None] = {}

        def acc(name: str | None) -> str:
            n = (name or "").strip() or batch["account"]
            return acc_map.get(n, n)

        # Anbieter des Vorgangs (Datenquelle, Profil, sonst Konto – z. B. Koinly-Wallet „Bitpanda“)
        provider = provider_of(source, batch["profile"], None) or provider_of(
            None, None, acc(r.account) if r.kind != M.DIRECT or r.row is None
            else (r.row.get("to_account") or r.row.get("from_account") or batch["account"]))

        def res(raw: str | None) -> str | None:
            if not raw:
                return None
            aid, how, key = resolver.resolve_for(raw, provider)
            syms[key if key != raw.strip().upper() else raw] = aid if aid else how
            if how in ("unknown", "ambiguous"):
                pa = identity(provider, raw) if key != raw.strip().upper() else None
                if pa is not None:
                    rc.errors.append(f"„{raw}“ bei {PROVIDER_LABEL.get(pa.provider, pa.provider)} ist {pa.name} – "
                                     f"Asset zuordnen (Zuordnung {key} gilt nur für diesen Anbieter)")
                else:
                    rc.errors.append(f"Asset für „{raw}“ zuordnen" + (" (mehrdeutig)" if how == "ambiguous" else ""))
            return aid

        base = dict.fromkeys(ROW_COLS, "")
        base["datetime"] = to_local_date(r.ts).isoformat() if r.date_only else r.ts.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        base["source"] = source
        base["source_ref"] = (r.ext_id or "")[:200]
        note = " · ".join(x for x in (r.label if r.kind != M.DIRECT else None, r.note) if x)
        base["note"] = note[:500]
        if r.kind == M.DIRECT and r.row is not None:
            row = {**base, **{k: v for k, v in r.row.items() if k in base and k not in ("source", "source_ref",
                                                                                          "tx_id", "flag")}}
            row["note"] = (r.row.get("note") or "")[:500]
            for col in ("from_asset", "to_asset", "fee_asset", "related_asset"):
                if row[col]:
                    row[col] = res(row[col]) or ""
            for col in ("from_account", "to_account"):
                if row[col]:
                    row[col] = acc(row[col])
            rc.symbols = syms
            return row
        a_out, a_in, a_fee = res(r.out_sym), res(r.in_sym), res(r.fee_sym)
        rc.symbols = syms
        if rc.errors:
            return None
        row = dict(base)
        account = acc(r.account)
        if r.fee_qty and a_fee:
            row["fee_asset"], row["fee_qty"] = a_fee, s(r.fee_qty)
        fiat = {aid: (aid in C.ISO_CURRENCIES and aid not in resolver.assets) or
                (aid in resolver.assets and resolver.assets[aid].is_fiat) for aid in (a_out, a_in) if aid}
        if r.kind in (M.TRADE, M.CONVERSION):
            if a_out and r.out_qty:
                row["from_account"], row["from_asset"], row["from_qty"] = account, a_out, s(r.out_qty)
            if a_in and r.in_qty:
                row["to_account"], row["to_asset"], row["to_qty"] = account, a_in, s(r.in_qty)
            if r.kind == M.CONVERSION:
                row["type"], row["tag"] = "corporate_action", "migration"
            elif row["from_asset"] and row["to_asset"]:
                fo, ti = fiat.get(a_out or "", False), fiat.get(a_in or "", False)
                row["type"] = "buy" if fo and not ti else "sell" if ti and not fo else "trade"
                if row["type"] in ("buy", "sell"):
                    fiat_aid, fiat_q = (a_out, r.out_qty) if fo else (a_in, r.in_qty)
                    crypto_q = r.in_qty if fo else r.out_qty
                    if fiat_aid and fiat_aid != "EUR" and fiat_q and crypto_q:
                        row["orig_price"], row["orig_ccy"] = s(money(fiat_q / crypto_q)), fiat_aid
            elif row["to_asset"]:
                row["type"] = "buy"
                row["note"] = " · ".join(x for x in (row["note"], "Zahlung von außen (z. B. Karte)") if x)[:500]
            elif row["from_asset"]:
                row["type"] = "sell"
                row["note"] = " · ".join(x for x in (row["note"], "Erlös nach außen (z. B. Karte)") if x)[:500]
            else:
                rc.errors.append("Handel ohne Mengen")
                return None
        elif r.kind == M.DEPOSIT:
            row["type"], row["tag"] = "deposit", r.tag or ""
            row["to_account"], row["to_asset"], row["to_qty"] = account, a_in or "", s(r.in_qty)
        elif r.kind == M.WITHDRAWAL:
            row["type"], row["tag"] = "withdrawal", r.tag or ""
            row["from_account"], row["from_asset"], row["from_qty"] = account, a_out or "", s(r.out_qty)
        elif r.kind == M.FEE:
            row["type"], row["tag"] = "withdrawal", "fee"
            row["from_account"], row["from_asset"], row["from_qty"] = account, a_fee or "", s(r.fee_qty)
            row["fee_asset"] = row["fee_qty"] = ""
        elif r.kind == M.TRANSFER:
            row["type"] = "transfer"
            row["from_account"], row["from_asset"], row["from_qty"] = account, a_out or "", s(r.out_qty)
            row["to_account"], row["to_asset"], row["to_qty"] = acc(r.to_account), a_in or "", s(r.in_qty)
            if r.in_qty and r.out_qty and r.in_qty > r.out_qty:
                row["to_qty"] = s(r.out_qty)
        else:
            rc.errors.append(f"Unbekannte Vorgangsart {r.kind}")
            return None
        return row

    def _value(self, rc: RowCtx, V: Valuer, first_pass: bool) -> None:
        row = rc.row
        assert row is not None
        r = rc.rec
        d = rc.d
        typ, tag = row["type"], row["tag"]
        fa, fq = row["from_asset"] or None, _row_d(row["from_qty"])
        ta, tq = row["to_asset"] or None, _row_d(row["to_qty"])
        need = typ in ("buy", "sell", "trade") or (typ in ("deposit", "withdrawal") and tag in NEED_VALUE_TAGS)
        want = need or (typ in ("deposit", "withdrawal") and not V.is_fiat(ta or fa))
        if rc.value_in:
            v = parse_number(rc.value_in)
            if v is not None and v >= 0:
                row["value_eur"], rc.value_src = s(money(v)), "Eingabe"
        if not row["value_eur"] and want and rc.value_src is None:
            hit: tuple[Decimal, str] | None = None
            implied = False
            if typ in ("buy", "sell", "trade"):
                if fa and fq and V.is_fiat(fa):
                    hit, implied = V.fx(fq, fa, d), True
                elif ta and tq and V.is_fiat(ta):
                    hit, implied = V.fx(tq, ta, d), True
            elif (ta or fa) and V.is_fiat(ta or fa) and (tq or fq):
                hit = V.fx(tq or fq, ta or fa, d)  # Fiat-Ertrag/-Gebühr: Wert = Betrag (umgerechnet)
            rv, rvc = (r.value, r.value_ccy) if r.value is not None else \
                (r.fee_value, r.fee_value_ccy) if r.kind == M.FEE else (None, None)
            if hit is None and rv is not None and rvc:
                conv = V.fx(rv, rvc, d)
                if conv:
                    hit, implied = (conv[0], f"Gegenwert laut Datei ({conv[1]})"), True
            if hit is None and typ in ("trade", "buy", "sell"):
                for aid, q in ((fa, fq), (ta, tq)):
                    a = V.assets.get(aid or "")
                    if aid and q and a is not None and a.symbol.upper() in (M.USD_STABLE | M.EUR_STABLE):
                        p = V.price(aid, d)
                        if p:
                            hit, implied = (q * p[0], p[1]), True
                            break
            if hit is None and not first_pass:
                for aid, q in ((ta, tq), (fa, fq)):
                    if aid and q and not V.is_fiat(aid):
                        p = V.price(aid, d)
                        if p:
                            hit = (q * p[0], f"{p[1]} × Menge")
                            break
            if hit is not None:
                row["value_eur"], rc.value_src = s(money(hit[0])), hit[1]
                if implied:
                    if typ == "buy":
                        V.add_implied(ta, d, hit[0], tq)
                    elif typ == "sell":
                        V.add_implied(fa, d, hit[0], fq)
                    elif typ == "trade":
                        V.add_implied(ta, d, hit[0], tq)
                        V.add_implied(fa, d, hit[0], fq)
        elif row["value_eur"] and rc.value_src is None:
            rc.value_src = "Datei"
        fee_a, fee_q = row["fee_asset"] or None, _row_d(row["fee_qty"])
        if fee_a and fee_q and not row["fee_eur"]:
            fhit: tuple[Decimal, str] | None = None
            if rc.fee_in:
                v = parse_number(rc.fee_in)
                if v is not None and v >= 0:
                    fhit = (v, "Eingabe")
            if fhit is None and V.is_fiat(fee_a):
                fhit = V.fx(fee_q, fee_a, d)
            if fhit is None and r.fee_value is not None and r.fee_value_ccy:
                conv = V.fx(r.fee_value, r.fee_value_ccy, d)
                if conv:
                    fhit = (conv[0], f"Gegenwert laut Datei ({conv[1]})")
            if fhit is None and row["value_eur"]:
                v = Decimal(row["value_eur"])
                for aid, q in ((fa, fq), (ta, tq)):
                    if aid == fee_a and q:
                        fhit = (v / q * fee_q, "Kurs des Vorgangs")
                        break
            if fhit is None and not first_pass:
                p = V.price(fee_a, d)
                if p:
                    fhit = (fee_q * p[0], f"{p[1]} × Menge")
            if fhit is not None:
                row["fee_eur"], rc.fee_src = s(money(fhit[0])), fhit[1]
        if first_pass:
            return
        if need and not row["value_eur"]:
            asset = ta if ta and not V.is_fiat(ta) else fa
            rc.errors.append(f"EUR-Wert fehlt (kein Kurs für {asset} am {fmt_de_date(d)}) – Wert eingeben oder "
                             "„Kurse laden“")
        elif want and not row["value_eur"]:
            rc.warnings.append("ohne EUR-Wert (Zu-/Abgang wird mit 0 € angesetzt, sofern kein Transfer)")
        if fee_a and fee_q and not row["fee_eur"]:
            rc.warnings.append(f"Gebühr {fee_q.normalize():f} {fee_a} ohne EUR-Wert (wird mit 0 € angesetzt)")

    # -- Dubletten ----------------------------------------------------------------------------------------
    def _same_events(self, rows: list[RowCtx], source: str) -> None:
        """Dasselbe Ereignis aus einer anderen Quelle (CSV-Import ↔ Datenquelle ↔ kuratierter Import): Treffer über
        Anbieter-ID bzw. Alias (Bitpanda-UUIDs) oder dieselbe Kennung derselben Quelle in einer Import-Buchung →
        „bekannt“ (geht nicht erneut in Bewertung und Lots ein); Treffer über den Transaktions-Hash nur bei gleicher
        Buchungsseite → „mögliche Dublette“ (Entscheidung beim Nutzer)."""
        aliases: dict[str, set[str]] = defaultdict(set)
        for a in self.db.q("SELECT key, tx_id FROM journal_event_alias"):
            aliases[a["tx_id"]].add(a["key"])
        by_id: dict[str, list[Any]] = defaultdict(list)
        by_hash: dict[str, list[Any]] = defaultdict(list)
        # Import-Buchungen – auch aus einem Portfolia-Export übernommene App-Buchungen (source „portfolia:csv:…“ bzw.
        # „portfolia:sync:…“, source_ref = Kennung), z. B. erneuter CSV-Import nach einer Neueinrichtung
        same_src: dict[str, str] = {}
        ref_ids: dict[str, str] = {}  # „koinly:<ID>“ → Import-Buchung (Steuertool-Export mit derselben ID)
        base, _ = self.ctx.effective_base()
        for t in base.txs if base is not None else []:
            row = {"tx_id": t.tx_id, "source": "Import", "status": "active"}
            # Anbieter-IDs laut source_ref sowie UUIDs in der Notiz (Koinly führt Bitpanda-UUIDs als „txhash“)
            keys = source_ref_keys(t.source, t.source_ref) | aliases.get(t.tx_id, set()) | note_identity_keys(
                t.source, t.to_account or t.from_account, t.note)
            for k in keys:
                by_id[k].append(row)
            if t.source_ref and (t.source or "").removeprefix("portfolia:") == source:
                same_src[t.source_ref] = t.tx_id
            src = (t.source or "").strip().lower()
            if t.source_ref and src in REF_ID_SOURCES:
                ref_ids.setdefault(f"{src}:{t.source_ref.strip().upper()}", t.tx_id)
        wanted: dict[int, tuple[set[str], str | None]] = {}
        for rc in rows:
            tid = same_src.get(rc.rec.ext_id or "")
            ext = rc.rec.ext_id or ""
            prefix, _, native = ext.partition(":")
            ref_tid = ref_ids.get(f"{prefix.lower()}:{native.strip().upper()}") if native else None
            if tid is not None or ref_tid is not None:
                rc.status = "known"
                rc.dup_of = [tid or ref_tid]  # type: ignore[list-item]
                rc.warnings.insert(0, f"bereits im Import enthalten als {tid or ref_tid} – gleiche Kennung"
                                      + ("" if tid else f" ({prefix}-ID)"))
                continue
            ids = identity_keys(rc.rec.event_key, rc.rec.aliases, rc.rec.ext_id)
            h = normalize_hash(rc.rec.txhash or derive_tx_hash(rc.rec.ext_id))
            if ids or h:
                wanted[rc.idx] = (ids, h)
        if not wanted:
            return
        for r in self.db.q("SELECT tx_id, source, status, external_id, event_key, tx_hash, type, from_asset, to_asset "
                           "FROM journal_tx WHERE status IN ('active', 'merged', 'deleted') AND source <> 'transfer' "
                           "AND source <> ?", (source,)):
            for k in identity_keys(r["event_key"], aliases.get(r["tx_id"], set()), r["external_id"]):
                by_id[k].append(r)
            h = normalize_hash(r["tx_hash"] or derive_tx_hash(r["external_id"]))
            if h:
                by_hash[h].append(r)
        for rc in rows:
            if rc.idx not in wanted:
                continue
            ids, h = wanted[rc.idx]
            hit: dict[str, Any] = {}
            for k in sorted(ids):
                for r in by_id.get(k, ()):
                    hit.setdefault(r["tx_id"], r)
            if hit:
                first = next(iter(hit.values()))
                rc.status = "known"
                rc.dup_of = list(hit)
                rc.warnings.insert(0, f"bereits vorhanden als {', '.join(list(hit)[:3])} "
                                      f"({source_label(first['source'])}"
                                      + (", gelöscht" if first["status"] == "deleted" else "") + ") – gleiche "
                                      "Anbieter-ID")
                continue
            row = rc.row
            if not h or rc.status != "new" or row is None:
                continue
            hits = list(dict.fromkeys(
                r["tx_id"] for r in by_hash.get(h, ())
                if (r["type"], r["from_asset"] or "", r["to_asset"] or "") == (row["type"], row["from_asset"],
                                                                                 row["to_asset"])))
            if hits:
                rc.status = "duplicate"
                rc.dup_of = hits
                rc.dup_same_account = True
                rc.warnings.insert(0, f"gleiche Blockchain-Transaktion bereits vorhanden: {', '.join(hits[:3])}")

    def _duplicates(self, rows: list[RowCtx], pf: Portfolio | None, source: str) -> None:
        if pf is None or not rows:
            return
        index: dict[tuple[str, str], list[Tx]] = defaultdict(list)
        transfers: dict[str, list[Tx]] = defaultdict(list)
        for t in pf.txs:
            if t.origin == "journal" and (t.source or "") == source:
                continue  # gleiche Quelle: Erkennung über die Quellkennung
            index[(t.from_asset or "", t.to_asset or "")].append(t)
            # erfasste Transfers (Import, Journal) – nicht die aus abgeglichenen Paaren entstandenen (PF-T): deren
            # Zu-/Abgänge sind über ihre Kennungen bekannt, ein neuer Vorgang ist ein anderer
            if t.type == "transfer" and t.from_asset and t.from_asset == t.to_asset and \
                    not (t.origin == "journal" and t.source == "transfer"):
                transfers[t.from_asset].append(t)
        for lst in index.values():
            lst.sort(key=lambda t: t.ts)
        used: set[str] = set()
        for rc in rows:
            row = rc.row
            if row is None:
                continue
            if self._covered_by_transfer(rc, transfers, used):
                continue
            key = (row["from_asset"], row["to_asset"])
            cands = index.get(key)
            if not cands:
                continue
            lo = bisect.bisect_left([t.ts for t in cands], rc.ts - timedelta(hours=15))
            fq, tq = _row_d(row["from_qty"]), _row_d(row["to_qty"])
            for t in cands[lo:]:
                if t.ts > rc.ts + timedelta(hours=15):
                    break
                if not _qty_eq(fq, t.from_qty) or not _qty_eq(tq, t.to_qty):
                    continue
                delta = abs((t.ts - rc.ts).total_seconds())
                if not (rc.rec.date_only or t.date_only):
                    if not (delta <= 600 or delta % 3600 <= 120 or delta % 3600 >= 3480):
                        continue
                elif t.date != rc.d:
                    continue
                rc.dup_of.append(t.tx_id)
                if row["from_account"] in (t.from_account, t.to_account) or \
                        row["to_account"] in (t.from_account, t.to_account):
                    rc.dup_same_account = rc.dup_same_account or bool(row["from_account"] or row["to_account"])
            if rc.dup_of:
                rc.status = "duplicate"
                rc.warnings.insert(0, f"ähnelt {', '.join(rc.dup_of[:3])}" + (" (gleiches Konto)" if
                                                                                rc.dup_same_account else ""))

    @staticmethod
    def _same_qty(rows: list[RowCtx], pf: Portfolio | None, source: str) -> None:
        """Gleiche exakte Menge desselben Assets auf demselben Konto und derselben Seite (Zu- bzw. Abgang) innerhalb
        von 36 Stunden – Muster „einmal manuell nachgetragen, einmal importiert bzw. als Transfer erfasst“. Gleiche
        Menge und Zeit beweisen keine Dublette; der Vorgang geht deshalb in die Prüfung (nie automatisch übernommen),
        die vorhandene Buchung bleibt unverändert. Ausgenommen: zwei verschiedene Blockchain-Transaktionen,
        wiederkehrende Erträge (Staking, Zinsen …) und Fiat."""
        if pf is None or not rows:
            return
        index: dict[tuple[str, str, str, Decimal], list[Tx]] = defaultdict(list)
        for t in pf.txs:
            if t.origin == "journal" and (t.source or "") == source:
                continue  # gleiche Quelle: Erkennung über die Quellkennung
            if t.origin == "plan":
                continue
            if t.to_account and t.to_asset and t.to_qty and t.type in ("deposit", "transfer"):
                index[("in", t.to_account, t.to_asset, t.to_qty)].append(t)
            if t.from_account and t.from_asset and t.from_qty and t.type in ("withdrawal", "transfer"):
                index[("out", t.from_account, t.from_asset, t.from_qty)].append(t)
        if not index:
            return
        for rc in rows:
            row = rc.row
            if row is None or row["type"] not in ("deposit", "withdrawal", "transfer") or rc.status != "new":
                continue
            if (row["tag"] or "") in C.INCOME_TAGS:
                continue
            h = normalize_hash(rc.rec.txhash or derive_tx_hash(rc.rec.ext_id))
            legs = []
            if row["to_asset"] and row["to_qty"] and row["type"] in ("deposit", "transfer"):
                legs.append(("in", row["to_account"], row["to_asset"], Decimal(row["to_qty"])))
            if row["from_asset"] and row["from_qty"] and row["type"] in ("withdrawal", "transfer"):
                legs.append(("out", row["from_account"], row["from_asset"], Decimal(row["from_qty"])))
            hits: list[Tx] = []
            for key in legs:
                if key[2] in C.ISO_CURRENCIES:
                    continue
                for t in index.get(key, ()):
                    if abs(t.ts - rc.ts) > SAME_QTY_WINDOW or t.tx_id in rc.dup_of:
                        continue
                    t_hashes = R.hashes_in(t.note, t.source_ref)
                    if h and t_hashes and h not in t_hashes:
                        continue  # zwei verschiedene Blockchain-Transaktionen
                    hits.append(t)
            if hits:
                hits.sort(key=lambda t: (abs(t.ts - rc.ts), t.tx_id))
                rc.status = "duplicate"
                rc.dup_same_account = True
                rc.dup_of = list(dict.fromkeys([*rc.dup_of, *(t.tx_id for t in hits)]))
                t = hits[0]
                rc.warnings.insert(0, f"gleiche Menge wie {t.tx_id} ({fmt_de_date(to_local_date(t.ts))}, "
                                      f"{_span_abs(t.ts - rc.ts)} Abstand) auf demselben Konto – möglicherweise "
                                      "doppelt erfasst; bitte prüfen")

    @staticmethod
    def _reconstructed(rows: list[RowCtx], pf: Portfolio | None) -> None:
        """Vorgang auf Konto und Asset einer rekonstruierten Buchung des kuratierten Imports (Quelle „reconstructed“
        bzw. Kennzeichen ``RECONSTRUCTED_*``) innerhalb von 7 Tagen: Die echte Abrechnung ersetzt womöglich die
        Schätzung (abweichende Menge, z. B. Durchschnittskurs) – nie still zusätzlich buchen, sondern prüfen. Die
        rekonstruierte Buchung bleibt unverändert; ersetzt wird sie nur im kuratierten Import."""
        if pf is None or not rows:
            return
        index: dict[tuple[str, str], list[Tx]] = defaultdict(list)
        for t in pf.txs:
            if t.origin != "import":
                continue
            if (t.source or "") != "reconstructed" and "RECONSTRUCTED" not in (t.flag or "").upper():
                continue
            for acc, aid in ((t.to_account, t.to_asset), (t.from_account, t.from_asset)):
                if acc and aid and aid not in C.ISO_CURRENCIES:
                    index[(acc, aid)].append(t)
        if not index:
            return
        for rc in rows:
            row = rc.row
            if row is None or rc.status != "new":
                continue
            keys = {(row["to_account"], row["to_asset"]), (row["from_account"], row["from_asset"])}
            hits = sorted({t.tx_id: t for k in keys if k[0] and k[1] for t in index.get(k, ())
                           if abs(t.ts - rc.ts) <= RECONSTRUCTED_WINDOW}.values(),
                          key=lambda t: (abs(t.ts - rc.ts), t.tx_id))
            if hits:
                t = hits[0]
                rc.status = "duplicate"
                rc.dup_same_account = True
                rc.dup_of = list(dict.fromkeys([*rc.dup_of, *(x.tx_id for x in hits)]))
                rc.warnings.insert(0, f"rekonstruierte Buchung {t.tx_id} ({fmt_de_date(to_local_date(t.ts))}) im "
                                      "kuratierten Import – ersetzt dieser Vorgang sie? Dann dort ersetzen statt "
                                      "zusätzlich übernehmen")

    @staticmethod
    def _covered_by_transfer(rc: RowCtx, transfers: Mapping[str, list[Tx]], used: set[str]) -> bool:
        """Zu- bzw. Abgang, der bereits Teil eines erfassten Transfers ist (z. B. Börsen-Auszahlung → Wallet, im
        kuratierten Import oder im Journal als Transfer gebucht) → mögliche Dublette auf demselben Konto: ohne
        Abwahl zählte die Menge doppelt."""
        row = rc.row
        assert row is not None
        if row["type"] == "deposit" and not row["tag"] and row["to_asset"]:
            asset, acc, qty, side = row["to_asset"], row["to_account"], _row_d(row["to_qty"]), "to"
        elif row["type"] == "withdrawal" and not row["tag"] and row["from_asset"]:
            asset, acc, qty, side = row["from_asset"], row["from_account"], _row_d(row["from_qty"]), "from"
        else:
            return False
        if not qty:
            return False
        for t in sorted(transfers.get(asset, ()), key=lambda t: abs((t.ts - rc.ts).total_seconds())):
            t_acc = t.to_account if side == "to" else t.from_account
            t_qty = t.to_qty if side == "to" else t.from_qty
            if t_acc != acc or not t_qty or t.tx_id in used:
                continue
            if side == "to":  # Eingang kommt nach dem Abgang (Netzwerkgebühr: Zugang ≤ Abgang)
                ok_time = t.ts - TRANSFER_BEFORE <= rc.ts <= t.ts + TRANSFER_AFTER
            else:
                ok_time = abs((t.ts - rc.ts).total_seconds()) <= TRANSFER_BEFORE.total_seconds()
            if ok_time and (_qty_eq(qty, t_qty) or (side == "to" and t.from_qty and _qty_eq(qty, t.from_qty))):
                used.add(t.tx_id)
                rc.status = "duplicate"
                rc.dup_of = [t.tx_id]
                rc.dup_same_account = True
                rc.warnings.insert(0, f"bereits als Transfer erfasst: {t.tx_id} ({t.from_account} → {t.to_account})"
                                      " – nicht erneut übernehmen")
                return True
        return False

    # -- Transfers ----------------------------------------------------------------------------------------
    def _transfers(self, bid: int, rows: list[RowCtx], pf: Portfolio | None, V: Valuer) -> None:
        """Abgänge und Zugänge desselben Kryptowerts auf verschiedenen Konten zu Transfer-Paaren zuordnen."""
        @dataclass
        class Side:
            ref: str
            account: str
            asset: str
            qty: Decimal
            ts: datetime
            txhash: str | None
            rc: RowCtx | None

        def eligible(rc: RowCtx, typ: str) -> bool:
            row = rc.row
            if row is None or row["type"] != typ or row["tag"]:
                return False
            if rc.status not in ("new", "duplicate") and not (rc.status == "before" and rc.decision == "include"):
                return False
            return not V.is_fiat(row["from_asset"] if typ == "withdrawal" else row["to_asset"])

        outs: list[Side] = []
        ins: list[Side] = []
        for rc in rows:
            if not rc.open:
                continue
            prev = (rc.pair_ref, rc.pair_ok)
            rc.pair_ref = rc.pair_conf = None
            rc.counterpart = None
            if eligible(rc, "withdrawal"):
                assert rc.row is not None
                outs.append(Side(f"b:{rc.idx}", rc.row["from_account"], rc.row["from_asset"],
                                 Decimal(rc.row["from_qty"]), rc.ts, rc.rec.txhash, rc))
            elif eligible(rc, "deposit"):
                assert rc.row is not None
                ins.append(Side(f"b:{rc.idx}", rc.row["to_account"], rc.row["to_asset"], Decimal(rc.row["to_qty"]),
                                rc.ts, rc.rec.txhash, rc))
            rc.pair_ok = prev[1] if prev[0] else None
            rc.prev_ref = prev[0]
        if not outs and not ins:
            return
        for jr in self.db.q(
                "SELECT tx_id, type, from_account, from_asset, from_qty, to_account, to_asset, to_qty, ts_utc, "
                "tx_hash FROM journal_tx WHERE status='active' AND (batch_id IS NULL OR batch_id<>?) AND type IN "
                "('deposit','withdrawal') AND (tag IS NULL OR tag='')", (bid,)):
            ts = parse_iso(jr["ts_utc"])
            if ts is None:
                continue
            h = normalize_hash(jr["tx_hash"])
            if jr["type"] == "withdrawal" and jr["from_asset"] and not V.is_fiat(jr["from_asset"]):
                outs.append(Side(f"j:{jr['tx_id']}", jr["from_account"], jr["from_asset"], Decimal(jr["from_qty"]), ts,
                                 h, None))
            elif jr["type"] == "deposit" and jr["to_asset"] and not V.is_fiat(jr["to_asset"]):
                ins.append(Side(f"j:{jr['tx_id']}", jr["to_account"], jr["to_asset"], Decimal(jr["to_qty"]), ts, h,
                                None))
        ins_by: dict[str, list[Side]] = defaultdict(list)
        for i in ins:
            ins_by[i.asset].append(i)
        for lst in ins_by.values():
            lst.sort(key=lambda x: x.ts)
        cands: list[tuple[tuple[int, Decimal, float], Side, Side, str, str]] = []
        for o in outs:
            lst = ins_by.get(o.asset)
            if not lst:
                continue
            lo = bisect.bisect_left([x.ts for x in lst], o.ts - TRANSFER_BEFORE)
            for i in lst[lo:]:
                if i.ts > o.ts + TRANSFER_AFTER:
                    break
                if i.account == o.account or (o.rc is None and i.rc is None):
                    continue
                hash_eq = bool(o.txhash and i.txhash and normalize_hash(o.txhash) == normalize_hash(i.txhash))
                ratio = i.qty / o.qty if o.qty else Decimal(0)
                if not hash_eq and not (TRANSFER_MIN_RATIO <= ratio <= Decimal("1.001")):
                    continue
                dt = abs((i.ts - o.ts).total_seconds())
                conf = "hoch" if hash_eq or (ratio >= Decimal("0.98") and dt <= 86400) else "mittel"
                why = ("gleiche Blockchain-Transaktion" if hash_eq else
                       f"Menge {ratio * 100:.1f} % des Abgangs, {_span(i.ts - o.ts)}")
                cands.append(((0 if hash_eq else 1, abs(1 - ratio), dt), o, i, conf, why))
        cands.sort(key=lambda x: x[0])
        used: set[str] = set()
        for _score, o, i, conf, why in cands:
            if o.ref in used or i.ref in used:
                continue
            used.update((o.ref, i.ref))
            for me, other in ((o, i), (i, o)):
                if me.rc is not None:
                    me.rc.pair_ref, me.rc.pair_conf, me.rc.pair_why = other.ref, conf, why
                    if me.rc.prev_ref != other.ref:
                        me.rc.pair_ok = None
        for rc in rows:
            if rc.open and rc.pair_ref is None:
                rc.pair_ok = None
        self._import_counterparts([s_ for s_ in (*outs, *ins) if s_.rc is not None and s_.ref not in used], pf, V)

    @staticmethod
    def _import_counterparts(sides: list[Any], pf: Portfolio | None, V: Valuer) -> None:
        """Hinweis, wenn ein nicht abgeglichener Zu-/Abgang zu einer Buchung im kuratierten Import passt – der
        Transfer lässt sich nur dort zusammenführen (Importbuchungen werden nie verändert)."""
        if pf is None or not sides:
            return
        idx: dict[tuple[str, str], list[Tx]] = defaultdict(list)
        for t in pf.txs:
            if t.origin != "import" or t.tag or t.type not in ("deposit", "withdrawal"):
                continue
            aid = t.to_asset if t.type == "deposit" else t.from_asset
            if aid and not V.is_fiat(aid):
                idx[(t.type, aid)].append(t)
        for side in sides:
            rc = side.rc
            want = "deposit" if rc.row["type"] == "withdrawal" else "withdrawal"
            for t in idx.get((want, side.asset), []):
                acc = t.to_account if want == "deposit" else t.from_account
                qty = (t.to_qty if want == "deposit" else t.from_qty) or Decimal(0)
                if acc == side.account or not qty:
                    continue
                o_ts, i_ts = (side.ts, t.ts) if want == "deposit" else (t.ts, side.ts)
                o_q, i_q = (side.qty, qty) if want == "deposit" else (qty, side.qty)
                if not (o_ts - TRANSFER_BEFORE <= i_ts <= o_ts + TRANSFER_AFTER):
                    continue
                if TRANSFER_MIN_RATIO <= i_q / o_q <= Decimal("1.001"):
                    rc.counterpart = t.tx_id
                    rc.warnings.append(f"passt zu {t.tx_id} im kuratierten Import ({acc}) – Transfer dort erfassen, "
                                       "sonst zählt der Vorgang als Zu-/Abgang; wird nicht automatisch übernommen")
                    break

    def _save(self, rows: list[RowCtx]) -> None:
        data = []
        for rc in rows:
            if not rc.open:
                continue
            msgs = {"errors": rc.errors[:10], "warnings": rc.warnings[:10], "value_src": rc.value_src,
                    "fee_src": rc.fee_src, "dup_of": rc.dup_of[:5], "dup_same": rc.dup_same_account,
                    "symbols": rc.symbols, "pair_why": rc.pair_why if rc.pair_ref else None, "recon": rc.recon,
                    "counterpart": rc.counterpart}
            data.append((json.dumps(rc.row, ensure_ascii=False) if rc.row is not None else None, rc.status,
                         json.dumps(msgs, ensure_ascii=False), rc.pair_ref, rc.pair_conf, rc.pair_ok, rc.id))
        if data:
            self.db.xmany("UPDATE csv_row SET row_json=?, status=?, messages=?, pair_ref=?, pair_conf=?, pair_ok=? "
                          "WHERE id=?", data)

    # -- Ansicht ------------------------------------------------------------------------------------------
    def rows(self, bid: int) -> list[RowCtx]:
        return self._load(bid)

    def overview(self, bid: int) -> dict[str, Any]:
        rows = self._load(bid)
        counts: dict[str, int] = defaultdict(int)
        for rc in rows:
            counts[rc.status] += 1
        # Zuordnen nur, was übernommen werden soll; Symbole nur aus Vorgängen vor dem Stichtag sind optional
        unknown: dict[str, dict[str, Any]] = {}
        unknown_old: dict[str, dict[str, Any]] = {}
        accounts: dict[str, int] = defaultdict(int)
        missing_price: dict[str, int] = defaultdict(int)
        for rc in rows:
            if not rc.open:
                continue
            target = unknown if rc.status in RELEVANT else unknown_old if rc.status == "before" else None
            for raw, v in rc.symbols.items() if target is not None else ():
                if v in ("unknown", "ambiguous"):
                    pk = split_provider_key(raw)  # Kürzel mit Anbieter-Identität, z. B. TH@BITPANDA
                    plain = pk[0] if pk else raw
                    u = target.setdefault(raw.upper(), {"symbol": raw.upper(), "display": raw, "count": 0,
                                                        "ambiguous": v == "ambiguous",
                                                        "hint": rc.rec.class_hint.get(plain), "spam": False,
                                                        "dirs": set(), "kinds": set(), "old": target is unknown_old,
                                                        "provider": pk[1] if pk else None})
                    u["count"] += 1
                    u["spam"] = u["spam"] or bool(rc.rec.review and "Spam" in rc.rec.review)
                    u["dirs"].add(_direction(rc.rec, plain))
                    u["kinds"].add(rc.rec.kind)
            for a in (rc.rec.account, rc.rec.to_account):
                if a:
                    accounts[a] += 1
            for e in rc.errors if rc.status in RELEVANT else ():
                if e.startswith("EUR-Wert fehlt") and rc.row is not None:
                    missing_price[rc.row["to_asset"] if rc.row["to_asset"] and rc.row["type"] != "sell"
                                  else rc.row["from_asset"]] += 1
        for k in [k for k in unknown_old if k in unknown]:
            del unknown_old[k]
        ordered = sorted(unknown.values(), key=lambda u: -u["count"])
        ordered_old = sorted(unknown_old.values(), key=lambda u: -u["count"])
        for i, u in enumerate([*ordered, *ordered_old]):
            u["i"] = i  # Feldindex im gemeinsamen Formular
        pairs = [rc for rc in rows if rc.open and rc.pair_ref and rc.row is not None and
                 rc.row["type"] == "withdrawal"]
        pairs += [rc for rc in rows if rc.open and rc.pair_ref and rc.pair_ref.startswith("j:") and rc.row is not None
                  and rc.row["type"] == "deposit"]
        batch = self.batch(bid)
        summ = json.loads(batch["summary_json"] or "{}") if batch is not None else {}
        syms = {s.strip().upper() for rc in rows for s in rc.rec.symbols()}
        auto = [(r["symbol"], r["asset_id"]) for r in self.db.q(
            "SELECT symbol, asset_id FROM csv_symbol WHERE origin='abgleich' ORDER BY symbol") if r["symbol"] in syms]
        return {"counts": dict(counts), "unknown": ordered, "unknown_old": ordered_old,
                "accounts": dict(accounts), "missing_price": dict(missing_price), "pairs": pairs,
                "to_commit": sum(1 for rc in rows if rc.open and rc.include() and rc.row is not None
                                 and not rc.errors), "total": len(rows),
                "by_idx": {rc.idx: rc for rc in rows}, "recon": summ.get("recon"), "auto_symbols": auto}

    # -- Eingaben -----------------------------------------------------------------------------------------
    @_locked
    def set_rows(self, bid: int, form: Mapping[str, Any]) -> None:
        rows = {rc.idx: rc for rc in self._load(bid)}
        data = []
        for key in form:
            k = str(key)
            if not k.startswith(("dec_", "val_", "fee_", "pair_")):
                continue
            kind, _, num_ = k.partition("_")
            if not num_.isdigit() or int(num_) not in rows:
                continue
            rc = rows[int(num_)]
            if not rc.open:
                continue
            v = str(form.get(k) or "").strip()
            if kind == "dec":
                rc.decision = v if v in ("include", "skip") else None
            elif kind == "val":
                rc.value_in = v[:40] or None
            elif kind == "fee":
                rc.fee_in = v[:40] or None
            elif kind == "pair":
                rc.pair_ok = 1 if v == "1" else 0 if v == "0" else None
                if rc.pair_ref and rc.pair_ref.startswith("b:") and int(rc.pair_ref[2:]) in rows:
                    partner = rows[int(rc.pair_ref[2:])]
                    partner.pair_ok = rc.pair_ok
                    data.append(partner)
            data.append(rc)
        if data:
            self.db.xmany("UPDATE csv_row SET decision=?, value_in=?, fee_in=?, pair_ok=? WHERE id=?",
                          [(rc.decision, rc.value_in, rc.fee_in, rc.pair_ok, rc.id) for rc in
                           {rc.id: rc for rc in data}.values()])
        self.evaluate(bid)

    @_locked
    def set_all(self, bid: int, status: str, decision: str | None) -> None:
        self.db.x("UPDATE csv_row SET decision=? WHERE batch_id=? AND status=?", (decision, bid, status))
        self.evaluate(bid)

    # -- Übernehmen ---------------------------------------------------------------------------------------
    @_locked
    def commit(self, bid: int, only_idx: set[int] | None = None) -> dict[str, Any]:
        """Ausgewählte Zeilen übernehmen. ``only_idx``: nur diese Zeilen (automatische Übernahme einer Datenquelle
        je eindeutigem Ereignis) – alle übrigen bleiben zur Prüfung offen. Validierung wie immer."""
        batch = self.batch(bid)
        if batch is None or batch["status"] in ("mapping", "reverted"):
            return {"errors": ["Stapel nicht übernehmbar."]}
        self.evaluate(bid)
        rows = self._load(bid)
        by_idx = {rc.idx: rc for rc in rows}
        chosen = [rc for rc in rows if rc.open and rc.include() and rc.row is not None and not rc.errors
                  and (only_idx is None or rc.idx in only_idx)]
        if not chosen:
            return {"errors": ["Keine übernehmbaren Zeilen (Status „neu“ bzw. ausgewählt, ohne Fehler)."]}
        assets = self.known_assets()
        classes = {aid: {"asset_class": a.asset_class} for aid, a in assets.items()}
        for c in C.ISO_CURRENCIES:
            classes.setdefault(c, {"asset_class": "fiat"})
        rep, parsed = validate_tx_rows([{**rc.row, "tx_id": f"Z{rc.idx}"} for rc in chosen], classes)  # type: ignore
        if rep.errors:
            return {"errors": [m.message for m in rep.errors[:10]]}
        p_by_idx = {rc.idx: p for rc, p in zip(chosen, parsed, strict=True)}
        source = self.source_of(batch)
        stamp = _now()
        js = self.journal
        created = merged = transfers = 0
        pairs: list[tuple[RowCtx, str]] = []  # (Zeile, Gegenbuchung) – bestätigte Paare
        with self.db.transaction() as c:
            for rc in chosen:
                p = p_by_idx[rc.idx]
                pair_other = rc.pair_ref if rc.pair_ref and pair_accepted(rc) else None
                if pair_other and pair_other.startswith("b:"):
                    other = by_idx.get(int(pair_other[2:]))
                    if other is None or not other.include() or other.idx not in p_by_idx:
                        pair_other = None
                status = "merged" if pair_other else "active"
                tx_id = js._insert(c, p, rc.value_src, None, stamp, None, source,
                                   external_id=rc.rec.ext_id, batch_id=bid, status=status, log=False,
                                   event_key=rc.rec.event_key or derive_event_key(rc.rec.ext_id),
                                   event_line=rc.rec.event_line,
                                   tx_hash=normalize_hash(rc.rec.txhash) or derive_tx_hash(rc.rec.ext_id),
                                   datasource_id=batch["datasource_id"])
                if rc.rec.aliases:
                    c.executemany("INSERT OR IGNORE INTO journal_event_alias(key, tx_id) VALUES (?,?)",
                                  [(a, tx_id) for a in rc.rec.aliases])
                rc.tx_id = tx_id
                rc.status = "merged" if pair_other else "committed"
                c.execute("UPDATE csv_row SET status=?, tx_id=? WHERE id=?", (rc.status, tx_id, rc.id))
                if pair_other:
                    pairs.append((rc, pair_other))
                    merged += 1
                else:
                    created += 1
            done: set[str] = set()
            for rc, other_ref in pairs:
                if rc.tx_id in done:
                    continue
                if other_ref.startswith("b:"):
                    other = by_idx[int(other_ref[2:])]
                    other_tx = other.tx_id
                else:
                    other_tx = other_ref[2:]
                    j = c.execute("SELECT status FROM journal_tx WHERE tx_id=?", (other_tx,)).fetchone()
                    if j is None or j["status"] != "active":
                        c.execute("UPDATE journal_tx SET status='active' WHERE tx_id=?", (rc.tx_id,))
                        c.execute("UPDATE csv_row SET status='committed' WHERE id=?", (rc.id,))
                        merged -= 1
                        created += 1
                        continue
                if other_tx is None:
                    continue
                t_id = self._merge(c, js, rc.tx_id or "", other_tx, bid, stamp)  # type: ignore[arg-type]
                if t_id:
                    transfers += 1
                done.update((rc.tx_id or "", other_tx))
            chosen_ids = {rc.id for rc in chosen}
            if batch["kind"] == "sync":  # offen bleibt, was noch eine Entscheidung braucht
                left = sum(1 for rc in rows if rc.open and rc.id not in chosen_ids and (
                    rc.status in ("invalid", "unclear") or (only_idx is not None and rc.status not in DONE)))
            else:
                left = sum(1 for rc in rows if rc.open and rc.id not in chosen_ids and rc.status == "invalid")
            c.execute("UPDATE csv_batch SET status=?, committed_at=?, updated_at=? WHERE id=?",
                      ("partial" if left else "committed", stamp, stamp, bid))
            js._log(c, "csv_commit", f"csv:{bid}", None, {"created": created, "merged": merged,
                                                           "transfers": transfers}, stamp)
        js.after_change()
        log.info("CSV-Stapel %s übernommen: %d Buchungen, %d Transfers", bid, created, transfers)
        return {"created": created, "transfers": transfers, "merged": merged, "errors": []}

    def _merge(self, c: Any, js: JournalService, a_tx: str, b_tx: str, bid: int, stamp: str) -> str | None:
        """Abgang + Zugang → Transfer-Buchung; beide Einzelbuchungen werden „merged“."""
        a = c.execute("SELECT * FROM journal_tx WHERE tx_id=?", (a_tx,)).fetchone()
        b = c.execute("SELECT * FROM journal_tx WHERE tx_id=?", (b_tx,)).fetchone()
        if a is None or b is None:
            return None
        w, dpt = (a, b) if a["type"] == "withdrawal" else (b, a)
        if w["type"] != "withdrawal" or dpt["type"] != "deposit" or w["from_asset"] != dpt["to_asset"]:
            return None
        fq, tq = Decimal(w["from_qty"]), Decimal(dpt["to_qty"])
        fee_asset, fee_qty, fee_eur = w["fee_asset"], w["fee_qty"], w["fee_eur"]
        if not fee_qty and dpt["fee_qty"]:
            fee_asset, fee_qty, fee_eur = dpt["fee_asset"], dpt["fee_qty"], dpt["fee_eur"]
        note = f"Transfer {w['from_account']} → {dpt['to_account']} (abgeglichen: {w['tx_id']}, {dpt['tx_id']})"
        row = {"tx_id": "T", "datetime": w["ts_utc"], "type": "transfer", "tag": "",
               "from_account": w["from_account"], "from_asset": w["from_asset"], "from_qty": s(fq),
               "to_account": dpt["to_account"], "to_asset": dpt["to_asset"], "to_qty": s(min(tq, fq)),
               "fee_asset": fee_asset or "", "fee_qty": fee_qty or "", "fee_eur": fee_eur or "", "value_eur": "",
               "note": note}
        classes = {x: {"asset_class": "crypto"} for x in (w["from_asset"], fee_asset) if x}
        if fee_asset and fee_asset in C.ISO_CURRENCIES:
            classes[fee_asset] = {"asset_class": "fiat"}
        rep, parsed = validate_tx_rows([row], classes)
        if rep.errors or not parsed:
            log.warning("Transfer %s/%s nicht zusammenführbar: %s", a_tx, b_tx, [m.message for m in rep.errors])
            return None
        t_id = js._insert(c, parsed[0], None, None, stamp, None, "transfer", batch_id=bid,
                          pair_refs=f"{w['tx_id']},{dpt['tx_id']}", log=True)
        c.execute("UPDATE journal_tx SET status='merged', merged_into=?, updated_at=? WHERE tx_id IN (?,?)",
                  (t_id, stamp, w["tx_id"], dpt["tx_id"]))
        return t_id

    # -- Rückgängig ---------------------------------------------------------------------------------------
    @_locked
    def revert(self, bid: int) -> dict[str, Any]:
        batch = self.batch(bid)
        if batch is None or batch["status"] not in ("partial", "committed"):
            return {"errors": ["Nur übernommene Stapel lassen sich rückgängig machen."]}
        stamp = _now()
        js = self.journal
        n = 0
        with self.db.transaction() as c:
            own = [r["tx_id"] for r in c.execute("SELECT tx_id FROM journal_tx WHERE batch_id=? AND source<>'transfer'",
                                                 (bid,))]
            own_set = set(own)
            transfers = [t for t in c.execute("SELECT * FROM journal_tx WHERE source='transfer' AND status IN "
                                              "('active','deleted')")
                         if t["batch_id"] == bid or own_set & {x.strip() for x in (t["pair_refs"] or "").split(",")}]
            for t in transfers:
                js.unpair_in(c, t, stamp, keep=frozenset({bid}))
            for tx in own:
                cur = c.execute("UPDATE journal_tx SET status='reverted', merged_into=NULL, updated_at=? WHERE tx_id=? "
                                "AND status <> 'reverted'", (stamp, tx))
                n += cur.rowcount
            c.execute("UPDATE csv_row SET status='new', tx_id=NULL WHERE batch_id=? AND status IN ('committed',"
                      "'merged')", (bid,))
            c.execute("UPDATE csv_batch SET status='reverted', reverted_at=?, updated_at=? WHERE id=?",
                      (stamp, stamp, bid))
            js._log(c, "csv_revert", f"csv:{bid}", None, {"reverted": n, "transfers": len(transfers)}, stamp)
        js.after_change()
        log.info("CSV-Stapel %s rückgängig gemacht (%d Buchungen)", bid, n)
        return {"reverted": n, "transfers": len(transfers), "errors": []}

    @_locked
    def reopen(self, bid: int) -> bool:
        """Rückgängig gemachten Stapel wieder als Vorschau öffnen (erneut übernehmen)."""
        batch = self.batch(bid)
        if batch is None or batch["status"] != "reverted":
            return False
        self.db.x("UPDATE csv_batch SET status='preview', updated_at=? WHERE id=?", (_now(), bid))
        self.evaluate(bid)
        return True

    # -- Kurse für neue Assets laden ----------------------------------------------------------------------
    def load_prices(self, bid: int) -> dict[str, Any]:
        """Tagesschlusskurse und Devisenkurse für die Assets und den Zeitraum des Stapels laden, dann neu bewerten."""
        rows = [rc for rc in self._load(bid) if rc.open and rc.row is not None]
        assets = self.known_assets()
        txs: list[Tx] = []
        for i, rc in enumerate(rows):
            row = rc.row
            assert row is not None
            try:
                txs.append(Tx(seq=i, tx_id=f"Z{rc.idx}", ts=rc.ts, date=rc.d, date_only=False, type=row["type"],
                              tag=row["tag"] or None, from_account=row["from_account"] or None,
                              from_asset=row["from_asset"] or None, from_qty=_row_d(row["from_qty"]),
                              to_account=row["to_account"] or None, to_asset=row["to_asset"] or None,
                              to_qty=_row_d(row["to_qty"]), fee_asset=row["fee_asset"] or None,
                              fee_qty=_row_d(row["fee_qty"]), fee_eur=None, value_eur=_row_d(row["value_eur"])))
            except (ArithmeticError, ValueError):
                continue
        if not txs:
            return {"skipped": "keine Zeilen"}
        used = {a for t in txs for a in (t.from_asset, t.to_asset, t.fee_asset) if a}
        pf_assets = {aid: assets.get(aid) or AssetInfo(asset_id=aid, name=aid, asset_class="fiat" if aid in
                                                       C.ISO_CURRENCIES else "crypto") for aid in used}
        pf = Portfolio(import_id=None, txs=txs, assets=pf_assets, accounts={})
        led = run_ledger(pf, self.ctx.engine_options())
        res = self.ctx.prices.backfill(pf, led, progress=lambda p: self.ctx.job_progress("csv_prices", p))
        self.ctx.invalidate_history()
        self.evaluate(bid)
        return res

    # -- Aufräumen ----------------------------------------------------------------------------------------
    def symbol_rows(self) -> list[Any]:
        return self.db.q("SELECT * FROM csv_symbol ORDER BY symbol")

    def account_rows(self) -> list[Any]:
        return self.db.q("SELECT * FROM csv_account ORDER BY name")


def _recon_text(m: R.RowMatch) -> str:
    where = "im kuratierten Import" if m.origin == "import" else "bereits in Portfolia erfasst"
    acc = f" · {', '.join(m.accounts)}" if m.accounts else ""
    txt = f"{where}: {', '.join(m.txs[:3])}{acc} (gleiche Blockchain-Transaktion)"
    if m.state != "full" and m.missing:
        txt += f" – dort nicht gefunden: {', '.join(m.missing)}"
    for sym, aid in m.other_asset.items():
        txt += f" – Gegenbuchung mit {aid} statt {sym}: Zuordnung prüfen"
    return txt


def _span(d: timedelta) -> str:
    secs = d.total_seconds()
    when = "nach dem Abgang" if secs >= 0 else "vor dem Abgang"
    secs = abs(secs)
    if secs < 120:
        return f"{int(secs)} s {when}"
    if secs < 7200:
        return f"{int(secs // 60)} min {when}"
    return f"{secs / 3600:.1f} h {when}".replace(".", ",")


def _span_abs(d: timedelta) -> str:
    secs = abs(d.total_seconds())
    if secs < 120:
        return f"{int(secs)} s"
    if secs < 7200:
        return f"{int(secs // 60)} min"
    return f"{secs / 3600:.1f} h".replace(".", ",")


def pair_accepted(rc: RowCtx) -> bool:
    """Transfer-Paar übernehmen: bestätigt – oder unbestätigt mit hoher Sicherheit (gleicher Hash bzw. ≥ 98 % der
    Menge innerhalb von 24 Stunden)."""
    return rc.pair_ok == 1 or (rc.pair_ok is None and rc.pair_conf == "hoch")


def _qty_eq(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs(b) * DUP_QTY_TOL, Decimal("1e-8"))


def _versioned(source: str) -> bool:
    """Anbieter einer Datenquelle mit versionierter Auswertung (Zeilen eines Ereignisses = Deutung, keine
    eigenständigen Bewegungen)."""
    from app.datasources.connector import connector_for

    conn = connector_for(source.removeprefix("sync:")) if source.startswith("sync:") else None
    return bool(conn is not None and conn.parser_version)


def _strip_prefix(msg: str) -> str:
    return msg.split(": ", 1)[1] if msg.startswith("Z") and ": " in msg[:12] else msg


def csv_service(ctx: Any) -> CsvImportService:
    svc = getattr(ctx, "_csv_service", None)
    if svc is None:
        svc = CsvImportService(ctx)
        ctx._csv_service = svc
    return svc
