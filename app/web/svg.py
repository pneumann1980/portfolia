"""Serverseitige Mini-Grafiken (Sparklines) als Inline-SVG – CSP-konform, ohne JS."""

from __future__ import annotations

import math
from collections.abc import Sequence

from markupsafe import Markup


def sparkline(values: Sequence[float], width: int = 84, height: int = 26, label: str = "") -> Markup:
    pts = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if len(pts) < 2:
        return Markup('<span class="muted small">–</span>')
    lo, hi = min(pts), max(pts)
    span = hi - lo or 1.0
    pad = 2.0
    step = (width - 2 * pad) / (len(pts) - 1)
    coords = [(pad + i * step, pad + (height - 2 * pad) * (1 - (v - lo) / span)) for i, v in enumerate(pts)]
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    lx, ly = coords[-1]
    change = pts[-1] / pts[0] - 1 if pts[0] else 0
    title = f"{label} 30 Tage: {change * 100:+.1f} %".replace(".", ",")
    # Nur Zahlen und escapter Titel werden eingesetzt – daher sicher.
    return Markup(  # noqa: S704
        f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" aria-label="{Markup.escape(title)}">'
        f"<title>{Markup.escape(title)}</title><path d=\"{d}\"/>"
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2"/></svg>'
    )
