"""0.84.0 Bitpanda-Lückenprüfung: Saldoverlauf, Import ohne Gegenstück, Gegenprüfung mit der alten API (nur lesend)."""

# ruff: noqa: F811  (Fixtures aus test_bitpanda werden importiert und als Argumente verwendet)
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from app.datasources import bitpanda as B
from app.datasources import bitpanda_gaps as G
from app.datasources import takeover as TK
from tests.test_bitpanda import api, client, ctx, master  # noqa: F401  (Fixtures)
from tests.test_takeover_gaps import _setup

D = Decimal
T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)


def entry(i: int, asset: str, qty: str, ts: datetime, typ: str = "deposit", fee: str = "0", **kw) -> dict:
    return {"id": f"e{i}", "src": "fiat", "asset": asset, "ts": ts, "qty": D(qty), "fee": D(fee), "type": typ,
            "status": "finished", "related": None, "internal": False, **kw}


def test_compare_matches_with_or_without_fee_and_each_leg_once():
    legs = [(T0, "EUR", D("-100.5"), D("-100")), (T0 + timedelta(hours=2), "EUR", D("50"), D("50")),
            (T0 + timedelta(days=5), "EUR", D("50"), D("50"))]
    es = [entry(1, "EUR", "-100", T0 + timedelta(hours=3), "withdrawal", fee="0.5"),  # Gebühr zusätzlich
          entry(2, "EUR", "50", T0),  # nächster Teil in der Zeit
          entry(3, "EUR", "50", T0 + timedelta(hours=1)),  # zweiter 50er: nur noch einer im Fenster → fehlt
          entry(4, "BTC", "0.1", T0)]  # Asset ohne Teil
    missing, hits, used = G.compare(es, legs)
    assert hits == 2 and used == {0, 1}
    assert sorted(e["id"] for e in missing) == ["e3", "e4"]


def test_internal_transfers_cancel_out():
    es = [entry(1, "BTC", "0.5", T0, "transfer", related="e2"), entry(2, "BTC", "-0.5", T0, "transfer", related="e1"),
          entry(3, "EUR", "20", T0, "transfer"), entry(4, "EUR", "-20", T0 + timedelta(minutes=1), "transfer"),
          entry(5, "EUR", "-20", T0 + timedelta(hours=1), "transfer")]  # ohne Gegenbuchung im Fenster
    assert G._cancel_internal(es) == {"e1", "e2", "e3", "e4"}


def test_normalize_legacy_items():
    items = [{"id": "f1", "attributes": {"fiat_id": "1", "amount": "10.00", "fee": "0.50", "in_or_out": "outgoing",
                                         "type": "withdrawal", "status": "finished",
                                         "time": {"date_iso8601": "2024-02-15T11:00:00+01:00", "unix": "0"}}},
             {"id": "f2", "attributes": {"fiat_id": "9", "amount": "x", "in_or_out": "incoming"}},  # unlesbar
             {"id": "f3", "attributes": {"fiat_id": "9", "amount": "3", "in_or_out": "incoming", "type": "deposit",
                                         "status": "pending", "time": {"unix": "1700000000"}}}]
    out = G.normalize(items, "fiat", {"1": "EUR"})
    assert [(e["id"], e["asset"], e["qty"], e["fee"], e["status"]) for e in out] == [
        ("f1", "EUR", D("-10.00"), D("0.50"), "finished"), ("f3", "FIAT#9", D(3), D(0), "pending")]
    assert out[0]["ts"] == datetime(2024, 2, 15, 10, tzinfo=UTC)


class Legacy:
    """Alte API laut Doku: ``data[].attributes``, ``meta.next_cursor``/``total_count``; nur GET, nur Lesen."""

    def __init__(self, fiat: list, crypto: list, key: str = "secret") -> None:
        self.fiat, self.crypto, self.key, self.calls = fiat, crypto, key, []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.method == "GET" and req.url.host == "api.bitpanda.com"
        if req.headers.get("x-api-key") != self.key:
            return httpx.Response(401, json={"errors": [{"code": "UNAUTHORIZED"}]})
        path = req.url.path.removeprefix("/v1")
        if path == "/fiatwallets":
            return httpx.Response(200, json={"data": [{"type": "fiat_wallet", "id": "fw1",
                                                       "attributes": {"fiat_id": "1", "fiat_symbol": "EUR"}}]})
        if path == "/wallets":
            return httpx.Response(200, json={"data": [{"type": "wallet", "id": "w1",
                                                       "attributes": {"cryptocoin_id": "1",
                                                                      "cryptocoin_symbol": "BTC"}}]})
        rows = {"/fiatwallets/transactions": self.fiat, "/wallets/transactions": self.crypto}.get(path)
        if rows is None:
            return httpx.Response(404)
        size = int(req.url.params.get("page_size") or 25)
        cur = req.url.params.get("cursor")
        start = next(i + 1 for i, r in enumerate(rows) if r["id"] == cur) if cur else 0
        page = rows[start:start + size]
        nxt = page[-1]["id"] if start + size < len(rows) else None
        return httpx.Response(200, json={"data": page, "meta": {"total_count": len(rows), "next_cursor": nxt,
                                                                "page_size": size}})


def _item(i: int, kind: str, qty: Decimal, ts: datetime, typ: str, **attrs) -> dict:
    a = {"amount": format(abs(qty), "f"), "fee": "0", "in_or_out": "incoming" if qty > 0 else "outgoing",
         "type": typ, "status": "finished", "time": {"date_iso8601": ts.isoformat(), "unix": str(int(ts.timestamp()))},
         ("fiat_id" if kind == "fiat" else "cryptocoin_id"): "1", **attrs}
    return {"type": "fiat_wallet_transaction" if kind == "fiat" else "transaction", "id": f"{kind}-{i}",
            "attributes": a}


def test_gap_report_and_legacy_crosscheck_explain_the_missing_withdrawal(config, master, api, monkeypatch):
    c, sid = _setup(api, config, "150")  # API fehlt eine Auszahlung über 200 €, der Import kennt nur 150
    try:
        assert G.analyse(ctx(c), sid) is None  # ohne Vollabruf kein Befund
        assert "Kein gespeicherter Vollabruf" in c.get(f"/settings/datasources/{sid}/luecken").text
        TK.run_fetch(ctx(c), sid)
        rep = G.analyse(ctx(c), sid)
        eur = next(x for x in rep["assets"] if x["asset"] == "EUR")
        assert eur["gaps_total"] == D(-200) and eur["filled"] == D(-150) and eur["rest"] == D(-50)
        assert eur["state"] == "Lücken belegt" and rep["gap_count"] == 1
        k5 = [g for g in rep["import_only"] if g["asset"] == "EUR" and g["kind"].startswith("withdrawal")]
        assert k5 and k5[0]["fillers"] == 1 and k5[0]["sum"] == D(-150)
        page = c.get(f"/settings/datasources/{sid}/luecken").text
        assert "Lücken belegt: 1" in page and "Gegenprüfung starten" in page
        assert "Lücken der API prüfen" in c.get(f"/settings/datasources/{sid}/uebernahme").text
        csv = c.get(f"/settings/datasources/{sid}/luecken.csv").text
        assert "Lücke laut Saldo;EUR" in csv and "secret" not in csv

        # alte API: alle Teile der neuen API + die fehlende Auszahlung + interne Umbuchung
        a = TK.assess(ctx(c), sid, plan=False)
        fiat, crypto, i = [], [], 0
        for ln in a.lines_by_ext.values():
            for asset, q in ln.legs.items():
                i += 1
                kind = "fiat" if asset == "EUR" else "crypto"
                (fiat if kind == "fiat" else crypto).append(_item(i, kind, ln.base.get(asset, q), ln.rec.ts, "buy"))
        gap_ts = datetime(2024, 2, 20, 9, tzinfo=UTC)
        fiat.append(_item(90, "fiat", D(-200), gap_ts, "withdrawal"))
        crypto += [_item(91, "crypto", D("0.01"), gap_ts, "transfer", related_wallet_transaction_id="crypto-92"),
                   _item(92, "crypto", D("-0.01"), gap_ts, "transfer", related_wallet_transaction_id="crypto-91")]
        fiat.sort(key=lambda r: r["attributes"]["time"]["unix"], reverse=True)
        legacy = Legacy(fiat * 1, crypto)
        monkeypatch.setattr(G, "LEGACY_PAGE", 2)  # mehrere Seiten
        monkeypatch.setattr(B.BitpandaConnector, "transport", httpx.MockTransport(legacy.handler))
        from tests.test_bitpanda import API_KEY

        legacy.key = API_KEY  # hinterlegter Schlüssel gilt auch für die alte API
        res = G.run_legacy(ctx(c), sid)
        assert res["status"] == "done", res
        assert res["meta"]["fiat"]["complete"] and res["meta"]["crypto"]["complete"]
        assert res["missing_n"] == 1 and res["internal"] == 2 and res["matched"] == i
        assert res["missing"][0]["asset"] == "EUR" and res["missing"][0]["qty"] == "-200"
        assert res["per_asset"] == [{"asset": "EUR", "missing_sum": "-200", "gap_total": "-200",
                                     "explains": "vollständig"}]
        assert all(r.url.host == "api.bitpanda.com" for r in legacy.calls)
        page = c.get(f"/settings/datasources/{sid}/luecken").text
        assert "1 nur in der alten API" in page and "vollständig" in page and "Erneut gegenprüfen" in page
        csv = c.get(f"/settings/datasources/{sid}/luecken.csv").text
        assert "nur alte API;EUR;fiat · withdrawal;2024;1;-200" in csv

        # Schlüssel abgelehnt → verständlicher Hinweis, eingegebener Schlüssel wird nicht gespeichert
        legacy.key = "anderer-schluessel-123"
        res = G.run_legacy(ctx(c), sid)
        assert res["status"] == "failed" and "lehnt diesen Schlüssel ab" in res["message"]
        res = G.run_legacy(ctx(c), sid, key="anderer-schluessel-123")
        assert res["status"] == "done"
        assert "anderer-schluessel" not in str(ctx(c).db.get_state(G.state_key(sid)))
    finally:
        c.__exit__(None, None, None)


def test_routes_only_for_bitpanda_and_reject_bad_keys(config, master, api):
    c, sid = _setup(api, config, None)
    try:
        assert c.get("/settings/datasources/99999/luecken").status_code == 404
        r = c.post(f"/settings/datasources/{sid}/luecken/alt", data={"legacy_key": "a\nb"},
                   headers={"X-CSRF-Token": c.token}, follow_redirects=False)
        assert r.status_code == 303 and "Ung%C3%BCltiger" in r.headers["location"]
        assert not G.legacy_available(G.LEGACY_UNTIL + timedelta(days=1))
    finally:
        c.__exit__(None, None, None)
