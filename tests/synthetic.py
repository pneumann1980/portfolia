"""Synthetischer Großimport (≈5.700 Transaktionen, ≈170 Assets) für Last- und Abnahmetests.

Deterministisch (fester Seed). Alle Vorgänge werden chronologisch erzeugt und Bestände dabei mitgeführt, damit
nie mehr verkauft wird als vorhanden ist; holdings_check wird aus dem Ledger berechnet und muss beim Import
exakt reproduziert werden.
"""

from __future__ import annotations

import heapq
import math
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.importer.zipbuilder import build_zip

START = date(2019, 1, 7)
END = date(2026, 9, 18)
Q8 = Decimal("0.00000001")
C2 = Decimal("0.01")


def _dec(v: float, q: Decimal = Q8) -> Decimal:
    return Decimal(str(v)).quantize(q)


class _Gen:
    def __init__(self, seed: int, extra_coins: int = 0) -> None:
        self.rng = random.Random(seed)
        self.bal: dict[tuple[str, str], Decimal] = {}
        self.rows: list[dict[str, Any]] = []
        self.n = 0
        self.stocks = [f"S{i:03d}" for i in range(40)]
        self.etfs = [f"E{i:02d}" for i in range(8)]
        self.bonds = ["B01", "B02"]
        self.coins = ["BTC", "ETH", "SOL", "BNB", "ADA", "DOT"] + [f"C{i:03d}" for i in range(112)] + [
            f"X{i:03d}" for i in range(extra_coins)]
        securities = self.stocks + self.etfs + self.bonds
        start = {a: self.rng.uniform(20, 300) for a in securities}
        start.update({"BTC": 3500.0, "ETH": 120.0, "SOL": 1.0, "BNB": 6.0, "ADA": 0.04, "DOT": 3.0})
        start.update({c: self.rng.uniform(0.001, 20) for c in self.coins if c.startswith(("C", "X"))})
        days = (END - START).days + 1
        self.paths: dict[str, list[float]] = {}
        for aid in securities + self.coins:
            p, vol, path = start[aid], (0.015 if aid in securities else 0.04), []
            for _ in range(days):
                p *= math.exp(self.rng.gauss(0.0002, vol))
                path.append(max(p, 1e-6))
            self.paths[aid] = path

    def px(self, aid: str, d: date) -> float:
        return self.paths[aid][(d - START).days]

    def have(self, acc: str, asset: str) -> Decimal:
        return self.bal.get((acc, asset), Decimal(0))

    def add(self, d: date, typ: str, *, tag: str = "", frm: tuple = ("", "", ""), to: tuple = ("", "", ""),
            fee: tuple = ("", "", ""), value: Any = "", related: str = "", hour: int = 10) -> None:
        self.n += 1
        self.rows.append({
            "tx_id": f"SYN-{self.n:06d}", "datetime": f"{d.isoformat()}T{hour:02d}:{self.n % 60:02d}:00Z",
            "type": typ, "tag": tag, "from_account": frm[0], "from_asset": frm[1], "from_qty": str(frm[2]),
            "to_account": to[0], "to_asset": to[1], "to_qty": str(to[2]), "fee_asset": fee[0],
            "fee_qty": str(fee[1]), "fee_eur": str(fee[2]), "value_eur": str(value), "related_asset": related})
        for acc, asset, q, sign in ((frm[0], frm[1], frm[2], -1), (to[0], to[1], to[2], 1)):
            if acc and asset and q != "":
                self.bal[(acc, asset)] = self.have(acc, asset) + sign * Decimal(str(q))
        if fee[0] and fee[1] != "":
            acc = frm[0] or to[0]
            self.bal[(acc, fee[0])] = self.have(acc, fee[0]) - Decimal(str(fee[1]))

    # -- Ereignisse ----------------------------------------------------------------------------------
    def deposit(self, d: date, acc: str) -> None:
        self.add(d, "deposit", to=(acc, "EUR", 2500), value=2500, hour=7)

    def savings_plan(self, d: date, e: str) -> None:
        eur = Decimal(self.rng.choice([50, 100, 150, 200]))
        q = _dec(float(eur) / self.px(e, d), Decimal("0.001"))
        if q > 0:
            self.add(d, "buy", frm=("Depot DE", "EUR", eur), to=("Depot DE", e, q), value=eur)

    def stock_trade(self, d: date) -> None:
        s = self.rng.choice(self.stocks)
        acc = self.rng.choice(["Depot DE", "Depot Neo", "Depot Ausland"])
        price = self.px(s, d)
        if self.rng.random() < 0.35 and self.have(acc, s) > 1:
            q = max(Decimal(1), (self.have(acc, s) * Decimal(str(self.rng.uniform(0.2, 0.8)))).quantize(Decimal(1)))
            v = _dec(float(q) * price, C2)
            self.add(d, "sell", frm=(acc, s, q), to=(acc, "EUR", v - Decimal("4.90")), value=v,
                     fee=("EUR", "4.90", "4.90"), hour=14)
        elif acc != "Depot Neo" or self.have(acc, "EUR") > 3000:
            q = Decimal(self.rng.randint(1, 30))
            v = _dec(float(q) * price, C2)
            if acc == "Depot Neo" and self.have(acc, "EUR") < v + 5:
                return
            self.add(d, "buy", frm=(acc, "EUR", v), to=(acc, s, q), value=v, fee=("EUR", "4.90", "4.90"), hour=14)

    def dividend(self, d: date, s: str) -> None:
        accs = [a for a in ("Depot DE", "Depot Neo", "Depot Ausland") if self.have(a, s) > 0]
        if not accs or self.rng.random() < 0.45:
            return
        acc = accs[0]
        gross = _dec(float(self.have(acc, s)) * self.px(s, d) * 0.006, C2)
        if gross <= 0:
            return
        self.add(d, "deposit", tag="dividend", to=(acc, "EUR", gross), value=gross, related=s, hour=8)
        if int(s[1:]) % 2 == 0:  # US-Titel: 15 % Quellensteuer
            wht = (gross * Decimal("0.15")).quantize(C2)
            self.add(d, "withdrawal", tag="withholding_tax", frm=(acc, "EUR", wht), value=wht, related=s, hour=8)

    def split(self, d: date, s: str, ratio: int) -> None:
        for acc in ("Depot DE", "Depot Neo", "Depot Ausland"):
            q = self.have(acc, s)
            if q > 0:
                self.add(d, "corporate_action", tag="split", frm=(acc, s, q), to=(acc, s, q * ratio), hour=6)

    def crypto(self, d: date) -> None:
        acc = self.rng.choice(["Börse A", "Börse B"])
        c = self.rng.choice(self.coins[:40] if self.rng.random() < 0.7 else self.coins)
        price = self.px(c, d)
        r = self.rng.random()
        if r < 0.55:
            eur = Decimal(self.rng.choice([25, 50, 100, 250, 500]))
            if self.have(acc, "EUR") < eur + 2:
                return
            self.add(d, "buy", frm=(acc, "EUR", eur), to=(acc, c, _dec(float(eur) / price)), value=eur,
                     fee=("EUR", "1.00", "1.00"), hour=12)
        elif r < 0.8:
            q = (self.have(acc, c) * Decimal(str(self.rng.uniform(0.1, 0.6)))).quantize(Q8)
            v = _dec(float(q) * price, C2)
            if q > 0 and v >= 1:
                self.add(d, "sell", frm=(acc, c, q), to=(acc, "EUR", v - Decimal("0.50")), value=v,
                         fee=("EUR", "0.50", "0.50"), hour=12)
        else:
            other = self.rng.choice(self.coins[:40])
            q = (self.have(acc, c) * Decimal(str(self.rng.uniform(0.2, 0.7)))).quantize(Q8)
            v = _dec(float(q) * price, C2)
            if other == c or q <= 0 or v < 5:
                return
            q2 = _dec(float(v) / self.px(other, d))
            fee_bnb = _dec(0.5 / self.px("BNB", d))
            if c != "BNB" and self.have(acc, "BNB") > fee_bnb * 3:
                self.add(d, "trade", frm=(acc, c, q), to=(acc, other, q2), value=v, fee=("BNB", fee_bnb, "0.50"),
                         hour=12)
            else:
                self.add(d, "trade", frm=(acc, c, q), to=(acc, other, q2), value=v, hour=12)

    def transfer(self, d: date) -> None:
        acc = self.rng.choice(["Börse A", "Börse B"])
        c = self.rng.choice(["BTC", "ETH", "SOL", "ADA", "DOT"])
        q = (self.have(acc, c) * Decimal("0.5")).quantize(Q8)
        fee_q = (q * Decimal("0.001")).quantize(Q8)
        if q <= fee_q or fee_q <= 0:
            return
        dst = self.rng.choice(["Wallet 1", "Wallet 2", "Wallet 3"])
        self.add(d, "transfer", frm=(acc, c, q - fee_q), to=(dst, c, q - fee_q),
                 fee=(c, fee_q, _dec(float(fee_q) * self.px(c, d), C2)), hour=16)

    def staking(self, d: date, c: str) -> None:
        q = (self.have("Börse A", c) * Decimal("0.004")).quantize(Q8)
        if q > 0:
            self.add(d, "deposit", tag="staking", to=("Börse A", c, q), value=_dec(float(q) * self.px(c, d), C2),
                     hour=2)


def generate(seed: int = 42, scale: int = 1) -> dict[str, Any]:
    """``scale`` > 1: mehr Handel, Transfers und Coins (Lasttest der Integritätsprüfung); 1 = unverändert."""
    g = _Gen(seed, extra_coins=0 if scale <= 1 else 60)
    rng = g.rng
    days = [START + timedelta(days=i) for i in range((END - START).days + 1)]
    trading = [x for x in days if x.weekday() < 5]
    ev: list[tuple[date, int, Any, tuple]] = []
    k = 0

    def push(d: date, fn: Any, *args: Any) -> None:
        nonlocal k
        k += 1
        ev.append((d, k, fn, args))

    for i in range(0, len(days), 30):
        for acc in ("Börse A", "Börse B", "Depot Neo"):
            push(days[i], g.deposit, acc)
    for i in range(0, len(trading), 21):
        for e in g.etfs:
            push(trading[i], g.savings_plan, e)
    for _ in range(900 * scale):
        push(rng.choice(trading), g.stock_trade)
    push(date(2021, 3, 1), lambda d: [g.add(d, "buy", frm=("Depot Ausland", "EUR", 5000),
                                             to=("Depot Ausland", b, 50), value=5000) for b in g.bonds])
    push(date(2024, 3, 1), lambda d: [g.add(d, "sell", frm=("Depot Ausland", b, 50),
                                             to=("Depot Ausland", "EUR", 4700), value=4700) for b in g.bonds])
    for s in g.stocks[:30]:
        for y in range(2019, 2027):
            for m in (3, 6, 9, 12):
                if date(y, m, 15) <= END:
                    push(date(y, m, 15), g.dividend, s)
    push(date(2022, 7, 18), g.split, "S004", 4)
    push(date(2024, 6, 10), g.split, "S011", 10)
    for _ in range(2600 * scale):
        push(rng.choice(days), g.crypto)
    for _ in range(170 * scale):
        push(rng.choice(days), g.transfer)
    for c in ("ETH", "SOL", "ADA", "DOT", "C001", "C002"):
        d = date(2019, 2, 1)
        while d <= END:
            push(d, g.staking, c)
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    heapq.heapify(ev)
    while ev:
        d, _, fn, args = heapq.heappop(ev)
        fn(d, *args)
    assets: list[dict[str, Any]] = [
        {"asset_id": "EUR", "name": "Euro", "asset_class": "fiat", "quote_source": "none", "quote_id": ""},
        {"asset_id": "USD", "name": "US-Dollar", "asset_class": "fiat", "quote_source": "none", "quote_id": ""},
    ]
    for s in g.stocks:
        us = int(s[1:]) % 2 == 0
        assets.append({"asset_id": s, "name": f"Aktie {s}", "asset_class": "security", "quote_source": "yahoo",
                       "quote_id": s if us else f"{s}.DE", "isin": ("US" if us else "DE") + f"00000{s[1:]}0001",
                       "category": "Aktien: Einzeltitel"})
    for e in g.etfs:
        assets.append({"asset_id": e, "name": f"Index ETF {e}", "asset_class": "security", "quote_source": "yahoo",
                       "quote_id": f"{e}.DE", "isin": f"IE00000{e[1:]}00001", "category": "Aktien: ETF",
                       "tax_type": "etf_equity" if e != "E07" else "etf_mixed"})
    for b in g.bonds:
        assets.append({"asset_id": b, "name": f"Anleihe {b}", "asset_class": "security", "quote_source": "none",
                       "quote_id": "", "isin": f"DE00000{b[1:]}00001", "category": "Anleihen", "tax_type": "bond"})
    for c in g.coins:
        idx = int(c[1:]) if c.startswith("C") else (int(c[1:]) % 100 if c.startswith("X") else -1)
        src = "coingecko" if idx < 90 else ("manual" if idx < 100 else "none")
        assets.append({"asset_id": c, "name": f"Coin {c}", "asset_class": "crypto", "quote_source": src,
                       "quote_id": c.lower() if src == "coingecko" else "",
                       "category": "Krypto: BTC/ETH" if c in ("BTC", "ETH") else "Krypto: Altcoins"})
    accounts = [
        {"account": "Depot DE", "broker": "Hausbank", "depot_group": "Depot 1"},
        {"account": "Depot Neo", "broker": "Neobroker", "depot_group": "Depot 2"},
        {"account": "Depot Ausland", "broker": "Auslandsbroker", "depot_group": "Depot 3",
         "tax_withholding": "foreign"},
        *({"account": f"Börse {x}", "broker": f"Exchange {x}", "depot_group": "Krypto"} for x in "AB"),
        *({"account": f"Wallet {x}", "broker": "Self-Custody", "depot_group": "Krypto"} for x in "123"),
    ]
    manual = [{"asset_id": c, "date": d.isoformat(), "price_eur": f"{g.px(c, d):.6f}", "source": "synthetisch"}
              for c in g.coins if c.startswith("C") and 90 <= int(c[1:]) < 100 for d in (date(2025, 1, 2), END)]
    return {"assets": assets, "accounts": accounts, "rows": g.rows, "manual": manual}


def write_zip(path: Path, seed: int = 42, scale: int = 1,
              mutate: Any = None) -> tuple[Path, int, int]:
    """``mutate(rows)``: Zeilen vor dem Schreiben ergänzen/ändern (z. B. Dubletten für Lasttests)."""
    from app.importer.validate import parse_decimal
    from app.ledger.engine import run_ledger
    from app.ledger.models import AccountInfo, AssetInfo, Portfolio, Tx
    from app.util.timeutil import parse_tx_datetime, to_local_date

    g = generate(seed, scale)
    if mutate is not None:
        mutate(g["rows"])
    txs = []
    for seq, r in enumerate(g["rows"]):
        ts, date_only = parse_tx_datetime(r["datetime"])
        txs.append(Tx(seq=seq, tx_id=r["tx_id"], ts=ts, date=to_local_date(ts), date_only=date_only, type=r["type"],
                      tag=r["tag"] or None, from_account=r["from_account"] or None, from_asset=r["from_asset"] or None,
                      from_qty=parse_decimal(r["from_qty"]), to_account=r["to_account"] or None,
                      to_asset=r["to_asset"] or None, to_qty=parse_decimal(r["to_qty"]),
                      fee_asset=r["fee_asset"] or None, fee_qty=parse_decimal(r["fee_qty"]),
                      fee_eur=parse_decimal(r["fee_eur"]), value_eur=parse_decimal(r["value_eur"]),
                      related_asset=r["related_asset"] or None))
    pf = Portfolio(import_id=None, txs=txs, assets={a["asset_id"]: AssetInfo.from_parsed(a) for a in g["assets"]},
                   accounts={a["account"]: AccountInfo.from_parsed(a) for a in g["accounts"]})
    res = run_ledger(pf)
    holdings = [{"asset_id": asset, "account": acc, "qty": format(q.normalize(), "f"), "as_of": END.isoformat()}
                for (acc, asset), q in sorted(res.balances.items()) if not pf.assets[asset].is_fiat]
    build_zip(path, transactions=g["rows"], assets=g["assets"], holdings_check=holdings, issues=[],
              manual_prices=g["manual"], accounts=g["accounts"], extra_tx_columns=["related_asset"],
              generated_at="2026-09-19T06:00:00Z", valuation_date=END.isoformat(), notes="Synthetischer Lasttest")
    return path, len(g["rows"]), len(g["assets"])
