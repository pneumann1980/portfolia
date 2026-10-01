"""Lesender Schnappschuss für die Diagnose.

Nur ``SELECT``-Abfragen und die vorhandenen Rechen-Caches des Anwendungskontexts (Portfolio, Ledger, Kurse) – kein
Schreibzugriff, keine Online-Abfrage. Die Regeln in :mod:`app.diagnosis.engine` arbeiten ausschließlich auf diesem
Schnappschuss; damit liefert eine erneute Prüfung bei gleichen Daten dieselben Befunde.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ledger.engine import LedgerResult
from app.ledger.models import Portfolio
from app.prices.models import PriceInfo
from app.util.timeutil import parse_iso, today_local

log = logging.getLogger(__name__)

OPEN_ROW_STATUSES = ("new", "invalid", "unclear", "duplicate")


@dataclass(frozen=True)
class JournalMeta:
    """Herkunft einer App-Buchung (Journal)."""

    tx_id: str
    source: str
    status: str
    external_id: str | None = None
    event_key: str | None = None
    event_line: int | None = None
    tx_hash: str | None = None
    datasource_id: int | None = None
    batch_id: int | None = None
    aliases: tuple[str, ...] = ()


@dataclass
class SourceState:
    """Datenquelle (Börse/Wallet) mit Zustand des letzten Abrufs."""

    id: int
    name: str
    kind: str  # exchange | wallet
    provider: str
    provider_label: str
    account: str
    state: str  # Anzeige (z. B. „vollständig synchronisiert“)
    complete: bool  # synchronisiert, vollständig, ohne erkannte Lücke
    gaps: list[str] = field(default_factory=list)
    limits: list[str] = field(default_factory=list)
    backfill: bool = False
    last_success_at: datetime | None = None
    addresses: list[str] = field(default_factory=list)
    balance_check: dict[str, Any] = field(default_factory=dict)  # Bestandsprüfung des Anbieters (z. B. Bitpanda)


@dataclass
class Observed:
    """Bestand laut Anbieter (``ds_balance``) – Zuordnung wie im Prüf-Stapel (gespeicherte Zuordnungen)."""

    source_id: int
    key: str
    name: str | None
    qty: Decimal
    observed_at: datetime | None
    asset_id: str | None
    how: str  # saved | symbol | id | koinly | alias | fiat | ignored | ambiguous | unknown


@dataclass
class OpenRow:
    """Offener Vorgang eines Prüf-Stapels (noch nicht gebucht)."""

    batch_id: int
    batch_kind: str  # csv | sync
    source: str
    status: str
    account: str | None
    assets: tuple[str, ...]
    ts: datetime | None


@dataclass
class Snapshot:
    now: datetime
    today: date
    pf: Portfolio | None
    ledger: LedgerResult | None
    prices: dict[str, PriceInfo] = field(default_factory=dict)
    journal: dict[str, JournalMeta] = field(default_factory=dict)
    sources: list[SourceState] = field(default_factory=list)
    observed: list[Observed] = field(default_factory=list)
    open_rows: list[OpenRow] = field(default_factory=list)
    asset_sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    token_keys: dict[str, list[str]] = field(default_factory=dict)  # Asset → SYMBOL@CHAIN:Contract
    import_ids: frozenset[str] = frozenset()
    max_age: dict[str, int | None] = field(default_factory=dict)  # crypto | security → Tage (None = unbegrenzt)
    journal_dups: dict[str, list[str]] = field(default_factory=dict)  # manuelle Buchung → ähnliche Import-Buchungen
    settings: Any = None


def _dec(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def collect(ctx: Any, now: datetime | None = None) -> Snapshot:
    """Schnappschuss aus dem laufenden Kontext (nur lesend)."""
    now = now or datetime.now(UTC)
    pf = ctx.portfolio()
    led = ctx.ledger()
    snap = Snapshot(now=now, today=today_local(), pf=pf, ledger=led, settings=ctx.settings)
    if pf is not None and led is not None:
        snap.prices = ctx.price_infos(pf, led)
    base = ctx.base_portfolio()
    snap.import_ids = frozenset(t.tx_id for t in base.txs) if base is not None else frozenset()
    from app.prices.fallback import DEFAULT_MAX_AGE, SETTING_KEYS

    for cls in ("crypto", "security"):
        try:
            v = int(ctx.settings.get(SETTING_KEYS[cls], DEFAULT_MAX_AGE[cls]))
        except (TypeError, ValueError):
            v = DEFAULT_MAX_AGE[cls]
        snap.max_age[cls] = v if v > 0 else None
    db = ctx.db
    snap.journal = _journal(db)
    snap.asset_sources = {r["asset_id"]: dict(r) for r in db.q("SELECT * FROM asset_source ORDER BY asset_id")}
    try:
        from app.journal.service import journal_service

        snap.journal_dups = journal_service(ctx).duplicates(ctx.recorded_portfolio())
    except Exception as e:  # Journal-Modul optional
        log.debug("Journal-Dubletten nicht verfügbar: %s", e)
    resolver = None
    try:
        from app.csvimport.service import SymbolResolver, csv_service

        csv = csv_service(ctx)
        saved = csv.saved_symbols()
        resolver = SymbolResolver(csv.known_assets(), saved)
        for sym, aid in sorted(saved.items()):
            if aid and "@" in sym and ":" in sym.split("@", 1)[1]:
                snap.token_keys.setdefault(aid, []).append(sym)
    except Exception as e:  # CSV-Modul optional
        log.debug("Symbol-Zuordnungen nicht verfügbar: %s", e)
    snap.sources, snap.observed = _sources(ctx, resolver)
    for o in snap.observed:
        if o.asset_id and "@" in o.key and ":" in o.key.split("@", 1)[1]:
            lst = snap.token_keys.setdefault(o.asset_id, [])
            if o.key.upper() not in (k.upper() for k in lst):
                lst.append(o.key)
    snap.open_rows = _open_rows(db, resolver)
    return snap


def _journal(db: Any) -> dict[str, JournalMeta]:
    aliases: dict[str, list[str]] = {}
    for r in db.q("SELECT key, tx_id FROM journal_event_alias ORDER BY tx_id, key"):
        aliases.setdefault(r["tx_id"], []).append(r["key"])
    out: dict[str, JournalMeta] = {}
    for r in db.q("SELECT tx_id, source, status, external_id, event_key, event_line, tx_hash, datasource_id, batch_id "
                  "FROM journal_tx ORDER BY id"):
        out[r["tx_id"]] = JournalMeta(r["tx_id"], r["source"] or "", r["status"], r["external_id"], r["event_key"],
                                      r["event_line"], r["tx_hash"], r["datasource_id"], r["batch_id"],
                                      tuple(aliases.get(r["tx_id"], ())))
    return out


def _sources(ctx: Any, resolver: Any) -> tuple[list[SourceState], list[Observed]]:
    try:
        from app.datasources.service import datasource_service
    except ImportError:  # pragma: no cover - Modul optional
        return [], []
    svc = datasource_service(ctx)
    states: list[SourceState] = []
    observed: list[Observed] = []
    for ds in svc.list():
        cov = ds.coverage
        text, _badge = ds.sync_state
        complete = ds.row["status"] == "synced" and bool(cov.get("complete")) and not cov.get("gaps") \
            and not cov.get("resume")
        states.append(SourceState(
            id=int(ds.id), name=ds.name, kind=ds.kind, provider=ds.provider, provider_label=ds.provider_label,
            account=ds.account, state=text, complete=complete, gaps=list(cov.get("gaps") or []),
            limits=list(ds.limits), backfill=ds.backfill_pending, last_success_at=parse_iso(ds.last_success_at),
            addresses=list(ds.addresses) if ds.is_wallet else [],
            balance_check=dict(cov.get("balances") or {}) if isinstance(cov.get("balances"), dict) else {}))
        for r in svc.balances(int(ds.id)):
            q = _dec(r["qty"])
            if q is None:
                continue
            aid, how = resolver.resolve(r["asset_key"]) if resolver is not None else (None, "unknown")
            observed.append(Observed(int(ds.id), r["asset_key"], r["name"], q, parse_iso(r["observed_at"]), aid, how))
    return states, observed


def _open_rows(db: Any, resolver: Any) -> list[OpenRow]:
    ph = ",".join("?" * len(OPEN_ROW_STATUSES))
    rows = db.q(f"SELECT r.batch_id, r.status, r.decision, r.rec_json, r.row_json, b.kind, b.source, b.profile, "
                f"b.account FROM csv_row r JOIN csv_batch b ON b.id = r.batch_id WHERE b.status IN ('preview', "
                f"'partial') AND r.status IN ({ph}) ORDER BY r.batch_id, r.idx", OPEN_ROW_STATUSES)
    out: list[OpenRow] = []
    for r in rows:
        if r["decision"] == "skip":
            continue
        account: str | None = r["account"]
        assets: list[str] = []
        ts = None
        try:
            row = json.loads(r["row_json"]) if r["row_json"] else None
        except ValueError:
            row = None
        try:
            rec = json.loads(r["rec_json"]) if r["rec_json"] else {}
        except ValueError:
            rec = {}
        if row:
            account = row.get("to_account") or row.get("from_account") or account
            assets = [a for a in (row.get("to_asset"), row.get("from_asset"), row.get("fee_asset")) if a]
        else:
            account = rec.get("account") or account
            for k in ("in_sym", "out_sym", "fee_sym"):
                sym = rec.get(k)
                if sym:
                    aid = resolver.resolve(str(sym))[0] if resolver is not None else None
                    assets.append(aid or str(sym))
        ts = parse_iso(rec.get("ts")) if isinstance(rec.get("ts"), str) else None
        source = r["source"] or (f"csv:{r['profile']}" if r["profile"] else "csv")
        out.append(OpenRow(int(r["batch_id"]), r["kind"] or "csv", source, r["status"], account,
                           tuple(dict.fromkeys(assets)), ts))
    return out
