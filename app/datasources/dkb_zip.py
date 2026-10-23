"""DKB-Wertpapierdokumente aus einem ZIP: sicheres Lesen des Archivs und Ablage unter selbst gebautem Pfad.

Vertrauensgrenze: Das Archiv stammt vom Benutzer, sein Inhalt gilt als **nicht vertrauenswürdig**.

* Mitglieder werden **im Speicher** gelesen und nie unter dem Pfad aus dem Archiv geschrieben (Zip-Slip, absolute
  Pfade, ``..``, Symlinks sind damit wirkungslos). Der Zielpfad entsteht allein aus dem **Inhalt** (Dokumentart,
  Depot, Datum, Kennung) und enthält nur ASCII-Zeichen.
* Grenzen (angelehnt an :mod:`app.importer.contract`): Archivgröße, Mitgliederzahl, Größe je Mitglied und insgesamt,
  Kompressionsverhältnis. Verschlüsselte Archive werden mit verständlicher Meldung abgelehnt; verschachtelte Archive
  werden nicht entpackt.
* **Nie überschreiben:** Gleicher Inhalt (SHA-256) unter gleichem Namen = bereits abgelegt, anderer Inhalt = Zusatz
  ``_2``, ``_3`` …
* Die Archivdatei selbst wird nicht dauerhaft gespeichert (nur für die Dauer des Laufs als Zwischendatei, damit ein
  unterbrochener Lauf fortgesetzt werden kann).
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.documentimport.dkb import Classified
from app.documentimport.extract import MAX_FILE
from app.importer import contract as C

MAX_ZIP_BYTES = C.MAX_ZIP_BYTES
MAX_RATIO = C.MAX_COMPRESSION_RATIO
MAX_ENTRIES = 5000
MAX_TOTAL = 1024 * 1024 * 1024  # entpackt insgesamt (deklariert)
CATEGORY = "Wertpapierdokumente"
_SAFE = re.compile(r"[^A-Za-z0-9_\-]+")
_DISPLAY = re.compile(r"[^\w.\- ()]+", re.UNICODE)


class ZipRejected(ValueError):
    """Archiv oder Mitglied nicht annehmbar (Meldung ohne Dateiinhalte, für die Anzeige bestimmt)."""


@dataclass(frozen=True)
class Member:
    index: int
    name: str  # nur zur Anzeige: Dateiname ohne Pfad, bereinigt
    size: int
    packed: int


@dataclass(frozen=True)
class Skipped:
    name: str
    reason: str


def display_name(raw: str, limit: int = 240) -> str:
    """Dateiname ohne Verzeichnisanteil, bereinigt für Anzeige und Hinweise (nie für einen Pfad verwendet). Zu lange
    Namen werden in der Mitte gekürzt, die Endung bleibt erhalten."""
    base = posixpath.basename(raw.replace("\\", "/")) or "(ohne Name)"
    name = _DISPLAY.sub("_", base).strip(" ") or "(ohne Name)"
    if len(name) > limit:
        name = name[:limit - 24] + "…" + name[-23:]
    return name


def is_zip(head: bytes) -> bool:
    return head[:4] in (b"PK\x03\x04", b"PK\x05\x06")


def open_zip(path: Path) -> zipfile.ZipFile:
    try:
        size = path.stat().st_size
    except OSError as e:
        raise ZipRejected("Die Archivdatei ist nicht mehr vorhanden.") from e
    if size > MAX_ZIP_BYTES:
        raise ZipRejected(f"Archiv zu groß (höchstens {MAX_ZIP_BYTES // (1024 * 1024)} MiB).")
    with path.open("rb") as f:
        if not is_zip(f.read(4)):
            raise ZipRejected("Das ist kein ZIP-Archiv (Dateityp wird am Inhalt erkannt, nicht am Namen).")
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ZipRejected("Das ZIP-Archiv ist beschädigt oder unvollständig.") from e


def members(zf: zipfile.ZipFile) -> tuple[list[Member], list[Skipped]]:
    """Mitglieder, die als PDF in Frage kommen, und übersprungene (mit Grund). Verstöße gegen die Grenzen lehnen das
    gesamte Archiv ab."""
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ZipRejected(f"Zu viele Einträge im Archiv (höchstens {MAX_ENTRIES}).")
    total = 0
    out: list[Member] = []
    skipped: list[Skipped] = []
    for i, info in enumerate(infos):
        if info.is_dir():
            continue
        name = display_name(info.filename)
        if info.flag_bits & 0x1:
            raise ZipRejected("Das Archiv ist passwortgeschützt (verschlüsselt) und kann nicht gelesen werden.")
        total += info.file_size
        if total > MAX_TOTAL:
            raise ZipRejected("Das Archiv ist entpackt zu groß.")
        if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_RATIO:
            raise ZipRejected("Auffälliges Kompressionsverhältnis (mögliche ZIP-Bombe) – Archiv abgelehnt.")
        parts = info.filename.replace("\\", "/").split("/")
        if parts[0] == "__MACOSX" or name.startswith(".") or name.lower() in ("thumbs.db", "desktop.ini"):
            continue  # Systemdateien: nicht zählen
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            skipped.append(Skipped(name, "Verknüpfung (Symlink) – nicht übernommen"))
            continue
        if name.lower().endswith((".zip", ".7z", ".rar", ".gz", ".tar")):
            skipped.append(Skipped(name, "verschachteltes Archiv – wird nicht entpackt"))
            continue
        if info.file_size > MAX_FILE:
            skipped.append(Skipped(name, f"zu groß (höchstens {MAX_FILE // (1024 * 1024)} MiB je Datei)"))
            continue
        if not name.lower().endswith(".pdf"):
            skipped.append(Skipped(name, "keine PDF-Datei"))
            continue
        out.append(Member(i, name, info.file_size, info.compress_size))
    return out, skipped


def read_member(zf: zipfile.ZipFile, m: Member) -> bytes:
    """Mitglied lesen – mit harter Obergrenze, auch wenn der Kopfeintrag eine kleinere Größe vortäuscht."""
    info = zf.infolist()[m.index]
    try:
        with zf.open(info) as f:
            data = f.read(MAX_FILE + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError, ValueError) as e:
        raise ZipRejected("Eintrag nicht lesbar (beschädigt).") from e
    if len(data) > MAX_FILE:
        raise ZipRejected(f"zu groß (höchstens {MAX_FILE // (1024 * 1024)} MiB je Datei)")
    if not data.startswith(b"%PDF-"):
        raise ZipRejected("Inhalt ist kein PDF (Dateityp wird am Inhalt erkannt).")
    return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(text: str, fallback: str) -> str:
    return (_SAFE.sub("-", text).strip("-_") or fallback)[:60]


def relative_path(c: Classified, n: int = 1) -> str:
    """Pfad relativ zum Ordner der Datenquelle – ausschließlich aus erkannten Merkmalen gebaut (ASCII)."""
    depot = f"Depot-{c.depot_short}" if c.depot_short else "Depot-unbekannt"
    stamp = c.doc_date or "ohne-Datum"
    ident = _slug(c.ident, "")
    stem = "_".join(x for x in (stamp, c.kind, ident) if x)
    if n > 1:
        stem += f"_{n}"
    return f"{CATEGORY}/{depot}/{stem}.pdf"


@dataclass(frozen=True)
class Placed:
    rel: str
    created: bool  # False = identischer Inhalt lag bereits dort


def place(root: Path, c: Classified, data: bytes) -> Placed:
    """Datei ablegen, ohne je etwas zu überschreiben. Der Zielpfad wird geprüft (unterhalb von ``root``)."""
    sha = sha256(data)
    base = root.resolve()
    for n in range(1, 1000):
        rel = relative_path(c, n)
        target = (base / rel).resolve()
        if not str(target).startswith(str(base) + os.sep):
            raise ZipRejected("Zielpfad ungültig.")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        except FileExistsError:
            try:
                if sha256(target.read_bytes()) == sha:
                    return Placed(rel, False)
            except OSError:
                pass
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return Placed(rel, True)
    raise ZipRejected("Kein freier Dateiname gefunden.")
