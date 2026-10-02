"""Gegenüberstellung neue Zeile ↔ vorhandene Buchung: Abweichungen und Zeitabstand (reine Logik)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.csvimport.compare import Side, _span, differences

D = Decimal
T0 = datetime(2025, 3, 1, 10, 0, tzinfo=UTC)


def side(**kw):
    base = {"label": "x", "ts": T0, "type": "buy", "tag": None, "out": ("Börse", "EUR", D("100")),
            "inn": ("Börse", "BTC", D("0.002")), "fee": None, "value_eur": D("100")}
    return Side(**{**base, **kw})


def test_identical_sides_have_no_differences():
    assert differences(side(), side()) == set()
    assert differences(side(ts=T0 + timedelta(seconds=30)), side()) == set()  # unter einer Minute: gleich


def test_each_field_is_compared():
    new = side()
    assert differences(new, side(ts=T0 + timedelta(hours=1))) == {"ts"}
    assert differences(new, side(type="sell")) == {"type"}
    assert differences(new, side(tag="staking")) == {"type"}
    assert differences(new, side(out=("Börse", "EUR", D("100.01")))) == {"out"}
    assert differences(new, side(inn=("Wallet", "BTC", D("0.002")))) == {"inn"}  # anderes Konto
    assert differences(new, side(inn=("", "BTC", D("0.002")))) == set()  # Konto unbekannt: kein Unterschied
    assert differences(new, side(fee=("EUR", D("1")))) == {"fee"}
    assert differences(new, side(value_eur=D("100.005"))) == set()  # Rundung bis 1 Cent
    assert differences(new, side(value_eur=D("101"))) == {"value"}
    assert differences(new, side(value_eur=None)) == set()  # ohne Wert nichts zu vergleichen
    assert differences(side(hashes=["0xaa"]), side(hashes=["0xbb"])) == {"hash"}
    assert differences(side(hashes=["0xaa"]), side(hashes=[])) == set()


def test_missing_time_and_missing_booking():
    assert "ts" in differences(side(ts=None, ts_missing=True), side())
    assert differences(side(), Side(label="TX-9", ts=None, found=False)) == set()


def test_span_text():
    assert _span(timedelta(0)) == "gleich"
    assert _span(timedelta(seconds=-45)) == "−45 s"
    assert _span(timedelta(hours=2, minutes=3)) == "+2 h 3 min"
    assert _span(timedelta(days=1, hours=1, minutes=5)) == "+1 Tag 1 h"
    assert _span(-timedelta(days=3)) == "−3 Tage"
