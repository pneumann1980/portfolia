"""Bekannte Ticker-/Token-Umstellungen (kuratiert, mit Beleg).

Nur Einträge, deren Verhältnis, Stichtag und Kennungen belegt sind; alles andere erkennt Portfolia aus dem
CoinGecko-Katalog bzw. den eigenen Daten als *Hinweis mit zu prüfendem Verhältnis*. Ein Eintrag bewirkt nie selbst
eine Änderung – er füllt nur den Vorschlag vor, den der Nutzer bestätigt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Known:
    old_cg: str                # CoinGecko-ID des bisherigen Coins
    old_symbol: str
    new_cg: str                # CoinGecko-ID des Nachfolgers
    new_symbol: str
    new_name: str
    ratio: Decimal             # neue Einheiten je bisheriger Einheit
    effective: date            # Beginn der Umstellung (Börsen/Wallets stellten teils später um)
    note: str


KNOWN: tuple[Known, ...] = (
    # Polygon Labs: POL ersetzt MATIC 1:1 als nativer Token von Polygon PoS ab 04.09.2024; CoinGecko führt den alten
    # Coin als „MATIC (migrated to POL)“ (matic-network) und den neuen als „POL (ex-MATIC)“ (polygon-ecosystem-token).
    Known("matic-network", "MATIC", "polygon-ecosystem-token", "POL", "POL (ex-MATIC)", Decimal(1), date(2024, 9, 4),
          "Polygon: MATIC → POL im Verhältnis 1:1 (Polygon PoS seit 04.09.2024; Börsen und Wallets stellten zu "
          "unterschiedlichen Terminen um)."),
)

BY_OLD_CG = {k.old_cg: k for k in KNOWN}
