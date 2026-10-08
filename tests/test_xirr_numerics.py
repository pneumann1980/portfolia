"""M24/AP4 – XIRR: Referenzfälle mit analytisch bekannten Lösungen, Mehrdeutigkeit, Grenzfälle, Laufzeit.

Referenzwerte stammen aus geschlossenen Formeln (Polynome in v = 1/(1+r) bei ganzzahligen Jahresabständen,
Endwerte bei konstanter Rendite), nicht aus der Implementierung.
"""

from __future__ import annotations

import math
import random
import time
from datetime import date, timedelta

import pytest

from app.analytics.performance import xirr_detail, xirr_search

D0 = date(2001, 1, 1)


def yearly(*cfs: float) -> list[tuple[date, float]]:
    """Zahlungen im Abstand von genau 365 Tagen (t = 0, 1, 2, … Jahre im Modell 365/Jahr)."""
    return [(D0 + timedelta(days=365 * i), float(c)) for i, c in enumerate(cfs)]


def from_v_roots(*vs: float) -> list[tuple[date, float]]:
    """Zahlungsreihe, deren NPV in v genau die Nullstellen ``vs`` hat: −Π(1 − v/v_k) (Koeffizienten je Jahr)."""
    poly = [1.0]
    for vk in vs:  # (1 − v/vk) multiplizieren
        nxt = [0.0] * (len(poly) + 1)
        for i, a in enumerate(poly):
            nxt[i] += a
            nxt[i + 1] -= a / vk
        poly = nxt
    return yearly(*(-a for a in poly))


def test_standard_cases():
    assert xirr_detail(yearly(-100, 110)) == (pytest.approx(0.10, abs=1e-12), False)
    # 40 Jahre, jährlich 1.000 € Einzahlung, Endwert bei konstant 5 %: Σ 1000·1,05^(40−k), k = 0…39
    fv = sum(1000 * 1.05 ** (40 - k) for k in range(40))
    rate, amb = xirr_detail(yearly(*([-1000.0] * 40), fv))
    assert not amb and rate == pytest.approx(0.05, abs=1e-9)


@pytest.mark.parametrize(("vs", "rs"), [
    ((1 / 2, 1 / 3), (1.0, 2.0)),                     # 100 % und 200 %
    ((1 / 1.1, 1 / 1.2), (0.1, 0.2)),                 # 10 % und 20 %
    ((1 / 2, 1 / 2.001), (1.0, 1.001)),               # 0,1 Prozentpunkte auseinander (feines Raster übersieht das)
    ((1 / 1.05, 1 / 1.5, 1 / 3), (0.05, 0.5, 2.0)),   # drei Lösungen
])
def test_multiple_solutions_are_found_and_flagged(vs, rs):
    res = xirr_search(from_v_roots(*vs))
    assert res.certain and res.roots == pytest.approx(sorted(rs), abs=1e-9)
    assert xirr_detail(from_v_roots(*vs)) == (None, True)


def test_double_root_without_sign_change_is_not_reported_as_unique():
    """−(1 − v)²: berührt null bei r = 0 ohne Vorzeichenwechsel – nicht sicher trennbar → nicht eindeutig."""
    res = xirr_search(yearly(-1, 2, -1))
    assert not res.certain
    assert xirr_detail(yearly(-1, 2, -1)) == (None, True)


def test_extreme_magnitudes_long_horizon_and_bounds():
    assert xirr_detail([(D0, -1e9), (D0 + timedelta(days=100), 1e-2), (D0 + timedelta(days=365), 1.1e9)])[0] == \
        pytest.approx(0.1, abs=1e-8)
    week, amb = xirr_detail([(D0, -100.0), (D0 + timedelta(days=7), 150.0)])  # +50 % in einer Woche
    assert not amb and week == pytest.approx(1.5 ** (365 / 7) - 1, rel=1e-6)
    assert xirr_detail(yearly(-100, 0.01))[0] == pytest.approx(-0.9999, abs=1e-9)
    assert xirr_detail(yearly(-100, -50)) == (None, False)  # keine Lösung (nur Auszahlungen)
    # Lösung jenseits des Suchbereichs mit mehreren Vorzeichenwechseln: keine erfundene Zahl
    r, _amb = xirr_detail(yearly(-1, 3e7, -3e7))
    assert r is None or math.isfinite(r)


def test_never_nan_or_infinite_and_fast_with_many_flows():
    rng = random.Random(11)
    for _ in range(200):
        n = rng.randint(2, 60)
        flows = [(D0 + timedelta(days=rng.randint(0, 365 * 50)), rng.choice([-1, 1]) * 10 ** rng.uniform(-3, 9))
                 for _ in range(n)]
        r, _amb = xirr_detail(flows)
        assert r is None or math.isfinite(r)
    flows = [(D0 + timedelta(days=i), rng.choice([-1, 1]) * rng.uniform(10, 5000)) for i in range(0, 4000, 2)]
    t = time.perf_counter()
    res = xirr_search(flows)
    assert time.perf_counter() - t < 2.0 and res.descartes >= len(res.roots)
