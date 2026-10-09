"""Binance (Spot-API, nur lesend) – strenger Mock nach der offiziellen Doku, synthetische Daten, ohne Netz.

Der Mock (``FakeBinance``) prüft jede signierte Anfrage wie Binance: ``X-MBX-APIKEY``, ``timestamp``/``recvWindow``,
HMAC-SHA256 über den Query-String, nur ``GET``, nur dokumentierte Pfade und Parameter, Höchstfenster je Endpunkt
(Ein-/Auszahlungen < 90 Tage, Ausschüttungen ≤ 180 Tage, Convert ≤ 30 Tage), ``limit``-Grenzen und Paging
(``fromId``, ``offset``, ``page``/``rows``, ``moreData``). Abgedeckt: Abbildung aller Vorgangsarten, ausstehende
Vorgänge halten den Abrufstand, Etappen mit Zeitbudget ohne Verlust/Dubletten, 429 mit ``Retry-After``, -1021
(Zeitabgleich), Ablehnung des Schlüssels, Warnung bei Handelsrechten, Schlüsselablage (Key + Secret, Hinweis nur aus
dem API-Key).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from app.datasources import binance as B
from app.datasources.service import datasource_service, join_key, key_hint
from tests.wallet_fakes import all_rows, ctx, make_client, post, source

D = Decimal
MASTER = base64.b64encode(bytes(range(32))).decode()
API_KEY = "bnTESTkey0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTU"
SECRET = "bnTESTsecretZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvutsrqponmlkjihgfedcba98"
DAY = 86_400_000
NOW = int(time.time() * 1000)


def ago(days: float) -> int:
    return int(NOW - days * DAY)


def utc_text(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000))


SIGNED = {"/api/v3/account", "/api/v3/myTrades", "/sapi/v1/capital/deposit/hisrec", "/sapi/v1/capital/withdraw/history",
          "/sapi/v1/asset/assetDividend", "/sapi/v1/asset/dribblet", "/sapi/v1/convert/tradeFlow",
          "/sapi/v1/fiat/payments", "/sapi/v1/fiat/orders"}
PARAMS = {"/api/v3/account": {"omitZeroBalances"}, "/api/v3/myTrades": {"symbol", "fromId", "limit"},
          "/sapi/v1/capital/deposit/hisrec": {"startTime", "endTime", "offset", "limit"},
          "/sapi/v1/capital/withdraw/history": {"startTime", "endTime", "offset", "limit"},
          "/sapi/v1/asset/assetDividend": {"startTime", "endTime", "limit"},
          "/sapi/v1/asset/dribblet": {"startTime", "endTime"},
          "/sapi/v1/convert/tradeFlow": {"startTime", "endTime", "limit"},
          "/sapi/v1/fiat/payments": {"transactionType", "beginTime", "endTime", "page", "rows"},
          "/sapi/v1/fiat/orders": {"transactionType", "beginTime", "endTime", "page", "rows"}}


def trade(sym: str, tid: int, qty: str, quote: str, buyer: bool, days: float, fee: str = "0",
          fee_asset: str = "BNB") -> dict:
    return {"symbol": sym, "id": tid, "orderId": 1000 + tid, "orderListId": -1, "price": "1", "qty": qty,
            "quoteQty": quote, "commission": fee, "commissionAsset": fee_asset, "time": ago(days), "isBuyer": buyer,
            "isMaker": False, "isBestMatch": True}


def data() -> dict[str, Any]:
    return {
        "balances": [{"asset": "BTC", "free": "0.4", "locked": "0.1"}, {"asset": "EUR", "free": "100", "locked": "0"},
                     {"asset": "BNB", "free": "0.01", "locked": "0"}],
        "canTrade": False,
        "symbols": [("BTCEUR", "BTC", "EUR"), ("ETHBTC", "ETH", "BTC"), ("BNBEUR", "BNB", "EUR"),
                    ("XRPEUR", "XRP", "EUR"), ("BTCUSDT", "BTC", "USDT")],
        "trades": {"BTCEUR": [trade("BTCEUR", 1, "0.1", "3000", True, 400, "0.001"),
                              trade("BTCEUR", 2, "0.05", "1600", False, 300, "0.8", "EUR"),
                              trade("BTCEUR", 3, "0.02", "700", True, 20)],
                   "ETHBTC": [trade("ETHBTC", 7, "1", "0.05", False, 100, "0.00005", "BTC")]},
        "deposits": [
            {"id": "d1", "amount": "0.5", "coin": "BTC", "network": "BTC", "status": 1, "address": "bc1qsecret",
             "addressTag": "", "txId": "ab" * 32, "insertTime": ago(500), "completeTime": ago(500),
             "transferType": 0, "confirmTimes": "2/2", "unlockConfirm": 0, "walletType": 0},
            {"id": "d2", "amount": "0.01", "coin": "BTC", "network": "BTC", "status": 0, "address": "bc1qsecret",
             "addressTag": "", "txId": "cd" * 32, "insertTime": ago(5), "transferType": 0, "walletType": 0},
            {"id": "d3", "amount": "9", "coin": "BTC", "network": "BTC", "status": 7, "address": "bc1qsecret",
             "addressTag": "", "txId": "ef" * 32, "insertTime": ago(200), "transferType": 0, "walletType": 0}],
        "withdrawals": [
            {"id": "w1", "amount": "0.9", "transactionFee": "0.002", "coin": "ETH", "status": 6,
             "address": "0xsecret", "txId": "0x" + "12" * 32, "applyTime": utc_text(ago(90)),
             "completeTime": utc_text(ago(90) + 60_000), "network": "ETH", "transferType": 0},
            {"id": "w2", "amount": "1", "transactionFee": "0.002", "coin": "ETH", "status": 4,
             "address": "0xsecret", "txId": "", "applyTime": utc_text(ago(2)), "network": "ETH",
             "transferType": 0}],
        "dividends": [
            {"id": 1, "amount": "0.0001", "asset": "BTC", "divTime": ago(150),
             "enInfo": "Simple Earn Flexible Interest", "tranId": 501, "direction": 1},
            {"id": 2, "amount": "0.002", "asset": "BNB", "divTime": ago(149), "enInfo": "BNB Vault", "tranId": 502,
             "direction": 1},
            {"id": 3, "amount": "0.003", "asset": "BNB", "divTime": ago(148), "enInfo": "Launchpool", "tranId": 503,
             "direction": 1}],
        "dust": [{"operateTime": ago(250), "totalTransferedAmount": "0.0012", "totalServiceChargeAmount": "0.00002",
                  "transId": 9001, "userAssetDribbletDetails": [
                      {"transId": 9001, "serviceChargeAmount": "0.00001", "amount": "3", "operateTime": ago(250),
                       "transferedAmount": "0.0007", "fromAsset": "XRP"},
                      {"transId": 9001, "serviceChargeAmount": "0.00001", "amount": "1.5", "operateTime": ago(250),
                       "transferedAmount": "0.0005", "fromAsset": "ADA"}]}],
        "converts": [
            {"quoteId": "q1", "orderId": 70001, "orderStatus": "SUCCESS", "fromAsset": "EUR", "fromAmount": "50",
             "toAsset": "BNB", "toAmount": "0.1", "ratio": "0.002", "inverseRatio": "500", "createTime": ago(60)},
            {"quoteId": "q2", "orderId": 70002, "orderStatus": "PROCESS", "fromAsset": "EUR", "fromAmount": "10",
             "toAsset": "BNB", "toAmount": "0.02", "ratio": "0.002", "inverseRatio": "500",
             "createTime": ago(59)}],
        "fiat_payments": {"0": [{"orderNo": "fp1", "sourceAmount": "200", "fiatCurrency": "EUR",
                                 "obtainAmount": "0.004", "cryptoCurrency": "BTC", "totalFee": "3.5",
                                 "price": "50000", "status": "Completed", "paymentMethod": "Card",
                                 "createTime": ago(700), "updateTime": ago(700)}], "1": []},
        "fiat_orders": {"0": [{"orderNo": "fo1", "fiatCurrency": "EUR", "indicatedAmount": "1000", "amount": "1000",
                               "totalFee": "0", "method": "SEPA", "status": "Successful",
                               "createTime": ago(800), "updateTime": ago(800)}], "1": []},
    }


class Clock:
    """Simulierte Zeit: Wartezeiten und Anfragen rücken sie vor (Etappen ohne echtes Warten)."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


class FakeBinance:
    def __init__(self, d: dict[str, Any], clock: Clock) -> None:
        self.d, self.clock = d, clock
        self.calls: list[httpx.Request] = []
        self.inject: list[tuple[str, httpx.Response]] = []
        self.convert_cap = 1000
        self.skew_ms = 0

    def err(self, status: int, code: int, msg: str) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "msg": msg})

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        self.clock.t += 0.05
        assert req.method == "GET", "nur lesende Aufrufe"
        assert req.url.scheme == "https" and req.url.host == "api.binance.com"
        path = req.url.path
        raw = req.url.query.decode()
        for i, (p, resp) in enumerate(self.inject):
            if p == path:
                del self.inject[i]
                return resp
        if path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": int(time.time() * 1000) + self.skew_ms})
        if path == "/api/v3/exchangeInfo":
            assert "X-MBX-APIKEY" not in req.headers and not raw
            return httpx.Response(200, json={"timezone": "UTC", "symbols": [
                {"symbol": s, "status": "TRADING", "baseAsset": b, "quoteAsset": q} for s, b, q in self.d["symbols"]]})
        assert path in SIGNED, f"nicht dokumentierter Pfad {path}"
        # -- Signatur wie Binance: HMAC-SHA256(Secret, Query ohne signature) hex, Key im Header
        assert req.headers.get("X-MBX-APIKEY") == API_KEY
        body, _, sig = raw.rpartition("&signature=")
        assert sig == hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest(), "Signatur falsch"
        q = dict(parse_qsl(body))
        assert SECRET not in raw and SECRET not in str(req.headers)
        assert int(q.pop("recvWindow")) <= 60000
        ts = int(q.pop("timestamp"))
        if abs(ts - (int(time.time() * 1000) + self.skew_ms)) > 10000:
            return self.err(400, -1021, "Timestamp for this request is outside of the recvWindow.")
        assert set(q) <= PARAMS[path], f"unbekannte Parameter {set(q) - PARAMS[path]} für {path}"
        return getattr(self, "r_" + path.rsplit("/", 1)[-1].replace("-", "_"))(q)

    @staticmethod
    def ok(body: Any) -> httpx.Response:
        return httpx.Response(200, json=body, headers={"x-mbx-used-weight-1m": "20"})

    def window(self, q: dict[str, str], max_days: float, *, strict: bool = True, start: str = "startTime"
               ) -> tuple[int, int]:
        if start not in q and "endTime" not in q:  # laut Doku optional: Standardzeitraum
            return int(time.time() * 1000) - int(max_days * DAY) + 1, int(time.time() * 1000)
        a, b = int(q[start]), int(q["endTime"])
        assert a <= b
        span = b - a
        assert (span < max_days * DAY) if strict else (span <= max_days * DAY), f"Fenster zu groß: {span / DAY:.1f} d"
        return a, b

    def r_account(self, q):
        return self.ok({"canTrade": self.d["canTrade"], "canWithdraw": False, "canDeposit": True,
                        "accountType": "SPOT", "permissions": ["SPOT"], "balances": self.d["balances"]})

    def r_myTrades(self, q):
        sym, frm, lim = q["symbol"], int(q.get("fromId", 0)), int(q.get("limit", 500))
        assert lim <= 1000
        if sym not in {s for s, *_ in self.d["symbols"]}:
            return self.err(400, -1121, "Invalid symbol.")
        rows = sorted((t for t in self.d["trades"].get(sym, []) if t["id"] >= frm), key=lambda t: t["id"])
        return self.ok(rows[:lim])

    def r_hisrec(self, q):
        a, b = self.window(q, 90)
        off, lim = int(q.get("offset", 0)), int(q.get("limit", 1000))
        assert lim <= 1000
        rows = [r for r in self.d["deposits"] if a <= r["insertTime"] <= b]
        return self.ok(rows[off:off + lim])

    def r_history(self, q):
        a, b = self.window(q, 90)
        off, lim = int(q.get("offset", 0)), int(q.get("limit", 1000))
        assert lim <= 1000
        rows = [r for r in self.d["withdrawals"]
                if a <= B._utc_text(r["applyTime"]).timestamp() * 1000 <= b]  # type: ignore[union-attr]
        return self.ok(rows[off:off + lim])

    def r_assetDividend(self, q):
        a, b = self.window(q, 180, strict=False)
        lim = int(q.get("limit", 20))
        assert lim <= 500
        rows = sorted((r for r in self.d["dividends"] if a <= r["divTime"] <= b), key=lambda r: -r["divTime"])
        return self.ok({"rows": rows[:lim], "total": len(rows)})

    def r_dribblet(self, q):
        a, b = int(q["startTime"]), int(q["endTime"])
        rows = [r for r in self.d["dust"] if a <= r["operateTime"] <= b][:100]
        return self.ok({"total": len(rows), "userAssetDribblets": rows})

    def r_tradeFlow(self, q):
        a, b = self.window(q, 30, strict=False)
        lim = min(int(q.get("limit", 100)), self.convert_cap)
        assert int(q.get("limit", 100)) <= 1000
        rows = [r for r in self.d["converts"] if a <= r["createTime"] <= b]
        return self.ok({"list": rows[:lim], "startTime": a, "endTime": b, "limit": lim,
                        "moreData": len(rows) > lim})

    def _fiat(self, q, key):
        a, b = self.window(q, 90, start="beginTime")
        page, rows_n = int(q.get("page", 1)), int(q.get("rows", 100))
        assert page >= 1 and rows_n <= 500
        rows = [r for r in self.d[key][q["transactionType"]] if a <= r["createTime"] <= b]
        return self.ok({"code": "000000", "message": "success", "data": rows[(page - 1) * rows_n:page * rows_n],
                        "total": len(rows), "success": True})

    def r_payments(self, q):
        return self._fiat(q, "fiat_payments")

    def r_orders(self, q):
        return self._fiat(q, "fiat_orders")

    def paths(self, path: str) -> list[httpx.Request]:
        return [c for c in self.calls if c.url.path == path]


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(B.BinanceConnector, "clock", staticmethod(c))
    monkeypatch.setattr(B.BinanceConnector, "sleep", staticmethod(c.sleep))
    return c


@pytest.fixture
def bn(monkeypatch, clock):
    fake = FakeBinance(data(), clock)
    monkeypatch.setattr(B.BinanceConnector, "transport", httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def create(c, *, key: str = API_KEY, secret: str | None = SECRET) -> int:
    form = {"kind": "exchange", "provider": "binance", "name": "Binance", "account": "Binance",
            "sync_interval_min": "0", "api_key": key}
    if secret is not None:
        form["api_secret"] = secret
    r = post(c, "/settings/datasources", **form)
    assert r.status_code == 303, r.text[:800]
    return int(re.search(r"/settings/datasources/(\d+)", r.headers["location"]).group(1))


def sync(c, sid):
    return datasource_service(ctx(c)).sync(sid, "manual")


def sync_all(c, sid, limit: int = 30) -> tuple[dict, int]:
    """Erstabruf in Etappen bis „synced“ (Zeitbudget je Lauf)."""
    for n in range(1, limit + 1):
        res = sync(c, sid)
        if res.get("status") != "partial":
            return res, n
    raise AssertionError("kein Abschluss")


def recs(c, sid) -> dict[str, Any]:
    return {k: v.rec for k, v in all_rows(c, sid).items()}


def test_key_parts_are_joined_and_hint_never_comes_from_secret():
    assert join_key("binance", API_KEY, SECRET) == (f"{API_KEY}:{SECRET}", [])
    assert join_key("binance", f"{API_KEY}:{SECRET}", None) == (f"{API_KEY}:{SECRET}", [])
    assert join_key("binance", API_KEY, None)[1] and join_key("binance", API_KEY, "x")[1]
    assert join_key("bitpanda", API_KEY, SECRET)[1]  # kein Secret bei anderen Anbietern
    assert key_hint("binance", f"{API_KEY}:{SECRET}") == API_KEY[-4:]
    with pytest.raises(B.K.ConnectorError):
        B.split_secret(API_KEY)


def test_full_sync_signed_and_mapped(client, bn):
    sid = create(client)
    hint = ctx(client).db.scalar("SELECT hint FROM data_source_secret WHERE source_id=?", (sid,))
    assert hint == API_KEY[-4:]
    res, stages = sync_all(client, sid)
    assert res.get("status") == "synced" and stages >= 2, res  # Auszahlungen ab 2017: 7 s je 89-Tage-Fenster
    r = recs(client, sid)
    t1 = r["binance:trade:BTCEUR:1#0"]
    assert (t1.kind, t1.out_sym, t1.out_qty, t1.in_sym, t1.in_qty) == ("trade", "EUR", D(3000), "BTC", D("0.1"))
    assert (t1.fee_sym, t1.fee_qty, t1.fee_basis, t1.value, t1.value_ccy) == ("BNB", D("0.001"), "extra", D(3000),
                                                                              "EUR")
    t2 = r["binance:trade:BTCEUR:2#0"]
    assert (t2.out_sym, t2.out_qty, t2.in_sym, t2.in_qty, t2.fee_sym) == ("BTC", D("0.05"), "EUR", D(1600), "EUR")
    # ETH kennt Portfolia erst aus der Auszahlung (Phase C) → Paar ETHBTC im zweiten Durchgang
    t7 = r["binance:trade:ETHBTC:7#0"]
    assert (t7.out_sym, t7.out_qty, t7.in_sym, t7.in_qty) == ("ETH", D(1), "BTC", D("0.05"))
    dep = r["binance:dep:d1#0"]
    assert (dep.kind, dep.in_sym, dep.in_qty, dep.txhash) == ("deposit", "BTC", D("0.5"), "ab" * 32)
    assert "binance:dep:d2#0" not in r and "binance:dep:d3#0" not in r  # ausstehend bzw. abgelehnt
    wd = r["binance:wd:w1#0"]
    assert (wd.kind, wd.out_sym, wd.out_qty, wd.fee_qty, wd.fee_basis) == ("withdrawal", "ETH", D("0.9"),
                                                                           D("0.002"), "open")
    assert wd.review and wd.txhash == "0x" + "12" * 32 and "binance:wd:w2#0" not in r
    assert "address" not in (wd.raw or {}) and "address" not in (dep.raw or {})  # Adressen nie gespeichert
    assert (r["binance:div:501#0"].tag, r["binance:div:501#0"].review) == ("interest", None)
    assert r["binance:div:502#0"].tag == "other_income" and "Ertragsart" in r["binance:div:502#0"].review
    assert r["binance:div:503#0"].tag == "airdrop"
    d0, d1 = r["binance:dust:9001#0"], r["binance:dust:9001#1"]
    assert (d0.out_sym, d0.in_sym, d0.in_qty, d1.out_sym) == ("ADA", "BNB", D("0.0005"), "XRP") and d0.review
    cv = r["binance:convert:70001#0"]
    assert (cv.out_sym, cv.out_qty, cv.in_sym, cv.in_qty, cv.value) == ("EUR", D(50), "BNB", D("0.1"), D(50))
    assert "binance:convert:70002#0" not in r
    fp = r["binance:fiatpay:fp1#0"]
    assert (fp.out_sym, fp.out_qty, fp.in_sym, fp.in_qty, fp.fee_qty, fp.fee_basis) == ("EUR", D(200), "BTC",
                                                                                       D("0.004"), D("3.5"), "open")
    fo = r["binance:fiat:fo1#0"]
    assert (fo.kind, fo.in_sym, fo.in_qty) == ("deposit", "EUR", D(1000))
    bal = {row["asset_key"]: row["qty"] for row in ctx(client).db.q(
        "SELECT asset_key, qty FROM ds_balance WHERE source_id=?", (sid,))}
    assert bal == {"BTC": "0.5", "EUR": "100", "BNB": "0.01"}
    assert "BTCUSDT" not in {c.url.params["symbol"] for c in bn.paths("/api/v3/myTrades")}  # USDT unbekannt
    assert "XRPEUR" in {c.url.params["symbol"] for c in bn.paths("/api/v3/myTrades")}  # XRP aus dem Staubumtausch
    cur = __import__("json").loads(source(client, sid)["cursor_json"])
    assert cur["streams"]["deposit"] <= ago(5) - B.OVERLAP_MS + 1000  # ausstehende Einzahlung hält den Stand
    assert cur["trades"]["BTCEUR"] == 4 and "sweep_after" not in cur


def test_incremental_run_books_pending_once_and_no_duplicates(client, bn):
    sid = create(client)
    sync_all(client, sid)
    n = len(recs(client, sid))
    bn.calls.clear()
    bn.d["deposits"][1]["status"] = 1  # d2 jetzt gutgeschrieben
    bn.d["trades"]["BTCEUR"].append(trade("BTCEUR", 4, "0.01", "350", True, 1))
    res = sync(client, sid)
    assert res.get("status") == "synced", res
    r = recs(client, sid)
    assert "binance:dep:d2#0" in r and "binance:trade:BTCEUR:4#0" in r and len(r) == n + 2
    froms = {c.url.params["symbol"]: int(c.url.params["fromId"]) for c in bn.paths("/api/v3/myTrades")}
    assert froms["BTCEUR"] == 4 and froms["ETHBTC"] == 8
    assert len(bn.paths("/sapi/v1/capital/deposit/hisrec")) == 1  # nur das letzte Fenster
    sync(client, sid)
    assert len(recs(client, sid)) == n + 2


def test_stages_with_time_budget_resume_without_loss(client, bn, clock, monkeypatch):
    monkeypatch.setattr(B.BinanceConnector, "deadline_s", 10_000.0)
    sid0 = create(client)
    res, stages = sync_all(client, sid0)
    assert res.get("status") == "synced" and stages == 1
    full = set(recs(client, sid0))
    datasource_service(ctx(client)).delete(sid0)
    monkeypatch.setattr(B.BinanceConnector, "deadline_s", 45.0)  # Auszahlungen: 7 s Abstand je Fenster
    sid = create(client)
    res, stages = sync_all(client, sid, 60)
    assert res.get("status") == "synced" and stages > 5, (res, stages)
    assert set(recs(client, sid)) == full


def test_dividend_and_convert_windows_split_instead_of_guessing_order(client, bn, monkeypatch):
    monkeypatch.setattr(B, "DIV_LIMIT", 2)
    bn.convert_cap = 1
    bn.d["converts"][1]["orderStatus"] = "SUCCESS"
    sid = create(client)
    assert sync_all(client, sid)[0].get("status") == "synced"
    r = recs(client, sid)
    assert {"binance:div:501#0", "binance:div:502#0", "binance:div:503#0"} <= set(r)
    assert {"binance:convert:70001#0", "binance:convert:70002#0"} <= set(r)
    spans = [int(c.url.params["endTime"]) - int(c.url.params["startTime"])
             for c in bn.paths("/sapi/v1/asset/assetDividend")]
    assert min(spans) < 180 * DAY / 2  # Fenster geteilt


def test_rate_limit_retry_after_and_timestamp_resync(client, bn, clock):
    sid = create(client)
    bn.inject.append(("/sapi/v1/capital/deposit/hisrec",
                      httpx.Response(429, headers={"Retry-After": "3"}, json={"code": -1003, "msg": "Too many"})))
    res, _ = sync_all(client, sid)
    assert res.get("status") == "synced", res
    assert 3.0 in clock.slept
    # Uhr des Servers weicht ab: -1021 → Zeitabgleich und Wiederholung
    bn.skew_ms = 30_000
    res = sync(client, sid)
    assert res.get("status") == "synced", res


def test_ban_418_defers_without_losing_progress(client, bn):
    sid = create(client)
    bn.inject.append(("/sapi/v1/capital/withdraw/history",
                      httpx.Response(418, headers={"Retry-After": "120"}, json={"code": -1003, "msg": "banned"})))
    res = sync(client, sid)
    assert res.get("status") == "partial" and "gesperrt" in res["message"], res
    cur = __import__("json").loads(source(client, sid)["cursor_json"])
    assert "deposit" in cur["streams"] and "withdraw" not in cur["streams"]
    assert sync_all(client, sid)[0].get("status") == "synced"
    assert "binance:wd:w1#0" in recs(client, sid)


def test_rejected_key_is_an_auth_error(client, bn):
    sid = create(client)
    bn.inject.append(("/api/v3/account", httpx.Response(401, json={
        "code": -2015, "msg": "Invalid API-key, IP, or permissions for action."})))
    res = sync(client, sid)
    assert res.get("kind") == "auth" and "Enable Reading" in res["error"]
    assert SECRET not in res["error"] and API_KEY not in res["error"]


def test_check_warns_when_key_may_trade(client, bn):
    bn.d["canTrade"] = True
    sid = create(client)
    r = post(client, f"/settings/datasources/{sid}/check")
    assert r.status_code == 303
    page = client.get(f"/settings/datasources/{sid}").text
    assert "Enable Reading" in page and "Handelsrechte" in page
    assert SECRET not in page and API_KEY not in page and API_KEY[-4:] in page


def test_form_requires_secret_and_never_echoes_it(client, bn):
    r = post(client, "/settings/datasources", kind="exchange", provider="binance", name="B", account="B",
             sync_interval_min="0", api_key=API_KEY)
    assert r.status_code == 400 and "Secret Key" in r.text and API_KEY not in r.text
    sid = create(client)
    page = client.get(f"/settings/datasources/{sid}").text
    assert 'name="api_secret"' in page and "Enable Reading" in page
    r = post(client, f"/settings/datasources/{sid}/key", api_key=API_KEY[::-1], api_secret="short")
    assert "error=" in r.headers["location"] and "short" not in r.headers["location"]


def test_signature_and_keys_never_reach_logs(client, bn, caplog):
    import logging

    from app.logging_setup import SecretRedactor

    assert "sig123456" not in SecretRedactor().redact("GET https://api.binance.com/x?a=1&signature=sig123456 200")
    sid = create(client)
    with caplog.at_level(logging.INFO):
        sync(client, sid)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in text and API_KEY not in text
