"""Profile für Exportformate von Börsen, Wallets und Steuertools sowie eigene Spaltenzuordnungen.

Jedes Profil erkennt sein Format an Pflichtspalten (Kopfzeile) und übersetzt Zeilen in :class:`Rec`. Die Formate
stammen aus der öffentlichen Dokumentation der Anbieter bzw. aus Beispielexporten; Anbieter ändern Spalten und
Vorgangsbezeichnungen gelegentlich. Unbekannte Vorgänge werden deshalb nie geraten, sondern als Fehlerzeile mit
Zeilennummer gemeldet – sie lassen sich über „Eigenes Format“ oder manuell erfassen.

Grundsätze:

* Interne Umbuchungen innerhalb eines Anbieters (Spot ↔ Earn/Staking/Funding) werden übersprungen – der Bestand
  bleibt auf dem einen Konto des Anbieters.
* Ein-/Auszahlungen von Kryptowerten bleiben zunächst Zu-/Abgänge; der Dienst gleicht sie anschließend mit
  Gegenbuchungen anderer Konten zu Überträgen ab (Anschaffungsdatum bleibt erhalten).
* Gebühren, die in derselben Einheit wie der Abgang anfallen, werden getrennt geführt (Menge ohne Gebühr).
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.csvimport import model as M
from app.csvimport.model import ParseOptions, ParseResult, Rec
from app.csvimport.reader import Table, amount_and_unit, norm, num, parse_ts, zone

ZERO = Decimal(0)


def hid(*parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:20]


class _Ids:
    """Stabile Kennungen für Zeilen ohne ID (gleiche Zeilen im selben Export werden durchnummeriert)."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.seen: dict[str, int] = defaultdict(int)

    def __call__(self, *parts: Any) -> str:
        h = hid(*parts)
        self.seen[h] += 1
        n = self.seen[h]
        return f"{self.prefix}:{h}" + (f"#{n}" if n > 1 else "")


def pos(v: Decimal | None) -> Decimal | None:
    return abs(v) if v is not None else None


def nz(v: Decimal | None) -> bool:
    return v is not None and v != 0


def sym(v: str | None) -> str | None:
    s = (v or "").strip()
    return s.upper() if s else None


class Profile:
    id = ""
    label = ""
    group = "Börse"  # Börse | Wallet | Steuertool | Portfolia | Eigenes Format
    account = ""  # Vorschlag für den Kontonamen
    multi_account = False  # Konten stehen in der Datei
    tz = "UTC"
    decimal = "."
    needs_asset = False
    hint = ""
    signatures: tuple[frozenset[str], ...] = ()

    def score(self, keys: set[str]) -> int:
        best = 0
        for sig in self.signatures:
            if sig <= keys:
                best = max(best, len(sig))
        return best

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:  # pragma: no cover - abstrakt
        raise NotImplementedError

    # Hilfen
    def ts(self, v: str | None, o: ParseOptions) -> datetime:
        return parse_ts(v, o.tz or zone(self.tz), o.dayfirst)[0]

    def n(self, v: str | None, o: ParseOptions) -> Decimal | None:
        return num(v, o.decimal or self.decimal)


def sig(*cols: str) -> frozenset[str]:
    return frozenset(norm(c) for c in cols)


def _guard(res: ParseResult, ln: int, fn: Callable[[], None]) -> None:
    try:
        fn()
    except (ValueError, ArithmeticError, KeyError) as e:
        res.error(ln, str(e) or type(e).__name__)


# ----------------------------------------------------------------------------------------------------
# Portfolia (einheitliches Format)
# ----------------------------------------------------------------------------------------------------

class PortfoliaProfile(Profile):
    id = "portfolia"
    label = "Portfolia / Datenvertrag (transactions.csv)"
    group = "Portfolia"
    account = ""
    multi_account = True
    hint = ("Spalten wie transactions.csv des Import-Formats (tx_id, datetime, type, tag, from_account, from_asset, "
            "from_qty, to_account, … ). Geeignet für selbst gepflegte Listen und Exporte anderer Portfolia-Instanzen.")
    signatures = (sig("datetime", "type", "from_asset", "to_asset"),)

    COLS = ("tx_id", "datetime", "type", "tag", "from_account", "from_asset", "from_qty", "to_account", "to_asset",
            "to_qty", "fee_asset", "fee_qty", "fee_eur", "value_eur", "orig_price", "orig_ccy", "source", "source_ref",
            "flag", "note", "related_asset")

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        ids = _Ids("pf")
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                row = {c: (r.get(c) or "").strip() for c in self.COLS}
                if row["flag"].lower() == "estimated":
                    res.skip("Portfolia: geschätzte Sparplan-Buchung (nicht übernommen)")
                    return
                ts, date_only = parse_ts(row["datetime"], o.tz or zone("UTC"), False)
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["datetime"]):
                    from app.util.timeutil import parse_tx_datetime

                    ts, date_only = parse_tx_datetime(row["datetime"])
                for side in ("from", "to"):
                    if row[f"{side}_asset"] and not row[f"{side}_account"]:
                        row[f"{side}_account"] = o.account
                for k in ("from_qty", "to_qty", "fee_qty", "fee_eur", "value_eur"):
                    if row[k]:
                        v = num(row[k], ".")
                        if v is None:
                            raise ValueError(f"{k}: keine Zahl ({row[k]!r})")
                        row[k] = format(v.normalize(), "f") if v != 0 else "0"
                res.recs.append(Rec(line=ln, ts=ts, kind=M.DIRECT, row=row, date_only=date_only,
                                    ext_id=f"pf:{row['tx_id']}" if row["tx_id"] else ids(*row.values()),
                                    account=row["from_account"] or row["to_account"] or o.account,
                                    note=row["note"] or None, label=row["type"]))

            _guard(res, ln, one)
        return res


# ----------------------------------------------------------------------------------------------------
# Steuertools: Koinly, Blockpit, CoinTracking
# ----------------------------------------------------------------------------------------------------

def _ccy_from_header(key: str | None) -> str | None:
    if not key:
        return None
    m = re.search(r"\(([a-z]{3})\)", key)
    return m.group(1).upper() if m else None


def _strip_id(v: str | None) -> str:
    return (v or "").split(";", 1)[0].strip()


class KoinlyProfile(Profile):
    id = "koinly"
    label = "Koinly (Transaktionsexport / Bulk-Edit)"
    group = "Steuertool"
    account = "Koinly"
    multi_account = True
    hint = ("Koinly → Transactions → Export (CSV) oder „Bulk edit in Excel“. Wallet-Namen werden zu Konten, von "
            "Koinly erkannte Transfers bleiben Transfers. Koinly-IDs (z. B. „BTC;123“) werden über koinly_id "
            "den Assets zugeordnet.")
    signatures = (
        sig("date", "type", "sending wallet", "sent amount", "sent currency", "receiving wallet", "received amount",
            "received currency"),
        sig("date (utc)", "type", "from wallet (read-only)", "from amount", "from currency", "to wallet (read-only)",
            "to amount", "to currency"),
    )

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        bulk = t.has("from amount", "to amount")
        res = ParseResult()
        ids = _Ids("koinly")
        if bulk:
            k = {"date": "date (utc)", "sw": "from wallet (read-only)", "sq": "from amount", "sc": "from currency",
                 "rw": "to wallet (read-only)", "rq": "to amount", "rc": "to currency", "label": "tag",
                 "id": "id (read-only)"}
            val_key = t.find("net value (read-only)")
            val_ccy_key = t.find("value currency (read-only)")
            fee_val_key = t.find("fee value (read-only)")
        else:
            k = {"date": "date", "sw": "sending wallet", "sq": "sent amount", "sc": "sent currency",
                 "rw": "receiving wallet", "rq": "received amount", "rc": "received currency", "label": "label",
                 "id": "id"}
            val_key = t.find_prefix("net value")
            val_ccy_key = None
            fee_val_key = t.find_prefix("fee value")
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                if bulk and (r.get("deleted") or "").strip().lower() in ("true", "1", "yes", "ja"):
                    res.skip("Koinly: in Koinly gelöschte Transaktion")
                    return
                ts = self.ts(r.get(k["date"]), o)
                typ = M.norm_label(r.get("type"))
                label = r.get(k["label"]) or ""
                sq, rq = pos(self.n(r.get(k["sq"]), o)), pos(self.n(r.get(k["rq"]), o))
                sc, rc = (r.get(k["sc"]) or "").strip(), (r.get(k["rc"]) or "").strip()
                sw, rw = _strip_id(r.get(k["sw"])), _strip_id(r.get(k["rw"]))
                fq = pos(self.n(r.get("fee amount"), o))
                fc = (r.get("fee currency") or "").strip() or None
                value = pos(self.n(r.get(val_key), o)) if val_key else None
                vccy = (_strip_id(r.get(val_ccy_key)) if val_ccy_key else _ccy_from_header(val_key)) or None
                if value is None and bulk:
                    value = pos(self.n(r.get("net worth amount"), o))
                    vccy = _strip_id(r.get("net worth currency")) or None
                fee_value = pos(self.n(r.get(fee_val_key), o)) if fee_val_key else None
                rec = Rec(line=ln, ts=ts, kind=M.TRADE, fee_sym=fc if nz(fq) else None, fee_qty=fq if nz(fq) else None,
                          value=value if nz(value) else None, value_ccy=vccy if nz(value) else None,
                          fee_value=fee_value if nz(fee_value) else None,
                          fee_value_ccy=vccy if nz(fee_value) else None,
                          txhash=(r.get("txhash") or "").strip() or None,
                          note=(r.get("description") or "").strip() or None, label=r.get("type") or label)
                rec.ext_id = f"koinly:{r[k['id']]}" if (r.get(k["id"]) or "").strip() else ids(*r.values())
                has_s, has_r = nz(sq) and sc, nz(rq) and rc
                if has_s and has_r:
                    same = _strip_id(sc).upper() == _strip_id(rc).upper()
                    if typ == "transfer" or (same and sw and rw and sw != rw):
                        rec.kind = M.TRANSFER
                        rec.account, rec.to_account = sw or o.account, rw or o.account
                    else:
                        rec.account = sw or rw or o.account
                    rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = sc, sq, rc, rq
                elif has_r:
                    rec.kind, rec.in_sym, rec.in_qty, rec.account = M.DEPOSIT, rc, rq, rw or o.account
                    rec.tag = M.income_tag(label)
                    if label and rec.tag is None and M.norm_label(label) not in ("", "deposit", "transfer"):
                        rec.note = " · ".join(x for x in (rec.note, f"Koinly-Label: {label}") if x)
                elif has_s:
                    rec.kind, rec.out_sym, rec.out_qty, rec.account = M.WITHDRAWAL, sc, sq, sw or o.account
                    rec.tag = M.out_tag(label)
                    if label and rec.tag is None and M.norm_label(label) not in ("", "withdrawal", "transfer"):
                        rec.note = " · ".join(x for x in (rec.note, f"Koinly-Label: {label}") if x)
                elif rec.fee_qty:
                    rec.kind, rec.account = M.FEE, sw or rw or o.account
                else:
                    res.skip("Koinly: Zeile ohne Mengen")
                    return
                res.recs.append(rec)

            _guard(res, ln, one)
        return res


class KoinlyUniversalProfile(Profile):
    id = "koinly_universal"
    label = "Koinly-Vorlage (Universal-Format)"
    group = "Steuertool"
    account = ""
    hint = ("Einfaches Format mit Date, Sent Amount/Currency, Received Amount/Currency, Fee Amount/Currency, "
            "Net Worth Amount/Currency, Label, Description, TxHash – eine Datei je Konto. Wird von vielen Börsen "
            "und Tools als „Koinly-Format“ angeboten.")
    signatures = (sig("date", "sent amount", "sent currency", "received amount", "received currency"),)

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        ids = _Ids("koinlyu")
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                ts = self.ts(r.get("date"), o)
                sq, rq = pos(self.n(r.get("sent amount"), o)), pos(self.n(r.get("received amount"), o))
                sc, rc = sym(r.get("sent currency")), sym(r.get("received currency"))
                fq, fc = pos(self.n(r.get("fee amount"), o)), sym(r.get("fee currency"))
                value, vccy = pos(self.n(r.get("net worth amount"), o)), sym(r.get("net worth currency"))
                label = r.get("label") or ""
                rec = Rec(line=ln, ts=ts, kind=M.TRADE, account=o.account, fee_sym=fc if nz(fq) else None,
                          fee_qty=fq if nz(fq) else None, value=value if nz(value) else None,
                          value_ccy=vccy if nz(value) else None, txhash=(r.get("txhash") or "").strip() or None,
                          note=(r.get("description") or "").strip() or None, label=label or None,
                          ext_id=ids(*r.values()))
                if nz(sq) and sc and nz(rq) and rc:
                    rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = sc, sq, rc, rq
                elif nz(rq) and rc:
                    rec.kind, rec.in_sym, rec.in_qty, rec.tag = M.DEPOSIT, rc, rq, M.income_tag(label)
                elif nz(sq) and sc:
                    rec.kind, rec.out_sym, rec.out_qty, rec.tag = M.WITHDRAWAL, sc, sq, M.out_tag(label)
                elif rec.fee_qty:
                    rec.kind = M.FEE
                else:
                    res.skip("Zeile ohne Mengen")
                    return
                res.recs.append(rec)

            _guard(res, ln, one)
        return res


BLOCKPIT_TYPES: dict[str, tuple[str, str | None]] = {
    "trade": (M.TRADE, None), "deposit": (M.DEPOSIT, None), "withdrawal": (M.WITHDRAWAL, None),
    "staking": (M.DEPOSIT, "staking"), "lending": (M.DEPOSIT, "lending"), "airdrop": (M.DEPOSIT, "airdrop"),
    "bounties": (M.DEPOSIT, "bonus"), "bounty": (M.DEPOSIT, "bonus"), "income": (M.DEPOSIT, "other_income"),
    "mining": (M.DEPOSIT, "mining"), "masternode": (M.DEPOSIT, "mining"), "cashback": (M.DEPOSIT, "cashback"),
    "gift": (M.WITHDRAWAL, "gift"), "payment": (M.WITHDRAWAL, "cost"), "spend": (M.WITHDRAWAL, "cost"),
    "lost": (M.WITHDRAWAL, "lost"), "stolen": (M.WITHDRAWAL, "stolen"), "fee": (M.FEE, None),
    "non_taxable_in": (M.DEPOSIT, None), "non_taxable_out": (M.WITHDRAWAL, None), "reward": (M.DEPOSIT, "reward"),
    "fork": (M.DEPOSIT, "fork"), "interest": (M.DEPOSIT, "interest"), "bonus": (M.DEPOSIT, "bonus"),
}


class BlockpitProfile(Profile):
    id = "blockpit"
    label = "Blockpit (Transaktionsexport)"
    group = "Steuertool"
    account = "Blockpit"
    multi_account = True
    hint = "Blockpit → Transaktionen → Export (CSV). Integrationsnamen werden zu Konten."
    signatures = (
        sig("date (utc)", "integration name", "label", "outgoing asset", "outgoing amount", "incoming asset",
            "incoming amount"),
        sig("blockpit id", "timestamp", "integration", "transaction type", "outgoing asset", "outgoing amount",
            "incoming asset", "incoming amount"),
    )

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        v2 = t.has("blockpit id")
        res = ParseResult()
        ids = _Ids("blockpit")
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                ts = self.ts(r.get("timestamp") if v2 else r.get("date (utc)"), o)
                label = (r.get("transaction type") if v2 else r.get("label")) or ""
                key = M.norm_label(label)
                acc = ((r.get("integration") if v2 else r.get("integration name")) or "").strip() or o.account
                oq, oc = pos(self.n(r.get("outgoing amount"), o)), sym(r.get("outgoing asset"))
                iq, ic = pos(self.n(r.get("incoming amount"), o)), sym(r.get("incoming asset"))
                fk = t.find("fee amount", "fee amount (optional)")
                fck = t.find("fee asset", "fee asset (optional)")
                fq, fc = pos(self.n(r.get(fk or ""), o)), sym(r.get(fck or ""))
                if key.startswith(("margin", "derivative", "futures")):
                    res.skip(f"Blockpit: {label} (Margin/Derivate nicht unterstützt)")
                    return
                kind, tag = BLOCKPIT_TYPES.get(key, (None, None))
                if kind is None:
                    kind = M.TRADE if (nz(oq) and nz(iq)) else (M.DEPOSIT if nz(iq) else M.WITHDRAWAL)
                    tag = M.income_tag(label) if kind == M.DEPOSIT else M.out_tag(label)
                if key == "gift" and nz(iq) and not nz(oq):
                    kind, tag = M.DEPOSIT, "gift_received"
                rec = Rec(line=ln, ts=ts, kind=kind, tag=tag, account=acc, label=label or None,
                          fee_sym=fc if nz(fq) else None, fee_qty=fq if nz(fq) else None,
                          txhash=((r.get("transaction id") if v2 else r.get("trx. id (optional)")) or "").strip()
                          or None,
                          note=((r.get("note") if v2 else r.get("comment (optional)")) or "").strip() or None)
                rec.ext_id = f"blockpit:{r['blockpit id']}" if v2 and r.get("blockpit id") else ids(*r.values())
                if kind == M.FEE:
                    if not rec.fee_qty and nz(oq):
                        rec.fee_sym, rec.fee_qty = oc, oq
                elif kind == M.TRADE:
                    if not (nz(oq) and nz(iq)):
                        raise ValueError(f"Blockpit: {label} ohne Ein- und Ausgang")
                    rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = oc, oq, ic, iq
                elif kind == M.DEPOSIT:
                    if not nz(iq):
                        raise ValueError(f"Blockpit: {label} ohne Eingang")
                    rec.in_sym, rec.in_qty = ic, iq
                else:
                    if not nz(oq):
                        raise ValueError(f"Blockpit: {label} ohne Ausgang")
                    rec.out_sym, rec.out_qty = oc, oq
                res.recs.append(rec)

            _guard(res, ln, one)
        return res


COINTRACKING_TYPES: dict[str, tuple[str, str | None]] = {
    "trade": (M.TRADE, None), "deposit": (M.DEPOSIT, None), "withdrawal": (M.WITHDRAWAL, None),
    "income": (M.DEPOSIT, "other_income"), "other_income": (M.DEPOSIT, "other_income"),
    "interest_income": (M.DEPOSIT, "interest"), "lending_income": (M.DEPOSIT, "lending"),
    "mining": (M.DEPOSIT, "mining"), "masternode": (M.DEPOSIT, "mining"), "minting": (M.DEPOSIT, "other_income"),
    "gift/tip": (M.DEPOSIT, "gift_received"), "reward/bonus": (M.DEPOSIT, "bonus"), "staking": (M.DEPOSIT, "staking"),
    "airdrop": (M.DEPOSIT, "airdrop"), "lost": (M.WITHDRAWAL, "lost"), "stolen": (M.WITHDRAWAL, "stolen"),
    "spend": (M.WITHDRAWAL, "cost"), "donation": (M.WITHDRAWAL, "donation"), "gift": (M.WITHDRAWAL, "gift"),
    "other_fee": (M.FEE, None), "fee": (M.FEE, None),
}


class CoinTrackingProfile(Profile):
    id = "cointracking"
    label = "CoinTracking (Trade-Liste)"
    group = "Steuertool"
    account = "CoinTracking"
    multi_account = True
    tz = "Europe/Berlin"
    hint = ("CoinTracking → Transaktionen eingeben → Export → CSV. Die Spalte „Exchange“ wird zum Konto. "
            "Zeitangaben gelten in der Zeitzone des CoinTracking-Kontos (Standard hier: Europe/Berlin).")
    signatures = (sig("type", "buy", "cur.", "sell", "cur._2", "exchange", "date"),)

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        ids = _Ids("cointracking")
        fee_key = t.find("fee")
        fee_ccy_key = "cur._3" if fee_key and "cur._3" in t.keys else None
        val_keys = [k for k in t.keys if k.startswith("value in ") and "btc" not in k]
        buy_val_key = next((k for k in t.keys if k.startswith("buy value in ")), None) or \
            (val_keys[0] if val_keys else None)
        sell_val_key = next((k for k in t.keys if k.startswith("sell value in ")), None) or \
            (val_keys[1] if len(val_keys) > 1 else None)
        id_key = t.find("trade-id", "trade id", "tx-id", "txid")
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                ts = self.ts(r.get("date"), o)
                label = r.get("type") or ""
                key = M.norm_label(label).replace("_(non_taxable)", "")
                if key.startswith(("margin", "derivatives", "futures")):
                    res.skip(f"CoinTracking: {label} (Margin/Derivate nicht unterstützt)")
                    return
                kind, tag = COINTRACKING_TYPES.get(key, (None, None))
                bq, bc = pos(self.n(r.get("buy"), o)), sym(r.get("cur."))
                sq, sc = pos(self.n(r.get("sell"), o)), sym(r.get("cur._2"))
                if kind is None:
                    kind = M.TRADE if (nz(bq) and nz(sq)) else (M.DEPOSIT if nz(bq) else M.WITHDRAWAL)
                    tag = M.income_tag(label) if kind == M.DEPOSIT else M.out_tag(label)
                fq = pos(self.n(r.get(fee_key), o)) if fee_key else None
                fc = sym(r.get(fee_ccy_key)) if fee_ccy_key else None
                bv = pos(self.n(r.get(buy_val_key), o)) if buy_val_key else None
                sv = pos(self.n(r.get(sell_val_key), o)) if sell_val_key else None
                vkey = buy_val_key if kind == M.DEPOSIT else sell_val_key if kind == M.WITHDRAWAL else buy_val_key
                value = bv if kind in (M.DEPOSIT, M.TRADE) else sv
                if kind == M.TRADE and not nz(value):
                    value, vkey = sv, sell_val_key
                vccy = (vkey or "").rsplit(" ", 1)[-1].upper() if vkey else None
                rec = Rec(line=ln, ts=ts, kind=kind, tag=tag, account=(r.get("exchange") or "").strip() or o.account,
                          label=label or None, fee_sym=fc if nz(fq) and fc else None,
                          fee_qty=fq if nz(fq) and fc else None, value=value if nz(value) else None,
                          value_ccy=vccy if nz(value) else None,
                          note=(r.get("comment") or r.get("group") or "").strip() or None)
                rec.ext_id = f"ct:{r[id_key]}" if id_key and r.get(id_key) else ids(*r.values())
                if kind == M.TRADE:
                    if not (nz(bq) and nz(sq)):
                        raise ValueError(f"CoinTracking: {label} ohne Kauf- und Verkaufsmenge")
                    rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = sc, sq, bc, bq
                elif kind == M.DEPOSIT:
                    rec.in_sym, rec.in_qty = bc, bq
                elif kind == M.FEE:
                    if not rec.fee_qty:
                        rec.fee_sym, rec.fee_qty = sc, sq
                else:
                    rec.out_sym, rec.out_qty = sc, sq
                if kind in (M.DEPOSIT, M.TRADE) and not nz(rec.in_qty):
                    raise ValueError(f"CoinTracking: {label} ohne Kaufmenge")
                if kind in (M.WITHDRAWAL, M.TRADE) and not nz(rec.out_qty):
                    raise ValueError(f"CoinTracking: {label} ohne Verkaufsmenge")
                res.recs.append(rec)

            _guard(res, ln, one)
        return res


# ----------------------------------------------------------------------------------------------------
# Börsen
# ----------------------------------------------------------------------------------------------------

BINANCE_INTERNAL = ("transfer between", "subscription", "redemption", "savings purchase", "staking purchase",
                    "pos savings purchase", "transfer_in", "transfer_out", "launchpad subscribe", "liquid swap add",
                    "liquid swap remove", "main and funding account transfer", "funding account transfer",
                    "sub-account transfer", "auto-invest subscription", "simple earn locked", "simple earn flexible s",
                    "simple earn flexible r", "dual investment", "eth 2.0 staking withdrawals")
BINANCE_TRADE = {"buy", "sell", "transaction related", "transaction buy", "transaction spend", "transaction sold",
                 "transaction revenue", "binance convert", "large otc trading", "small assets exchange bnb",
                 "stablecoins auto-conversion", "eth 2.0 staking", "auto-invest transaction", "buy crypto with card",
                 "buy crypto with fiat"}
BINANCE_FEE = {"fee", "transaction fee", "bnb fee deduction"}
BINANCE_INCOME: dict[str, str] = {
    "commission history": "bonus", "referrer rebates": "bonus", "commission rebate": "bonus",
    "commission fee shared with you": "bonus", "referral kickback": "bonus", "referral commission": "bonus",
    "airdrop assets": "airdrop", "cash voucher distribution": "bonus", "simple earn flexible airdrop": "airdrop",
    "campaign related reward": "bonus", "hodler airdrops distribution": "airdrop", "megadrop rewards": "airdrop",
    "super bnb mining": "mining", "savings interest": "interest", "simple earn flexible interest": "interest",
    "pool distribution": "mining", "savings distribution": "interest", "launchpool interest": "interest",
    "pos savings interest": "staking", "staking rewards": "staking", "eth 2.0 staking rewards": "staking",
    "liquid swap rewards": "interest", "simple earn locked rewards": "staking", "dot slot auction rewards": "staking",
    "launchpool earnings withdrawal": "airdrop", "bnb vault rewards": "staking", "swap farming rewards": "interest",
    "card cashback": "cashback", "binance card cashback": "cashback", "crypto box": "gift_received",
    "staking rewards distribution": "staking", "distribution": "airdrop", "launchpool airdrop": "airdrop",
    "launchpool airdrop - system distribution": "airdrop", "launchpool airdrop - user claim distribution": "airdrop",
    "mission reward distribution": "bonus", "task center rewards": "bonus",
}
BINANCE_COST = {"binance card spending": "cost", "asset recovery": "cost", "leveraged coin consolidation": "cost",
                "crypto box payment": "gift"}
BINANCE_DEPOSIT = {"deposit", "fiat deposit", "fiat ocbs - add fiat and fees", "p2p trading", "binance pay",
                   "pay", "receive", "c2c transfer"}
BINANCE_WITHDRAW = {"withdraw", "fiat withdraw", "fiat withdrawal", "send", "withdrawal"}


class BinanceProfile(Profile):
    id = "binance"
    label = "Binance (Kontoauszug / Transaction History)"
    account = "Binance"
    hint = ("Binance → Orders → Transaction History → Generate all statements (CSV). Spot-, Funding- und Earn-Konten "
            "werden zu einem Konto zusammengefasst; Futures/Margin werden nicht übernommen.")
    signatures = (sig("utc_time", "account", "operation", "coin", "change"),)

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        ids = _Ids("binance")
        groups: dict[str, list[tuple[int, str, str, Decimal, str, dict[str, str]]]] = defaultdict(list)
        order: list[str] = []
        for ln, r in t.dicts():
            res.rows_read += 1
            try:
                op = (r.get("operation") or "").strip()
                opl = op.lower()
                acc = (r.get("account") or "").strip().lower()
                coin = sym(r.get("coin")) or ""
                change = self.n(r.get("change"), o)
                ts_raw = (r.get("utc_time") or "").strip()
                if change is None or not coin:
                    res.error(ln, "Binance: Menge oder Coin fehlt")
                    continue
                if any(x in acc for x in ("future", "margin", "option")) and opl not in ("transfer_in", "transfer_out"):
                    res.skip("Binance: Futures/Margin/Optionen (nicht unterstützt)")
                    continue
                if any(opl.startswith(x) for x in BINANCE_INTERNAL) or "transfer between" in opl:
                    res.skip("Binance: interne Umbuchung (Spot/Earn/Funding/Staking)")
                    continue
                if change == 0:
                    res.skip("Binance: Zeile mit Menge 0")
                    continue
                ts = self.ts(ts_raw, o)
                note = (r.get("remark") or "").strip() or None
                if opl in BINANCE_TRADE or opl in BINANCE_FEE or opl.startswith(("token swap", "transaction ")):
                    if ts_raw not in groups:
                        order.append(ts_raw)
                    groups[ts_raw].append((ln, opl, coin, change, op, r))
                    continue
                ext = ids(ts_raw, acc, op, coin, change)
                if opl in BINANCE_INCOME or opl.startswith(("launchpool airdrop", "airdrop")):
                    tag = BINANCE_INCOME.get(opl, "airdrop")
                    if change > 0:
                        res.recs.append(Rec(line=ln, ts=ts, kind=M.DEPOSIT, in_sym=coin, in_qty=change, tag=tag,
                                            account=o.account, ext_id=ext, label=op, note=note))
                    else:
                        res.recs.append(Rec(line=ln, ts=ts, kind=M.WITHDRAWAL, out_sym=coin, out_qty=-change,
                                            tag="cost", account=o.account, ext_id=ext, label=op, note=note))
                elif opl in BINANCE_COST:
                    if change < 0:
                        res.recs.append(Rec(line=ln, ts=ts, kind=M.WITHDRAWAL, out_sym=coin, out_qty=-change,
                                            tag=BINANCE_COST[opl], account=o.account, ext_id=ext, label=op, note=note))
                    else:
                        res.recs.append(Rec(line=ln, ts=ts, kind=M.DEPOSIT, in_sym=coin, in_qty=change,
                                            tag="cashback", account=o.account, ext_id=ext, label=op, note=note))
                elif opl in BINANCE_DEPOSIT or opl in BINANCE_WITHDRAW:
                    if change > 0:
                        res.recs.append(Rec(line=ln, ts=ts, kind=M.DEPOSIT, in_sym=coin, in_qty=change,
                                            account=o.account, ext_id=ext, label=op, note=note))
                    else:
                        res.recs.append(Rec(line=ln, ts=ts, kind=M.WITHDRAWAL, out_sym=coin, out_qty=-change,
                                            account=o.account, ext_id=ext, label=op, note=note))
                else:
                    res.error(ln, f"Binance: unbekannte Operation „{op}“ – bitte manuell erfassen")
            except (ValueError, ArithmeticError) as e:
                res.error(ln, str(e))
        for key in order:
            self._group(groups[key], o, res, ids)
        return res

    def _group(self, rows: list[tuple[int, str, str, Decimal, str, dict[str, str]]], o: ParseOptions,
               res: ParseResult, ids: _Ids) -> None:
        ln0 = rows[0][0]
        ts = self.ts(rows[0][5].get("utc_time"), o)
        label = ", ".join(dict.fromkeys(r[4] for r in rows))
        ext = ids("group", rows[0][5].get("utc_time"), *(f"{r[1]}:{r[2]}:{r[3]}" for r in rows))
        conversion = all(r[1].startswith("token swap") for r in rows)
        fees: dict[str, Decimal] = defaultdict(Decimal)
        ins: list[tuple[str, Decimal]] = []
        outs: list[tuple[str, Decimal]] = []
        for _ln, opl, coin, change, _op, _r in rows:
            if opl in BINANCE_FEE:
                fees[coin] += -change
            elif change > 0:
                ins.append((coin, change))
            else:
                outs.append((coin, -change))
        in_coins = list(dict.fromkeys(c for c, _ in ins))
        out_coins = list(dict.fromkeys(c for c, _ in outs))
        pairs: list[tuple[str | None, Decimal | None, str | None, Decimal | None, str | None]] = []
        note = None
        if len(in_coins) == 1 and len(out_coins) == 1:
            pairs.append((out_coins[0], sum((q for _, q in outs), ZERO), in_coins[0], sum((q for _, q in ins), ZERO),
                          None))
        elif ins and outs and len(ins) == len(outs):
            for (oc, oq), (ic, iq) in zip(outs, ins, strict=True):  # Kleinstbeträge: Zeilen paarweise
                pairs.append((oc, oq, ic, iq, None))
        elif len(in_coins) == 1 and len(out_coins) > 1:
            total_in = sum((q for _, q in ins), ZERO)
            share = total_in / len(out_coins)
            note = "Aufteilung der Zugangsmenge geschätzt (gleiche Anteile)"
            for c in out_coins:
                pairs.append((c, sum((q for x, q in outs if x == c), ZERO), in_coins[0], share, note))
        elif ins and not outs:
            for c in in_coins:
                pairs.append((None, None, c, sum((q for x, q in ins if x == c), ZERO), None))
        elif outs and not ins:
            for c in out_coins:
                res.recs.append(Rec(line=ln0, ts=ts, kind=M.WITHDRAWAL, out_sym=c,
                                    out_qty=sum((q for x, q in outs if x == c), ZERO), account=o.account,
                                    ext_id=f"{ext}:{c}", label=label, note="Binance: Abgang ohne Gegenbuchung"))
        elif ins or outs:
            res.error(ln0, f"Binance: Vorgang „{label}“ mit mehreren Ein- und Ausgängen nicht eindeutig")
            return
        fee_items = [(c, q) for c, q in fees.items() if q > 0]
        for i, (oc, oq, ic, iq, n) in enumerate(pairs):
            fee = fee_items.pop(0) if fee_items and i == 0 else None
            res.recs.append(Rec(line=ln0, ts=ts, kind=M.CONVERSION if conversion else M.TRADE, out_sym=oc, out_qty=oq,
                                in_sym=ic, in_qty=iq, fee_sym=fee[0] if fee else None,
                                fee_qty=fee[1] if fee else None, account=o.account,
                                ext_id=ext if i == 0 else f"{ext}:{i}", label=label, note=n or note))
        for c, q in fee_items:
            res.recs.append(Rec(line=ln0, ts=ts, kind=M.FEE, fee_sym=c, fee_qty=q, account=o.account,
                                ext_id=f"{ext}:fee:{c}", label=label))


class BitpandaProfile(Profile):
    id = "bitpanda"
    label = "Bitpanda (Transaktionsverlauf)"
    account = "Bitpanda"
    hint = ("Bitpanda → Verlauf → Transaktionen exportieren (CSV). Die Präambel am Dateianfang wird übersprungen. "
            "Aktien/ETFs (Bitpanda Stocks) werden als Wertpapiere vorgeschlagen.")
    signatures = (
        sig("transaction id", "timestamp", "transaction type", "in/out", "amount fiat", "fiat", "amount asset",
            "asset"),
        sig("id", "type", "in/out", "amount fiat", "fiat", "amount asset", "asset", "status", "created at"),
    )

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        v2 = t.has("transaction id")
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                if not v2 and (r.get("status") or "").strip().lower() not in ("", "finished"):
                    res.skip("Bitpanda: nicht abgeschlossene Transaktion")
                    return
                ts = self.ts(r.get("timestamp") if v2 else r.get("created at"), o)
                typ = ((r.get("transaction type") if v2 else r.get("type")) or "").strip().lower()
                inout = (r.get("in/out") or "").strip().lower()
                asset, fiat = sym(r.get("asset")), sym(r.get("fiat"))
                qa, qf = pos(self.n(r.get("amount asset"), o)), pos(self.n(r.get("amount fiat"), o))
                fee = pos(self.n(r.get("fee"), o))
                fee_asset = sym(r.get("fee asset")) or asset
                cls = (r.get("asset class") or "").lower()
                hint = "security" if ("stock" in cls or "etf" in cls or "etc" in cls) else (
                    "fiat" if cls == "fiat" else "crypto")
                rid = ((r.get("transaction id") if v2 else r.get("id")) or "").strip()
                rec = Rec(line=ln, ts=ts, kind=M.TRADE, account=o.account, label=typ or None,
                          ext_id=f"bitpanda:{rid}" if rid else None, fee_sym=fee_asset if nz(fee) else None,
                          fee_qty=fee if nz(fee) else None, value=qf if nz(qf) and fiat else None,
                          value_ccy=fiat if nz(qf) else None)
                if asset and hint != "fiat":
                    rec.class_hint[asset] = hint
                if typ == "buy":
                    rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = fiat, qf, asset, qa
                elif typ == "sell":
                    rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = asset, qa, fiat, qf
                elif typ in ("deposit", "withdrawal", "transfer", "refund"):
                    is_fiat = hint == "fiat" or not nz(qa) or asset == fiat
                    s, q = (fiat or asset, qf) if is_fiat else (asset, qa)
                    incoming = inout.startswith("in") if inout else typ in ("deposit", "refund")
                    if incoming:
                        rec.kind, rec.in_sym, rec.in_qty = M.DEPOSIT, s, q
                        if typ == "transfer":
                            rec.tag = "reward"
                            rec.note = "Bitpanda-Transfer (Eingang): meist Reward/Staking – bitte prüfen"
                        elif typ == "refund":
                            rec.tag = "refund"
                    else:
                        rec.kind, rec.out_sym, rec.out_qty = M.WITHDRAWAL, s, q
                    if is_fiat:
                        rec.value = rec.value_ccy = None
                elif typ == "ico":
                    rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = fiat, qf, asset, qa
                else:
                    raise ValueError(f"Bitpanda: unbekannter Transaktionstyp „{typ}“")
                if rec.kind == M.TRADE and not (nz(rec.out_qty) and nz(rec.in_qty)):
                    raise ValueError(f"Bitpanda: {typ} ohne Menge oder Betrag")
                if (rec.kind == M.DEPOSIT and not nz(rec.in_qty)) or (rec.kind == M.WITHDRAWAL and not nz(rec.out_qty)):
                    raise ValueError(f"Bitpanda: {typ} ohne Menge")
                res.recs.append(rec)

            _guard(res, ln, one)
        ids = _Ids("bitpanda")
        for rec in res.recs:
            if rec.ext_id is None:
                rec.ext_id = ids(rec.line, rec.ts, rec.kind, rec.in_sym, rec.in_qty, rec.out_sym, rec.out_qty)
        return res


KRAKEN_INTERNAL_SUB = {"spottostaking", "stakingfromspot", "stakingtospot", "spotfromstaking", "spottofutures",
                       "spotfromfutures", "allocation", "deallocation", "autoallocation", "migration"}


class KrakenProfile(Profile):
    id = "kraken"
    label = "Kraken (Ledgers)"
    account = "Kraken"
    hint = ("Kraken → Documents → Export → Ledgers (CSV, alle Felder). Staking-Varianten (ETH2.S, DOT.S …) "
            "werden dem Basis-Asset zugeordnet; Umbuchungen zwischen Spot und Staking/Earn sind intern.")
    signatures = (sig("txid", "refid", "time", "type", "asset", "amount", "fee"),)

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        groups: dict[str, list[tuple[int, dict[str, str]]]] = defaultdict(list)
        order: list[str] = []
        for ln, r in t.dicts():
            res.rows_read += 1
            try:
                if not (r.get("txid") or "").strip():
                    res.skip("Kraken: Zeile ohne txid (Vormerkung/Duplikat)")
                    continue
                typ = (r.get("type") or "").strip().lower()
                sub = (r.get("subtype") or "").strip().lower()
                if typ in ("trade", "spend", "receive", "adjustment") or (typ == "transfer" and
                                                                           sub == "delistingconversion"):
                    ref = (r.get("refid") or r.get("txid") or "").strip()
                    if ref not in groups:
                        order.append(ref)
                    groups[ref].append((ln, r))
                    continue
                ts = self.ts(r.get("time"), o)
                asset = M.kraken_symbol(r.get("asset") or "")
                amt = self.n(r.get("amount"), o) or ZERO
                fee = pos(self.n(r.get("fee"), o)) or ZERO
                base = Rec(line=ln, ts=ts, kind=M.DEPOSIT, account=o.account, ext_id=f"kraken:{r['txid'].strip()}",
                           label=f"{typ}{'/' + sub if sub else ''}", fee_sym=asset if fee else None,
                           fee_qty=fee if fee else None)
                if typ in ("deposit", "withdrawal"):
                    if amt > 0:
                        base.in_sym, base.in_qty = asset, amt
                        if typ == "withdrawal":
                            base.note = "Rückbuchung einer Auszahlung"
                    elif amt < 0:
                        base.kind, base.out_sym, base.out_qty = M.WITHDRAWAL, asset, -amt
                        if typ == "deposit":
                            base.note = "Rückbuchung einer Einzahlung"
                    elif fee:
                        base.kind = M.FEE
                    else:
                        res.skip("Kraken: Zeile mit Menge 0")
                        continue
                elif typ in ("staking", "dividend", "earn", "credit", "transfer"):
                    if sub in KRAKEN_INTERNAL_SUB or (typ == "staking" and amt < 0) or (typ == "transfer" and amt <= 0):
                        res.skip("Kraken: interne Umbuchung (Spot/Staking/Earn)")
                        continue
                    if amt <= 0:
                        res.skip("Kraken: Zeile mit Menge 0")
                        continue
                    base.in_sym, base.in_qty = asset, amt
                    if typ == "staking" or (typ == "earn" and sub in ("reward", "")):
                        base.tag = "staking"
                    elif typ == "dividend":
                        base.tag = "reward"
                    elif typ == "credit":
                        base.tag = "bonus"
                    elif sub == "airdrop" or typ == "transfer":
                        base.tag = "airdrop"
                        if typ == "transfer" and sub != "airdrop":
                            base.note = "Kraken-Transfer ohne Untertyp (meist Airdrop/Fork) – bitte prüfen"
                    else:
                        res.error(ln, f"Kraken: unbekannter Untertyp „{typ}/{sub}“")
                        continue
                elif typ in ("margin", "rollover", "settled", "sale", "nfttrade", "nftcreatorfee", "nftrebate",
                             "invite bonus", "futures"):
                    res.skip(f"Kraken: {typ} (Margin/Futures/NFT nicht unterstützt)")
                    continue
                else:
                    res.error(ln, f"Kraken: unbekannter Typ „{typ}“")
                    continue
                res.recs.append(base)
            except (ValueError, ArithmeticError) as e:
                res.error(ln, str(e))
        for ref in order:
            self._group(ref, groups[ref], o, res)
        return res

    def _group(self, ref: str, rows: list[tuple[int, dict[str, str]]], o: ParseOptions, res: ParseResult) -> None:
        ln0, r0 = rows[0]
        try:
            ts = self.ts(r0.get("time"), o)
        except ValueError as e:
            res.error(ln0, str(e))
            return
        ins: dict[str, Decimal] = defaultdict(Decimal)
        outs: dict[str, Decimal] = defaultdict(Decimal)
        fees: dict[str, Decimal] = defaultdict(Decimal)
        for _ln, r in rows:
            a = M.kraken_symbol(r.get("asset") or "")
            amt = self.n(r.get("amount"), o) or ZERO
            fee = pos(self.n(r.get("fee"), o)) or ZERO
            if amt > 0:
                ins[a] += amt
            elif amt < 0:
                outs[a] += -amt
            if fee:
                fees[a] += fee
        label = "/".join(dict.fromkeys((r.get("type") or "").lower() for _, r in rows))
        if len(ins) == 1 and len(outs) == 1:
            (ic, iq), (oc, oq) = next(iter(ins.items())), next(iter(outs.items()))
            fee_items = list(fees.items())
            fc, fq = fee_items[0] if fee_items else (None, None)
            res.recs.append(Rec(line=ln0, ts=ts, kind=M.TRADE, out_sym=oc, out_qty=oq, in_sym=ic, in_qty=iq,
                                fee_sym=fc, fee_qty=fq, account=o.account, ext_id=f"kraken:{ref}", label=label))
            for c, q in fee_items[1:]:
                res.recs.append(Rec(line=ln0, ts=ts, kind=M.FEE, fee_sym=c, fee_qty=q, account=o.account,
                                    ext_id=f"kraken:{ref}:fee:{c}", label=label))
        elif not ins and not outs and fees:
            for c, q in fees.items():
                res.recs.append(Rec(line=ln0, ts=ts, kind=M.FEE, fee_sym=c, fee_qty=q, account=o.account,
                                    ext_id=f"kraken:{ref}:fee:{c}", label=label))
        elif ins and not outs and label in ("adjustment", "receive"):
            for c, q in ins.items():
                res.recs.append(Rec(line=ln0, ts=ts, kind=M.DEPOSIT, in_sym=c, in_qty=q, account=o.account,
                                    ext_id=f"kraken:{ref}:{c}", label=label,
                                    note="Kraken-Anpassung ohne Gegenbuchung – bitte prüfen"))
        else:
            res.error(ln0, f"Kraken: Handel {ref} nicht eindeutig ({len(outs)} Abgänge, {len(ins)} Zugänge)")


class CoinbaseProfile(Profile):
    id = "coinbase"
    label = "Coinbase (Transaktionsbericht)"
    account = "Coinbase"
    hint = ("Coinbase → Profil → Berichte → Transaktionsverlauf (CSV). Käufe, Verkäufe, Konvertierungen, Staking- und "
            "Earn-Belohnungen werden erkannt; Coinbase-Advanced-Aufträge ebenso.")
    signatures = (sig("timestamp", "transaction type", "asset", "quantity transacted"),)

    CONVERT_RE = re.compile(r"Converted\s+([\d.,]+)\s+(\S+)\s+to\s+([\d.,]+)\s+(\S+)", re.I)

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        ids = _Ids("coinbase")
        keys = t.keys
        ccy_key = next((k for k in keys if k in ("price currency", "spot price currency")), None)
        hdr_ccy = next((k.split(" ", 1)[0].upper() for k in keys if re.match(r"^[a-z]{3} (spot price|subtotal)", k)),
                       None)
        sub_key = next((k for k in keys if "subtotal" in k), None)
        total_key = next((k for k in keys if k.startswith("total") or " total" in k), None)
        fee_key = next((k for k in keys if "fees" in k and not k.startswith(("total", "subtotal"))
                        and "total" not in k.split("(", 1)[0]), None)
        id_key = "id" if "id" in keys else None
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                ts = self.ts(r.get("timestamp"), o)
                typ = (r.get("transaction type") or "").strip()
                tl = typ.lower()
                asset = sym(r.get("asset"))
                qty = pos(self.n(r.get("quantity transacted"), o))
                ccy = sym(r.get(ccy_key)) if ccy_key else None
                ccy = ccy or hdr_ccy or "EUR"
                sub = pos(self.n(r.get(sub_key), o)) if sub_key else None
                total = pos(self.n(r.get(total_key), o)) if total_key else None
                fees = pos(self.n(r.get(fee_key), o)) if fee_key else None
                notes = (r.get("notes") or "").strip()
                if sub is None and total is not None:
                    sub = total - (fees or ZERO) if tl.endswith("buy") else total + (fees or ZERO)
                rec = Rec(line=ln, ts=ts, kind=M.TRADE, account=o.account, label=typ or None, note=notes or None,
                          value=sub if nz(sub) else None, value_ccy=ccy if nz(sub) else None,
                          ext_id=f"coinbase:{r[id_key]}" if id_key and r.get(id_key) else ids(*r.values()))
                fee_leg = (ccy, fees) if nz(fees) else (None, None)
                if not nz(qty):
                    res.skip("Coinbase: Zeile mit Menge 0")
                    return
                if tl in ("buy", "advanced trade buy", "recurring buy"):
                    rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = ccy, sub, asset, qty
                    rec.fee_sym, rec.fee_qty = fee_leg
                elif tl in ("sell", "advanced trade sell"):
                    rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = asset, qty, ccy, sub
                    rec.fee_sym, rec.fee_qty = fee_leg
                elif tl == "convert":
                    m = self.CONVERT_RE.search(notes)
                    if not m:
                        raise ValueError("Coinbase: Konvertierung ohne lesbare Notiz („Converted … to …“)")
                    rec.out_sym, rec.out_qty = asset, qty
                    rec.in_sym, rec.in_qty = sym(m.group(4)), num(m.group(3), ".")
                    if nz(fees):
                        rec.note = f"{notes} (Spread {fees} {ccy} im Kurs enthalten)"
                elif tl in ("send", "exchange deposit", "pro deposit", "withdrawal"):
                    rec.kind, rec.out_sym, rec.out_qty = M.WITHDRAWAL, asset, qty
                    if tl == "withdrawal":
                        rec.fee_sym, rec.fee_qty = fee_leg
                elif tl in ("receive", "exchange withdrawal", "pro withdrawal", "deposit"):
                    rec.kind, rec.in_sym, rec.in_qty = M.DEPOSIT, asset, qty
                    nl = notes.lower()
                    if tl == "receive" and "coinbase earn" in nl:
                        rec.tag = "reward"
                    elif tl == "receive" and ("reward" in nl or "referral" in nl or "bonus" in nl):
                        rec.tag = "bonus"
                    if tl == "deposit":
                        rec.fee_sym, rec.fee_qty = fee_leg
                elif tl in ("rewards income", "staking income", "inflation reward", "staking reward"):
                    rec.kind, rec.in_sym, rec.in_qty, rec.tag = M.DEPOSIT, asset, qty, "staking"
                elif tl in ("coinbase earn", "learning reward"):
                    rec.kind, rec.in_sym, rec.in_qty, rec.tag = M.DEPOSIT, asset, qty, "reward"
                elif tl in ("interest payout", "usdc rewards"):
                    rec.kind, rec.in_sym, rec.in_qty, rec.tag = M.DEPOSIT, asset, qty, "interest"
                elif tl in ("donation",):
                    rec.kind, rec.out_sym, rec.out_qty, rec.tag = M.WITHDRAWAL, asset, qty, "donation"
                elif tl in ("admin debit", "subscription", "card spend", "coinbase card spend"):
                    rec.kind, rec.out_sym, rec.out_qty, rec.tag = M.WITHDRAWAL, asset, qty, "cost"
                elif tl in ("retail staking transfer", "retail unstaking transfer", "staking transfer",
                            "unstaking transfer", "retail eth2 deprecation"):
                    res.skip("Coinbase: interne Staking-Umbuchung")
                    return
                else:
                    raise ValueError(f"Coinbase: unbekannter Vorgang „{typ}“")
                if rec.kind == M.TRADE and not (nz(rec.in_qty) and nz(rec.out_qty)):
                    raise ValueError(f"Coinbase: {typ} ohne Betrag (Subtotal)")
                res.recs.append(rec)

            _guard(res, ln, one)
        return res


CDC_TRADE = {"crypto_exchange", "crypto_viban_exchange", "viban_purchase", "trading.limit_order.crypto_wallet.exchange",
             "trading.limit_order.fiat_wallet.purchase_commit", "trading.limit_order.fiat_wallet.sell_commit",
             "trading.limit_order.cash_account.purchase_commit", "trading.limit_order.cash_account.sell_commit"}
CDC_BUY_EXT = {"van_purchase", "crypto_purchase", "recurring_buy_order", "trading.crypto_purchase.google_pay",
               "trading.crypto_purchase.apple_pay"}
CDC_SELL_EXT = {"crypto_to_van_sell_order"}
CDC_INCOME: dict[str, str] = {
    "crypto_earn_interest_paid": "interest", "crypto_earn_extra_interest_paid": "interest",
    "mco_stake_reward": "staking", "supercharger_reward_to_app_credited": "staking",
    "rewards_platform_deposit_credited": "reward", "referral_bonus": "bonus", "referral_gift": "bonus",
    "referral_card_cashback": "cashback", "transfer_cashback": "cashback", "reimbursement": "cashback",
    "gift_card_reward": "bonus", "campaign_reward": "bonus", "admin_wallet_credited": "bonus",
    "staking_reward": "staking", "crypto_earn_staking_reward": "staking", "pay_checkout_reward": "cashback",
}
CDC_COST = {"crypto_payment": "cost", "card_top_up": "cost", "card_cashback_reverted": "cost",
            "reimbursement_reverted": "cost", "crypto_transfer": None}
CDC_DEPOSIT = {"crypto_deposit", "exchange_to_crypto_transfer", "viban_deposit", "crypto_payment_refund",
               "trading.limit_order.fiat_wallet.deposit"}
CDC_WITHDRAW = {"crypto_withdrawal", "crypto_to_exchange_transfer", "viban_withdrawal", "viban_card_top_up"}
CDC_INTERNAL = ("crypto_earn_program_created", "crypto_earn_program_withdrawn", "lockup_", "dynamic_coin_swap",
                "interest_swap", "crypto_wallet_swap", "supercharger_deposit", "supercharger_withdrawal",
                "council_node_deposit_created", "trading.limit_order.", "finance.lockup", "crypto_earn_program")


class CryptoComProfile(Profile):
    id = "cryptocom"
    label = "Crypto.com App (Krypto- und Fiat-Wallet)"
    account = "Crypto.com App"
    hint = ("Crypto.com App → Konten → Krypto-Wallet → Verlauf exportieren (CSV); die Fiat-Wallet ebenso. Beide "
            "Dateien können nacheinander importiert werden – identische Zeilen werden erkannt.")
    signatures = (sig("timestamp (utc)", "transaction description", "currency", "amount", "to currency",
                      "to amount", "native currency", "native amount", "transaction kind"),)

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        ids = _Ids("cdc")
        dust: dict[str, list[tuple[int, dict[str, str]]]] = defaultdict(list)
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                kind = (r.get("transaction kind") or "").strip().lower()
                desc = (r.get("transaction description") or "").strip()
                if kind.startswith("dust_conversion"):
                    dust[(r.get("timestamp (utc)") or "").strip()].append((ln, r))
                    return
                if kind in CDC_TRADE or kind in CDC_BUY_EXT or kind in CDC_SELL_EXT:
                    pass
                elif any(kind.startswith(x) for x in CDC_INTERNAL):
                    res.skip("Crypto.com: interne Umbuchung (Earn/Lockup/Supercharger)")
                    return
                ts = self.ts(r.get("timestamp (utc)"), o)
                c, a = sym(r.get("currency")), self.n(r.get("amount"), o)
                tc, ta = sym(r.get("to currency")), pos(self.n(r.get("to amount"), o))
                nv, nc = pos(self.n(r.get("native amount"), o)), sym(r.get("native currency"))
                rec = Rec(line=ln, ts=ts, kind=M.TRADE, account=o.account, label=desc or kind,
                          value=nv if nz(nv) else None, value_ccy=nc if nz(nv) else None,
                          txhash=(r.get("transaction hash") or "").strip() or None, ext_id=ids(*r.values()))
                if a is None or a == 0:
                    res.skip("Crypto.com: Zeile mit Menge 0")
                    return
                if kind in CDC_TRADE:
                    if tc and nz(ta):
                        rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = c, abs(a), tc, ta
                    elif a > 0:
                        rec.in_sym, rec.in_qty = c, a
                    else:
                        rec.out_sym, rec.out_qty = c, -a
                elif kind in CDC_BUY_EXT:
                    rec.in_sym, rec.in_qty = c, abs(a)
                    if tc and nz(ta) and a < 0:
                        rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = c, abs(a), tc, ta
                elif kind in CDC_SELL_EXT:
                    rec.out_sym, rec.out_qty = c, abs(a)
                elif kind in CDC_INCOME:
                    if a > 0:
                        rec.kind, rec.in_sym, rec.in_qty, rec.tag = M.DEPOSIT, c, a, CDC_INCOME[kind]
                    else:
                        rec.kind, rec.out_sym, rec.out_qty, rec.tag = M.WITHDRAWAL, c, -a, "cost"
                elif kind in CDC_COST:
                    if a < 0:
                        rec.kind, rec.out_sym, rec.out_qty, rec.tag = M.WITHDRAWAL, c, -a, CDC_COST[kind]
                    else:
                        rec.kind, rec.in_sym, rec.in_qty, rec.tag = M.DEPOSIT, c, a, "refund"
                elif kind in CDC_DEPOSIT or kind in CDC_WITHDRAW:
                    if a > 0:
                        rec.kind, rec.in_sym, rec.in_qty = M.DEPOSIT, c, a
                    else:
                        rec.kind, rec.out_sym, rec.out_qty = M.WITHDRAWAL, c, -a
                    if kind == "crypto_payment_refund":
                        rec.tag = "refund"
                else:
                    raise ValueError(f"Crypto.com: unbekannter Vorgang „{kind or desc}“")
                res.recs.append(rec)

            _guard(res, ln, one)
        for key, rows in dust.items():
            self._dust(key, rows, o, res, ids)
        return res

    def _dust(self, key: str, rows: list[tuple[int, dict[str, str]]], o: ParseOptions, res: ParseResult,
              ids: _Ids) -> None:
        ln0 = rows[0][0]
        try:
            ts = self.ts(key, o)
            debits = [(sym(r.get("currency")), pos(self.n(r.get("amount"), o)), pos(self.n(r.get("native amount"), o)))
                      for _, r in rows if (r.get("transaction kind") or "").lower() == "dust_conversion_debited"]
            credits = [(sym(r.get("currency")), pos(self.n(r.get("amount"), o)), sym(r.get("native currency")))
                       for _, r in rows if (r.get("transaction kind") or "").lower() == "dust_conversion_credited"]
        except ValueError as e:
            res.error(ln0, str(e))
            return
        if len(credits) != 1 or not debits:
            res.error(ln0, "Crypto.com: Kleinstbetrag-Umwandlung nicht eindeutig")
            return
        cc, cq, nccy = credits[0]
        total = sum((d[2] or ZERO for d in debits), ZERO)
        for i, (dc, dq, dv) in enumerate(debits):
            share = (dv / total) if total and dv else Decimal(1) / len(debits)
            res.recs.append(Rec(line=ln0, ts=ts, kind=M.TRADE, out_sym=dc, out_qty=dq, in_sym=cc,
                                in_qty=(cq or ZERO) * share, value=dv if nz(dv) else None,
                                value_ccy=nccy if nz(dv) else None, account=o.account, label="Kleinstbeträge → CRO",
                                ext_id=ids("dust", key, dc, dq, i),
                                note=None if len(debits) == 1 else "Zugangsmenge anteilig nach Gegenwert aufgeteilt"))


# ----------------------------------------------------------------------------------------------------
# Wallets
# ----------------------------------------------------------------------------------------------------

LEDGER_FEE_ONLY = {"fees", "reveal", "bond", "unbond", "withdraw_unbonded", "delegate", "undelegate", "redelegate",
                   "opt_in", "opt_out", "freeze", "unfreeze", "vote", "set_controller", "nominate", "chill",
                   "approve", "stake", "unstake", "legacy_reward_claim"}


class LedgerLiveProfile(Profile):
    id = "ledger"
    label = "Ledger Live (Operationen)"
    group = "Wallet"
    account = "Ledger"
    multi_account = False
    hint = ("Ledger Live → Einstellungen → Konten → Operationen exportieren (CSV). Standard: alle Konten der Datei "
            "werden zu einem Konto „Ledger“; optional je Ledger-Konto ein eigenes Konto.")
    signatures = (sig("operation date", "currency ticker", "operation type", "operation amount", "operation fees",
                      "account name"),)

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        per_account = bool((o.mapping or {}).get("accounts_from_file"))
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                status = (r.get("status") or "").strip().lower()
                if status and status != "confirmed":
                    res.skip("Ledger Live: nicht bestätigte Operation")
                    return
                ts = self.ts(r.get("operation date"), o)
                typ = (r.get("operation type") or "").strip().upper()
                asset = sym(r.get("currency ticker"))
                amt = pos(self.n(r.get("operation amount"), o)) or ZERO
                fee = pos(self.n(r.get("operation fees"), o)) or ZERO
                h = (r.get("operation hash") or "").strip()
                acc_name = (r.get("account name") or "").strip()
                acc = acc_name if per_account and acc_name else o.account
                cv_key = t.find("countervalue at operation date")
                cv = pos(self.n(r.get(cv_key), o)) if cv_key else None
                cvc = sym(r.get("countervalue ticker"))
                rec = Rec(line=ln, ts=ts, kind=M.DEPOSIT, account=acc, label=typ, txhash=h or None,
                          ext_id=f"ledger:{h}:{typ}:{acc_name}:{asset}" if h else None,
                          value=cv if nz(cv) and cvc else None, value_ccy=cvc if nz(cv) else None)
                if typ == "IN":
                    rec.in_sym, rec.in_qty = asset, amt
                elif typ in ("REWARD", "REWARD_PAYOUT"):
                    rec.in_sym, rec.in_qty, rec.tag = asset, amt, "staking"
                elif typ in ("OUT", "NFT_OUT"):
                    if typ == "NFT_OUT" or amt <= fee:
                        rec.kind, rec.fee_sym, rec.fee_qty = M.FEE, asset, fee
                    else:
                        rec.kind, rec.out_sym, rec.out_qty = M.WITHDRAWAL, asset, amt - fee
                        rec.fee_sym, rec.fee_qty = (asset, fee) if fee else (None, None)
                        if cv and amt:
                            rec.value = cv * (amt - fee) / amt
                elif typ.lower() in LEDGER_FEE_ONLY:
                    if not fee:
                        res.skip("Ledger Live: Operation ohne Gebühr (z. B. Delegation)")
                        return
                    rec.kind, rec.fee_sym, rec.fee_qty = M.FEE, asset, fee
                    rec.value = rec.value_ccy = None
                elif typ in ("NFT_IN", "NONE"):
                    res.skip("Ledger Live: NFT/ohne Wertbewegung")
                    return
                else:
                    raise ValueError(f"Ledger Live: unbekannte Operation „{typ}“")
                if rec.kind == M.DEPOSIT and not nz(rec.in_qty):
                    res.skip("Ledger Live: Zeile mit Menge 0")
                    return
                res.recs.append(rec)

            _guard(res, ln, one)
        ids = _Ids("ledger")
        for rec in res.recs:
            if rec.ext_id is None:
                rec.ext_id = ids(rec.ts, rec.kind, rec.in_sym, rec.in_qty, rec.out_sym, rec.out_qty, rec.fee_qty)
        return res


class TrezorProfile(Profile):
    id = "trezor"
    label = "Trezor Suite / Trezor Wallet"
    group = "Wallet"
    account = "Trezor"
    needs_asset = True
    hint = ("Trezor Suite → Konto → Export (CSV). Ältere Exporte enthalten die Währung nicht – dann im Upload "
            "angeben (oder sie steht im Dateinamen, z. B. „…-BTC-…csv“).")
    signatures = (
        sig("timestamp", "type", "transaction id", "fee", "fee unit", "amount", "amount unit"),
        sig("type", "transaction id", "addresses", "fee", "total"),
        sig("date", "time", "tx id", "address", "tx type", "value", "tx total"),
    )

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        v2 = t.has("amount unit")
        legacy = t.has("tx type")
        fallback = (o.default_asset or "").strip().upper()
        if not fallback:
            m = re.search(r"[-_]([A-Za-z]{3,5})[-_.]", o.filename or "")
            fallback = m.group(1).upper() if m else ""
        sent: dict[str, Rec] = {}
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                if v2:
                    ts = parse_ts(r.get("timestamp"), o.tz or zone(self.tz))[0]
                    typ = (r.get("type") or "").strip().upper()
                    asset, amt = sym(r.get("amount unit")), pos(self.n(r.get("amount"), o)) or ZERO
                    fee, fee_u = pos(self.n(r.get("fee"), o)) or ZERO, sym(r.get("fee unit"))
                    txid = (r.get("transaction id") or "").strip()
                    note = (r.get("label") or "").strip() or None
                    value = pos(self.n(r.get(t.find_prefix("fiat") or ""), o))
                    vccy = _ccy_from_header(t.find_prefix("fiat"))
                elif legacy:
                    ts = self.ts(f"{r.get('date', '')} {r.get('time', '')}".strip(), o)
                    typ = {"IN": "RECV", "OUT": "SENT"}.get((r.get("tx type") or "").strip().upper(),
                                                           (r.get("tx type") or "").strip().upper())
                    asset = fallback
                    amt = pos(self.n(r.get("value"), o)) or ZERO
                    total = pos(self.n(r.get("tx total"), o)) or ZERO
                    fee, fee_u = (total - amt if typ == "SENT" and total > amt else ZERO), fallback
                    if typ == "SELF":
                        fee = total
                    txid = (r.get("tx id") or "").strip()
                    note, value, vccy = (r.get("address label") or "").strip() or None, None, None
                else:
                    raw_ts = r.get("date & time") or r.get("timestamp")
                    ts = self.ts(raw_ts, o)
                    typ = (r.get("type") or "").strip().upper()
                    asset = fallback
                    amt = pos(self.n(r.get("total"), o)) or ZERO
                    fee, fee_u = pos(self.n(r.get("fee"), o)) or ZERO, fallback
                    txid = (r.get("transaction id") or "").strip()
                    note, value, vccy = None, None, None
                if not asset:
                    raise ValueError("Trezor: Währung nicht in der Datei – bitte beim Upload angeben (z. B. BTC)")
                base = Rec(line=ln, ts=ts, kind=M.DEPOSIT, account=o.account, label=typ, txhash=txid or None,
                           ext_id=f"trezor:{txid}:{typ}:{asset}" if txid else None, note=note,
                           value=value if nz(value) else None, value_ccy=vccy if nz(value) else None)
                if typ == "RECV":
                    base.in_sym, base.in_qty = asset, amt
                    if not nz(amt):
                        res.skip("Trezor: Zeile mit Menge 0")
                        return
                elif typ == "SENT":
                    if txid and txid in sent:  # mehrere Ausgänge derselben Transaktion
                        prev = sent[txid]
                        prev.out_qty = (prev.out_qty or ZERO) + amt
                        if prev.value is not None and base.value is not None:
                            prev.value += base.value
                        return
                    base.kind, base.out_sym, base.out_qty = M.WITHDRAWAL, asset, amt
                    base.fee_sym, base.fee_qty = (fee_u or asset, fee) if fee else (None, None)
                    if txid:
                        sent[txid] = base
                elif typ in ("SELF", "FAILED"):
                    if not fee:
                        res.skip("Trezor: Eigenüberweisung ohne Gebühr")
                        return
                    base.kind, base.fee_sym, base.fee_qty = M.FEE, fee_u or asset, fee
                    base.value = base.value_ccy = None
                else:
                    raise ValueError(f"Trezor: unbekannter Typ „{typ}“")
                res.recs.append(base)

            _guard(res, ln, one)
        ids = _Ids("trezor")
        for rec in res.recs:
            if rec.ext_id is None:
                rec.ext_id = ids(rec.ts, rec.kind, rec.in_qty, rec.out_qty, rec.fee_qty)
        return res


class ElectrumProfile(Profile):
    id = "electrum"
    label = "Electrum (Bitcoin-Wallet)"
    group = "Wallet"
    account = "Electrum"
    tz = "Europe/Berlin"
    needs_asset = True
    hint = ("Electrum → Wallet → Verlauf → Exportieren (CSV). Zeitangaben sind Ortszeit; Währung ist BTC, falls "
            "nicht anders angegeben.")
    signatures = (sig("transaction_hash", "label", "value", "timestamp"),)

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        asset = (o.default_asset or "BTC").upper()
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                ts = self.ts(r.get("timestamp"), o)
                val = self.n(r.get("value"), o)
                if val is None:
                    raise ValueError("Electrum: Betrag fehlt")
                fee = pos(self.n(r.get("fee"), o)) or ZERO
                h = (r.get("transaction_hash") or "").strip()
                rec = Rec(line=ln, ts=ts, kind=M.DEPOSIT, account=o.account, txhash=h or None,
                          ext_id=f"electrum:{h}" if h else hid(*r.values()),
                          note=(r.get("label") or "").strip() or None, label="Empfang" if val > 0 else "Versand")
                if val > 0:
                    rec.in_sym, rec.in_qty = asset, val
                elif val < 0:
                    sent = -val - fee
                    if sent <= 0:
                        rec.kind, rec.fee_sym, rec.fee_qty, rec.label = M.FEE, asset, -val, "Gebühr"
                    else:
                        rec.kind, rec.out_sym, rec.out_qty = M.WITHDRAWAL, asset, sent
                        rec.fee_sym, rec.fee_qty = (asset, fee) if fee else (None, None)
                else:
                    res.skip("Electrum: Zeile mit Betrag 0")
                    return
                res.recs.append(rec)

            _guard(res, ln, one)
        return res


class ExodusProfile(Profile):
    id = "exodus"
    label = "Exodus"
    group = "Wallet"
    account = "Exodus"
    hint = "Exodus → Einstellungen → Hilfe → Transaktionen exportieren (CSV)."
    signatures = (
        sig("date", "type", "outamount", "outcurrency", "feeamount", "feecurrency", "inamount", "incurrency"),
        sig("txid", "date", "type", "coinamount", "fee"),
    )

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        v2 = t.has("coinamount")
        ids = _Ids("exodus")
        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                ts = self.ts(r.get("date"), o)
                typ = (r.get("type") or "").strip().lower()
                note = (r.get("personalnote") or "").strip() or None
                rec = Rec(line=ln, ts=ts, kind=M.DEPOSIT, account=o.account, label=typ, note=note)
                if v2:
                    q, c = amount_and_unit(r.get("coinamount"), o.decimal or ".")
                    fq, fc = amount_and_unit(r.get("fee"), o.decimal or ".")
                    q, fq = pos(q), pos(fq)
                    rec.txhash = (r.get("txid") or "").strip() or None
                    rec.fee_sym, rec.fee_qty = (fc, fq) if nz(fq) else (None, None)
                    if "failed" in typ:
                        if not nz(fq):
                            res.skip("Exodus: fehlgeschlagen ohne Gebühr")
                            return
                        rec.kind = M.FEE
                    elif typ == "deposit":
                        rec.in_sym, rec.in_qty, rec.fee_sym, rec.fee_qty = c, q, None, None
                    elif typ == "withdrawal":
                        rec.kind, rec.out_sym, rec.out_qty = M.WITHDRAWAL, c, q
                    else:
                        raise ValueError(f"Exodus: unbekannter Typ „{typ}“")
                    rec.ext_id = f"exodus:{rec.txhash}:{typ}" if rec.txhash else ids(*r.values())
                else:
                    oq, oc = pos(self.n(r.get("outamount"), o)), sym(r.get("outcurrency"))
                    iq, ic = pos(self.n(r.get("inamount"), o)), sym(r.get("incurrency"))
                    fq, fc = pos(self.n(r.get("feeamount"), o)), sym(r.get("feecurrency"))
                    rec.fee_sym, rec.fee_qty = (fc, fq) if nz(fq) and fc else (None, None)
                    rec.txhash = (r.get("outtxid") or r.get("intxid") or "").strip() or None
                    oid = (r.get("orderid") or "").strip()
                    if "failed" in typ:
                        if not rec.fee_qty:
                            res.skip("Exodus: fehlgeschlagen ohne Gebühr")
                            return
                        rec.kind = M.FEE
                    elif typ == "deposit":
                        rec.in_sym, rec.in_qty, rec.fee_sym, rec.fee_qty = ic, iq, None, None
                    elif typ == "withdrawal":
                        rec.kind, rec.out_sym, rec.out_qty = M.WITHDRAWAL, oc, oq
                    elif typ == "exchange":
                        rec.kind, rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = M.TRADE, oc, oq, ic, iq
                    else:
                        raise ValueError(f"Exodus: unbekannter Typ „{typ}“")
                    rec.ext_id = (f"exodus:{oid}" if oid else f"exodus:{rec.txhash}:{typ}" if rec.txhash
                                  else ids(*r.values()))
                if (rec.kind == M.DEPOSIT and not nz(rec.in_qty)) or (rec.kind == M.WITHDRAWAL and not nz(rec.out_qty)):
                    res.skip("Exodus: Zeile mit Menge 0")
                    return
                res.recs.append(rec)

            _guard(res, ln, one)
        return res


# ----------------------------------------------------------------------------------------------------
# Eigenes Format (Spaltenzuordnung)
# ----------------------------------------------------------------------------------------------------

MAPPING_FIELDS: dict[str, str] = {
    "date": "Datum/Zeit (Pflicht)",
    "type": "Vorgangsart/Label (optional)",
    "out_qty": "Abgang: Menge",
    "out_sym": "Abgang: Währung/Asset",
    "in_qty": "Zugang: Menge",
    "in_sym": "Zugang: Währung/Asset",
    "qty": "Menge mit Vorzeichen (statt Zu-/Abgang)",
    "sym": "Währung/Asset (zur Menge mit Vorzeichen)",
    "fee_qty": "Gebühr: Menge",
    "fee_sym": "Gebühr: Währung/Asset",
    "value": "Gegenwert (optional)",
    "value_ccy": "Währung des Gegenwerts (optional)",
    "account": "Konto/Wallet (optional)",
    "ext_id": "Transaktions-ID (optional, für Wiederholungsimporte)",
    "txhash": "Blockchain-Hash (optional)",
    "note": "Notiz/Beschreibung (optional)",
}


class MappingProfile(Profile):
    """Eigene Spaltenzuordnung (gespeichert in ``csv_mapping``)."""

    group = "Eigenes Format"
    multi_account = True

    def __init__(self, mid: int | None, name: str, spec: dict[str, Any]) -> None:
        self.mid = mid
        self.id = f"mapping:{mid}" if mid else "mapping:new"
        self.label = f"Eigenes Format: {name}"
        self.spec = spec
        self.account = spec.get("account_default") or ""
        self.tz = spec.get("tz") or "UTC"
        self.decimal = spec.get("decimal") or "."
        header = spec.get("header") or []
        self.signatures = (frozenset(norm(h) for h in header),) if header else ()

    def parse(self, t: Table, o: ParseOptions) -> ParseResult:
        res = ParseResult()
        cols: dict[str, str] = {k: norm(v) for k, v in (self.spec.get("columns") or {}).items() if v}
        type_map = {M.norm_label(k): v for k, v in (self.spec.get("type_map") or {}).items()}
        fixed_ccy = (self.spec.get("value_ccy_fixed") or "").strip().upper() or None
        ids = _Ids("map")
        if "date" not in cols:
            res.error(t.header_line, "Zuordnung: Spalte für Datum/Zeit fehlt")
            return res

        def g(r: dict[str, str], f: str) -> str:
            return (r.get(cols[f]) or "").strip() if f in cols else ""

        for ln, r in t.dicts():
            res.rows_read += 1

            def one(ln: int = ln, r: dict[str, str] = r) -> None:
                ts, date_only = parse_ts(g(r, "date"), o.tz or zone(self.tz), o.dayfirst)
                label = g(r, "type")
                oq, oc = pos(self.n(g(r, "out_qty"), o)), sym(g(r, "out_sym"))
                iq, ic = pos(self.n(g(r, "in_qty"), o)), sym(g(r, "in_sym"))
                if "qty" in cols:
                    q = self.n(g(r, "qty"), o)
                    s = sym(g(r, "sym")) or sym(o.default_asset)
                    if q is not None and q > 0:
                        iq, ic = q, s
                    elif q is not None and q < 0:
                        oq, oc = -q, s
                fq, fc = pos(self.n(g(r, "fee_qty"), o)), sym(g(r, "fee_sym"))
                value = pos(self.n(g(r, "value"), o))
                vccy = sym(g(r, "value_ccy")) or fixed_ccy
                mapped = type_map.get(M.norm_label(label)) if label else None
                if mapped == "skip":
                    res.skip(f"Zuordnung: „{label}“ übersprungen")
                    return
                rec = Rec(line=ln, ts=ts, date_only=date_only, kind=M.TRADE, account=g(r, "account") or o.account,
                          label=label or None, note=g(r, "note") or None, txhash=g(r, "txhash") or None,
                          fee_sym=(fc or oc or ic) if nz(fq) else None, fee_qty=fq if nz(fq) else None,
                          value=value if nz(value) and vccy else None, value_ccy=vccy if nz(value) else None)
                rec.ext_id = f"map:{g(r, 'ext_id')}" if g(r, "ext_id") else ids(*r.values())
                has_o, has_i = nz(oq) and oc, nz(iq) and ic
                kind, _, tag = (mapped or "").partition(":")
                if has_o and has_i:
                    rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = oc, oq, ic, iq
                    if kind == "conversion":
                        rec.kind = M.CONVERSION
                elif has_i:
                    rec.kind, rec.in_sym, rec.in_qty = M.DEPOSIT, ic, iq
                    rec.tag = tag or (None if kind == "deposit" else M.income_tag(label))
                elif has_o:
                    rec.kind, rec.out_sym, rec.out_qty = M.WITHDRAWAL, oc, oq
                    rec.tag = tag or (None if kind == "withdrawal" else M.out_tag(label))
                elif rec.fee_qty:
                    rec.kind = M.FEE
                else:
                    res.skip("Zuordnung: Zeile ohne Mengen")
                    return
                res.recs.append(rec)

            _guard(res, ln, one)
        return res


# ----------------------------------------------------------------------------------------------------
# Register & Erkennung
# ----------------------------------------------------------------------------------------------------

BUILTIN: list[Profile] = [BinanceProfile(), BitpandaProfile(), KrakenProfile(), CoinbaseProfile(), CryptoComProfile(),
                          LedgerLiveProfile(), TrezorProfile(), ElectrumProfile(), ExodusProfile(), KoinlyProfile(),
                          KoinlyUniversalProfile(), BlockpitProfile(), CoinTrackingProfile(), PortfoliaProfile()]
PROFILES: dict[str, Profile] = {p.id: p for p in BUILTIN}


def detect(keys: list[str], extra: list[Profile] | None = None) -> Profile | None:
    ks = set(keys)
    best: tuple[int, Profile] | None = None
    for p in [*(extra or []), *BUILTIN]:
        s = p.score(ks)
        if s and (best is None or s > best[0]):
            best = (s, p)
    return best[1] if best else None


def header_matcher(extra: list[Profile] | None = None) -> Callable[[list[str]], bool]:
    profiles = [*(extra or []), *BUILTIN]

    def match(keys: list[str]) -> bool:
        ks = set(unique_norm(keys))
        return any(p.score(ks) for p in profiles)

    return match


def unique_norm(keys: list[str]) -> list[str]:
    from app.csvimport.reader import unique_keys

    return unique_keys(keys)
