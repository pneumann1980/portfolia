"""Polkadot über Subscan (PubFi-Gateway bzw. direkt) – synthetische Daten, ohne Netz.

Abgedeckt: SS58-Prüfung und Umrechnung (Präfix 42 → 0, Kusama abgelehnt), Schlüsselpflicht mit klarer Anzeige,
Relay Chain und Asset Hub getrennt, Gebühr nur aus eigenen Extrinsics (``fee_used``), Staking ohne Bestandsbewegung
(nur Gebühr), XCM zur Prüfung, Migrationsereignis als ungeklärt (nicht gebucht), Asset-Hub-Token mit eigener
Kennung, Plausibilisierung ``amount`` ↔ ``amount_v2``, Rewards als Staking-Ertrag, Paginierung, wiederholter Abruf
ohne Doppelungen, direkter Subscan-Zugang mit ``X-API-Key``.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources.chains import polkadot as PD
from app.datasources.chains.codec import ss58_encode
from app.datasources.providers import PROVIDERS, normalize_address
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import (
    MASTER,
    FakeSubscan,
    all_rows,
    balances,
    create_wallet,
    ctx,
    make_client,
    set_provider_key,
    source,
)

D = Decimal
ME, EXT, EXCH, MIG = (ss58_encode(bytes([b]) * 32, 0) for b in (0x55, 0x66, 0x77, 0x88))
KEY = "pubfi_test_key_0123456789abcdef"
T0 = 1_760_000_000


def H(n: int) -> str:
    return "0x" + f"{n:064x}"


def tr(block: int, idx: str, ev: int, frm: str, to: str, amount: str, v2: str | None = None, sym: str = "DOT",
       uid: str = "DOT", module: str = "balances", h: int | None = None) -> dict:
    return {"block_num": block, "block_timestamp": T0 + block, "extrinsic_index": idx, "event_idx": ev, "from": frm,
            "to": to, "amount": amount, "amount_v2": v2 if v2 is not None else "", "asset_symbol": sym,
            "asset_unique_id": uid, "module": module, "success": True, "hash": H(h or block), "fee": "0"}


def ex(block: int, idx: str, call: str, fee: str, fee_used: str, success: bool = True) -> dict:
    mod, _, fn = call.partition(".")
    return {"block_num": block, "block_timestamp": T0 + block, "extrinsic_index": idx, "extrinsic_hash": H(block),
            "call_module": mod, "call_module_function": fn, "fee": fee, "fee_used": fee_used, "success": success,
            "signer": ME}


def nets() -> dict:
    return {
        "polkadot": {"tip": 1000,
                     "transfers": [tr(100, "100-2", 5, EXT, ME, "50", "500000000000"),
                                   tr(110, "110-1", 3, ME, EXCH, "10", "100000000000"),
                                   tr(140, "140-1", 2, ME, EXT, "5", "50000000000", module="xcmpallet")],
                     "extrinsics": [ex(110, "110-1", "balances.transfer_keep_alive", "160000000", "150000000"),
                                    ex(120, "120-1", "staking.bond", "200000000", "200000000"),
                                    ex(140, "140-1", "xcmPallet.limited_teleport_assets", "300000000", "300000000")],
                     "rewards": [{"block_num": 130, "block_timestamp": T0 + 130, "event_index": "130-7",
                                  "amount": "12345678900", "account": ME, "era": 1000, "event_id": "Rewarded"}],
                     "tokens": [{"symbol": "DOT", "unique_id": "DOT", "decimals": 10, "balance": "0"}]},
        "assethub-polkadot": {"tip": 2000,
                              "transfers": [tr(500, "500-0", 1, MIG, ME, "34.98", module="ahmigrator"),
                                            tr(510, "510-2", 4, EXT, ME, "25.5", sym="USDT",
                                               uid="standard_assets/1984", module="assets"),
                                            tr(520, "520-1", 2, EXT, ME, "1", "20000000000")],
                              "extrinsics": [], "rewards": [],
                              "tokens": [{"symbol": "DOT", "unique_id": "DOT", "decimals": 10,
                                          "balance": "350000000000", "bonded": "100000000000"},
                                         {"symbol": "USDT", "unique_id": "standard_assets/1984", "decimals": 6,
                                          "balance": "25500000"}]},
    }


@pytest.fixture
def scan(monkeypatch):
    fake = FakeSubscan(nets(), KEY)
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    return fake


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def sync(c, sid):
    return datasource_service(ctx(c)).sync(sid, "manual")


def k(net: str, idx: str, sub: str) -> str:
    return f"polkadot:{net}-{idx}:{ME}#{sub}"


def test_ss58_validation():
    p = PROVIDERS["polkadot"]
    assert normalize_address(p, ME) == (ME, None)
    generic = ss58_encode(b"\x55" * 32, 42)
    assert normalize_address(p, generic) == (ME, None)  # dasselbe Konto im Polkadot-Format
    kusama = ss58_encode(b"\x55" * 32, 2)
    assert "anderen Netzes" in normalize_address(p, kusama)[1]
    assert "Prüfsumme" in normalize_address(p, ME[:-1] + ("a" if ME[-1] != "a" else "b"))[1]


def test_missing_key_is_shown_and_nothing_is_fetched(client, scan):
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    res = sync(client, sid)
    assert "Schlüssel" in res["error"] and not scan.calls
    page = client.get("/settings/datasources").text
    assert "Schlüssel fehlt" in page and "PubFi" in page


def test_relay_and_asset_hub_events(client, scan):
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    r = rows[k("rc", "100-2", "tr:5")].rec
    assert (r.kind, r.in_sym, r.in_qty, r.review) == ("deposit", "DOT", D(50), None)
    r = rows[k("rc", "110-1", "tr:3")].rec
    assert (r.kind, r.out_qty, r.fee_qty, r.fee_sym, r.review) == ("withdrawal", D(10), D("0.015"), "DOT", None)
    assert r.txhash == H(110)
    r = rows[k("rc", "120-1", "fee")].rec  # staking.bond: gebunden bleibt im Konto → nur Gebühr
    assert r.kind == "fee" and r.fee_qty == D("0.02") and r.label == "staking.bond"
    r = rows[k("rc", "140-1", "tr:2")].rec
    assert r.kind == "withdrawal" and "XCM" in r.review and r.fee_qty == D("0.03")
    rw = rows[f"polkadot:reward-rc-130-7:{ME}#reward"].rec
    assert (rw.kind, rw.in_qty, rw.tag) == ("deposit", D("1.23456789"), "staking")
    mig = rows[k("ah", "500-0", "migration")]
    assert mig.rec.kind == "review" and mig.status == "unclear" and "Migration" in mig.rec.note
    usdt = rows[k("ah", "510-2", "tr:4")].rec
    assert usdt.in_sym == "USDT@DOTAH:standard_assets/1984" and usdt.in_qty == D("25.5")
    odd = rows[k("ah", "520-1", "tr:2")].rec
    assert odd.in_qty == D(1) and "nicht eindeutig" in odd.review
    b = balances(client, sid)
    assert b["DOT"] == "35" and b["USDT@DOTAH:standard_assets/1984"] == "25.5"
    note = ctx(client).db.scalar("SELECT note FROM ds_balance WHERE source_id=? AND asset_key='DOT'", (sid,))
    assert "gebunden 10" in note
    cur = json.loads(source(client, sid)["cursor_json"])
    assert cur["nets"]["polkadot"]["block"] == 1001 and cur["nets"]["assethub-polkadot"]["block"] == 2001
    # Gateway: nur Free-Routen, Bearer, keine Query-Parameter (siehe FakeSubscan)
    assert all(c.url.path.endswith(":free") for c in scan.calls)
    n = len(all_rows(client, sid))
    datasource_service(ctx(client)).reset_cursor(sid)
    sync(client, sid)
    assert len(all_rows(client, sid)) == n


def test_pagination_capped_lists_continue_next_run(client, scan, monkeypatch):
    monkeypatch.setattr(PD, "ROW", 1)
    monkeypatch.setattr(PD, "MAX_PAGES", 2)
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    res = sync(client, sid)
    assert res["status"] == "partial", res
    for _ in range(6):
        if datasource_service(ctx(client)).sync(sid, "manual").get("status") == "synced":
            break
    rows = all_rows(client, sid)
    for key in (k("rc", "100-2", "tr:5"), k("rc", "110-1", "tr:3"), k("rc", "140-1", "tr:2"),
                k("ah", "510-2", "tr:4"), f"polkadot:reward-rc-130-7:{ME}#reward"):
        assert key in rows, key
    assert len(rows) == len(set(rows))


def test_xcm_to_own_account_on_other_chain_is_not_netted_to_zero(client, scan):
    """Relay Chain → Asset Hub an dieselbe Adresse: Abgang auf der signierenden Chain, Zugang auf der Ziel-Chain –
    beide zur Prüfung (Umbuchung), nie stillschweigend zu null saldiert."""
    scan.nets["polkadot"]["transfers"].append(tr(150, "150-1", 4, ME, ME, "7", "70000000000", module="xcmpallet"))
    scan.nets["polkadot"]["extrinsics"].append(ex(150, "150-1", "xcmPallet.limited_teleport_assets", "1", "1"))
    scan.nets["assethub-polkadot"]["transfers"].append(tr(530, "530-0", 9, ME, ME, "6.99", module="polkadotxcm"))
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    sync(client, sid)
    rows = all_rows(client, sid)
    out = rows[k("rc", "150-1", "tr:4")].rec
    inn = rows[k("ah", "530-0", "tr:9")].rec
    assert (out.kind, out.out_qty) == ("withdrawal", D(7)) and "XCM" in out.review
    assert (inn.kind, inn.in_qty) == ("deposit", D("6.99")) and "XCM" in inn.review


def test_direct_subscan_with_api_key_header(client, scan):
    set_provider_key(client, "subscan", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT", chain_provider="subscan")
    assert sync(client, sid)["status"] == "synced"
    hosts = {c.url.host for c in scan.calls}
    assert hosts == {"polkadot.api.subscan.io", "assethub-polkadot.api.subscan.io"}
    assert all(c.headers.get("x-api-key") == KEY and "authorization" not in c.headers for c in scan.calls)


def test_provider_error_keeps_cursor(client, scan):
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    sync(client, sid)
    cursor = source(client, sid)["cursor_json"]
    scan.fail_next(httpx.Response(200, json={"code": 10001, "message": "API rate limit exceeded"}))
    res = sync(client, sid)
    assert "drosselt" in res["error"] and source(client, sid)["cursor_json"] == cursor


def test_pubfi_401_and_403_are_told_apart(client, scan):
    """PubFi: 401 = Schlüssel unbekannt, 403 = Konto für die kostenlose Route nicht freigeschaltet (laut Doku).
    Die Meldung nennt den Grund aus der Antwort und die Anfrage-ID; beides wird nie als „Subscan-Schlüssel“ gedeutet."""
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    body = {"error": {"code": "pubfi.forbidden", "message": "Account is not authorized for this route"}}
    scan.fail_next(httpx.Response(403, json=body, headers={"pubfi-request-id": "req-1234"}))
    err = sync(client, sid)["error"]
    assert "nicht freigeschaltet" in err and "pubfi.forbidden" in err and "req-1234" in err
    assert "Subscan direkt" in err
    scan.fail_next(httpx.Response(401, json={"error": {"code": "pubfi.unauthorized", "message": "Unauthorized"}}))
    err = sync(client, sid)["error"]
    assert "kennt den Schlüssel nicht" in err and "pubfi.unauthorized" in err and "pubfi.ai" in err


# ---- Zeilenlimit der kostenlosen Route, Bestand per öffentlichem RPC -----------------------------------------------

def _row_capped(scan, limit: int, routes: tuple[str, ...], seen: list[int], min_page: int = 0):
    real = scan.handler

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.pubfi.ai" and any(r in req.url.path for r in routes):
            body = json.loads(req.content or b"{}")
            row, page = int(body.get("row") or 0), int(body.get("page") or 0)
            seen.append(row)
            if row > limit and page >= min_page:  # wie live: „row_limit_exceeded“ der kostenlosen Route
                return httpx.Response(403, json={"error": {"code": "row_limit_exceeded",
                                                           "message": "row limit exceeded for this route"}},
                                      headers={"pubfi-request-id": "req-row"})
        return real(req)

    return handler


def test_row_limit_of_the_free_route_reduces_rows_and_is_remembered(client, scan, monkeypatch):
    """Beobachtet (10/2026): PubFi lehnte die Bestandsabfrage mit row=100 ab (HTTP 403 „row_limit_exceeded“). Die
    Zeilenzahl wird stufenweise verkleinert (100 → 50 → 20 …), die zulässige Stufe für den Prozess gemerkt."""
    seen: list[int] = []
    monkeypatch.setattr(WalletConnector, "transport",
                        httpx.MockTransport(_row_capped(scan, 20, ("account/tokens",), seen)))
    monkeypatch.setattr(PD, "_ROW_CAP", {})
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    ok, msg = datasource_service(ctx(client)).check(sid)
    assert ok, msg
    assert seen[:3] == [100, 50, 20]  # Stufen bis zur zulässigen Größe
    assert PD._ROW_CAP == {"pubfi": 20} and balances(client, sid)["DOT"] == "35"
    seen.clear()
    assert datasource_service(ctx(client)).check(sid)[0]
    assert set(seen) == {20}  # gemerkt: keine Fehlversuche mehr


def test_row_limit_in_the_middle_of_a_paged_list_restarts_with_smaller_pages(client, scan, monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(PD, "ROW", 2)
    monkeypatch.setattr(PD, "_ROW_CAP", {})
    monkeypatch.setattr(WalletConnector, "transport",
                        httpx.MockTransport(_row_capped(scan, 1, ("v2/scan/transfers",), seen, min_page=1)))
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    assert rows[k("rc", "100-2", "tr:5")].rec.in_qty == D(50) and rows[k("rc", "110-1", "tr:3")].rec.out_qty == D(10)
    assert PD._ROW_CAP == {"pubfi": 1}
    assert len([r for r in rows if "rc-100" in r]) == len({r for r in rows if "rc-100" in r})  # keine Dubletten


def test_row_limit_even_for_a_single_row_is_a_clear_scope_error(client, scan, monkeypatch):
    """Selbst eine Zeile wird abgelehnt: klare Meldung, der Bestand kommt trotzdem aus dem öffentlichen RPC."""
    seen: list[int] = []
    monkeypatch.setattr(PD, "_ROW_CAP", {})
    monkeypatch.setattr(WalletConnector, "transport",
                        httpx.MockTransport(_row_capped(scan, 0, ("account/tokens",), seen)))
    _with_rpc_on(monkeypatch, free=2 * 10**10)
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    ok, msg = datasource_service(ctx(client)).check(sid)
    assert not ok and "einzelne Zeile" in msg and "Subscan direkt" in msg
    assert "Bestand laut öffentlichem RPC: 2 DOT" in msg
    assert seen == [100, 50, 20, 10, 5, 1]  # alle Stufen versucht


def _with_rpc_on(monkeypatch, free: int, reserved: int = 0):
    """Wie :func:`_with_rpc`, aber über dem bereits gesetzten Transport (z. B. mit Zeilenlimit)."""
    cur = WalletConnector.transport
    inner = cur.handler  # type: ignore[union-attr]

    class _Scan:
        handler = staticmethod(inner)

    return _with_rpc(_Scan, monkeypatch, free, reserved)


def _with_rpc(scan, monkeypatch, free: int, reserved: int = 0, on_assethub: bool = True):
    """Öffentliche Parity-RPCs: ``state_getStorage`` für System.Account (SCALE AccountInfo, 80 Byte)."""
    real = scan.handler
    asked: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host in ("rpc.polkadot.io", "polkadot-asset-hub-rpc.polkadot.io"):
            body = json.loads(req.content)
            asked.append({"host": req.url.host, **body})
            assert body["method"] == "state_getStorage"
            if req.url.host.startswith("rpc.") or not on_assethub:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
            info = (0).to_bytes(16, "little") + free.to_bytes(16, "little") + reserved.to_bytes(16, "little") \
                + bytes(32)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + info.hex()})
        return real(req)

    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(handler))
    return asked


def test_without_any_key_the_balance_still_comes_from_the_public_rpc(client, scan, monkeypatch):
    """Kein PubFi-Schlüssel (oder Anbieter gesperrt): Bestand laut öffentlichem RPC als Ist-Bestand."""
    asked = _with_rpc(scan, monkeypatch, free=350_000_000_000, reserved=10_000_000_000)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")  # kein Schlüssel
    ok, msg = datasource_service(ctx(client)).check(sid)
    assert not ok and "Schlüssel" in msg and "Bestand laut öffentlichem RPC: 36 DOT" in msg
    assert balances(client, sid) == {"DOT": "36"}
    assert not [c for c in scan.calls if c.url.host == "api.pubfi.ai"]
    from app.datasources.chains.codec import ss58_decode

    pub = ss58_decode(ME)[1]
    key = asked[0]["params"][0]
    assert key.startswith("0x26aa394eea5630e07c48ae0c9558cef7b99d880ec681799c0cf30e8886371da9")
    assert key.endswith(pub.hex())
    assert {a["host"] for a in asked} == {"rpc.polkadot.io", "polkadot-asset-hub-rpc.polkadot.io"}


def test_unknown_account_on_both_chains_has_balance_zero_via_rpc(client, scan, monkeypatch):
    _with_rpc(scan, monkeypatch, free=0, on_assethub=False)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    ok, msg = datasource_service(ctx(client)).check(sid)
    assert not ok and "Bestand laut öffentlichem RPC: 0 DOT" in msg and balances(client, sid) == {"DOT": "0"}


# ---- Verlaufsfenster der kostenlosen Route („history_window_exceeded“) ---------------------------------------------

def _windowed(scan, seen: list[tuple[int, int]], max_width: int | None = None, min_lo: int = 0):
    """Wie live (10/2026): ohne Blockbereich bzw. mit zu breitem/zu altem Bereich antwortet die kostenlose PubFi-Route
    mit HTTP 403 „history_window_exceeded“ – schon bei ``row=1``."""
    real = scan.handler
    routes = ("v2/scan/transfers", "v2/scan/extrinsics", "v2/scan/account/reward_slash")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.pubfi.ai" and any(r in req.url.path for r in routes):
            br = json.loads(req.content or b"{}").get("block_range")
            deny = httpx.Response(403, json={"error": {"code": "history_window_exceeded",
                                                       "message": "history_window_exceeded"}})
            if not br:
                return deny
            lo, hi = (int(x) for x in str(br).split("-"))
            seen.append((lo, hi))
            if (max_width is not None and hi - lo + 1 > max_width) or lo < min_lo:
                return deny
        return real(req)

    return handler


def test_history_window_is_halved_until_accepted_and_remembered(client, scan, monkeypatch):
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(PD, "MIN_WINDOW", 50)
    monkeypatch.setattr(PD, "_WINDOW_CAP", {})
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(_windowed(scan, seen, max_width=300)))
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    assert rows[k("rc", "100-2", "tr:5")].rec.in_qty == D(50) and rows[k("rc", "110-1", "tr:3")].rec.out_qty == D(10)
    assert rows[k("ah", "520-1", "tr:2")].rec.in_qty == D(1)  # auch das zweite Netz, in Fenstern bis zur Spitze
    assert PD._WINDOW_CAP == {"pubfi:polkadot": 250, "pubfi:assethub-polkadot": 250}
    seen.clear()
    scan.nets["polkadot"]["tip"], scan.nets["assethub-polkadot"]["tip"] = 1100, 2100
    assert sync(client, sid)["status"] == "synced"  # Folgelauf: Breite aus Abrufstand, keine Fehlversuche mehr
    assert seen and all(hi - lo + 1 <= 250 for lo, hi in seen)


def test_history_window_cap_is_kept_in_the_cursor_across_restarts(client, scan, monkeypatch):
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(PD, "MIN_WINDOW", 50)
    monkeypatch.setattr(PD, "_WINDOW_CAP", {})
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(_windowed(scan, seen, max_width=300)))
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    assert sync(client, sid)["status"] == "synced"
    monkeypatch.setattr(PD, "_WINDOW_CAP", {})  # Prozessneustart
    scan.nets["polkadot"]["tip"] = 1100
    seen.clear()
    assert sync(client, sid)["status"] == "synced"
    assert all(hi - lo + 1 <= 250 for lo, hi in seen)


def test_history_window_lookback_limit_keeps_the_balance_and_reports_the_gap(client, scan, monkeypatch):
    """Rückblick-Grenze (auch das kleinste Fenster in alten Blöcken scheitert): kein Fehler, kein erfundener Verlauf –
    der Bestand laut öffentlichem RPC bleibt als Ist-Bestand, die fehlende Historie ist eine ausgewiesene Lücke."""
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(PD, "MIN_WINDOW", 50)
    monkeypatch.setattr(PD, "_WINDOW_CAP", {})
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(_windowed(scan, seen, min_lo=900)))
    _with_rpc_on(monkeypatch, free=4 * 10**10)
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    res = sync(client, sid)
    assert not res.get("error"), res
    assert not all_rows(client, sid)  # nichts gebucht
    assert balances(client, sid)["DOT"] == "4"
    text = json.dumps(res, ensure_ascii=False) + client.get("/settings/datasources").text
    assert "Historie" in text and "Verlauf" in text


def test_check_works_when_the_route_rejects_unbounded_history(client, scan, monkeypatch):
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(_windowed(scan, seen, max_width=300)))
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    ok, msg = datasource_service(ctx(client)).check(sid)
    assert ok, msg
    assert seen and all(hi - lo + 1 <= PD.MIN_WINDOW + 1 for lo, hi in seen)
