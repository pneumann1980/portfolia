"""Dateien für den Börsen- und Wallet-Import annehmen: CSV, Excel (XLSX) und ZIP-Archive – sicher und nachvollziehbar.

* **CSV/TXT** werden unverändert weitergereicht (Kodierung, Trennzeichen, Präambel erkennt :mod:`.reader`).
* **XLSX** (Office Open XML) wird ohne Fremdbibliothek gelesen: nur Zellwerte, keine Formeln, keine Makros, keine
  externen Verknüpfungen; XML über ``defusedxml`` (keine Entitäten, keine externen Referenzen). Datumszellen werden
  anhand des Zahlenformats als ISO-Zeitpunkt ausgegeben. Makro-Arbeitsmappen (``.xlsm``), Binärformate (``.xls``,
  ``.xlsb``) und verschlüsselte Dateien werden abgelehnt.
* **ZIP**: höchstens :data:`MAX_MEMBERS` Dateien, :data:`MAX_TOTAL` Bytes entpackt, Kompressionsverhältnis höchstens
  :data:`MAX_RATIO`, keine verschachtelten Archive, keine verschlüsselten Einträge. Es wird **nichts auf die Platte
  entpackt** – Pfade dienen nur als Bezeichnung (Pfadangriffe wie ``../`` sind damit wirkungslos und werden
  zusätzlich gemeldet).
* **Portfolia-eigene Formate** werden erkannt und nicht als Börsenexport eingelesen: Import-ZIP bzw. Gesamtexport
  (``manifest.json`` + ``transactions.csv``) gehören in den Importordner bzw. zur Neueinrichtung. Der CSV-Export der
  Buchungen (``transactions.csv`` allein) ist dagegen ein gültiges CSV-Format (Profil „Portfolia“).

Jede angenommene Datei behält ihre Herkunft (Archiv, Eintrag, Tabellenblatt, Prüfsumme) für den Prüf-Stapel.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from defusedxml import ElementTree as ET

MAX_MEMBERS = 50
MAX_TOTAL = 100 * 1024 * 1024  # entpackt, alle Einträge zusammen
MAX_MEMBER = 30 * 1024 * 1024
MAX_RATIO = 100
MAX_CELLS = 3_000_000
TABLE_EXT = (".csv", ".txt", ".tsv")
XLSX_EXT = (".xlsx",)
REFUSED_EXT = {".xlsm": "Excel-Arbeitsmappe mit Makros (.xlsm) – Makros werden nie ausgeführt; bitte als .xlsx "
                        "oder CSV speichern",
               ".xls": "altes Excel-Binärformat (.xls) – bitte als .xlsx oder CSV speichern",
               ".xlsb": "Excel-Binärformat (.xlsb) – bitte als .xlsx oder CSV speichern",
               ".ods": "OpenDocument-Tabelle – bitte als .xlsx oder CSV speichern"}
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}
_DATE_FMT_IDS = set(range(14, 23)) | {45, 46, 47}
_EPOCH = datetime(1899, 12, 30)


class FileError(ValueError):
    """Datei bzw. Archiv wird nicht angenommen (Grund im Text)."""


@dataclass
class Part:
    """Eine einzulesende Tabelle mit Herkunft."""

    name: str  # Anzeigename (Datei bzw. „Archiv › Eintrag › Blatt“)
    data: bytes  # CSV-Text (UTF-8) bzw. Originalbytes einer CSV-Datei
    fmt: str  # csv | xlsx
    origin: dict[str, Any] = field(default_factory=dict)


@dataclass
class Unpacked:
    parts: list[Part] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # übersprungene Einträge, Hinweise
    kind: str = "table"  # table | portfolia_contract | portfolia_full


def _ext(name: str) -> str:
    m = re.search(r"(\.[A-Za-z0-9]{1,5})$", name)
    return m.group(1).lower() if m else ""


def _label(name: str) -> str:
    """Pfad nur als Bezeichnung: letzte Bestandteile, keine Steuerzeichen."""
    clean = re.sub(r"[\x00-\x1f]", "", name.replace("\\", "/"))
    parts = [p for p in clean.split("/") if p not in ("", ".", "..")]
    return "/".join(parts[-2:])[:120] or "Datei"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def unpack(data: bytes, filename: str) -> Unpacked:
    """Hochgeladene Datei in einzulesende Tabellen zerlegen (ohne Schreibzugriff, ohne Ausführung)."""
    name = _label(filename or "upload.csv")
    ext = _ext(name)
    if ext in REFUSED_EXT:
        raise FileError(f"{name}: {REFUSED_EXT[ext]}.")
    if data[:4] == b"PK\x03\x04" or ext == ".zip":
        if ext in XLSX_EXT or (ext != ".zip" and _is_xlsx(data)):
            return Unpacked(parts=xlsx_parts(data, name, {"file": name, "sha256": sha256(data)}))
        return _zip(data, name)
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise FileError(f"{name}: Office-Binärformat bzw. verschlüsselte Datei – bitte als .xlsx oder CSV speichern.")
    return Unpacked(parts=[Part(name, data, "csv", {"file": name, "sha256": sha256(data)})])


def _is_xlsx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return "xl/workbook.xml" in zf.namelist()
    except zipfile.BadZipFile:
        return False


def _zip(data: bytes, name: str) -> Unpacked:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise FileError(f"{name}: kein gültiges ZIP-Archiv.") from e
    out = Unpacked()
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        names = {i.filename.replace("\\", "/").split("/")[-1].lower() for i in infos}
        if "manifest.json" in names and "transactions.csv" in names:
            out.kind = "portfolia_full" if any("portfolia/state.json" in i.filename.replace("\\", "/")
                                               for i in infos) else "portfolia_contract"
            return out
        if len(infos) > MAX_MEMBERS:
            raise FileError(f"{name}: zu viele Dateien im Archiv ({len(infos)}, höchstens {MAX_MEMBERS}).")
        total = sum(i.file_size for i in infos)
        if total > MAX_TOTAL:
            raise FileError(f"{name}: entpackt zu groß ({total // 1024 // 1024} MB, höchstens "
                            f"{MAX_TOTAL // 1024 // 1024} MB).")
        for i in infos:
            label = _label(i.filename)
            if ".." in i.filename.replace("\\", "/").split("/") or i.filename.startswith(("/", "\\")):
                out.notes.append(f"{label}: unzulässiger Pfad im Archiv – nur als Bezeichnung verwendet")
            if i.flag_bits & 0x1:
                raise FileError(f"{name}: verschlüsselter Eintrag {label} – nicht unterstützt.")
            if i.file_size > MAX_MEMBER:
                out.notes.append(f"{label}: übersprungen (größer als {MAX_MEMBER // 1024 // 1024} MB)")
                continue
            if i.compress_size and i.file_size / max(i.compress_size, 1) > MAX_RATIO:
                raise FileError(f"{name}: Eintrag {label} ist ungewöhnlich stark komprimiert (möglicher Archiv-Angriff)"
                                " – abgelehnt.")
            ext = _ext(label)
            if ext == ".zip":
                out.notes.append(f"{label}: verschachteltes Archiv übersprungen – bitte einzeln hochladen")
                continue
            if ext in REFUSED_EXT:
                out.notes.append(f"{label}: übersprungen – {REFUSED_EXT[ext]}")
                continue
            if ext not in (*TABLE_EXT, *XLSX_EXT):
                out.notes.append(f"{label}: übersprungen (kein CSV/XLSX)")
                continue
            with zf.open(i) as fh:
                raw = fh.read(MAX_MEMBER + 1)
            if len(raw) > MAX_MEMBER:
                out.notes.append(f"{label}: übersprungen (zu groß)")
                continue
            origin = {"archive": name, "archive_sha256": sha256(data), "member": label, "sha256": sha256(raw)}
            disp = f"{name} › {label}"
            if ext in XLSX_EXT:
                try:
                    out.parts += xlsx_parts(raw, disp, origin)
                except FileError as e:
                    out.notes.append(str(e))
            else:
                out.parts.append(Part(disp, raw, "csv", origin))
    if not out.parts and out.kind == "table":
        raise FileError(f"{name}: keine CSV- oder XLSX-Datei im Archiv." + (" " + "; ".join(out.notes[:5])
                                                                              if out.notes else ""))
    return out


# ----------------------------------------------------------------------------------------------------
# XLSX
# ----------------------------------------------------------------------------------------------------

def _xml(zf: zipfile.ZipFile, path: str, limit: int = MAX_MEMBER) -> Any:
    try:
        info = zf.getinfo(path)
    except KeyError:
        return None
    if info.file_size > limit or (info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_RATIO):
        raise FileError(f"{path}: zu groß bzw. ungewöhnlich stark komprimiert.")
    with zf.open(info) as fh:
        return ET.fromstring(fh.read())


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):  # type: ignore[union-attr]
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _date_styles(zf: zipfile.ZipFile) -> set[int]:
    root = _xml(zf, "xl/styles.xml")
    if root is None:
        return set()
    custom = {}
    for nf in root.findall("m:numFmts/m:numFmt", _NS):
        code = (nf.get("formatCode") or "").lower()
        code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", code)
        is_date = bool(re.search(r"[dyh]", code)) or (bool(re.search(r"m", code)) and not re.search(r"[0#]", code))
        custom[int(nf.get("numFmtId") or 0)] = is_date
    out = set()
    for i, xf in enumerate(root.findall("m:cellXfs/m:xf", _NS)):
        fid = int(xf.get("numFmtId") or 0)
        if fid in _DATE_FMT_IDS or custom.get(fid):
            out.add(i)
    return out


def _serial(v: str) -> str:
    try:
        f = float(v)
    except ValueError:
        return v
    dt = _EPOCH + timedelta(days=f)
    dt = dt.replace(microsecond=0) if dt.microsecond < 500_000 else dt.replace(microsecond=0) + timedelta(seconds=1)
    return dt.date().isoformat() if f == int(f) else dt.isoformat(sep=" ")


def xlsx_parts(data: bytes, name: str, origin: dict[str, Any]) -> list[Part]:
    """Alle nicht leeren Tabellenblätter einer XLSX-Datei als CSV (UTF-8). Formeln werden nicht berechnet: es gilt
    der in der Datei gespeicherte Wert; Zellen ohne gespeicherten Wert bleiben leer."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise FileError(f"{name}: keine gültige XLSX-Datei.") from e
    with zf:
        nl = set(zf.namelist())
        if "xl/vbaProject.bin" in nl:
            raise FileError(f"{name}: Arbeitsmappe enthält Makros – Makros werden nie ausgeführt; bitte ohne Makros "
                            "(.xlsx) bzw. als CSV speichern.")
        if "EncryptedPackage" in nl or "xl/workbook.xml" not in nl:
            raise FileError(f"{name}: verschlüsselte bzw. unvollständige Arbeitsmappe.")
        shared: list[str] = []
        sst = _xml(zf, "xl/sharedStrings.xml")
        if sst is not None:
            for si in sst.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        dates = _date_styles(zf)
        wb = _xml(zf, "xl/workbook.xml")
        rels = _xml(zf, "xl/_rels/workbook.xml.rels")
        targets = {r.get("Id"): r.get("Target") for r in (rels.findall("pr:Relationship", _NS) if rels is not None
                                                          else [])}
        sheets = []
        for sh in wb.findall("m:sheets/m:sheet", _NS):
            rid = sh.get(f"{{{_NS['r']}}}id")
            target = (targets.get(rid) or "").lstrip("/")
            path = target if target.startswith("xl/") else f"xl/{target}"
            if path in nl:
                sheets.append((sh.get("name") or path, path))
        parts: list[Part] = []
        cells = 0
        for sheet_name, path in sheets:
            root = _xml(zf, path)
            rows: list[list[str]] = []
            for row in root.iter(f"{{{_NS['m']}}}row"):
                vals: dict[int, str] = {}
                for c in row.findall("m:c", _NS):
                    cells += 1
                    if cells > MAX_CELLS:
                        raise FileError(f"{name}: zu viele Zellen (höchstens {MAX_CELLS}).")
                    ref, t = c.get("r") or "", c.get("t")
                    v = c.find("m:v", _NS)
                    if t == "inlineStr":
                        text = "".join(x.text or "" for x in c.iter(f"{{{_NS['m']}}}t"))
                    elif v is None:
                        text = ""
                    elif t == "s":
                        i = int(v.text or 0)
                        text = shared[i] if 0 <= i < len(shared) else ""
                    elif t == "b":
                        text = "TRUE" if v.text == "1" else "FALSE"
                    elif t in ("str", "e"):
                        text = v.text or ""
                    else:
                        text = v.text or ""
                        if int(c.get("s") or 0) in dates:
                            text = _serial(text)
                    idx = _col(ref) if ref else len(vals)
                    vals[idx] = text
                if vals:
                    width = max(vals) + 1
                    rows.append([vals.get(i, "") for i in range(width)])
            rows = [r for r in rows if any(x.strip() for x in r)]
            if len(rows) < 2:
                continue
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerows(rows)
            disp = name if len(sheets) == 1 else f"{name} › {sheet_name}"
            parts.append(Part(disp, buf.getvalue().encode("utf-8"), "xlsx", {**origin, "sheet": sheet_name}))
    if not parts:
        raise FileError(f"{name}: keine Tabelle mit Daten gefunden.")
    return parts
