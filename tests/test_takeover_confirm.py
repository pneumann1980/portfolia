"""Quellenübernahme mit Bestätigung (Binance, Blockchain-Wallets): Stufen, Bestand „nicht schlechter“, Zeitraum der
Quelle (Buchungen davor bleiben), Steuerjahre vor/nach der Übernahme. Mock und Konto wie ``test_takeover`` (Bitpanda
als Träger, der Modus wird für den Test auf „bestätigt“ gestellt)."""

# ruff: noqa: F811  (Fixtures aus test_bitpanda werden importiert und als Argumente verwendet)
from __future__ import annotations

from decimal import Decimal

import pytest

from app.datasources import takeover as TK
from tests.helpers import tx
from tests.test_bitpanda import api, client, ctx, master  # noqa: F401  (Fixtures)
from tests.test_takeover import holdings, ids, staged, started, sync_txs

D = Decimal


@pytest.fixture
def confirm_mode(monkeypatch):
    monkeypatch.setattr(TK, "PROOF", ())
    monkeypatch.setattr(TK, "CONFIRM", ("bitpanda",))


def test_modes_per_provider():
    import importlib

    for mod in ("evm", "bitcoin", "solana", "polkadot", "peaq"):  # Anbindungen registrieren sich beim Import
        importlib.import_module(f"app.datasources.chains.{mod}")
    assert TK.mode("bitpanda") == "proof" and TK.mode("binance") == "confirm"
    for wallet in ("peaq", "polkadot", "ethereum", "bitcoin", "solana"):
        assert TK.mode(wallet) == "confirm", wallet
    assert TK.mode("kraken") == "" and not TK.supported("kraken") and TK.supported("binance")


def test_without_confirmation_the_fetch_check_blocks_and_with_it_the_takeover_is_confirmed(config, master, api,
                                                                                         confirm_mode):
    c, sid = started(config, api)
    try:
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        assert a.mode == "confirm" and a.tier == "bestätigt" and not a.ok
        assert [b.key for b in a.blockers] == ["fetch"] and "bestätigt" in a.blockers[0].label
        refused = TK.apply(ctx(c), sid, a.token)
        assert refused["errors"] and "nicht erbracht" in refused["errors"][0] and not sync_txs(c)
        ok = TK.assess(ctx(c), sid, confirm=True)
        assert ok.ok and ok.confirm and ok.token != a.token  # Token bindet die Bestätigung
        wrong = TK.apply(ctx(c), sid, ok.token)  # ohne Bestätigung ausgeführt → Vorschau gilt nicht
        assert wrong["errors"] and not sync_txs(c)
        res = TK.apply(ctx(c), sid, ok.token, confirm=True)
        assert res["errors"] == [], res
        t = TK.latest(ctx(c).db, sid)
        assert t["status"] == "applied" and "bestätigt" in t["message"]
        assert holdings(c) == {"EUR": D("250"), "BTC": D("0.00445")}
        out = TK.undo(ctx(c), sid)
        assert out["errors"] == [] and {"K1", "K2", "K3", "K9", "K10"} <= ids(c)
    finally:
        c.__exit__(None, None, None)


def test_confirmed_takeover_accepts_a_deviation_that_does_not_get_worse(config, master, api, confirm_mode):
    """Beobachtet 0,004 BTC: Import 0,0045 (Abstand 0,0005), Quelle 0,00445 (0,00045) → nicht schlechter → erlaubt.
    Im Modus „belegt“ wäre derselbe Fall gesperrt (exakter Bestand fehlt)."""
    c, sid = started(config, api, btc="0.004")
    try:
        staged(c, sid)
        a = TK.assess(ctx(c), sid, confirm=True)
        assert a.ok, [(x.label, x.detail) for x in a.blockers]
        btc = next(b for b in a.balances if b["asset"] == "BTC")
        assert not btc["ok"] and btc["not_worse"]
        res = TK.apply(ctx(c), sid, a.token, confirm=True)
        assert res["errors"] == [], res
        assert holdings(c)["BTC"] == D("0.00445")
        t = TK.latest(ctx(c).db, sid)
        assert "nicht schlechter" in t["message"] and '"exact": false' in t["result_json"]
    finally:
        c.__exit__(None, None, None)


def test_confirmed_takeover_is_blocked_when_the_balance_would_get_worse(config, master, api, confirm_mode):
    """Beobachtet 0,0045 BTC: der Import stimmt exakt, die Quelle (0,00445) wäre schlechter → gesperrt."""
    c, sid = started(config, api, btc="0.0045")
    try:
        staged(c, sid)
        a = TK.assess(ctx(c), sid, confirm=True)
        assert not a.ok and [b.key for b in a.blockers] == ["balance"]
        assert "BTC: jetzt 0.0045, danach 0.00445" in a.blockers[0].detail
        before = holdings(c)
        res = TK.apply(ctx(c), sid, a.token, confirm=True)
        assert res["errors"] and holdings(c) == before and not sync_txs(c)
    finally:
        c.__exit__(None, None, None)


def test_bookings_before_the_first_event_of_the_source_stay(config, master, api, confirm_mode):
    rows = [
        tx("K0", "2023-06-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "100"), value="100"),  # vor der Quelle
        tx("K1", "2024-02-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "500"), value="500"),
        tx("K2", "2024-02-02T09:01:00Z", "buy", frm=("Bitpanda", "EUR", "250"), to=("Bitpanda", "BTC", "0.0055"),
           value="251.5"),
        tx("K3", "2024-02-12T12:00:00Z", "withdrawal", tag="gift", frm=("Bitpanda", "BTC", "0.001"), value="55"),
    ]
    c, sid = started(config, api, rows=rows)
    try:
        staged(c, sid)
        a = TK.assess(ctx(c), sid, confirm=True)
        assert a.window_from and [t.tx_id for t in a.before] == ["K0"]
        assert "K0" not in {t.tx_id for t in a.hide} and {"K1", "K2", "K3"} <= {t.tx_id for t in a.hide}
        assert any("vor dem ersten Ereignis" in why for _t, why in a.kept)
        eur = next(b for b in a.balances if b["asset"] == "EUR")
        assert eur["after"] == D("350") and eur["observed"] == D("250") and eur["not_worse"]  # K0 zählt weiter
        assert a.ok, [(x.label, x.detail) for x in a.blockers]
        res = TK.apply(ctx(c), sid, a.token, confirm=True)
        assert res["errors"] == [], res
        assert "K0" in ids(c) and not ({"K1", "K2", "K3"} & ids(c))
        assert holdings(c)["EUR"] == D("350")
    finally:
        c.__exit__(None, None, None)


def test_result_shows_tax_year_effect_measured_before_and_after(config, master, api):
    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        assert TK.apply(ctx(c), sid, a.token)["errors"] == []
        import json

        result = json.loads(TK.latest(ctx(c).db, sid)["result_json"])
        assert result["tier"] == "belegt" and result["check"]["exact"] is True and "years" in result
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Übernommen" in page and "Stufe" in page and "belegt" in page
    finally:
        c.__exit__(None, None, None)


def test_page_offers_the_confirmation_and_passes_it_to_the_apply(config, master, api, confirm_mode):
    c, sid = started(config, api)
    try:
        TK.run_fetch(ctx(c), sid)
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Prüfung und Bestätigung" in page and "vollständige Historie" in page
        assert "Die Voraussetzungen sind nicht erfüllt" in page
        page = c.get(f"/settings/datasources/{sid}/uebernahme?confirm=1").text
        assert 'name="confirmed" value="1"' in page and "API maßgeblich machen" in page
    finally:
        c.__exit__(None, None, None)


def test_prueffstapel_hint_points_to_the_takeover(config, master, api):
    from app.csvimport.module import _takeover_hint

    c, sid = started(config, api)
    try:
        b = {"kind": "sync", "datasource_id": sid, "status": "preview"}
        hint = _takeover_hint(ctx(c), b, {"counts": {"before": 3, "duplicate": 2, "new": 5}})
        assert hint == {"n": 5, "sid": sid, "mode": "proof"}
        assert _takeover_hint(ctx(c), b, {"counts": {"new": 5}}) is None
        assert _takeover_hint(ctx(c), {**b, "kind": "csv"}, {"counts": {"before": 1}}) is None
        a = staged(c, sid)
        assert TK.apply(ctx(c), sid, a.token)["errors"] == []
        assert _takeover_hint(ctx(c), b, {"counts": {"before": 3}}) is None  # bereits übernommen
    finally:
        c.__exit__(None, None, None)
