"""Nachlauf nach Stichtag-Aktionen (0.104.0): gespeicherte Klärungsfälle an den neuen Stand anpassen.

Klärungsfälle (*Datenqualität → Bestände abgleichen* u. a.) sind gespeichert und werden sonst erst beim nächsten
Prüflauf neu berechnet. Nach Festschreiben, Rücknahme, Ausgleich, Korrektur und Neu-Freigabe ändert sich der Stand
grundlegend – ohne Nachlauf zeigten die Fälle weiter den Stand davor. Der Nachlauf recherchiert nicht, übernimmt keine
Lösungen und bucht nichts (``clarify.refresh``); im Betrieb läuft er im Hintergrund.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)


def refresh_cases(ctx: Any) -> None:
    def work() -> None:
        from app.diagnosis import clarify as K

        try:
            n = K.refresh(ctx)
            log.info("Klärungsfälle nach Stichtag-Aktion aktualisiert: %s", "Folgelauf vorgemerkt" if n is None else n)
        except Exception:  # der Nachlauf darf die Aktion nie verfälschen; die Anzeige filtert erledigte Fälle ohnehin
            log.exception("Aktualisierung der Klärungsfälle nach einer Stichtag-Aktion fehlgeschlagen")

    if getattr(ctx, "scheduler", None) is None:  # Tests, CLI: direkt
        work()
        return
    threading.Thread(target=work, name="cutover-refresh", daemon=True).start()
