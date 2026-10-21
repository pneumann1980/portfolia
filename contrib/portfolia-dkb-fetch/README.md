# portfolia-dkb-fetch

Hilfsprogramm für [Portfolia](https://github.com/pneumann1980/portfolia): ruft die **Postbox** der DKB ab (alle Dokumente,
nur lesend) und legt sie nach `<Ziel>/<Kategorie>/<Konto>/<Datum>_<Name>.pdf` ab. Es nutzt die Bibliothek
[dkb-robo](https://github.com/grindsa/dkb-robo) (GPL-3.0) und steht deshalb ebenfalls unter **GPL-3.0-or-later**. Portfolia
(MIT) enthält diesen Code nicht, sondern installiert ihn auf Knopfdruck in eine eigene Python-Umgebung und startet ihn als
Unterprozess – Austausch nur über stdin/stdout (JSON) und Dateien.

Nicht für den Einsatz außerhalb von Portfolia gedacht, aber auch eigenständig lauffähig:

```sh
echo '{"user": "…", "password": "…"}' | python -m portfolia_dkb_fetch --out ./dkb
```

* Anmeldung mit **Freigabe in der DKB-App** (Zweiter Faktor); andere Verfahren (chipTAN) werden nicht unterstützt.
* **Nur lesend:** Dokumente werden nicht als gelesen markiert, es wird nichts an die Bank gesendet außer Anmeldung, Liste, Download.
* Zugangsdaten nur über stdin, nie als Argument oder Umgebungsvariable; sie werden nicht gespeichert und nicht ausgegeben.
* Ein Anmeldeversuch je Aufruf, keine Wiederholungen (Schutz vor Kontosperre).

## Schnittstelle (JSON-Zeilen auf stdout)

Eingabe (stdin): `{"user": "…", "password": "…", "mfa_device": 0}` · Argumente: `--out DIR`, `--known DATEI` (JSON-Liste bereits
geladener Dokument-IDs), `--version` (gibt `{"version": …, "libs": {…}}` aus).

| Ereignis | Felder |
| --- | --- |
| `hello` | `fetcher`, `dkb_robo` |
| `login` | – |
| `awaiting_2fa` | `timeout_s` |
| `logged_in` | `accounts` (Anzahl) |
| `listed` | `total`, `new` |
| `document` | `id`, `category`, `doctype`, `account`, `date`, `subject`, `rel` (Pfad relativ zu `--out`), `size`, `checksum_ok`, `status` (`downloaded`/`exists`) |
| `document_error` | `id`, `message` |
| `done` | `downloaded`, `existing`, `failed`, `cancelled` |
| `error` | `kind` (`dependency`, `login`, `2fa_timeout`, `2fa_canceled`, `network`, `changed`, `other`), `message` |

Rückgabewert: 0 = Lauf beendet (auch mit einzelnen Dokumentfehlern), 2 = Fehler.
