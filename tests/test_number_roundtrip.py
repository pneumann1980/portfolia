"""Regression: Mengen mit genau drei Nachkommastellen (z. B. 2.125) wurden beim Weiterreichen an die Formular-
Erfassung als deutsche Tausendertrennung gelesen (2.125 → 2125).

Betroffen waren Ausbuchen (Verlust/Totalausfall), das Bearbeiten synchronisierter bzw. importierter Buchungen im
Expertenmodus (vorbelegte Felder unverändert speichern) und Token-Migrationen mit Dezimalverhältnis.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.context import AppContext
from app.journal import forms
from app.journal.service import journal_service
from app.util.numbers import parse_number


@pytest.mark.parametrize("v", ["2.125", "61.725", "999.999", "1.000001", "0.125", "123456.789",
                               "0.000000000000000001", "5", "1000"])
def test_form_value_roundtrip_is_exact(v):
    d = Decimal(v)
    assert parse_number(forms.s_de(d)) == d
    assert parse_number(forms.s_de(-d)) == -d


@pytest.fixture
def ctx(config):
    c = AppContext(config)
    c.db.migrate()
    js = journal_service(c)
    assert not js.save_asset({"asset_id": "XTK", "name": "Testtoken", "asset_class": "crypto",
                              "quote_source": "none"}).errors
    res = js.save({"kind": "buy", "asset": "XTK", "qty": "2,125", "amount": "100", "ccy": "EUR",
                   "account": "Wallet", "date": "2025-01-10", "time": "10:00"})
    assert not res.errors
    return c


def bal(ctx, account="Wallet", asset="XTK"):
    ctx.invalidate_overlay()
    return ctx.ledger().balances.get((account, asset), Decimal(0))


def test_writeoff_of_three_decimal_quantity(ctx):
    from app.journal.writeoff import writeoff_service

    assert bal(ctx) == Decimal("2.125")
    svc = writeoff_service(ctx)
    key = next(c.key for c in svc.candidates() if c.asset.asset_id == "XTK")
    res = svc.book([key], "2025-06-01", "lost", "Test")
    assert not res.errors, res.errors
    assert bal(ctx) == 0  # vorher: −2.122,875 (2125 statt 2,125 ausgebucht)


def test_editing_synced_booking_without_changes_keeps_quantities(ctx):
    js = journal_service(ctx)
    row = ctx.db.q1("SELECT * FROM journal_tx WHERE status='active' ORDER BY id DESC LIMIT 1")
    ctx.db.x("UPDATE journal_tx SET form_json=NULL WHERE id=?", (row["id"],))  # wie synchronisiert/CSV
    row = js.get(row["tx_id"])
    data = js.form_data(row)
    assert data["kind"] == "expert"
    res = js.save(data, row["tx_id"])
    assert not res.errors, res.errors
    assert bal(ctx) == Decimal("2.125")
    after = js.get(row["tx_id"])
    assert Decimal(after["to_qty"]) == Decimal("2.125") and Decimal(after["value_eur"]) == Decimal("100")
