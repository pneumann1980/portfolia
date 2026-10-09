"""Dokumentimport (M25): sichere, lokale Extraktion von Text und Zeilen aus PDFs und Bildern.

* **Dateityp aus dem Inhalt** (Signatur), nie aus Dateiname oder Content-Type; Polyglotte/unbekannte Inhalte werden
  abgewiesen.
* **PDF:** eingebetteter Text über PDFium (``pypdfium2``, Apache-2.0/BSD) – zeichengenau mit Koordinaten, daraus
  Zeilen mit erkannten Spaltenabständen (Tabellen). Keine Formulare, kein JavaScript, keine eingebetteten Dateien,
  keine Links werden ausgeführt oder geöffnet; passwortgeschützte PDFs werden abgelehnt.
* **OCR nur bei Bedarf** (Scan-Seite ohne brauchbaren Text, Bilder/Screenshots): Tesseract lokal per Kommandozeile
  (TSV mit Wort-Konfidenz), Vorverarbeitung mit Pillow (EXIF-Drehung, Graustufen, dunkler Hintergrund → invertiert,
  Kontrast, Vergrößerung kleiner Schrift). Unsichere Zahlenzeilen werden **gezielt** als Ausschnitt nachgelesen statt
  die ganze Seite erneut zu erkennen.
* **Grenzen:** Dateigröße, Seiten, Pixel, Textmenge, Zeit je OCR-Aufruf; der Aufrufer (:mod:`.worker`) begrenzt
  zusätzlich Speicher, CPU-Zeit und Gesamtlaufzeit des Prozesses. Temporäre Dateien liegen nur in einem eigenen
  Verzeichnis und werden immer gelöscht.

Keine Netzwerkzugriffe. Ergebnisse sind **Belegstellen** (Seite, Zeile, Ausschnitt), keine Buchungen.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

MAX_FILE = 25 * 1024 * 1024
MAX_PAGES = 50
MAX_PIXELS = 30_000_000  # Bild bzw. gerenderte PDF-Seite
MAX_PAGE_PT = 4_000_000  # PDF-Seitenfläche in pt² (A4 ≈ 0,5 Mio.) – Schutz vor riesigen Seiten
MAX_TEXT = 400_000  # Zeichen je Seite
MAX_CHARS = 200_000  # Zeichen je PDF-Seite für die Zeilenrekonstruktion
OCR_TIMEOUT = 45  # Sekunden je Tesseract-Aufruf
MIN_TEXT_CHARS = 24  # weniger eingebetteter Text → Seite gilt als Scan
OCR_DPI_SCALE = 300 / 72
RECHECK_MAX = 12  # gezielte Nach-OCR je Seite (unsichere Zahlenzeilen)
RECHECK_CONF = 75.0
FORMATS = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
_NUMERIC = re.compile(r"\d[\d.,]*\d|\d")


class DocumentError(ValueError):
    """Ungültige, nicht unterstützte, zu große oder nicht lesbare Eingabe (Text ohne Dateiinhalte)."""


@dataclass
class Line:
    text: str
    page: int
    no: int
    conf: float | None = None  # OCR: mittlere Wort-Konfidenz 0–100; PDF-Text: None (eingebettet = exakt)
    box: tuple[float, float, float, float] | None = None  # relativ zur Seite: x0, y0, x1, y1 (0–1, y nach unten)
    rechecked: bool = False


@dataclass
class Page:
    number: int
    method: str  # text | ocr
    lines: list[Line] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conf: float | None = None
    width: float = 0.0
    height: float = 0.0

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


@dataclass
class DocumentResult:
    sha256: str
    file_type: str
    size: int = 0
    pages: list[Page] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ocr_used: bool = False
    ms: dict[str, int] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    def lines(self) -> list[Line]:
        return [ln for p in self.pages for ln in p.lines]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> DocumentResult:
        pages = []
        for p in d.get("pages") or []:
            lines = [Line(**{**ln, "box": tuple(ln["box"]) if ln.get("box") else None}) for ln in p.get("lines") or []]
            pages.append(Page(**{**p, "lines": lines}))
        return cls(**{**d, "pages": pages})


# ----------------------------------------------------------------------------------------------------
# Dateityp
# ----------------------------------------------------------------------------------------------------

def sniff(data: bytes) -> str:
    """Dateityp anhand der Signatur (Magic Bytes). Leere, zu große und unbekannte Inhalte → :class:`DocumentError`."""
    if not data:
        raise DocumentError("Leere Datei.")
    if len(data) > MAX_FILE:
        raise DocumentError(f"Datei zu groß (höchstens {MAX_FILE // (1024 * 1024)} MiB).")
    head = data[:1024]
    if data.startswith(b"%PDF-") or (b"%PDF-" in head and head.index(b"%PDF-") < 8):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"PK":
        raise DocumentError("ZIP-/Office-Dateien werden hier nicht verarbeitet (CSV-/ZIP-Import verwenden).")
    raise DocumentError("Nicht unterstützter Dateiinhalt (erwartet: PDF, PNG, JPEG oder WebP).")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------------------------------------

def _pdf_lines(textpage: Any, width: float, height: float, page_no: int) -> list[Line]:
    """Zeichen mit Koordinaten → Zeilen. Große horizontale Lücken werden zu „  “ (Spaltengrenze einer Tabelle)."""
    n = min(textpage.count_chars(), MAX_CHARS)
    if n <= 0:
        return []
    text = textpage.get_text_range(0, n)
    chars: list[tuple[float, float, float, float, str]] = []
    for i in range(min(n, len(text))):
        ch = text[i]
        if ch in "\r\n":
            continue
        left, bottom, right, top = textpage.get_charbox(i, loose=True)
        if right <= left and ch.isspace():
            continue
        chars.append((left, bottom, right, top, ch))
    if not chars:
        return []
    heights = [c[3] - c[1] for c in chars if c[3] > c[1]]
    h_med = median(heights) if heights else 8.0
    widths = [c[2] - c[0] for c in chars if c[2] > c[0] and not c[4].isspace()]
    w_med = median(widths) if widths else 4.0
    rows: list[list[tuple[float, float, float, float, str]]] = []
    for c in sorted(chars, key=lambda c: (-(c[1] + c[3]) / 2, c[0])):
        mid = (c[1] + c[3]) / 2
        if rows:
            r = rows[-1]
            r_mid = sum((x[1] + x[3]) / 2 for x in r) / len(r)
            if abs(r_mid - mid) <= max(h_med * 0.45, 1.0):
                r.append(c)
                continue
        rows.append([c])
    out: list[Line] = []
    for r in rows:
        r.sort(key=lambda c: c[0])
        parts: list[str] = []
        prev_right = None
        for left, _b, right, _t, ch in r:
            if prev_right is not None:
                gap = left - prev_right
                if gap > w_med * 2.2:
                    if parts and parts[-1] != " ":
                        parts.append("  ")
                    elif parts:
                        parts[-1] = "  "
                elif gap > w_med * 0.35 and not ch.isspace() and parts and not parts[-1].endswith(" "):
                    parts.append(" ")
            parts.append(ch)
            if not ch.isspace():
                prev_right = right
        s = re.sub(r"(?<! ) (?! )", " ", "".join(parts)).strip()
        s = re.sub(r" {3,}", "  ", s)
        if not s:
            continue
        x0 = min(c[0] for c in r) / width if width else 0.0
        x1 = max(c[2] for c in r) / width if width else 1.0
        y0 = 1 - max(c[3] for c in r) / height if height else 0.0
        y1 = 1 - min(c[1] for c in r) / height if height else 1.0
        out.append(Line(s[:2000], page_no, len(out) + 1, None, (round(x0, 4), round(y0, 4), round(x1, 4),
                                                                 round(y1, 4))))
    return out


def _open_pdf(data: bytes) -> Any:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - Abhängigkeit fehlt
        raise DocumentError("PDF-Bibliothek (pypdfium2) nicht installiert.") from exc
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        msg = str(exc).lower()
        if "password" in msg:
            raise DocumentError("Passwortgeschützte PDFs werden nicht verarbeitet – bitte ungeschützt exportieren.") \
                from exc
        raise DocumentError("Beschädigtes oder ungültiges PDF.") from exc


def render_page(data: bytes, page_no: int, scale: float = 2.0) -> Any:
    """Eine PDF-Seite als PIL-Bild (für OCR bzw. die Belegvorschau). Pixelgrenze wird eingehalten."""
    pdf = _open_pdf(data)
    try:
        if not 1 <= page_no <= len(pdf):
            raise DocumentError("Seite nicht vorhanden.")
        page = pdf[page_no - 1]
        try:
            w, h = page.get_size()
            if w * h > MAX_PAGE_PT:
                raise DocumentError("PDF-Seite ist zu groß.")
            scale = min(scale, (MAX_PIXELS / max(w * h, 1.0)) ** 0.5)
            bitmap = page.render(scale=scale, may_draw_forms=False)
            try:
                return bitmap.to_pil().convert("RGB")
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        pdf.close()


def _extract_pdf(data: bytes, res: DocumentResult, language: str, ocr: bool) -> None:
    pdf = _open_pdf(data)
    try:
        n = len(pdf)
        if n > MAX_PAGES:
            raise DocumentError(f"PDF hat {n} Seiten – höchstens {MAX_PAGES} je Dokument.")
        if pdf.count_attachments():
            res.warnings.append("Eingebettete Dateien im PDF wurden ignoriert.")
        for i in range(n):
            page = pdf[i]
            try:
                w, h = page.get_size()
                if w * h > MAX_PAGE_PT:
                    raise DocumentError(f"PDF-Seite {i + 1} ist zu groß.")
                tp = page.get_textpage()
                try:
                    lines = _pdf_lines(tp, w, h, i + 1)
                finally:
                    tp.close()
                pg = Page(i + 1, "text", lines, width=w, height=h)
                if sum(len(ln.text) for ln in lines) < MIN_TEXT_CHARS:
                    if not ocr:
                        pg.warnings.append("Kein eingebetteter Text – OCR ist abgeschaltet.")
                    else:
                        scale = min(OCR_DPI_SCALE, (MAX_PIXELS / max(w * h, 1.0)) ** 0.5)
                        bitmap = page.render(scale=scale, may_draw_forms=False)
                        try:
                            img = bitmap.to_pil().convert("RGB")
                        finally:
                            bitmap.close()
                        pg = _ocr_page(img, i + 1, language)
                        pg.width, pg.height = w, h
                        res.ocr_used = True
                res.pages.append(pg)
            finally:
                page.close()
    finally:
        pdf.close()


# ----------------------------------------------------------------------------------------------------
# OCR
# ----------------------------------------------------------------------------------------------------

def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def _prepare(img: Any) -> tuple[Any, float, list[str]]:
    """Vorverarbeitung für Tesseract: Graustufen, dunkler Hintergrund → invertiert, Kontrast, kleine Schrift
    vergrößern. Rückgabe: (Bild, Skalierungsfaktor, Hinweise)."""
    from PIL import ImageOps, ImageStat

    notes: list[str] = []
    g = ImageOps.grayscale(img)
    mean = ImageStat.Stat(g).mean[0]
    if mean < 110:  # dunkles Design (Dark Mode, Wallet-Apps)
        g = ImageOps.invert(g)
        notes.append("dunkler Hintergrund – invertiert")
    g = ImageOps.autocontrast(g, cutoff=0)  # kein Abschneiden: Text kann <1 % der Fläche sein (Scan)
    factor = 1.0
    w, h = g.size
    if h < 1400 and w * h * 4 <= MAX_PIXELS:  # Screenshot mit kleiner Schrift: 2× vergrößern
        factor = 2.0
        g = g.resize((w * 2, h * 2), resample=3)  # BICUBIC
        notes.append("vergrößert (kleine Schrift)")
    return g, factor, notes


def _tesseract(img: Any, language: str, psm: int, extra: list[str] | None = None) -> str:
    """Tesseract als Prozess ohne Shell – Eingabe als temporäre PNG-Datei in einem eigenen Verzeichnis."""
    exe = shutil.which("tesseract")
    if exe is None:
        raise DocumentError("Lokale OCR (Tesseract) ist nicht installiert – Screenshots und Scans können nicht "
                            "gelesen werden.")
    with tempfile.TemporaryDirectory(prefix="portfolia-ocr-") as d:
        path = os.path.join(d, "page.png")
        img.save(path, format="PNG")
        env = {"PATH": os.environ.get("PATH", ""), "OMP_THREAD_LIMIT": "1", "LANG": "C.UTF-8"}
        if os.environ.get("TESSDATA_PREFIX"):
            env["TESSDATA_PREFIX"] = os.environ["TESSDATA_PREFIX"]
        try:
            r = subprocess.run([exe, path, "stdout", "-l", language, "--psm", str(psm), *(extra or [])],  # noqa: S603
                               capture_output=True, timeout=OCR_TIMEOUT, check=False, env=env)
        except subprocess.TimeoutExpired as exc:
            raise DocumentError("OCR-Zeitlimit überschritten.") from exc
        if r.returncode != 0:
            raise DocumentError("Lokale OCR fehlgeschlagen (Sprachdaten deu/eng installiert?).")
        return r.stdout.decode("utf-8", errors="replace")[:MAX_TEXT * 4]


def _tsv_lines(tsv: str, page_no: int, w: int, h: int) -> list[Line]:
    words: dict[tuple[int, int, int], list[tuple[int, int, int, int, float, str]]] = {}
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        txt = cols[11].strip()
        if not txt:
            continue
        try:
            key = (int(cols[2]), int(cols[3]), int(cols[4]))
            left, top, width, height, conf = int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]), float(cols[10])
        except ValueError:
            continue
        words.setdefault(key, []).append((left, top, width, height, conf, txt))
    out: list[Line] = []
    for key in sorted(words, key=lambda k: (min(x[1] for x in words[k]), k)):
        ws = sorted(words[key], key=lambda x: x[0])
        ch_w = median([x[2] / max(len(x[5]), 1) for x in ws]) if ws else 8
        parts: list[str] = []
        prev = None
        for left, _top, width, _h, _c, txt in ws:
            if prev is not None:
                parts.append("  " if left - prev > ch_w * 2.5 else " ")
            parts.append(txt)
            prev = left + width
        conf = sum(x[4] for x in ws if x[4] >= 0) / max(1, sum(1 for x in ws if x[4] >= 0))
        x0, y0 = min(x[0] for x in ws), min(x[1] for x in ws)
        x1, y1 = max(x[0] + x[2] for x in ws), max(x[1] + x[3] for x in ws)
        out.append(Line("".join(parts)[:2000], page_no, len(out) + 1, round(conf, 1),
                        (round(x0 / w, 4), round(y0 / h, 4), round(x1 / w, 4), round(y1 / h, 4))))
    return out


def _ocr_page(img: Any, page_no: int, language: str) -> Page:
    from PIL import ImageOps

    w0, h0 = img.size
    if w0 <= 0 or h0 <= 0 or w0 * h0 > MAX_PIXELS:
        raise DocumentError("Bildabmessungen überschreiten das Limit.")
    prepared, _factor, notes = _prepare(img)
    tsv = _tesseract(prepared, language, 6, ["-c", "preserve_interword_spaces=1", "tsv"])
    pw, ph = prepared.size
    lines = _tsv_lines(tsv, page_no, pw, ph)
    pg = Page(page_no, "ocr", lines, warnings=notes)
    # gezieltes Nachlesen unsicherer Zahlenzeilen (Ausschnitt, 2× größer, eine Zeile) statt Neu-OCR der Seite
    rechecks = 0
    for ln in lines:
        if rechecks >= RECHECK_MAX:
            break
        if ln.conf is None or ln.conf >= RECHECK_CONF or not _NUMERIC.search(ln.text) or ln.box is None:
            continue
        x0, y0, x1, y1 = ln.box
        pad = 0.004
        crop = prepared.crop((int(max(0.0, x0 - pad) * pw), int(max(0.0, y0 - pad * 2) * ph),
                              int(min(1.0, x1 + pad) * pw), int(min(1.0, y1 + pad * 2) * ph)))
        if crop.size[0] < 4 or crop.size[1] < 4:
            continue
        big = crop.resize((crop.size[0] * 2, crop.size[1] * 2), resample=3)
        big = ImageOps.expand(big, border=12, fill=255)
        rechecks += 1
        try:
            tsv2 = _tesseract(big, language, 7, ["-c", "preserve_interword_spaces=1", "tsv"])
        except DocumentError:
            continue
        alt = _tsv_lines(tsv2, page_no, big.size[0], big.size[1])
        if len(alt) == 1 and alt[0].conf is not None and alt[0].conf > ln.conf + 5:
            ln.text, ln.conf, ln.rechecked = alt[0].text, alt[0].conf, True
    confs = [ln.conf for ln in lines if ln.conf is not None]
    pg.conf = round(sum(confs) / len(confs), 1) if confs else None
    if rechecks:
        pg.warnings.append(f"{rechecks} unsichere Zeile(n) gezielt nachgelesen")
    return pg


def _extract_image(data: bytes, res: DocumentResult, language: str) -> None:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover
        raise DocumentError("Pillow ist für den Bildimport erforderlich.") from exc
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # Dekompressionsbomben: Pillow bricht vor dem Dekodieren ab
    try:
        with Image.open(io.BytesIO(data)) as pic:
            w, h = pic.size
            if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
                raise DocumentError("Bildabmessungen überschreiten das Limit.")
            if getattr(pic, "n_frames", 1) > 1:
                res.warnings.append("Animiertes/mehrteiliges Bild – nur das erste Bild wird gelesen.")
            pic.load()
            img = ImageOps.exif_transpose(pic).convert("RGB")
    except DocumentError:
        raise
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError) as exc:
        raise DocumentError("Beschädigtes, zu großes oder unlesbares Bild.") from exc
    pg = _ocr_page(img, 1, language)
    pg.width, pg.height = float(img.size[0]), float(img.size[1])
    res.pages.append(pg)
    res.ocr_used = True


# ----------------------------------------------------------------------------------------------------
# Einstieg
# ----------------------------------------------------------------------------------------------------

def extract_document(data: bytes, *, language: str = "deu+eng", ocr: bool = True) -> DocumentResult:
    """CPU-basierte Extraktion ohne Netzwerkzugriff. Für nicht vertrauenswürdige Dateien über
    :func:`app.documentimport.worker.extract_isolated` aufrufen (Speicher-, CPU- und Zeitlimit)."""
    import time

    kind = sniff(data)
    res = DocumentResult(sha256(data), kind, len(data))
    t0 = time.perf_counter()
    if kind == "pdf":
        _extract_pdf(data, res, language, ocr)
    else:
        if not ocr:
            raise DocumentError("Bilder brauchen OCR – OCR ist abgeschaltet.")
        _extract_image(data, res, language)
    res.ms["extract"] = int((time.perf_counter() - t0) * 1000)
    for p in res.pages:
        total = sum(len(ln.text) for ln in p.lines)
        if total > MAX_TEXT:
            raise DocumentError(f"Seite {p.number}: zu viel Text für eine sichere Analyse.")
    if not any(p.lines for p in res.pages):
        res.warnings.append("Kein lesbarer Text gefunden.")
    return res
