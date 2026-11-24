"""0.121.1 – Kauf-/Verkaufsmarker auf der gezeichneten Kurslinie: kein Ausdünnen der Linie, Marker ohne Kurs am
Ausführungskurs (hohl)."""

from __future__ import annotations

import re
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "app/static/js/charts.js").read_text(encoding="utf-8")


def _price_builder() -> str:
    start = JS.index("function price(el, d)")
    return JS[start:JS.index("\n  function ", start + 10)]


def test_price_line_is_not_downsampled():
    body = _price_builder()
    assert 'type: "line"' in body and not re.search(r"sampling\s*:", body)


def test_markers_outside_line_use_execution_price_and_are_hollow():
    body = _price_builder()
    assert "if (x < lineT[0] || x > lineT[hi]) return null;" in body
    assert "value: [m.t, m.price], _m: m, _off: true" in body and "color: t.surface, borderColor: color" in body
    assert "kein Kurs vor" in JS
