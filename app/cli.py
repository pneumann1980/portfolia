"""Kommandozeile: ``python -m app <befehl>``.

Befehle:
  hash-password            Passwort-Hash für AUTH_PASSWORD_HASH erzeugen (PBKDF2-SHA256)
  validate <datei.zip>     Import-Datei prüfen, ohne zu importieren
  sample-zip <ziel.zip>    Beispiel-Import mit anonymisierten Testdaten erzeugen
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
    else:
        print(__doc__)
        sys.exit(1)
