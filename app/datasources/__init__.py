"""Datenquellen: Börsenkonten und öffentliche Wallet-Adressen, die über Connectoren synchronisiert werden.

Connectoren liefern normalisierte Buchungen (Zwischenformat der CSV-Pipeline, :class:`app.csvimport.model.Rec`)
mit stabiler Ereignis-ID. Diese durchlaufen denselben Weg wie ein CSV-Import – Symbolzuordnung, EUR-Bewertung,
Import-Validator, Dubletten- und Transfer-Abgleich, Prüfung vor dem Übernehmen – und landen als Journal-Buchungen
(Quelle ``sync:<anbieter>``) im Portfolio. Portfolio-, Performance- und Steuerlogik bleiben unverändert.
"""
