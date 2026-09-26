"""PDF-Renderer (ReportLab) für :class:`app.tax.document.Doc` – länderneutral.

* Schrift: Bitstream Vera (mit ReportLab ausgeliefert, eingebettet; deckt €, Umlaute, § ab).
* A4 hoch/quer gemischt, Kopf- und Fußzeile mit „Seite x von y“.
* Lange Tabellen werden in Blöcken gesetzt (Kopfzeile wiederholt), Beträge rechtsbündig, de-DE-Format.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    KeepTogether,
    LongTable,
    NextPageTemplate,
    PageTemplate,
    Paragraph,
    TableStyle,
)
from reportlab.platypus import PageBreak as RLPageBreak
from reportlab.platypus import Spacer as RLSpacer
from reportlab.platypus import Table as RLTable

from app.tax import document as D
from app.tax.base import Column, Table, fmt_date, fmt_eur, fmt_num, fmt_qty, fmt_value  # noqa: F401

_FONT_LOCK = threading.Lock()
_FONTS: dict[str, str] | None = None

INK = colors.HexColor("#1d2330")
INK2 = colors.HexColor("#566074")
LINE = colors.HexColor("#d5dae1")
HEAD_BG = colors.HexColor("#eef1f5")
ZEBRA = colors.HexColor("#f7f8fa")
WARN_BG = colors.HexColor("#fff4e0")
CRIT_BG = colors.HexColor("#fde8e8")
GOOD_BG = colors.HexColor("#e7f6ee")
CHUNK_ROWS = 400
MARGIN_X = 15 * mm
MARGIN_TOP = 18 * mm
MARGIN_BOTTOM = 16 * mm


def fonts() -> dict[str, str]:
    """Vera registrieren (einmalig); Fallback Helvetica (WinAnsi deckt €/Umlaute ebenfalls ab)."""
    global _FONTS
    with _FONT_LOCK:
        if _FONTS is not None:
            return _FONTS
        base = Path(reportlab.__file__).resolve().parent / "fonts"
        files = {"Vera": "Vera.ttf", "Vera-Bold": "VeraBd.ttf", "Vera-Italic": "VeraIt.ttf",
                 "Vera-BoldItalic": "VeraBI.ttf"}
        try:
            for name, fn in files.items():
                pdfmetrics.registerFont(TTFont(name, os.fspath(base / fn)))
            pdfmetrics.registerFontFamily("Vera", normal="Vera", bold="Vera-Bold", italic="Vera-Italic",
                                          boldItalic="Vera-BoldItalic")
            _FONTS = {"regular": "Vera", "bold": "Vera-Bold", "italic": "Vera-Italic"}
        except Exception:  # pragma: no cover - nur wenn die Schriftdateien fehlen
            _FONTS = {"regular": "Helvetica", "bold": "Helvetica-Bold", "italic": "Helvetica-Oblique"}
        return _FONTS


# ----------------------------------------------------------------------------------------------------
# Seitenvorlagen
# ----------------------------------------------------------------------------------------------------

class _NumberedCanvas(rl_canvas.Canvas):
    """Canvas, das nach dem Satz „Seite x von y“ und Kopf/Fuß zeichnet."""

    header_left = ""
    header_right = ""
    footer_left = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved: list[dict[str, Any]] = []

    def showPage(self) -> None:
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._decorate(total)
            super().showPage()
        super().save()

    def _decorate(self, total: int) -> None:
        f = fonts()
        w, h = self._pagesize
        self.saveState()
        self.setStrokeColor(LINE)
        self.setLineWidth(0.5)
        self.line(MARGIN_X, h - 12 * mm, w - MARGIN_X, h - 12 * mm)
        self.line(MARGIN_X, 11 * mm, w - MARGIN_X, 11 * mm)
        self.setFillColor(INK2)
        self.setFont(f["regular"], 7.5)
        self.drawString(MARGIN_X, h - 10.5 * mm, self.header_left[:140])
        self.drawRightString(w - MARGIN_X, h - 10.5 * mm, self.header_right)
        self.drawString(MARGIN_X, 7.5 * mm, self.footer_left[:180])
        self.drawRightString(w - MARGIN_X, 7.5 * mm, f"Seite {self._pageNumber} von {total}")
        self.restoreState()


def _canvas_class(doc: D.Doc, header_right: str) -> type[_NumberedCanvas]:
    return type("DocCanvas", (_NumberedCanvas,), {"header_left": doc.title + (f" – {doc.subtitle}" if doc.subtitle
                                                                               else ""),
                                                  "header_right": header_right, "footer_left": doc.footer})


# ----------------------------------------------------------------------------------------------------
# Renderer
# ----------------------------------------------------------------------------------------------------

class Renderer:
    def __init__(self) -> None:
        f = fonts()
        self.f = f
        self.st = {
            "h1": ParagraphStyle("h1", fontName=f["bold"], fontSize=15, leading=19, textColor=INK, spaceAfter=6,
                                 spaceBefore=2),
            "h2": ParagraphStyle("h2", fontName=f["bold"], fontSize=11.5, leading=15, textColor=INK, spaceBefore=10,
                                 spaceAfter=4),
            "normal": ParagraphStyle("n", fontName=f["regular"], fontSize=9, leading=12.5, textColor=INK,
                                     alignment=TA_LEFT, spaceAfter=4),
            "strong": ParagraphStyle("s", fontName=f["bold"], fontSize=9, leading=12.5, textColor=INK, spaceAfter=4),
            "small": ParagraphStyle("sm", fontName=f["regular"], fontSize=7.8, leading=10.5, textColor=INK2,
                                    spaceAfter=3),
            "note": ParagraphStyle("no", fontName=f["italic"], fontSize=8.2, leading=11, textColor=INK2,
                                   spaceAfter=4),
            "warn": ParagraphStyle("w", fontName=f["regular"], fontSize=8.6, leading=12, textColor=INK,
                                   backColor=WARN_BG, borderPadding=(4, 5, 4, 5), spaceBefore=4, spaceAfter=8),
            "cell": ParagraphStyle("c", fontName=f["regular"], fontSize=7.4, leading=9.2, textColor=INK),
            "cellb": ParagraphStyle("cb", fontName=f["bold"], fontSize=7.4, leading=9.2, textColor=INK),
            "cellh": ParagraphStyle("ch", fontName=f["bold"], fontSize=7.2, leading=8.8, textColor=INK),
            "cellr": ParagraphStyle("cr", fontName=f["regular"], fontSize=7.4, leading=9.2, textColor=INK,
                                    alignment=TA_RIGHT),
            "cellbr": ParagraphStyle("cbr", fontName=f["bold"], fontSize=7.4, leading=9.2, textColor=INK,
                                     alignment=TA_RIGHT),
            "kv": ParagraphStyle("kv", fontName=f["regular"], fontSize=8.8, leading=11.5, textColor=INK),
            "kvl": ParagraphStyle("kvl", fontName=f["regular"], fontSize=8.8, leading=11.5, textColor=INK2),
            "bullet": ParagraphStyle("b", fontName=f["regular"], fontSize=8.6, leading=11.6, textColor=INK,
                                     leftIndent=10, bulletIndent=0, spaceAfter=2),
        }

    # -- Hilfen -----------------------------------------------------------------------------------
    def para(self, text: str, style: str = "normal") -> Paragraph:
        return Paragraph(escape(text).replace("\n", "<br/>"), self.st[style])

    @staticmethod
    def _width(orientation: str) -> float:
        page = landscape(A4) if orientation == "landscape" else A4
        return page[0] - 2 * MARGIN_X

    # -- Blöcke -------------------------------------------------------------------------------------
    def _widths(self, t: Table, avail: float) -> list[float]:
        """Mindestbreite je Spalte (längstes Wort im Kopf, längster Zahlenwert), Rest nach Gewicht."""
        sample = t.rows[:300] + ([t.totals] if t.totals else [])
        need = []
        for c in t.columns:
            words = c.title.split() or [""]
            w = max(stringWidth(x, self.f["bold"], 7.2) for x in words)
            vals = [fmt_value(r.get(c.key), c.kind) for r in sample if r.get(c.key) not in (None, "")]
            if vals and c.kind != "text":
                w = max(w, max(stringWidth(v, self.f["bold"], 7.4) for v in vals))
            elif vals:  # Text: bis ca. 30 mm ohne Umbruch, längere Texte umbrechen
                w = max(w, min(max(stringWidth(v, self.f["regular"], 7.4) for v in vals), 85))
            need.append(w + 7)
        total_need = sum(need)
        if total_need >= avail:
            return [avail * n / total_need for n in need]
        extra = avail - total_need
        weight = sum(c.width for c in t.columns) or 1
        return [n + extra * c.width / weight for n, c in zip(need, t.columns, strict=True)]

    def table(self, t: Table, orientation: str) -> list[Any]:
        avail = self._width(orientation)
        widths = self._widths(t, avail)
        head = [Paragraph(escape(c.title), self.st["cellh"]) for c in t.columns]
        numeric = [c.kind in ("eur", "qty", "int", "pct") for c in t.columns]

        def cell(c: Column, v: Any, w: float, bold: bool = False) -> Any:
            s = fmt_value(v, c.kind)
            if c.kind == "text" and stringWidth(s, self.f["bold" if bold else "regular"], 7.4) > w - 6:
                return Paragraph(escape(s), self.st["cellb" if bold else "cell"])
            return s

        body = [[cell(c, r.get(c.key), w) for c, w in zip(t.columns, widths, strict=True)] for r in t.rows]
        out: list[Any] = [CondPageBreak(38 * mm)]
        if t.title:
            out.append(Paragraph(escape(t.title), self.st["h2"]))
        if t.note:
            out.append(self.para(t.note, "small"))
        if not body:
            out.append(self.para("Keine Vorgänge.", "small"))
            return out
        chunks = [body[i:i + CHUNK_ROWS] for i in range(0, len(body), CHUNK_ROWS)]
        for ci, chunk in enumerate(chunks):
            rows = [head, *chunk]
            last_chunk = ci == len(chunks) - 1
            if last_chunk and t.totals:
                rows.append([cell(c, t.totals.get(c.key), w, True) if t.totals.get(c.key) is not None else ""
                             for c, w in zip(t.columns, widths, strict=True)])
            tbl = LongTable(rows, colWidths=widths, repeatRows=1)
            style = [
                ("FONT", (0, 0), (-1, -1), self.f["regular"], 7.4),
                ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2.2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]
            for i, is_num in enumerate(numeric):
                if is_num:
                    style.append(("ALIGN", (i, 1), (i, -1), "RIGHT"))
            for r in range(2, len(rows), 2):
                style.append(("BACKGROUND", (0, r), (-1, r), ZEBRA))
            if last_chunk and t.totals:
                style += [("LINEABOVE", (0, -1), (-1, -1), 0.8, INK2), ("FONT", (0, -1), (-1, -1), self.f["bold"], 7.4),
                          ("BACKGROUND", (0, -1), (-1, -1), HEAD_BG)]
            tbl.setStyle(TableStyle(style))
            out.append(tbl)
        out.append(RLSpacer(1, 3 * mm))
        return out

    def kv(self, rows: list[tuple[str, str]], orientation: str) -> RLTable:
        avail = self._width(orientation)
        data = [[Paragraph(escape(k), self.st["kvl"]), Paragraph(escape(v), self.st["kv"])] for k, v in rows]
        t = RLTable(data, colWidths=[avail * 0.34, avail * 0.66])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 1)]))
        return t

    def lines(self, lines: list[Any], orientation: str) -> RLTable:
        avail = self._width(orientation)
        data, style = [], [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 2),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 2),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("ALIGN", (1, 0), (1, -1), "RIGHT")]
        for i, ln in enumerate(lines):
            label = (" " * 4 * ln.indent) + ln.label
            st = "cellb" if ln.strong else "cell"
            if ln.kind == "text":
                val = ln.text
            elif ln.kind == "pct":
                val = fmt_value(ln.amount, "pct")
            elif ln.kind == "int":
                val = fmt_value(ln.amount, "int")
            else:
                val = fmt_eur(ln.amount) if ln.amount is not None else "–"
            if ln.kind == "text" and not ln.text and ln.amount is None:  # Zwischenüberschrift
                data.append([Paragraph(escape(label), self.st["cellb"]), "", ""])
                style += [("SPAN", (0, i), (-1, i)), ("TOPPADDING", (0, i), (-1, i), 6)]
                continue
            note = Paragraph(escape(ln.note), self.st["small"]) if ln.note else ""
            data.append([Paragraph(escape(label), self.st[st]), Paragraph(escape(val), self.st[st + "r"]), note])
            if ln.strong:
                style.append(("LINEABOVE", (0, i), (1, i), 0.6, LINE))
        t = RLTable(data, colWidths=[avail * 0.50, avail * 0.20, avail * 0.30])
        t.setStyle(TableStyle(style))
        return t

    def meters(self, meters: list[Any], orientation: str) -> RLTable:
        avail = self._width(orientation)
        data = [[Paragraph("Grenze", self.st["cellh"]), Paragraph("Wert", self.st["cellh"]),
                 Paragraph("Grenze", self.st["cellh"]), Paragraph("Status", self.st["cellh"])]]
        style = [("BACKGROUND", (0, 0), (-1, 0), HEAD_BG), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("ALIGN", (1, 1), (2, -1), "RIGHT"), ("FONT", (0, 0), (-1, -1), self.f["regular"], 7.6),
                 ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE)]
        for i, m in enumerate(meters, start=1):
            if m.kind == "freigrenze":
                status = ("Freigrenze erreicht – Betrag voll steuerpflichtig" if m.state == "crit"
                          else ("keine positiven Einkünfte" if m.value <= 0 else "unter der Freigrenze – steuerfrei"))
            else:
                status = "ausgeschöpft" if m.state == "warn" else "nicht ausgeschöpft"
            if m.note:
                status += f" ({m.note})"
            data.append([Paragraph(escape(m.label), self.st["cell"]), fmt_eur(m.value), fmt_eur(m.limit),
                         Paragraph(escape(status), self.st["cell"])])
            bg = {"crit": CRIT_BG, "warn": WARN_BG}.get(m.state, GOOD_BG)
            style.append(("BACKGROUND", (3, i), (3, i), bg))
        t = RLTable(data, colWidths=[avail * 0.36, avail * 0.16, avail * 0.14, avail * 0.34])
        t.setStyle(TableStyle(style))
        return t

    def fields(self, fields: list[Any], show_lines: bool, orientation: str) -> list[Any]:
        avail = self._width(orientation)
        out: list[Any] = []
        forms: dict[str, list[Any]] = {}
        for fld in fields:
            forms.setdefault(fld.form, []).append(fld)
        for form, items in forms.items():
            data = [[Paragraph("Abschnitt / Feld", self.st["cellh"]), Paragraph("Zeile", self.st["cellh"]),
                     Paragraph("Wert", self.st["cellh"])]]
            for fld in items:
                label = f"{fld.section}: {fld.label}" if fld.section else fld.label
                if fld.note:
                    label += f"\n{fld.note}"
                val = fld.text if fld.text is not None else fmt_eur(fld.amount)
                data.append([Paragraph(escape(label).replace("\n", "<br/><font size='6.8' color='#566074'>")
                                       + ("</font>" if fld.note else ""), self.st["cell"]),
                             (fld.line or "–") if show_lines else "–", Paragraph(escape(val), self.st["cellb"])])
            t = RLTable(data, colWidths=[avail * 0.66, avail * 0.10, avail * 0.24], repeatRows=1)
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD_BG), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("FONT", (0, 0), (-1, -1), self.f["regular"], 7.6),
                                   ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("ALIGN", (1, 1), (1, -1), "CENTER")]))
            out += [KeepTogether([Paragraph(escape(form), self.st["h2"]), t]), RLSpacer(1, 2 * mm)]
        return out

    # -- Dokument ------------------------------------------------------------------------------------
    def render(self, doc: D.Doc, path: Path, header_right: str = "Portfolia") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        pw, ph = A4
        pt = PageTemplate(id="portrait", pagesize=A4,
                          frames=[Frame(MARGIN_X, MARGIN_BOTTOM, pw - 2 * MARGIN_X, ph - MARGIN_TOP - MARGIN_BOTTOM,
                                        id="fp", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)])
        lw, lh = landscape(A4)
        lt = PageTemplate(id="landscape", pagesize=(lw, lh),
                          frames=[Frame(MARGIN_X, MARGIN_BOTTOM, lw - 2 * MARGIN_X, lh - MARGIN_TOP - MARGIN_BOTTOM,
                                        id="fl", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)])
        templates = [pt, lt] if doc.orientation == "portrait" else [lt, pt]
        rl = BaseDocTemplate(os.fspath(tmp), pagesize=A4 if doc.orientation == "portrait" else (lw, lh),
                             pageTemplates=templates, title=doc.title, author="Portfolia", subject=doc.subject,
                             keywords=doc.keywords, creator="Portfolia", leftMargin=MARGIN_X, rightMargin=MARGIN_X,
                             topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM)
        story: list[Any] = []
        orient = doc.orientation
        for b in doc.blocks:
            if isinstance(b, D.PageBreak):
                if b.orientation and b.orientation != orient:
                    story.append(NextPageTemplate(b.orientation))
                    orient = b.orientation
                story.append(RLPageBreak())
            elif isinstance(b, D.H1):
                story.append(Paragraph(escape(b.text), self.st["h1"]))
            elif isinstance(b, D.H2):
                story += [CondPageBreak(30 * mm), Paragraph(escape(b.text), self.st["h2"])]
            elif isinstance(b, D.P):
                story.append(self.para(b.text, b.style))
            elif isinstance(b, D.KV):
                story.append(self.kv(b.rows, orient))
            elif isinstance(b, D.Lines):
                story.append(self.lines(b.lines, orient))
            elif isinstance(b, D.Meters):
                if b.meters:
                    story.append(self.meters(b.meters, orient))
            elif isinstance(b, D.TableBlock):
                story += self.table(b.table, orient)
            elif isinstance(b, D.Fields):
                story += self.fields(b.fields, b.show_lines, orient)
            elif isinstance(b, D.Bullets):
                for it in b.items:
                    story.append(Paragraph(escape(it), self.st["bullet"], bulletText="•"))
            elif isinstance(b, D.Spacer):
                story.append(RLSpacer(1, b.height_mm * mm))
        rl.build(story, canvasmaker=_canvas_class(doc, header_right))
        os.replace(tmp, path)
        return path


def render(doc: D.Doc, path: Path, header_right: str = "Portfolia") -> Path:
    return Renderer().render(doc, path, header_right)
