"""Kommandozeile: ``python -m app <befehl>``.

Befehle:
  hash-password            Passwort-Hash für AUTH_PASSWORD_HASH erzeugen (PBKDF2-SHA256)
  validate <datei.zip>     Import-Datei prüfen, ohne zu importieren
  sample-zip <ziel.zip>    Beispiel-Import mit anonymisierten Testdaten erzeugen
  backup                   Sicherung der App-Datenbank nach /data/backups erstellen
  master-key               Neuen Master-Key für verschlüsselte API-Keys ausgeben (nur Ausgabe, nichts wird gespeichert)
  credentials status       Master-Key und gespeicherte API-Keys prüfen (ohne Schlüssel anzuzeigen)
  credentials rotate       Gespeicherte API-Keys mit dem aktuellen Master-Key neu verschlüsseln
                           (früheren Key als PORTFOLIA_MASTER_KEY_OLD_FILE bereitstellen)
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    if cmd == "hash-password":
        from app.web.security import hash_password

        pw = args[0] if args else getpass.getpass("Passwort: ")
        if not args and pw != getpass.getpass("Wiederholen: "):
            print("Passwörter stimmen nicht überein", file=sys.stderr)
            sys.exit(1)
        print(hash_password(pw))
    elif cmd == "validate" and args:
        from app.importer.validate import validate_zip

        rep, parsed = validate_zip(Path(args[0]))
        print(json.dumps(json.loads(rep.to_json()), indent=2, ensure_ascii=False))
        if parsed:
            print(f"OK: {parsed.counts}")
        sys.exit(0 if rep.ok else 2)
    elif cmd == "sample-zip" and args:
        from app.importer.sample import write_sample_zip

        path = write_sample_zip(Path(args[0]))
        print(f"Beispiel-Import geschrieben: {path}")
    elif cmd == "backup":
        from app.config import Config
        from app.context import AppContext
        from app.jobs.maintenance import backup_now

        ctx = AppContext(Config.from_env())
        ctx.startup()
        res = backup_now(ctx, "cli")
        print(f"Sicherung erstellt: {res['file']} ({res['bytes'] // 1024} KB)")
    elif cmd == "master-key":
        from app.datasources.vault import generate_master_key

        print(generate_master_key())
        print("\nDirekt in eine Datei umleiten, z. B. auf Unraid: docker exec Portfolia python -m app master-key > "
              "/boot/config/portfolia/master.key – Ordner per Pfad-Zuordnung nur lesend als /run/secrets/portfolia "
              "einbinden, PORTFOLIA_MASTER_KEY_FILE=/run/secrets/portfolia/master.key setzen, Container neu starten. "
              "Den Key getrennt von den Datenbank-Sicherungen aufbewahren (README → Master-Key).", file=sys.stderr)
    elif cmd == "credentials" and args and args[0] in ("status", "rotate"):
        from app.config import Config
        from app.context import AppContext
        from app.datasources.service import datasource_service

        ctx = AppContext(Config.from_env())
        ctx.startup()
        svc = datasource_service(ctx)
        if args[0] == "rotate":
            res = svc.rotate_keys()
            print(f"Neu verschlüsselt: {res['rotated']}")
            for e in res["errors"]:
                print(f"Fehler: {e}", file=sys.stderr)
            sys.exit(1 if res["errors"] else 0)
        st = svc.key_stats()
        v = st["vault"]
        state = f"vorhanden (ID {v['key_id']}, {v['source']})" if v["available"] else "fehlt"
        print(f"Master-Key: {state}")
        if v["old_key_id"]:
            print(f"Früherer Master-Key: ID {v['old_key_id']}")
        if v["error"]:
            print(f"Fehler: {v['error']}")
        print(f"Gespeicherte API-Keys: {st['total']} (mit anderem Master-Key: {st['stale']})")
    else:
        print(__doc__)
        sys.exit(1)
