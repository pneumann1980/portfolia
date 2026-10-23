# Konten und Anbindungen (ab 0.45.0)

**Ein Konto ist die Einheit, auf die Portfolia bucht; eine Datenquelle ist eine Anbindung dieses Kontos.** Die frühere Seite
*Einstellungen → Datenquellen* und die Seite *Konten* sind zu **einer Seite `/accounts`** zusammengeführt (Hauptnavigation: *Konten*).

## Konten-Seite
* **Karten** wie bisher bei den Datenquellen: Wallets (nach Gruppe, mit Wert, Status, Sync, Bestandsabgleich, Hinweisen), Börsen/Depots (Import,
  Sync, DKB-ZIP) und **Weitere Konten ohne Anbindung** (manuell bzw. per Datei-Import, auch leere Konten). Doppelte Anbindungen werden oben
  als Hinweis mit *Zusammenführen* angeboten.
* **Konto verwalten …** an jeder Karte: Stammdaten (Broker, Depotgruppe, Notiz), Umbenennen (mit Vorschau, rückgängig), Zusammenführen,
  Entfernen (ausblenden, rückgängig), Löschen (nur leere, selbst angelegte Konten), Verweise auf Konto-Details, Buchungen und Belege.
* **Konto anlegen** ohne Anbindung, **+ Wallet**, **+ Börse / Depot** oben auf der Seite.
* **Konto-Details** (`/accounts/detail?account=…`): Übersicht, Anbindungen (mit ZIP-Import und Steuervergleich bei DKB), Belege je Jahr,
  Verwaltung.

## Eine Gruppe
Es gibt nur noch die **Depotgruppe** des Kontos. Sie bestimmt die Gruppierung auf der Konten-Seite **und** bei Positionen und Performance.
Eine ältere *Wallet-Gruppe* (reine Anzeige) wird weiter angezeigt, solange das Konto keine Depotgruppe hat; wird die Gruppe an einem Konto
gesetzt, geht sie als Depotgruppe in die Stammdaten über und die Wallet-Gruppe der Quelle wird abgelöst. *Gruppe umbenennen* wirkt auf beide.
Es gibt keine automatische Migration – bestehende Gruppierungen bei Positionen ändern sich erst, wenn Sie eine Gruppe setzen.

## Zugänge
`/settings/zugaenge` (Einstellungen → Zugänge): Master-Key, gespeicherte API-Keys und Anbieter-Schlüssel gelten für alle Konten und stehen
deshalb nicht auf der Konten-Seite. Die alte Adresse `/settings/datasources` leitet auf `/accounts` weiter (Suchparameter und Meldungen bleiben);
die technischen Adressen `/settings/datasources/<id>/…` (Details, Sync, Import, DKB) bleiben unverändert.

## Belege
`/belege` (siehe `docs/DKB_DOCUMENTS.md`): Archiv der Bankdokumente je Konto/Jahr/Art mit Ansehen, Download und ZIP.
