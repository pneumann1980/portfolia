"""Länderneutrales Dokumentmodell für PDF-Berichte (Regelwerk baut, Renderer zeichnet)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.tax.base import FormField, Line, Meter, Table


@dataclass
class H1:
    text: str


@dataclass
class H2:
    text: str


@dataclass
class P:
    text: str
    style: str = "normal"  # normal | small | note | warn | strong


@dataclass
class KV:
    rows: list[tuple[str, str]]


@dataclass
class Lines:
    lines: list[Line]


@dataclass
class Meters:
    meters: list[Meter]


@dataclass
class TableBlock:
    table: Table


@dataclass
class Fields:
    fields: list[FormField]
    show_lines: bool = True


@dataclass
class Bullets:
    items: list[str]
    style: str = "normal"


@dataclass
class Spacer:
    height_mm: float = 4


@dataclass
class PageBreak:
    orientation: str | None = None  # None = wie bisher | portrait | landscape


@dataclass
class Doc:
    title: str
    subtitle: str = ""
    filename: str = "bericht.pdf"
    subject: str = ""
    keywords: str = ""
    footer: str = ""
    orientation: str = "portrait"
    blocks: list[Any] = field(default_factory=list)

    def add(self, *blocks: Any) -> Doc:
        self.blocks.extend(blocks)
        return self
