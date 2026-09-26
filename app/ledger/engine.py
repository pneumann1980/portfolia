"""Ledger-Engine: Bestände, Lots (FIFO/LIFO/HIFO), Veräußerungen, Erträge, Zahlungsströme.

Konventionen (siehe README „Datenvertrag“):

* ``from_qty`` ist die Menge, die das Abgangskonto *ohne* Gebühr verlässt; ``fee_qty`` wird dem
  Abgangskonto (bzw. ohne Abgangsbein dem Zugangskonto) zusätzlich belastet.
* ``value_eur`` ist der Brutto-Gegenwert ohne Gebühr. Einstand Kauf = value_eur + fee_eur,
  Erlös Verkauf/Tausch = value_eur − fee_eur, Einstand Tausch-Zugang = value_eur.
* Gebühren in Nicht-Fiat-Assets sind eigene Abgänge (Lot-Verbrauch, Erlös = fee_eur).
* Scope ``global``: Lot-Reihenfolge je Asset über alle Konten; die Kontenzuordnung der Lots wird
  per Umbuchung konsistent gehalten (für die Anzeige je Konto und für kontobezogene Vorgänge wie
  Transfers/Kapitalmaßnahmen). Scope ``account``: Lots werden je Konto verbraucht.

Die Engine ist rein (keine DB, keine Kurse) und vollständig deterministisch.
"""

from __future__ import annotations

import bisect
import decimal
import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from app.importer import contract as C
from app.ledger.models import Portfolio, Tx

ZERO = Decimal(0)
DUST = Decimal("1e-12")
_CTX = decimal.Context(prec=60)


@dataclass(frozen=True)
class EngineOptions:
    scope: str = "global"  # global | account
    method: str = "fifo"  # fifo | lifo | hifo
    income_tags: frozenset[str] = frozenset(C.INCOME_TAGS)
    loss_tags: frozenset[str] = frozenset(C.LOSS_TAGS)
    gift_out_tags: frozenset[str] = frozenset(C.GIFT_OUT_TAGS)
    tax_tags: frozenset[str] = frozenset({"withholding_tax", "tax"})
    unmatched_transfers_as_flows: bool = True
    cash_overrides: tuple[tuple[str, bool], ...] = ()  # (account, cash_tracked)
    until: date | None = None  # nur Transaktionen bis einschließlich dieses Datums
    # Stichtage (Tagesende), zu denen der Lot-Bestand festgehalten wird (z. B. 31.12. für die Vorabpauschale)
    snapshot_dates: tuple[date, ...] = ()


@dataclass(slots=True)
class Lot:
    id: int
    root_id: int
    asset: str
    account: str
    qty: Decimal
    cost: Decimal
    acq_ts: datetime
    acq_date: date
    acq_tx: str
    origin: str  # buy | trade | income | deposit | corporate_action | phantom
    income_tag: str | None = None

    @property
    def unit_cost(self) -> Decimal:
        return self.cost / self.qty if self.qty else ZERO

    def sort_key(self) -> tuple:
        return (self.acq_ts, self.root_id, self.id)


@dataclass(slots=True)
class DisposalPart:
    lot_id: int
    lot_root: int
    account: str
    qty: Decimal
    cost: Decimal
    proceeds: Decimal
    acq_ts: datetime | None
    acq_date: date | None
    origin: str
    missing_basis: bool = False

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost


@dataclass(slots=True)
class Disposal:
    tx_id: str
    ts: datetime
    date: date
    asset: str
    account: str
    qty: Decimal
    proceeds: Decimal
    kind: str  # sell | trade | fee | transfer_fee | spend | lost | gift | withdrawal
    parts: list[DisposalPart]
    tag: str | None = None
    fee_eur: Decimal = ZERO  # im Erlös bereits abgezogene Gebühr (Info für Steuerberichte)

    @property
    def cost(self) -> Decimal:
        return sum((p.cost for p in self.parts), ZERO)

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost

    @property
    def missing_basis(self) -> bool:
        return any(p.missing_basis for p in self.parts)


@dataclass(slots=True)
class IncomeEvent:
    tx_id: str
    ts: datetime
    date: date
    asset: str
    account: str
    qty: Decimal
    value_eur: Decimal
    tag: str
    related_asset: str | None
    fiat: bool


@dataclass(slots=True)
class FeeEvent:
    tx_id: str
    date: date
    asset: str | None
    qty: Decimal
    eur: Decimal
    kind: str
    account: str | None


@dataclass(slots=True)
class TaxEvent:
    """Gezahlte/einbehaltene Steuern (z. B. Quellensteuer, tag=withholding_tax)."""

    tx_id: str
    date: date
    eur: Decimal
    tag: str
    account: str | None
    related_asset: str | None


@dataclass(slots=True)
class Flow:
    """Externer Zahlungsstrom (Portfolio-Grenze). amount > 0 = Zufluss, < 0 = Abfluss.

    ``amount`` ``None``: Betrag unbekannt (z. B. USD-Einzahlung ohne value_eur) – wird von der
    Historienberechnung mit ``qty`` × Tageskurs/Devisenkurs bewertet.
    """

    tx_id: str
    ts: datetime
    date: date
    amount: Decimal | None
    kind: str
    account: str | None
    asset: str | None
    qty: Decimal = ZERO


@dataclass(slots=True)
class AssetFlow:
    """Zahlungsstrom auf Positionsebene (für Rendite je Asset/Kategorie/Segment).

    ``amount`` in EUR; ``None`` bedeutet: zum Marktwert (qty × Tageskurs) bewerten.
    """

    tx_id: str
    date: date
    asset: str
    amount: Decimal | None
    qty: Decimal
    kind: str


@dataclass(slots=True)
class QtyEvent:
    date: date
    asset: str
    account: str
    delta: Decimal


@dataclass(slots=True)
class LedgerIssue:
    severity: str
    code: str
    message: str
    tx_id: str | None = None
    asset: str | None = None
    account: str | None = None


@dataclass
class LedgerResult:
    options: EngineOptions
    balances: dict[tuple[str, str], Decimal]
    lots: list[Lot]
    disposals: list[Disposal]
    income: list[IncomeEvent]
    fees: list[FeeEvent]
    taxes: list[TaxEvent]
    flows: list[Flow]
    asset_flows: list[AssetFlow]
    qty_events: list[QtyEvent]
    issues: list[LedgerIssue]
    cash_tracked: dict[str, bool]
    first_date: date | None
    last_date: date | None
    tx_count: int = 0
    tx_by_asset: dict[str, list[str]] = field(default_factory=dict)
    lot_snapshots: dict[date, list[Lot]] = field(default_factory=dict)

    # -- Aggregationen -------------------------------------------------------------------------
    def holdings_by_asset(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for (_acc, asset), q in self.balances.items():
            out[asset] += q
        return {a: q for a, q in out.items() if abs(q) > DUST}

    def holdings_by_account(self, account: str) -> dict[str, Decimal]:
        return {a: q for (acc, a), q in self.balances.items() if acc == account and abs(q) > DUST}

    def lots_for(self, asset: str, account: str | None = None) -> list[Lot]:
        return [lot for lot in self.lots if lot.asset == asset and (account is None or lot.account == account)]

    def cost_basis(self, asset: str, account: str | None = None) -> Decimal:
        return sum((lot.cost for lot in self.lots_for(asset, account)), ZERO)

    def realized_by_asset(self, kinds: frozenset[str] | None = None) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for d in self.disposals:
            if kinds is None or d.kind in kinds:
                out[d.asset] += d.gain
        return dict(out)

    def income_by_asset(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for e in self.income:
            key = e.related_asset if (e.fiat and e.related_asset) else e.asset
            out[key] += e.value_eur
        return dict(out)


# Für die Kennzahl „realisierter G/V“ (Abgänge an Dritte/ohne Gegenwert zählen nicht als Realisierung)
REALIZED_KINDS = frozenset({"sell", "trade", "fee", "transfer_fee", "spend", "lost"})


def detect_cash_accounts(pf: Portfolio, opts: EngineOptions) -> dict[str, bool]:
    """Konten mit Cash-Führung: explizite Fiat-Ein-/Auszahlungen, Fiat-Transfers oder Devisentausch.

    Konten ohne Cash-Führung (typisch: Wertpapierdepot, dessen Verrechnungskonto nicht im Ledger
    steht) behandeln Käufe als Einzahlung und Verkaufserlöse als Auszahlung.
    """
    tracked: set[str] = set()
    for t in pf.txs:
        fa = pf.assets.get(t.from_asset) if t.from_asset else None
        ta = pf.assets.get(t.to_asset) if t.to_asset else None
        f_fiat = bool(fa and fa.is_fiat)
        t_fiat = bool(ta and ta.is_fiat)
        tag = (t.tag or "").lower()
        if t.type in ("deposit", "withdrawal", "transfer") and tag not in opts.income_tags \
                and tag not in opts.tax_tags and tag not in opts.loss_tags:
            if f_fiat and t.from_account:
                tracked.add(t.from_account)
            if t_fiat and t.to_account:
                tracked.add(t.to_account)
        if f_fiat and t_fiat:
            for acc in (t.from_account, t.to_account):
                if acc:
                    tracked.add(acc)
    result = {acc: acc in tracked for acc in pf.all_accounts()}
    for acc, val in opts.cash_overrides:
        result[acc] = val
    return result


class _Engine:
    def __init__(self, pf: Portfolio, opts: EngineOptions) -> None:
        self.pf = pf
        self.opts = opts
        self.cash = detect_cash_accounts(pf, opts)
        self.lots: dict[str, list[Lot]] = defaultdict(list)
        self.ids = itertools.count(1)
        self.bal: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self.disposals: list[Disposal] = []
        self.income: list[IncomeEvent] = []
        self.fees: list[FeeEvent] = []
        self.taxes: list[TaxEvent] = []
        self.flows: list[Flow] = []
        self.aflows: list[AssetFlow] = []
        self.qty_events: list[QtyEvent] = []
        self.issues: list[LedgerIssue] = []
        self._neg_reported: set[tuple[str, str]] = set()
        self.tx_by_asset: dict[str, list[str]] = defaultdict(list)
        self._unmatched = 0

    # -- Helfer ------------------------------------------------------------------------------
    def is_fiat(self, asset: str | None) -> bool:
        return bool(asset) and self.pf.asset(asset).is_fiat  # type: ignore[arg-type]

    def issue(self, severity: str, code: str, message: str, tx: Tx | None = None, asset: str | None = None,
              account: str | None = None) -> None:
        self.issues.append(LedgerIssue(severity, code, message, tx.tx_id if tx else None, asset, account))

    def _book(self, tx: Tx, account: str, asset: str, delta: Decimal) -> None:
        """Bestand buchen (Fiat auf Konten ohne Cash-Führung wird nicht geführt)."""
        if self.is_fiat(asset) and not self.cash.get(account, False):
            return
        key = (account, asset)
        new = self.bal[key] + delta
        if abs(new) <= DUST:
            new = ZERO
        self.bal[key] = new
        self.qty_events.append(QtyEvent(tx.date, asset, account, delta))
        if new < -DUST and key not in self._neg_reported:
            self._neg_reported.add(key)
            self.issue("warning", "negative_balance",
                       f"Negativer Bestand {asset} auf {account} nach {tx.tx_id}: {new.normalize():f}", tx, asset,
                       account)

    def _order(self, lots: list[Lot]) -> list[Lot]:
        if self.opts.method == "lifo":
            return list(reversed(lots))
        if self.opts.method == "hifo":
            return sorted(lots, key=lambda lot: (-lot.unit_cost, lot.sort_key()))
        return lots

    def _insert(self, lot: Lot) -> None:
        lst = self.lots[lot.asset]
        bisect.insort(lst, lot, key=Lot.sort_key)

    def _new_lot(self, asset: str, account: str, qty: Decimal, cost: Decimal, ts: datetime, d: date, tx_id: str,
                 origin: str, income_tag: str | None = None, root_id: int | None = None) -> Lot:
        lid = next(self.ids)
        lot = Lot(lid, root_id or lid, asset, account, qty, cost, ts, d, tx_id, origin, income_tag)
        self._insert(lot)
        return lot

    def _compact(self, asset: str) -> None:
        self.lots[asset] = [lot for lot in self.lots[asset] if lot.qty > DUST]

    def _take(self, lot: Lot, qty: Decimal) -> Decimal:
        """Menge aus Lot entnehmen, anteiligen Einstand zurückgeben."""
        with decimal.localcontext(_CTX):
            if qty >= lot.qty:
                cost = lot.cost
                lot.qty = ZERO
                lot.cost = ZERO
            else:
                cost = lot.cost * qty / lot.qty
                lot.qty -= qty
                lot.cost -= cost
        return cost

    # -- Lot-Operationen ----------------------------------------------------------------------
    def acquire(self, tx: Tx, asset: str, account: str, qty: Decimal, cost: Decimal, origin: str,
                income_tag: str | None = None) -> None:
        if self.is_fiat(asset) or qty <= 0:
            return
        self._new_lot(asset, account, qty, max(cost, ZERO), tx.ts, tx.date, tx.tx_id, origin, income_tag)

    def dispose(self, tx: Tx, asset: str, account: str, qty: Decimal, proceeds: Decimal, kind: str,
                fee_eur: Decimal = ZERO) -> Disposal | None:
        if self.is_fiat(asset) or qty <= 0:
            return None
        pool = self.lots[asset]
        candidates = pool if self.opts.scope == "global" else [lot for lot in pool if lot.account == account]
        parts: list[DisposalPart] = []
        remaining = qty
        for lot in self._order(candidates):
            if remaining <= DUST:
                break
            if lot.qty <= DUST:
                continue
            take = min(lot.qty, remaining)
            cost = self._take(lot, take)
            parts.append(DisposalPart(lot.id, lot.root_id, lot.account, take, cost, ZERO, lot.acq_ts, lot.acq_date,
                                      lot.origin))
            remaining -= take
        if remaining > DUST:
            parts.append(DisposalPart(0, 0, account, remaining, ZERO, ZERO, None, None, "phantom", True))
            self.issue("warning", "missing_lots",
                       f"Fehlbestand bei Abgang {asset} ({kind}) auf {account}: {remaining.normalize():f} ohne "
                       "Anschaffung – Einstand 0 €, Haltedauer unbekannt", tx, asset, account)
        with decimal.localcontext(_CTX):
            for p in parts:
                p.proceeds = proceeds * p.qty / qty
        self._compact(asset)
        if self.opts.scope == "global":
            self._relabel(tx, asset, account, parts)
        d = Disposal(tx.tx_id, tx.ts, tx.date, asset, account, qty, proceeds, kind, parts, tx.tag, fee_eur)
        self.disposals.append(d)
        return d

    def _relabel(self, tx: Tx, asset: str, account: str, parts: list[DisposalPart]) -> None:
        """Globaler FIFO: verbrauchte Lots anderer Konten durch Lots des abgebenden Kontos ersetzen."""
        need: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for p in parts:
            if not p.missing_basis and p.account != account:
                need[p.account] += p.qty
        for other, q in need.items():
            moved = self._move(tx, asset, account, other, q, create_phantom=False)
            if q - moved > DUST:
                self.issue("info", "attribution_gap",
                           f"Kontenzuordnung {asset}: {(q - moved).normalize():f} konnten nicht von {account} nach "
                           f"{other} umgebucht werden (Datenlücke)", tx, asset, account)

    def _move(self, tx: Tx, asset: str, src: str, dst: str, qty: Decimal, *, create_phantom: bool) -> Decimal:
        """Lots (älteste zuerst) von src nach dst umbuchen. Rückgabe: bewegte Menge."""
        if qty <= 0 or self.is_fiat(asset):
            return ZERO
        remaining = qty
        new_lots: list[Lot] = []
        for lot in [lot for lot in self.lots[asset] if lot.account == src]:
            if remaining <= DUST:
                break
            take = min(lot.qty, remaining)
            if take >= lot.qty:
                lot.account = dst
            else:
                cost = self._take(lot, take)
                new_lots.append(Lot(next(self.ids), lot.root_id, asset, dst, take, cost, lot.acq_ts, lot.acq_date,
                                    lot.acq_tx, lot.origin, lot.income_tag))
            remaining -= take
        for lot in new_lots:
            self._insert(lot)
        moved = qty - max(remaining, ZERO)
        if remaining > DUST and create_phantom:
            self._new_lot(asset, dst, remaining, ZERO, tx.ts, tx.date, tx.tx_id, "phantom")
            self.issue("warning", "missing_lots",
                       f"Transfer {asset} von {src}: {remaining.normalize():f} ohne vorhandene Lots – "
                       "als Zugang ohne Einstand behandelt", tx, asset, src)
        return moved

    def convert(self, tx: Tx, src_asset: str, src_acc: str, src_qty: Decimal, dst_asset: str, dst_acc: str,
                dst_qty: Decimal) -> None:
        """Kapitalmaßnahme/Migration: Lots mit Einstand und Anschaffungsdatum übertragen."""
        if src_qty <= 0:
            return
        with decimal.localcontext(_CTX):
            ratio = dst_qty / src_qty
        remaining = src_qty
        consumed: list[tuple[Lot, Decimal, Decimal]] = []
        for lot in [lot for lot in self.lots[src_asset] if lot.account == src_acc]:
            if remaining <= DUST:
                break
            take = min(lot.qty, remaining)
            cost = self._take(lot, take)
            consumed.append((lot, take, cost))
            remaining -= take
        self._compact(src_asset)
        for lot, take, cost in consumed:
            with decimal.localcontext(_CTX):
                q = take * ratio
            nl = Lot(next(self.ids), lot.root_id, dst_asset, dst_acc, q, cost, lot.acq_ts, lot.acq_date, lot.acq_tx,
                     lot.origin if lot.origin != "phantom" else "phantom", lot.income_tag)
            self._insert(nl)
        if remaining > DUST:
            with decimal.localcontext(_CTX):
                q = remaining * ratio
            self._new_lot(dst_asset, dst_acc, q, ZERO, tx.ts, tx.date, tx.tx_id, "phantom")
            self.issue("warning", "missing_lots",
                       f"Kapitalmaßnahme {src_asset}→{dst_asset}: {remaining.normalize():f} ohne Lots auf {src_acc}",
                       tx, src_asset, src_acc)

    # -- Flüsse ---------------------------------------------------------------------------------
    def flow(self, tx: Tx, amount: Decimal | None, kind: str, account: str | None, asset: str | None,
             qty: Decimal = ZERO) -> None:
        if amount is not None and amount == 0:
            return
        if amount is None and qty == 0:
            return
        self.flows.append(Flow(tx.tx_id, tx.ts, tx.date, amount, kind, account, asset, qty))

    def fiat_amount(self, asset: str | None, qty: Decimal, v: Decimal) -> Decimal | None:
        """EUR-Betrag eines Fiat-Beins: value_eur, sonst bei EUR die Menge, sonst unbekannt (None)."""
        if v:
            return v
        if asset == "EUR":
            return qty
        return None

    def aflow(self, tx: Tx, asset: str | None, amount: Decimal | None, qty: Decimal, kind: str) -> None:
        if not asset:
            return
        if amount is not None and amount == 0 and qty == 0:
            return
        self.aflows.append(AssetFlow(tx.tx_id, tx.date, asset, amount, qty, kind))

    # -- Verarbeitung ---------------------------------------------------------------------------
    def process(self, tx: Tx) -> None:
        tag = (tx.tag or "").lower()
        fa, fasset, fq = tx.from_account, tx.from_asset, tx.from_qty or ZERO
        ta, tasset, tq = tx.to_account, tx.to_asset, tx.to_qty or ZERO
        v = tx.value_eur if tx.value_eur is not None else ZERO
        fee_eur = tx.fee_eur or ZERO
        fee_asset = tx.fee_asset if (tx.fee_asset and tx.fee_qty) else None
        fee_qty = tx.fee_qty or ZERO
        fee_acc = fa or ta
        f_fiat = self.is_fiat(fasset)
        t_fiat = self.is_fiat(tasset)
        fee_fiat = fee_asset is None or self.is_fiat(fee_asset)  # fee_eur ohne Asset = bar bezahlt
        fee_fiat_eur = fee_eur if fee_fiat else ZERO
        for a in (fasset, tasset, fee_asset, tx.related_asset):
            if a:
                self.tx_by_asset[a].append(tx.tx_id)

        typ = tx.type
        if typ in ("buy", "sell", "trade"):
            if fasset and tasset:
                if f_fiat and t_fiat:
                    typ = "fx"
                elif f_fiat:
                    typ = "buy"
                elif t_fiat:
                    typ = "sell"
                else:
                    typ = "trade"
            elif tasset:
                typ = "buy" if not t_fiat else "deposit"
            elif fasset:
                typ = "sell" if not f_fiat else "withdrawal"
        elif typ == "transfer" and fasset and tasset and fasset != tasset:
            typ = "conversion"
        elif typ == "corporate_action":
            typ = "conversion" if fasset else "ca_in"

        # -- Bestände ------------------------------------------------------------------------------
        if fasset:
            self._book(tx, fa, fasset, -fq)  # type: ignore[arg-type]
        if tasset:
            self._book(tx, ta, tasset, tq)  # type: ignore[arg-type]
        if fee_asset and fee_acc:
            self._book(tx, fee_acc, fee_asset, -fee_qty)

        # -- Lots, Erträge, Flüsse ------------------------------------------------------------
        if typ == "buy":
            cost = v + fee_eur
            self.acquire(tx, tasset, ta, tq, cost, "buy")  # type: ignore[arg-type]
            external = (not fasset) or (f_fiat and not self.cash.get(fa or "", False))
            if external:
                self.flow(tx, v + fee_fiat_eur, "buy_external", ta, tasset)
            else:
                self.aflow(tx, fasset, -(v + fee_fiat_eur), -fq, "buy")
            self.aflow(tx, tasset, cost, tq, "buy")
            if fee_asset and not fee_fiat:
                self.aflow(tx, fee_asset, -fee_eur, -fee_qty, "fee_for_buy")
        elif typ == "sell":
            proceeds = v - fee_eur
            self.dispose(tx, fasset, fa, fq, proceeds, "sell", fee_eur)  # type: ignore[arg-type]
            external = (not tasset) or (t_fiat and not self.cash.get(ta or "", False))
            if external:
                self.flow(tx, -(v - fee_fiat_eur), "sell_external", fa, fasset)
            else:
                self.aflow(tx, tasset, v - fee_fiat_eur, tq, "sell")
            self.aflow(tx, fasset, -proceeds, -fq, "sell")
            if fee_asset and not fee_fiat:
                self.aflow(tx, fee_asset, -fee_eur, -fee_qty, "fee_for_sell")
        elif typ == "trade":
            self.dispose(tx, fasset, fa, fq, v - fee_eur, "trade", fee_eur)  # type: ignore[arg-type]
            self.acquire(tx, tasset, ta, tq, v, "trade")  # type: ignore[arg-type]
            self.aflow(tx, fasset, -(v - fee_eur), -fq, "trade")
            self.aflow(tx, tasset, v, tq, "trade")
            if fee_eur:
                if not fee_fiat or (self.cash.get(fee_acc or "", False) and fee_asset):
                    self.aflow(tx, fee_asset, -fee_eur, -fee_qty, "fee_for_trade")
                else:
                    # Fiat-Gebühr aus nicht geführtem Cash-Konto: externer Zufluss, der die Gebühr bezahlt
                    self.flow(tx, fee_eur, "fee_external", fee_acc, fee_asset)
        elif typ == "fx":
            self.aflow(tx, fasset, -v if v else None, -fq, "fx")
            self.aflow(tx, tasset, v if v else None, tq, "fx")
        elif typ == "deposit":
            self._deposit(tx, tag, ta, tasset, tq, v, t_fiat)  # type: ignore[arg-type]
        elif typ == "withdrawal":
            self._withdrawal(tx, tag, fa, fasset, fq, v, f_fiat)  # type: ignore[arg-type]
        elif typ == "transfer":
            if not f_fiat:
                moved_qty = min(tq, fq) if tq else fq
                self._move(tx, fasset, fa, ta, moved_qty, create_phantom=True)  # type: ignore[arg-type]
                if fq > tq and tq > 0:
                    implicit = fq - tq
                    self.dispose(tx, fasset, fa, implicit, ZERO if fee_asset else fee_eur,  # type: ignore[arg-type]
                                 "transfer_fee")
                    self.fees.append(FeeEvent(tx.tx_id, tx.date, fasset, implicit, ZERO if fee_asset else fee_eur,
                                              "transfer_implicit", fa))
                    self.issue("info", "implicit_fee",
                               f"Transfer {fasset}: Differenz {implicit.normalize():f} als Transfergebühr behandelt",
                               tx, fasset, fa)
            else:
                src_cash = self.cash.get(fa or "", False)
                dst_cash = self.cash.get(ta or "", False)
                if src_cash and not dst_cash:
                    amt = self.fiat_amount(fasset, fq, v)
                    self.flow(tx, -amt if amt is not None else None, "cash_out", fa, fasset, -fq)
                    self.aflow(tx, fasset, -amt if amt is not None else None, -fq, "cash_out")
                elif dst_cash and not src_cash:
                    amt = self.fiat_amount(tasset, tq, v)
                    self.flow(tx, amt, "cash_in", ta, tasset, tq)
                    self.aflow(tx, tasset, amt, tq, "cash_in")
        elif typ == "conversion":
            self.convert(tx, fasset, fa, fq, tasset, ta, tq)  # type: ignore[arg-type]
            if fasset != tasset:
                self.aflow(tx, fasset, -v if v else None, -fq, "conversion")
                self.aflow(tx, tasset, v if v else None, tq, "conversion")
        elif typ == "ca_in":
            self.acquire(tx, tasset, ta, tq, v, "corporate_action")  # type: ignore[arg-type]
            self.issue("info", "ca_without_from",
                       f"Kapitalmaßnahme ohne Abgangsbein ({tasset}): Einstand {v} € – bitte prüfen", tx, tasset, ta)

        # -- Gebührenbein --------------------------------------------------------------------------
        if fee_asset and fee_acc and fee_qty > 0:
            kind = "transfer_fee" if typ == "transfer" else "fee"
            if not self.is_fiat(fee_asset):
                self.dispose(tx, fee_asset, fee_acc, fee_qty, fee_eur, kind)
            self.fees.append(FeeEvent(tx.tx_id, tx.date, fee_asset, fee_qty, fee_eur, kind, fee_acc))
        elif fee_eur > 0 and not fee_asset:
            self.fees.append(FeeEvent(tx.tx_id, tx.date, None, ZERO, fee_eur, "fee", fee_acc))

    def _deposit(self, tx: Tx, tag: str, ta: str, asset: str, qty: Decimal, v: Decimal, fiat: bool) -> None:
        o = self.opts
        if tag in o.income_tags:
            if fiat:
                amt = self.fiat_amount(asset, qty, v)
                self.income.append(IncomeEvent(tx.tx_id, tx.ts, tx.date, asset, ta, qty, amt or ZERO, tag,
                                               tx.related_asset, True))
                if not self.cash.get(ta, False):
                    # Ertrag fließt auf ein nicht geführtes Konto: Ausschüttung = Abfluss (wie Portfolio Performance)
                    self.flow(tx, -amt if amt is not None else None, "income_paid_out", ta, asset, -qty)
                    if tx.related_asset:
                        self.aflow(tx, tx.related_asset, -amt if amt is not None else None, ZERO, "distribution")
                elif tx.related_asset:
                    self.aflow(tx, asset, amt, qty, "distribution_cash")
                    self.aflow(tx, tx.related_asset, -amt if amt is not None else None, ZERO, "distribution")
            else:
                self.income.append(IncomeEvent(tx.tx_id, tx.ts, tx.date, asset, ta, qty, v, tag, tx.related_asset,
                                               False))
                self.acquire(tx, asset, ta, qty, v, "income", tag)
            return
        if fiat:
            if self.cash.get(ta, False):
                amt = self.fiat_amount(asset, qty, v)
                self.flow(tx, amt, "deposit", ta, asset, qty)
                self.aflow(tx, asset, amt, qty, "deposit")
            return
        # Nicht-Fiat-Zugang ohne Ertrags-Tag: nicht zugeordneter Transfer / Schenkung / Depotübertrag
        self.acquire(tx, asset, ta, qty, v, "deposit")
        self._unmatched += 1
        if o.unmatched_transfers_as_flows:
            self.flow(tx, v if v else None, "transfer_in_external", ta, asset, qty)
            self.aflow(tx, asset, v if v else None, qty, "transfer_in_external")

    def _withdrawal(self, tx: Tx, tag: str, fa: str, asset: str, qty: Decimal, v: Decimal, fiat: bool) -> None:
        o = self.opts
        if fiat:
            amt = self.fiat_amount(asset, qty, v)
            if tag in o.tax_tags:
                self.taxes.append(TaxEvent(tx.tx_id, tx.date, amt or ZERO, tag, fa, tx.related_asset))
                if not self.cash.get(fa, False):
                    # Konto ohne Cash-Führung: Ertrag/Erlös wurde brutto als Abfluss gebucht, ausgezahlt wurde
                    # netto – die einbehaltene Steuer mindert den Abfluss.
                    self.flow(tx, amt, "tax_withheld", fa, asset, qty)
                return
            if tag in o.loss_tags:
                self.fees.append(FeeEvent(tx.tx_id, tx.date, asset, qty, amt or ZERO, "cost", fa))
                return
            if self.cash.get(fa, False):
                self.flow(tx, -amt if amt is not None else None, "withdrawal", fa, asset, -qty)
                self.aflow(tx, asset, -amt if amt is not None else None, -qty, "withdrawal")
            return
        if tag in ("lost", "stolen", "burn"):
            self.dispose(tx, asset, fa, qty, ZERO, "lost")
            return
        if tag in o.loss_tags:  # cost / fee: Bezahlung mit Krypto
            self.dispose(tx, asset, fa, qty, v, "spend")
            self.fees.append(FeeEvent(tx.tx_id, tx.date, asset, qty, v, "cost", fa))
            return
        kind = "gift" if tag in o.gift_out_tags else "withdrawal"
        self.dispose(tx, asset, fa, qty, v, kind)
        if kind == "withdrawal":
            self._unmatched += 1
        if o.unmatched_transfers_as_flows or kind == "gift":
            self.flow(tx, -v if v else None, "transfer_out_external" if kind == "withdrawal" else "gift_out", fa,
                      asset, -qty)
            self.aflow(tx, asset, -v if v else None, -qty, kind)

    def _snapshot(self) -> list[Lot]:
        out = [Lot(lot.id, lot.root_id, lot.asset, lot.account, lot.qty, lot.cost, lot.acq_ts, lot.acq_date,
                   lot.acq_tx, lot.origin, lot.income_tag)
               for lst in self.lots.values() for lot in lst if lot.qty > DUST]
        out.sort(key=Lot.sort_key)
        return out

    def run(self) -> LedgerResult:
        txs = sorted(self.pf.txs, key=lambda t: (t.ts, t.seq))
        if self.opts.until is not None:
            txs = [t for t in txs if t.date <= self.opts.until]
        pending = sorted(set(self.opts.snapshot_dates))
        snapshots: dict[date, list[Lot]] = {}
        for t in txs:
            while pending and t.date > pending[0]:
                snapshots[pending.pop(0)] = self._snapshot()
            self.process(t)
        for d in pending:
            snapshots[d] = self._snapshot()
        if self._unmatched:
            how = ("als externe Zahlungsströme zum Marktwert" if self.opts.unmatched_transfers_as_flows
                   else "ohne Zahlungsstrom")
            self.issue("info", "unmatched_transfers",
                       f"{self._unmatched} Krypto-/Wertpapier-Zu- oder Abgänge ohne Gegenbuchung ({how})")
        lots = [lot for lst in self.lots.values() for lot in lst if lot.qty > DUST]
        lots.sort(key=Lot.sort_key)
        balances = {k: v for k, v in self.bal.items() if v != 0}
        return LedgerResult(
            options=self.opts,
            balances=balances,
            lots=lots,
            disposals=self.disposals,
            income=self.income,
            fees=self.fees,
            taxes=self.taxes,
            flows=self.flows,
            asset_flows=self.aflows,
            qty_events=self.qty_events,
            issues=self.issues,
            cash_tracked=self.cash,
            first_date=txs[0].date if txs else None,
            last_date=txs[-1].date if txs else None,
            tx_count=len(txs),
            tx_by_asset=dict(self.tx_by_asset),
            lot_snapshots=snapshots,
        )


def run_ledger(pf: Portfolio, opts: EngineOptions | None = None) -> LedgerResult:
    return _Engine(pf, opts or EngineOptions()).run()
