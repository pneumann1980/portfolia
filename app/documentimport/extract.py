"""Dokumentimport: reine, konservative Extraktion ohne Ledger-Schreibzugriff.

Alle Kandidaten behalten ihre Belegstelle. Unsichere Felder werden niemals still
als ausgeführte Transaktionswerte übernommen.
"""
from __future__ import annotations

import hashlib
import io
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_FILE = 25 * 1024 * 1024
MAX_PAGES = 50
MAX_PIXELS = 25_000_000
MAX_TEXT = 2_000_000
OCR_TIMEOUT = 20
FORMATS = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


class DocumentError(ValueError):
    """Ungültige, nicht unterstützte oder zu große Eingabe."""


@dataclass(frozen=True)
class Evidence:
    value: str
    status: str  # belegt | rekonstruiert | geschaetzt | ungeloest
    source: str
    location: str
    reason: str = ""


@dataclass
class DocumentPage:
    number: int
    text: str
    method: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class DocumentResult:
    sha256: str
    file_type: str
    pages: list[DocumentPage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def sniff(data: bytes) -> str:
    if not data or len(data) > MAX_FILE:
        raise DocumentError("Leere oder zu große Datei (maximal 25 MiB).")
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    raise DocumentError("Nicht unterstützter Dateiinhalt.")


def financial_decimal(text: str, *, decimal_hint: str | None = None) -> Decimal:
    """Explizit mehrdeutige Einzeltrenner abweisen statt Mengen falsch zu deuten."""
    s = text.strip().replace("\u00a0", "").replace(" ", "").replace("−", "-")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    s = re.sub(r"^[€$£]", "", s)
    s = re.sub(r"[€$£]$", "", s)
    if not re.fullmatch(r"[+-]?\d[\d.,]*", s):
        raise DocumentError("Unbekanntes Zahlenformat.")
    dot, comma = s.rfind("."), s.rfind(",")
    if dot >= 0 and comma >= 0:
        sep = "." if dot > comma else ","
    elif dot >= 0 or comma >= 0:
        sep = "." if dot >= 0 else ","
        before, after = s.lstrip("+-").split(sep)
        if len(after) == 3 and 1 <= len(before) <= 3 and not decimal_hint:
            raise DocumentError("Mehrdeutiger Zahlentrenner: Dezimalformat bestätigen.")
        if s.count(sep) > 1:
            raise DocumentError("Mehrdeutige Gruppierung: Zahlenformat bestätigen.")
        elif decimal_hint in (".", ","):
            sep = decimal_hint
    else:
        sep = "."
    group = "," if sep == "." else "."
    s = s.replace(group, "").replace(sep, ".")
    try:
        v = Decimal(s)
    except InvalidOperation as exc:
        raise DocumentError("Ungültige Zahl.") from exc
    if not v.is_finite():
        raise DocumentError("Ungültige Zahl.")
    return v


def _ocr_png(image: bytes, language: str) -> str:
    """Nur temporäres Raster; keine Shell, kein externes OCR-API."""
    with tempfile.TemporaryDirectory(prefix="portfolia-ocr-") as directory:
        path = Path(directory) / "page.png"
        path.write_bytes(image)
        try:
            result = subprocess.run(  # noqa: S603
                ["tesseract", str(path), "stdout", "-l", language],
                capture_output=True, timeout=OCR_TIMEOUT, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise DocumentError("Lokale OCR nicht verfügbar oder Zeitlimit überschritten.") from exc
        if result.returncode:
            raise DocumentError("Lokale OCR fehlgeschlagen.")
        return result.stdout.decode("utf-8", errors="replace")[:MAX_TEXT]


def _image_text(data: bytes, language: str) -> str:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise DocumentError("Pillow ist für Bildimport erforderlich.") from exc
    try:
        with Image.open(io.BytesIO(data)) as picture:
            width, height = picture.size
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                raise DocumentError("Bildabmessungen überschreiten das Limit.")
            picture.load()
            converted = ImageOps.exif_transpose(picture).convert("RGB")
            buffer = io.BytesIO()
            converted.save(buffer, format="PNG")
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise DocumentError("Beschädigtes oder unlesbares Bild.") from exc
    return _ocr_png(buffer.getvalue(), language)


def extract_document(data: bytes, *, language: str = "deu+eng") -> DocumentResult:
    """CPU-basierte Extraktion. Keine Netzwerkanfragen, keine aktiven PDF-Inhalte."""
    kind = sniff(data)
    result = DocumentResult(hashlib.sha256(data).hexdigest(), kind)
    if kind != "pdf":
        result.pages.append(DocumentPage(1, _image_text(data, language), "ocr"))
        return result
    try:
        import fitz
    except ImportError as exc:
        raise DocumentError("PyMuPDF ist für PDF-Import erforderlich.") from exc
    try:
        with fitz.open(stream=data, filetype="pdf") as pdf:
            if pdf.needs_pass:
                raise DocumentError("Passwortgeschützte PDFs werden nicht verarbeitet.")
            if len(pdf) > MAX_PAGES:
                raise DocumentError("PDF überschreitet das Seitenlimit.")
            for index, page in enumerate(pdf):
                text = page.get_text("text", sort=True)[:MAX_TEXT]
                method = "text"
                if len(text.strip()) < 32:
                    if page.rect.width * page.rect.height > 3_000_000:
                        raise DocumentError("PDF-Seite ist für OCR zu groß.")
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    if pix.width * pix.height > MAX_PIXELS:
                        raise DocumentError("PDF-Rendering überschreitet das Pixellimit.")
                    text = _ocr_png(pix.tobytes("png"), language)
                    method = "ocr"
                result.pages.append(DocumentPage(index + 1, text, method))
    except DocumentError:
        raise
    except (RuntimeError, ValueError, OSError) as exc:
        raise DocumentError("Beschädigtes oder ungültiges PDF.") from exc
    return result


# Nur Belegkandidaten – keine automatische Buchungsfreigabe.
_FIELD_PATTERNS = {
    "isin": re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b"),
    "txhash_evm": re.compile(r"\b0x[a-fA-F0-9]{64}\b"),
    "date": re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b"),
}


def field_evidence(document: DocumentResult) -> dict[str, list[Evidence]]:
    fields: dict[str, list[Evidence]] = {}
    for page in document.pages:
        for name, pattern in _FIELD_PATTERNS.items():
            for match in pattern.finditer(page.text):
                fields.setdefault(name, []).append(
                    Evidence(match.group(), "belegt", "document:" + document.sha256,
                             f"Seite {page.number}, Zeichen {match.start()}-{match.end()}",
                             "Textfund; fachliche Zuordnung noch zu prüfen")
                )
    return fields
