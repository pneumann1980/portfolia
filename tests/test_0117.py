"""0.117 – eine Skala für Abstände, Schriftgrößen und -stärken: Schutz gegen neue freie Werte."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "app"
CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
BODY = CSS[CSS.index("* { box-sizing: border-box; }"):]
SPACING = r"(?<![\w-])(?:padding|margin|gap|row-gap|column-gap)(?:-(?:top|right|bottom|left))?\s*:([^;}]+)"


def test_font_sizes_and_weights_use_tokens():
    free = [m.group(0) for m in re.finditer(r"(?<![\w-])font-size\s*:\s*([^;}]+)", BODY)
            if re.search(r"(?<![\w.-])(\d*\.?\d+)rem", m.group(1))
            and all(float(v) <= 1.4 for v in re.findall(r"(\d*\.?\d+)rem", m.group(1)))]
    assert free == [], free[:5]  # nur Anzeigegrößen über 1,4 rem dürfen frei sein
    assert re.findall(r"font-weight\s*:\s*\d{3}", BODY) == []


def test_spacing_uses_the_scale():
    allowed = {0, 1, 96}  # Haarlinie und Freiraum für die untere Leiste
    free = sorted({int(float(v)) for m in re.finditer(SPACING, BODY)
                   for v in re.findall(r"(?<![\w.#(-])(\d*\.?\d+)px", m.group(1))} - allowed)
    assert free == [], free


def test_tokens_defined_once_and_used():
    for name in ("--sp-8", "--sp-16", "--fs-sm", "--fs-base", "--fw-semi", "--gap-block", "--control-h"):
        assert len(re.findall(re.escape(name) + r"\s*:", CSS)) == 1, name
        assert f"var({name})" in CSS, name


def test_inline_styles_stay_rare():
    n = sum(len(re.findall(r'style="', f.read_text(encoding="utf-8"))) for f in (ROOT / "templates").rglob("*.html"))
    assert n <= 140, n  # 596 vor 0.117; neue Abstände über Hilfsklassen (mt-8, gap-8 …) statt style="…"
