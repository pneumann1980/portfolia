"""Kleine Balkendiagramme (Inline-SVG, ohne JavaScript, CSP-konform) für Jahreskennzahlen der Unternehmensanalyse.

Eine Reihe je Diagramm (Titel benennt sie), dünne Balken mit Abstand, Nulllinie als Bezug, negative Werte unterhalb in
Warnfarbe, Wert je Balken als Tooltip (``<title>``) und als Beschriftung. Fehlende Jahre bleiben leer (kein Balken).
"""

from __future__ import annotations

from typing import Any

from markupsafe import Markup, escape


def _short(v: float) -> str:
    a = abs(v)
    if a >= 1e12:
        s, u = v / 1e12, " Bio."
    elif a >= 1e9:
        s, u = v / 1e9, " Mrd."
    elif a >= 1e6:
        s, u = v / 1e6, " Mio."
    else:
        s, u = v, ""
    return f"{s:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".") + u


def bars(fund: dict[str, Any], key: str, title: str, unit: str | None = None, width: int = 520, height: int = 150
         ) -> Markup:
    years = fund.get("years") or []
    row = (fund.get("rows") or {}).get(key) or {}
    pts = [(y, (row.get(y) or {}).get("v")) for y in years]
    vals = [v for _y, v in pts if v is not None]
    if len(vals) < 2:
        return Markup("")
    ccy = escape(unit if unit is not None else (fund.get("currency") or ""))
    hi, lo = max(0.0, *vals), min(0.0, *vals)
    span = (hi - lo) or 1.0
    top, bottom, left = 18, 22, 4
    plot_h = height - top - bottom
    zero = top + plot_h * hi / span
    slot = (width - left * 2) / len(pts)
    bw = max(6.0, min(36.0, slot * 0.5))
    parts = [f'<svg class="disc-bars" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
             f"<title>{escape(title)}</title>",
             f'<line class="axis" x1="{left}" x2="{width - left}" y1="{zero:.1f}" y2="{zero:.1f}"/>']
    for i, (y, v) in enumerate(pts):
        cx = left + slot * i + slot / 2
        parts.append(f'<text class="lbl" x="{cx:.1f}" y="{height - 6}" text-anchor="middle">{escape(y[:4])}</text>')
        if v is None:
            parts.append(f'<text class="lbl miss" x="{cx:.1f}" y="{zero - 4:.1f}" text-anchor="middle">fehlt</text>')
            continue
        h = max(1.0, plot_h * abs(v) / span)
        ytop = zero - h if v >= 0 else zero
        cls = "bar" if v >= 0 else "bar neg"
        label = f"{_short(v)} {ccy}"
        parts.append(f'<rect class="{cls}" x="{cx - bw / 2:.1f}" y="{ytop:.1f}" width="{bw:.1f}" height="{h:.1f}" '
                     f'rx="3"><title>{escape(y)}: {escape(label)}</title></rect>')
        ty = ytop - 4 if v >= 0 else zero - 4  # negative Werte: Beschriftung über der Nulllinie (nicht über der Achse)
        parts.append(f'<text class="val" x="{cx:.1f}" y="{ty:.1f}" text-anchor="middle">{escape(_short(v))}</text>')
    parts.append("</svg>")
    # alle eingesetzten Texte sind escaped, Zahlen formatiert – daher sicher
    cap = f"{escape(title)} ({ccy})" if ccy else escape(title)
    return Markup(f'<figure class="disc-chart"><figcaption>{cap}</figcaption>'  # noqa: S704
                  + "".join(parts) + "</figure>")
