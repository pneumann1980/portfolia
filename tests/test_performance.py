"""TTWROR und IRR gegen Referenzbeispiele (Portfolio-Performance-Methodik, Excel-XIRR)."""

from datetime import date, timedelta

import numpy as np
import pytest

from app.analytics.performance import annualize, drawdown, irr_for_period, ttwror, xirr


def test_ttwror_neutralizes_deposits():
    # Tag 0: 1000; Tag 1: Einzahlung 1000 (Tagesbeginn), Tagesende 2100 → r1 = 2100/2000−1 = 5 %
    # Tag 2: 2200 → r2 = 2200/2100−1; gesamt = 1,05 × 1,047619 − 1 = 10 %
    v = np.array([1000.0, 2100.0, 2200.0])
    inflow = np.array([1000.0, 1000.0, 0.0])
    outflow = np.zeros(3)
    assert ttwror(v, inflow, outflow) == pytest.approx(0.10, abs=1e-12)


def test_ttwror_withdrawal_at_end_of_day():
    # Tag 1: Wert steigt auf 1100, gleichzeitig Entnahme 500 → Tagesende 600: r = (600+500)/1000−1 = 10 %
    v = np.array([1000.0, 600.0])
    assert ttwror(v, np.array([1000.0, 0.0]), np.array([0.0, 500.0])) == pytest.approx(0.10)


def test_ttwror_first_day_and_empty_days():
    # Depot startet leer, erster Kauf an Tag 2
    v = np.array([0.0, 0.0, 1050.0, 1100.0])
    i = np.array([0.0, 0.0, 1000.0, 0.0])
    assert ttwror(v, i, np.zeros(4)) == pytest.approx(0.10)


def test_ttwror_period_slice_matches_product():
    v = np.array([100.0, 110.0, 99.0, 120.0])
    z = np.zeros(4)
    assert ttwror(v, z, z, start=1, end=3) == pytest.approx(120 / 110 - 1)


def test_xirr_excel_reference():
    # Excel-Dokumentation XINTZINSFUSS: 0,373362535
    flows = [(date(2008, 1, 1), -10000), (date(2008, 3, 1), 2750), (date(2008, 10, 30), 4250),
             (date(2009, 2, 15), 3250), (date(2009, 4, 1), 2750)]
    assert xirr(flows) == pytest.approx(0.373362535, abs=1e-6)


def test_xirr_simple_and_no_solution():
    assert xirr([(date(2020, 1, 1), -1000), (date(2020, 12, 31), 1100)]) == pytest.approx(0.10, abs=1e-3)
    assert xirr([(date(2020, 1, 1), 1000), (date(2021, 1, 1), 1100)]) is None
    assert xirr([(date(2020, 1, 1), -1000)]) is None


def test_irr_for_period_uses_start_value_flows_and_end_value():
    start = date(2021, 1, 1)
    dates = [start + timedelta(days=i) for i in range(366)]
    values = np.zeros(366)
    values[0] = 1000.0
    values[365] = 2310.0
    flows = np.zeros(366)
    flows[182] = 1000.0  # Einzahlung nach ~6 Monaten
    r = irr_for_period(dates, values, flows, 0, 365)
    # Referenz: −1000 (t0), −1000 (t=182d), +2310 (t=365d)
    ref = xirr([(dates[0], -1000.0), (dates[182], -1000.0), (dates[365], 2310.0)])
    assert r == pytest.approx(ref)
    assert 0.18 < r < 0.22


def test_annualize_and_drawdown():
    assert annualize(0.21, 730) == pytest.approx(0.1, abs=1e-3)
    assert annualize(0.05, 100) is None
    dd = drawdown(np.array([100.0, 120.0, 90.0, 130.0]))
    assert dd.tolist() == pytest.approx([0.0, 0.0, -0.25, 0.0])
