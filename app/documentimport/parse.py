"""Zahlen, Beträge, Währungen, Datum und Uhrzeit aus Belegen – deterministisch, mit Decimal.

Zahlenformat je Dokument: Eindeutige Schreibweisen (``1.234,56``, ``1,234.56``, ``12,34 €``, ``0,00001234``) legen
die Konvention fest (:func:`convention`). Mehrdeutige Einzelwerte (``1.234`` = 1234 oder 1,234?) werden nur mit
dieser Konvention gelesen – sonst gelten sie als **ungelöst** (:class:`Ambiguous`), nie geraten.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

CURRENCY_SIGNS = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY", "₣": "CHF", "CHF": "CHF", "Fr.": "CHF"}
FIAT = frozenset({"EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "TRY",
                  "HKD", "SGD", "CNY", "NZD", "ZAR", "MXN", "BRL", "INR", "KRW"})
NUM_RE = re.compile(r"(?<![\w.,])[-−+]?\(?(?:\d{1,3}(?:[.,'   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\)?(?![\w])")
_MONTHS = {"jan": 1, "januar": 1, "january": 1, "feb": 2, "februar": 2, "february": 2, "mär": 3, "mar": 3,
           "märz": 3, "maerz": 3, "march": 3, "apr": 4, "april": 4, "mai": 5, "may": 5, "jun": 6, "juni": 6,
           "june": 6, "jul": 7, "juli": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
           "okt": 10, "oct": 10, "oktober": 10, "october": 10, "nov": 11, "november": 11, "dez": 12, "dec": 12,
           "dezember": 12, "december": 12}
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))
DATE_PATTERNS = [
    ("dmy", re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)")),
    ("iso", re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")),
    ("slash", re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)")),
    ("d_mon_y", re.compile(rf"(?<!\w)(\d{{1,2}})\.?\s+({_MON})\.?\s+(\d{{4}})(?!\d)", re.I)),
    ("mon_d_y", re.compile(rf"(?<!\w)({_MON})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})(?!\d)", re.I)),
]
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?:\s*(AM|PM|am|pm))?"
                     r"(?:\s*(UTC|GMT|MEZ|MESZ|CET|CEST|Z|[+-]\d{2}:?\d{2}))?(?!\d)")
LOCAL_TZ = ZoneInfo("Europe/Berlin")


class Ambiguous(ValueError):
    """Wert ist ohne weitere Angabe nicht eindeutig lesbar (Grund im Text)."""


@dataclass(frozen=True)
class Convention:
    decimal: str | None  # "," | "." | None (unbekannt)
    evidence: str = ""


def _clean(raw: str) -> tuple[str, bool]:
    s = raw.strip().replace(" ", " ").replace(" ", " ").replace("−", "-").replace("–", "-")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        s, neg = s[1:-1], True
    for sign in sorted(CURRENCY_SIGNS, key=len, reverse=True):
        s = s.replace(sign, "")
    s = re.sub(r"\b[A-Z]{3}\b", "", s).strip()
    if s.endswith("-") and not s.startswith("-"):  # „1.234,56-“ (Soll-Kennzeichen)
        s, neg = s[:-1], True
    if s.startswith("-"):
        s, neg = s[1:], not neg
    elif s.startswith("+"):
        s = s[1:]
    return s.strip(), neg


def classify(raw: str) -> str | None:
    """Welche Dezimalkonvention belegt dieser Wert eindeutig? "," | "." | None."""
    s, _neg = _clean(raw)
    s = s.replace(" ", "").replace("'", "")
    dot, com = s.rfind("."), s.rfind(",")
    if dot >= 0 and com >= 0:
        return "," if com > dot else "."
    if dot < 0 and com < 0:
        return None
    sep = "," if com >= 0 else "."
    if s.count(sep) > 1:
        return "." if sep == "," else ","  # mehrfach = Gruppierung → anderes Zeichen ist Dezimaltrenner
    before, after = s.split(sep)
    if len(after) != 3 or before in ("0", "") or before.startswith("0"):
        return sep
    return None


def convention(texts: list[str]) -> Convention:
    """Dezimalkonvention eines Dokuments aus den eindeutigen Zahlen (Mehrheit, Gleichstand → unbekannt)."""
    votes: Counter[str] = Counter()
    for t in texts:
        for m in NUM_RE.finditer(t):
            c = classify(m.group())
            if c:
                votes[c] += 1
    if not votes:
        return Convention(None, "keine eindeutige Zahl im Dokument")
    (top, n), *rest = votes.most_common()
    if rest and rest[0][1] == n:
        return Convention(None, f"widersprüchliche Zahlenformate ({dict(votes)})")
    label = "deutsch (1.234,56)" if top == "," else "international (1,234.56)"
    return Convention(top, f"{label}: {n} eindeutige Werte" + (f", {rest[0][1]} abweichend" if rest else ""))


def number(raw: str, conv: Convention | None = None) -> Decimal:
    """Zahl als Decimal. Mehrdeutig ohne Dokumentkonvention → :class:`Ambiguous`."""
    s, neg = _clean(raw)
    s = s.replace("'", "")
    if re.fullmatch(r"\d{1,3}(?: \d{3})+(?:[.,]\d+)?", s):
        s = s.replace(" ", "")
    s = s.replace(" ", "")
    if not re.fullmatch(r"\d[\d.,]*", s or "x"):
        raise Ambiguous(f"keine Zahl: {raw!r}")
    c = classify(s)
    dec = c or (conv.decimal if conv else None)
    if c is None and ("," in s or "." in s) and dec is None:
        raise Ambiguous(f"„{raw.strip()}“: Tausender- oder Dezimaltrenner? Zahlenformat bestätigen")
    if dec is None:
        dec = "."
    group = "," if dec == "." else "."
    if s.count(dec) > 1:
        raise Ambiguous(f"„{raw.strip()}“: Trennzeichen widersprechen dem Dokumentformat")
    if dec in s and group in s and s.rfind(group) > s.rfind(dec):
        raise Ambiguous(f"„{raw.strip()}“: Trennzeichen widersprechen dem Dokumentformat")
    if group in s and not re.fullmatch(rf"\d{{1,3}}(?:{re.escape(group)}\d{{3}})*(?:{re.escape(dec)}\d+)?", s):
        raise Ambiguous(f"„{raw.strip()}“: ungültige Zifferngruppierung")
    try:
        v = Decimal(s.replace(group, "").replace(dec, "."))
    except InvalidOperation as e:
        raise Ambiguous(f"keine Zahl: {raw!r}") from e
    if not v.is_finite():
        raise Ambiguous(f"keine endliche Zahl: {raw!r}")
    return -v if neg else v


def currency_of(text: str) -> str | None:
    """Währung (ISO) in einem Textstück: Symbol oder ISO-Code einer Fiat-Währung."""
    for sign, code in CURRENCY_SIGNS.items():
        if sign in text and not sign.isalpha():
            return code
    for m in re.finditer(r"\b([A-Z]{3})\b", text):
        if m.group(1) in FIAT:
            return m.group(1)
    return None


@dataclass(frozen=True)
class Amount:
    value: Decimal
    currency: str | None
    raw: str
    start: int
    end: int


def amounts(text: str, conv: Convention | None) -> tuple[list[Amount], list[str]]:
    """Alle Zahlen einer Zeile mit (nachgestellter bzw. vorangestellter) Währung. Mehrdeutige → Hinweise."""
    out: list[Amount] = []
    notes: list[str] = []
    for m in NUM_RE.finditer(text):
        raw = m.group()
        if re.fullmatch(r"\d{1,2}", raw.strip()) and _in_date(text, m.start()):
            continue
        if _in_date(text, m.start()) or _in_time(text, m.start()):
            continue
        try:
            v = number(raw, conv)
        except Ambiguous as e:
            notes.append(str(e))
            continue
        tail = text[m.end():m.end() + 6]
        head = text[max(0, m.start() - 4):m.start()]
        ccy = None
        mt = re.match(r"\s*(€|\$|£|[A-Z]{3}\b)", tail)
        mh = re.search(r"(€|\$|£|\b[A-Z]{3})\s*$", head)
        if mt:
            ccy = CURRENCY_SIGNS.get(mt.group(1), mt.group(1))
        elif mh:
            ccy = CURRENCY_SIGNS.get(mh.group(1), mh.group(1))
        out.append(Amount(v, ccy, raw, m.start(), m.end()))
    return out, notes


def _in_date(text: str, pos: int) -> bool:
    for _k, rx in DATE_PATTERNS:
        for m in rx.finditer(text):
            if m.start() <= pos < m.end():
                return True
    return False


def _in_time(text: str, pos: int) -> bool:
    return any(m.start() <= pos < m.end() for m in TIME_RE.finditer(text))


def _year(y: str) -> int:
    n = int(y)
    return n + 2000 if n < 100 else n


def dates(text: str, *, us_slash: bool = False) -> tuple[list[tuple[date, int, int]], list[str]]:
    """Datumsangaben einer Zeile. ``dd/mm/yyyy`` vs. ``mm/dd/yyyy``: ohne Hinweis nur eindeutige Werte (Tag > 12)."""
    out: list[tuple[date, int, int]] = []
    notes: list[str] = []
    for kind, rx in DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "dmy":
                    d = date(_year(m.group(3)), int(m.group(2)), int(m.group(1)))
                elif kind == "iso":
                    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif kind == "slash":
                    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    if a <= 12 and b <= 12 and a != b and not us_slash:
                        notes.append(f"„{m.group()}“: Tag/Monat-Reihenfolge unklar")
                        continue
                    d = date(y, a, b) if (us_slash or b > 12) else date(y, b, a)
                elif kind == "d_mon_y":
                    d = date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
                else:
                    d = date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
            except (ValueError, KeyError):
                continue
            if not (1990 <= d.year <= 2100):
                continue
            if any(s <= m.start() < e for _d, s, e in out):
                continue
            out.append((d, m.start(), m.end()))
    out.sort(key=lambda x: x[1])
    return out, notes


def times(text: str) -> list[tuple[time, str | None, int, int]]:
    """Uhrzeiten (Stunde, Minute, Sekunde) mit optionaler Zeitzone; „12:00“-Platzhalter bleiben erhalten."""
    out = []
    for m in TIME_RE.finditer(text):
        h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        ampm = (m.group(4) or "").lower()
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        out.append((time(h, mi, se), m.group(5), m.start(), m.end()))
    return out


def tz_of(label: str | None) -> tuple[object, str]:
    """Zeitzone laut Beleg bzw. Annahme (lokal Europe/Berlin) mit Begründung."""
    if not label:
        return LOCAL_TZ, "ohne Zeitzonenangabe – als Ortszeit Europe/Berlin gelesen"
    u = label.upper()
    if u in ("UTC", "GMT", "Z"):
        return UTC, "laut Beleg UTC"
    if u in ("MEZ", "CET"):
        return timezone(timedelta(hours=1)), "laut Beleg MEZ"
    if u in ("MESZ", "CEST"):
        return timezone(timedelta(hours=2)), "laut Beleg MESZ"
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", label)
    if m:
        off = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return timezone(off if m.group(1) == "+" else -off), f"laut Beleg UTC{label}"
    return LOCAL_TZ, "Zeitzone unbekannt – als Ortszeit Europe/Berlin gelesen"


def combine(d: date, t: time | None, tz_label: str | None) -> tuple[datetime, bool, str]:
    """Datum (+ Uhrzeit) → UTC-Zeitpunkt; ohne Uhrzeit: 12:00 Ortszeit als **Sortierhilfe** mit date_only=True."""
    tz, why = tz_of(tz_label)
    if t is None:
        return datetime(d.year, d.month, d.day, 12, 0, tzinfo=LOCAL_TZ).astimezone(UTC), True, \
            "nur Datum belegt – keine Uhrzeit erfunden"
    return datetime.combine(d, t, tzinfo=tz).astimezone(UTC), False, why  # type: ignore[arg-type]
