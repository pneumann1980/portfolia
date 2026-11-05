"""0.94.1: Zuordnungen der Quellenübernahme für Wallets/Chains (``SYMBOL@PULSECHAIN``) gelten auch für die Vorgänge.

Fehlerbild (PulseChain): „Als Spam ignorieren“ bzw. „Zuordnen“ speicherte die Kennung nur für den Anbieter; der
Bestand berücksichtigte sie, die Vorgänge nicht (Anbieter wurde nur für Bitpanda erkannt) – das Problem blieb in
der Liste, die Sperre „Alle Vorgänge sind eindeutig und buchbar“ blieb bestehen, die Übernahme ging nicht weiter.
Zusätzlich lehnte „Entfernen“ Token-Kennungen (``SYM@PLS:0x…@PULSECHAIN``) als „Unbekannte Zuordnung“ ab."""

# ruff: noqa: F811  (Fixtures werden importiert und als Argumente verwendet)
from __future__ import annotations

from decimal import Decimal

from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from tests.test_takeover import wait
from tests.test_takeover_wallet_review import _decide_rows, priced  # noqa: F401  (Fixture)
from tests.test_wallets_pulse_peaq import FORK, A, X, client, pulse, tx  # noqa: F401  (Fixtures)
from tests.wallet_fakes import create_wallet, ctx, post

TOK = "0x" + "ab" * 20
SPAM = f"SPAM@PLS:{TOK}"


def _wallet_with_spam(c, fake) -> int:
    fake.chains[369]["tokentx"] = [
        {**tx(FORK + 300, 7, X, A, 5 * 10**18), "contractAddress": TOK, "tokenSymbol": "SPAM", "tokenName": "Spam",
         "tokenDecimal": "18"}]
    fake.chains[369]["tokenbalance"] = {TOK: str(5 * 10**18)}
    sid = create_wallet(c, "pulsechain", A, name="Ledger PLS")
    assert not TK.run_fetch(ctx(c), sid)["errors"]
    return sid


def _maps(a) -> list[str]:
    return sorted(p["symbol"] for p in a.problems if p["kind"] == "map")


def _blocked_lines(a) -> str:
    return " ".join(b.detail for b in a.blockers if b.key == "lines")


def _wallet_txs(c, status: str = "active") -> list[dict]:
    return [dict(r) for r in ctx(c).db.q("SELECT * FROM journal_tx WHERE source='sync:pulsechain' AND status=? "
                                         "ORDER BY id", (status,))]


def _map(c, sid, symbol: str, **form) -> str:
    r = post(c, f"/settings/datasources/{sid}/uebernahme/zuordnen", symbol=symbol, **form)
    assert r.status_code == 303, r.text[:500]
    return r.headers["location"]


def test_spam_ignore_on_wallet_takeover_clears_problem_and_line_blocker_and_can_be_removed(client, pulse):
    c = client
    sid = _wallet_with_spam(c, pulse)
    a = TK.assess(ctx(c), sid)
    assert _maps(a) == ["PLS", SPAM]
    assert SPAM in _blocked_lines(a)
    loc = _map(c, sid, SPAM, action="spam")
    assert "msg=" in loc and "Spam" in loc
    key = next(k for k in csv_service(ctx(c)).saved_symbols() if k.startswith("SPAM@"))
    assert key == f"SPAM@PLS:{TOK}@PULSECHAIN".upper() and csv_service(ctx(c)).saved_symbols()[key] is None
    b = TK.assess(ctx(c), sid)
    assert _maps(b) == ["PLS"]  # Problem verschwindet (nicht nur beim Bestand)
    assert SPAM not in _blocked_lines(b), _blocked_lines(b)
    assert not any("SPAM" in str(x.get("name")) for x in b.balances)
    assert not _wallet_txs(c)  # Markieren bucht nichts
    page = c.get(f"/settings/datasources/{sid}/uebernahme").text
    assert key in page  # unter Zuordnungen sichtbar
    # Entfernen funktioniert auch für Token-Kennungen (Chain + Contract) und macht das Problem wieder sichtbar
    r = post(c, f"/settings/datasources/{sid}/uebernahme/zuordnung-entfernen", key=key)
    assert r.status_code == 303 and "msg=" in r.headers["location"] and "error=" not in r.headers["location"]
    assert key not in csv_service(ctx(c)).saved_symbols()
    again = TK.assess(ctx(c), sid)
    assert _maps(again) == ["PLS", SPAM] and SPAM in _blocked_lines(again)


def test_wallet_takeover_proceeds_after_mapping_and_spam_and_undo_restores(client, pulse, priced):
    c = client
    sid = _wallet_with_spam(c, pulse)
    loc = _map(c, sid, "PLS", action="new", new_id="PLS", new_name="PulseChain", new_class="crypto")
    assert "msg=" in loc, loc
    assert csv_service(ctx(c)).saved_symbols()["PLS@PULSECHAIN"] == "PLS"
    _map(c, sid, SPAM, action="spam")
    a = TK.assess(ctx(c), sid)
    assert not _maps(a)
    assert "lines" not in {b.key for b in a.blockers}, [(b.key, b.detail) for b in a.blockers]
    assert "observed" not in {b.key for b in a.blockers}, [(b.key, b.detail) for b in a.blockers]
    assert [b.key for b in a.blockers] == ["fetch"]  # nur noch die Vollständigkeitsbestätigung fehlt
    assert _decide_rows(c, sid) == 1  # Fork-Eröffnung: prüfbedürftig (0.94.2: nie still ausgelassen)
    page = c.get(f"/settings/datasources/{sid}/uebernahme?confirm=1").text  # Bedienweg: Bestätigung ankreuzen
    ok = TK.assess(ctx(c), sid, confirm=True)
    assert ok.ok, [(b.key, b.detail) for b in ok.blockers]
    assert f'name="token" value="{ok.token}"' in page and 'name="confirmed" value="1"' in page
    r = post(c, f"/settings/datasources/{sid}/uebernahme/uebernehmen", token=ok.token, confirm="1", confirmed="1")
    assert r.status_code == 303
    wait(sid)
    last = dict(TK.latest(ctx(c).db, sid))
    assert last["status"] == "applied", last
    booked = _wallet_txs(c)
    assert booked and all("SPAM" not in (t["from_asset"] or "") + (t["to_asset"] or "") for t in booked)
    pls = sum((Decimal(t["to_qty"] or 0) if t["to_asset"] == "PLS" else 0) for t in booked)
    assert pls > 0
    held = dict(ctx(c).ledger().holdings_by_account("Ledger PLS"))
    assert set(held) == {"PLS"} and held["PLS"] > 0  # Spam-Token weder gebucht noch im Bestand
    back = post(c, f"/settings/datasources/{sid}/uebernahme/zurueck")
    assert back.status_code == 303 and not _wallet_txs(c)
    assert TK.latest(ctx(c).db, sid)["status"] == "undone"
