"""CSV-Dateien aus Exporten robust lesen: Kodierung, Trennzeichen, Präambel, Zahlen und Zeitpunkte.

* Kodierung: UTF-8 (mit/ohne BOM), UTF-16 (mit BOM), sonst Windows-1252.
* Trennzeichen: ``,`` ``;`` Tab ``|`` – gewählt wird das Zeichen, mit dem die meisten Zeilen gleich viele Felder
  (mindestens drei) ergeben.
* Kopfzeile: die erste Zeile, die ein Profil erkennt; ohne Profiltreffer die erste Zeile, ab der die Feldanzahl
  stabil bleibt. Präambeln (Bitpanda, Coinbase) werden so übersprungen.
* Doppelte Spaltennamen (CoinTracking: „Cur.“) werden durchnummeriert: ``cur.``, ``cur._2``, …
* Zahlen: Dezimalpunkt oder -komma, Tausendertrennzeichen, Währungszeichen und -kürzel, Unicode-Minus,
  Klammern als Minus. Mehrdeutiges „1,234“ entscheidet der Hinweis des Profils bzw. der Zuordnung.
* Zeitpunkte: ISO 8601, Unix-Zeit (s/ms), „18.03.2021 09:32“, „03/18/2021“, „2021-03-18 09:32:55 UTC“,
  „Mon Sep 27 2021 10:00:00 GMT+0200“; ohne Zeitzone gilt die Zeitzone des Profils bzw. der Auswahl.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

MAX_ROWS = 300_000
DELIMITERS = (",", ";", "\t", "|")


class CsvError(ValueError):
    """Datei ist keine lesbare CSV-Datei."""


def decode(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace"), "cp1252"


def norm(h: str) -> str:
    """Spaltenname vergleichbar machen (Kleinbuchstaben, einfache Leerzeichen, ohne BOM/Anführungszeichen)."""
    h = h.replace("﻿", "").replace("\xa0", " ").strip().strip('"').strip()
    return re.sub(r"\s+", " ", h).lower()


@dataclass
class Table:
    header: list[str]  # Originalnamen
    keys: list[str]  # normalisierte, eindeutige Namen
    rows: list[list[str]]
    lines: list[int]  # Zeilennummer (1-basiert) je Datenzeile
    delimiter: str
    encoding: str
    header_line: int
    preamble: list[str] = field(default_factory=list)

    def dicts(self) -> Iterator[tuple[int, dict[str, str]]]:
        n = len(self.keys)
        for ln, r in zip(self.lines, self.rows, strict=True):
            vals = [c.strip() for c in r[:n]] + [""] * max(0, n - len(r))
            yield ln, dict(zip(self.keys, vals, strict=True))

    def has(self, *names: str) -> bool:
        return all(norm(n) in self.keys for n in names)

    def find(self, *candidates: str) -> str | None:
        """Erster vorhandener Spaltenschlüssel aus mehreren Schreibweisen."""
        for c in candidates:
            k = norm(c)
            if k in self.keys:
                return k
        return None

    def find_prefix(self, prefix: str) -> str | None:
        p = norm(prefix)
        return next((k for k in self.keys if k.startswith(p)), None)


def unique_keys(header: list[str]) -> list[str]:
    out: list[str] = []
    seen: dict[str, int] = {}
    for h in header:
        k = norm(h) or "spalte"
        if k in seen:
            seen[k] += 1
            k = f"{k}_{seen[k]}"
        else:
            seen[k] = 1
        out.append(k)
    return out


def _rows(text: str, delimiter: str, limit: int | None = None) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    out = []
    try:
        for r in reader:
            out.append(r)
            if limit is not None and len(out) >= limit:
                break
    except csv.Error as e:
        if limit is None:
            raise CsvError(f"CSV-Struktur fehlerhaft (Zeile {reader.line_num}): {e}") from e
    return out


def sniff_delimiter(text: str) -> str:
    best, best_score = ",", -1.0
    for d in DELIMITERS:
        rows = [r for r in _rows(text, d, 60) if any(c.strip() for c in r)]
        if not rows:
            continue
        counts: dict[int, int] = {}
        for r in rows:
            if len(r) >= 3:
                counts[len(r)] = counts.get(len(r), 0) + 1
        if not counts:
            continue
        width, freq = max(counts.items(), key=lambda x: (x[1], x[0]))
        score = freq + width / 1000
        if score > best_score:
            best, best_score = d, score
    return best


def read_table(data: bytes, header_match: Callable[[list[str]], bool] | None = None,
               delimiter: str | None = None) -> Table:
    if not data.strip():
        raise CsvError("Die Datei ist leer.")
    if data[:4] == b"PK\x03\x04":
        raise CsvError("Das ist eine ZIP-Datei – bitte die enthaltene CSV-Datei hochladen (ein vollständiger "
                       "Portfolia-Export gehört ins Importverzeichnis).")
    if data[:5] in (b"%PDF-",) or (b"\x00\x00" in data[:2048] and not data.startswith((b"\xff\xfe", b"\xfe\xff"))):
        raise CsvError("Keine Textdatei – bitte den CSV-Export der Börse/Wallet verwenden (Excel: „Speichern unter → "
                       "CSV“).")
    text, enc = decode(data)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    d = delimiter or sniff_delimiter(text)
    rows = _rows(text, d)
    if len(rows) > MAX_ROWS:
        raise CsvError(f"Zu viele Zeilen ({len(rows)}; höchstens {MAX_ROWS}). Bitte den Export in Zeiträume "
                       "aufteilen.")
    # physische Zeilennummern (mehrzeilige Felder verschieben die Zählung – für Hinweise genügt das)
    idx = None
    for i, r in enumerate(rows[:60]):
        cells = [c for c in r if c.strip()]
        if len(cells) < 2:
            continue
        if header_match is not None and header_match([norm(c) for c in r]):
            idx = i
            break
    if idx is None:
        for i, r in enumerate(rows[:60]):
            if len([c for c in r if c.strip()]) < 3:
                continue
            nxt = [x for x in rows[i + 1:i + 4] if any(c.strip() for c in x)]
            if not nxt or all(len(x) >= len(r) - 1 for x in nxt):
                idx = i
                break
    if idx is None:
        raise CsvError("Keine Kopfzeile gefunden (mindestens drei Spalten erwartet).")
    header = [c.replace("﻿", "").strip() for c in rows[idx]]
    while header and not header[-1]:
        header.pop()
    body, lines = [], []
    for i, r in enumerate(rows[idx + 1:], start=idx + 2):
        if not any(c.strip() for c in r):
            continue
        body.append(r)
        lines.append(i)
    return Table(header=header, keys=unique_keys(header), rows=body, lines=lines, delimiter=d, encoding=enc,
                 header_line=idx + 1, preamble=[d.join(r) for r in rows[:idx]][:10])


# ----------------------------------------------------------------------------------------------------
# Zahlen
# ----------------------------------------------------------------------------------------------------

_NUM_CHARS = re.compile(r"[^0-9.,eE+\-]")
_THOUSANDS_COMMA = re.compile(r"^-?\d{1,3}(,\d{3})+$")
_THOUSANDS_DOT = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def num(v: str | None, decimal: str = ".") -> Decimal | None:
    """Zahl aus einer Zelle; leer, „-“, „n/a“ → None. ``decimal`` ist der erwartete Dezimaltrenner (Hinweis)."""
    if v is None:
        return None
    s = v.strip().replace("−", "-").replace("–", "-").replace("\xa0", "").replace(" ", "")
    if s in ("", "-", "--", "n/a", "N/A", "null", "None", "NaN", "nan"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    # Exponentialschreibweise erhalten, alles andere außer Ziffern/Trennern/Vorzeichen entfernen (€, $, BTC …)
    m = re.search(r"[-+]?[0-9][0-9.,]*(?:[eE][-+]?\d+)?|[-+]?[.,][0-9]+(?:[eE][-+]?\d+)?", s)
    if not m:
        return None
    lead = s[:m.start()]
    t = m.group(0)
    if "-" in lead and not t.startswith(("-", "+")):
        t = "-" + t
    mant, exp = [*re.split(r"[eE]", t, maxsplit=1), ""][:2]
    if "," in mant and "." in mant:
        if mant.rfind(",") > mant.rfind("."):
            mant = mant.replace(".", "").replace(",", ".")
        else:
            mant = mant.replace(",", "")
    elif "," in mant:
        if (_THOUSANDS_COMMA.match(mant) and decimal == ".") or mant.count(",") > 1:
            mant = mant.replace(",", "")
        else:
            mant = mant.replace(",", ".")
    elif ("." in mant and mant.count(".") > 1) or (_THOUSANDS_DOT.match(mant) and decimal == ","):
        mant = mant.replace(".", "")
    try:
        d = Decimal(mant + (f"e{exp}" if exp else ""))
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    return -abs(d) if neg else d


def amount_and_unit(v: str | None, decimal: str = ".") -> tuple[Decimal | None, str | None]:
    """„-0,5 BTC“ → (Decimal('-0.5'), 'BTC')."""
    if not v or not v.strip():
        return None, None
    s = v.strip()
    m = re.match(r"^(.*?)[\s\xa0]+([A-Za-z][A-Za-z0-9._-]{0,19})$", s)
    if m:
        return num(m.group(1), decimal), m.group(2).upper()
    return num(s, decimal), None


# ----------------------------------------------------------------------------------------------------
# Zeitpunkte
# ----------------------------------------------------------------------------------------------------

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?\s*"
                  r"(Z|UTC|GMT|[+-]\d{2}:?\d{2}|[+-]\d{2})?$", re.I)
_DMY = re.compile(r"^(\d{1,2})([./-])(\d{1,2})\2(\d{2,4})(?:[,\s]+(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?"
                  r"\s*(AM|PM)?)?\s*(Z|UTC|GMT|[+-]\d{2}:?\d{2})?$", re.I)
_YMD_SLASH = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
_JS = re.compile(r"^(?:[A-Za-z]{3},?\s+)?([A-Za-z]{3})\s+(\d{1,2}),?\s+(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*"
                 r"(?:GMT|UTC)?\s*([+-]\d{2}:?\d{2})?")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), start=1)}
_MONTHS.update({"mär": 3, "mai": 5, "okt": 10, "dez": 12})


def _tzinfo(s: str | None, default: ZoneInfo | timezone) -> ZoneInfo | timezone:
    if not s:
        return default
    s = s.upper()
    if s in ("Z", "UTC", "GMT"):
        return UTC
    sign = -1 if s.startswith("-") else 1
    digits = s[1:].replace(":", "")
    hh, mm = int(digits[:2]), int(digits[2:4] or 0)
    return timezone(sign * timedelta(hours=hh, minutes=mm))


def _mk(y: int, mo: int, d: int, h: int, mi: int, sec: int, us: int, tz: ZoneInfo | timezone) -> datetime:
    if y < 100:
        y += 2000
    return datetime(y, mo, d, h, mi, sec, us, tzinfo=tz).astimezone(UTC)


def parse_ts(v: str | None, tz: ZoneInfo | timezone = UTC, dayfirst: bool | None = None) -> tuple[datetime, bool]:
    """Zeitpunkt (UTC) und ob nur ein Datum angegeben war. ``dayfirst`` None = automatisch (Punkt → Tag zuerst)."""
    if v is None or not v.strip():
        raise ValueError("Zeitpunkt fehlt")
    s = v.strip().replace("\xa0", " ")
    if re.fullmatch(r"\d{9,10}(\.\d+)?", s):
        return datetime.fromtimestamp(float(s), UTC), False
    if re.fullmatch(r"\d{12,13}", s):
        return datetime.fromtimestamp(int(s) / 1000, UTC), False
    m = _ISO.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if m.group(4) is None:
            return _mk(y, mo, d, 12, 0, 0, 0, tz), True
        frac = (m.group(7) or "0")[:6].ljust(6, "0")
        return _mk(y, mo, d, int(m.group(4)), int(m.group(5)), int(m.group(6) or 0), int(frac),
                   _tzinfo(m.group(8), tz)), False
    m = _DMY.match(s)
    if m:
        a, sep, b, y = int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))
        first_day = dayfirst if dayfirst is not None else sep in (".", "-")
        if a > 12:
            first_day = True
        elif b > 12:
            first_day = False
        d, mo = (a, b) if first_day else (b, a)
        if m.group(5) is None:
            return _mk(y, mo, d, 12, 0, 0, 0, tz), True
        h = int(m.group(5))
        ampm = (m.group(8) or "").upper()
        if ampm == "PM" and h < 12:
            h += 12
        elif ampm == "AM" and h == 12:
            h = 0
        return _mk(y, mo, d, h, int(m.group(6)), int(m.group(7) or 0), 0, _tzinfo(m.group(9), tz)), False
    m = _YMD_SLASH.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if m.group(4) is None:
            return _mk(y, mo, d, 12, 0, 0, 0, tz), True
        return _mk(y, mo, d, int(m.group(4)), int(m.group(5)), int(m.group(6) or 0), 0, tz), False
    m = _JS.match(s)
    if m and m.group(1)[:3].lower() in _MONTHS:
        mo = _MONTHS[m.group(1)[:3].lower()]
        return _mk(int(m.group(3)), mo, int(m.group(2)), int(m.group(4)), int(m.group(5)), int(m.group(6) or 0), 0,
                   _tzinfo(m.group(7), tz)), False
    try:  # seltene Formate (z. B. „27 Sep 2021 10:00“); python-dateutil ist über pandas vorhanden
        from dateutil import parser as du

        dt = du.parse(s, dayfirst=bool(dayfirst), fuzzy=True)
    except (ImportError, ValueError, OverflowError) as e:
        raise ValueError(f"Zeitpunkt nicht lesbar: {v!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC), False


def zone(name: str | None) -> ZoneInfo | timezone:
    if not name or name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        return UTC
