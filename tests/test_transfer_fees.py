"""Transfer-Zusammenführung erhält beide Gebühren am jeweils belasteten Konto (Visuelle Fallklärung, Punkt 3.2).

Pflichtfall: Konto C erhält 2 BTC, sendet 1 BTC an D; C zahlt zusätzlich 0,01 BTC, D zusätzlich 0,02 BTC.
Vorher und nachher C = 0,99 BTC und D = 0,98 BTC; Rücknahme stellt den Ausgangszustand wieder her.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.csvimport.service import csv_service
from app.journal.service import journal_service
from app.util.timeutil import iso, utcnow
from tests.test_journal import _client
from tests.test_wallets_evm import _book

D = Decimal


@pytest.fixture
def c(config):
    with _client(config) as client:
        client.get("/settings")
        client.token = client.cookies.get("portfolia_csrf")
        yield client


def _balances(c) -> dict[tuple[str, str], Decimal]:
    return {k: v for k, v in c.app.state.ctx.ledger().balances.items() if k[1] == "BTC" and v}


def _setup(c, w_fee: str = "0.01", d_fee: str = "0.02", w_hash: str = "", d_hash: str = "") -> tuple[str, str]:
    _book(c, "2026-01-01T10:00:00Z", type="deposit", to_account="C", to_asset="BTC", to_qty="2", value_eur="60000")
    w = _book(c, "2026-02-01T10:00:00Z", type="withdrawal", from_account="C", from_asset="BTC", from_qty="1",
              fee_asset="BTC", fee_qty=w_fee, value_eur="30000")
    d = _book(c, "2026-02-01T10:20:00Z", type="deposit", to_account="D", to_asset="BTC", to_qty="1",
              fee_asset="BTC", fee_qty=d_fee, value_eur="30000")
    if w_hash or d_hash:
        db = c.app.state.ctx.db
        db.x("UPDATE journal_tx SET tx_hash=? WHERE tx_id=?", (w_hash, w))
        db.x("UPDATE journal_tx SET tx_hash=? WHERE tx_id=?", (d_hash, d))
    return w, d


def _merge(c, w: str, d: str) -> str | None:
    svc, ctx = csv_service(c.app.state.ctx), c.app.state.ctx
    js = journal_service(ctx)
    with ctx.db.transaction() as conn:
        t = svc._merge(conn, js, w, d, None, iso(utcnow()))
    js.after_change()
    return t


def test_merge_keeps_both_fees_on_their_own_accounts_and_is_fully_reversible(c):
    w, d = _setup(c)
    before = _balances(c)
    assert before == {("C", "BTC"): D("0.99"), ("D", "BTC"): D("0.98")}
    t = _merge(c, w, d)
    assert t
    assert _balances(c) == before  # rein organisatorisch: dieselbe wirtschaftliche Wirkung
    db = c.app.state.ctx.db
    kids = db.q("SELECT * FROM journal_tx WHERE group_ref=?", (t,))
    assert len(kids) == 1
    fee = kids[0]
    assert (fee["type"], fee["tag"], fee["from_account"], fee["from_asset"], fee["from_qty"]) == \
        ("withdrawal", "fee", "D", "BTC", "0.02")  # belastet das Zugangskonto, Asset und Menge erhalten
    assert w in fee["pair_refs"] and d in fee["pair_refs"] and fee["status"] == "active"
    parent = db.q1("SELECT * FROM journal_tx WHERE tx_id=?", (t,))
    assert (parent["fee_asset"], parent["fee_qty"], parent["from_account"]) == ("BTC", "0.01", "C")
    # Rücknahme: der Transfer und seine Gebührenbuchung gelten nicht mehr, beide Einzelbuchungen wieder
    assert journal_service(c.app.state.ctx).unpair(t)
    assert db.scalar("SELECT status FROM journal_tx WHERE tx_id=?", (fee["tx_id"],)) == "reverted"
    assert _balances(c) == before


def test_fee_of_the_recipient_is_kept_even_if_the_sender_paid_none(c):
    """Früher wurde die Empfängergebühr dem Abgangskonto belastet, wenn der Abgang keine Gebühr trug."""
    w, d = _setup(c, w_fee="", d_fee="0.02")
    before = _balances(c)
    assert before == {("C", "BTC"): D("1.00"), ("D", "BTC"): D("0.98")}
    assert _merge(c, w, d)
    assert _balances(c) == before


def test_same_onchain_transaction_echoing_the_same_fee_counts_it_once(c):
    """Beide Seiten melden dieselbe Netzwerkgebühr derselben Onchain-Transaktion: eine Gebühr, nicht zwei."""
    h = "0x" + "ab" * 32
    w, d = _setup(c, w_fee="0.01", d_fee="0.01", w_hash=h, d_hash=h)
    assert _merge(c, w, d)
    db = c.app.state.ctx.db
    assert db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='transfer_fee'") == 0
    assert _balances(c)[("C", "BTC")] == D("0.99")


def test_export_and_restore_keep_both_fees(c, config, tmp_path):
    """Export → frische Installation: dieselben Bestände C = 0,99 und D = 0,98 BTC (Transfer und Gebührenbuchung
    gehören zum Export wie jede andere Buchung)."""
    from app.jobs import tasks
    from tests.test_full_export import _client as client_for
    from tests.test_full_export import _other, _place

    w, d = _setup(c)
    assert _merge(c, w, d)
    expected = _balances(c)
    body = c.get("/journal/export.zip").content
    cfg = _other(config, tmp_path, "b")
    _place(cfg, body, "portfolia-export.zip")
    with client_for(cfg) as c2:
        assert tasks.import_check(c2.app.state.ctx, "test").status == "imported"
        assert _balances(c2) == expected == {("C", "BTC"): D("0.99"), ("D", "BTC"): D("0.98")}
