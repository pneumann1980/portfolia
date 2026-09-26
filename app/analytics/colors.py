"""Konsistente Farben je Asset über alle Ansichten.

Identität trägt primär die Beschriftung; die Farbe kodiert das Segment (Aktien/Krypto/Cash, validierte
Kategorialfarben 1–3) und innerhalb des Segments eine stabile Abstufung (per Hash der asset_id).
Dadurch hat jedes Asset überall dieselbe Farbe, ohne mehr als drei Farbtöne zu verwenden.
Werte stammen aus der dokumentierten Palette (siehe docs/palette.md), geprüft mit dem Dataviz-Validator.
"""

from __future__ import annotations

import hashlib

SEGMENTS = ("Aktien", "Krypto", "Cash")

# Stufen je Segment: [dunkler, etwas dunkler, Basis, heller] – Basis = validierte Kategorialfarbe
TINTS = {
    "light": {
        "Aktien": ["#005cb8", "#196ac7", "#2a78d6", "#3b88e7"],
        "Krypto": ["#cb4b0c", "#db5a23", "#eb6834", "#fd7845"],
        "Cash": ["#0a9063", "#00a06e", "#1baf7a", "#36bf89"],
        "Sonstige": ["#6b6a66", "#76756f", "#898781", "#9a9892"],
    },
    "dark": {
        "Aktien": ["#2474d1", "#2f7edb", "#3987e5", "#4492f1"],
        "Krypto": ["#c44608", "#ce4f19", "#d95926", "#e56433"],
        "Cash": ["#028a60", "#0f9468", "#199e70", "#2ca97a"],
        "Sonstige": ["#6b6a66", "#76756f", "#898781", "#9a9892"],
    },
}


def _h(key: str) -> int:
    return int(hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:8], 16)


def tint_index(key: str) -> int:
    return _h(key) % 4


def segment_color(segment: str, mode: str = "light") -> str:
    return TINTS[mode].get(segment, TINTS[mode]["Sonstige"])[2]


def asset_color(asset_id: str, segment: str, mode: str = "light") -> str:
    return TINTS[mode].get(segment, TINTS[mode]["Sonstige"])[tint_index(asset_id)]


def asset_colors(asset_id: str, segment: str) -> dict[str, str]:
    return {"light": asset_color(asset_id, segment, "light"), "dark": asset_color(asset_id, segment, "dark")}
