"""Portfolia-Name und Logo in den Steuer-PDFs (Kopfzeile jeder Seite, Titelblock Seite 1, Fallback ohne Logo)."""

from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from app.tax import document as D
from app.tax import pdf as P


def _doc(brand: bool = True, pages: int = 3) -> D.Doc:
    doc = D.Doc(title="Testbericht", subtitle="Probe", footer="Fuß", brand=brand)
    doc.add(D.H1("Titel"), D.P("Text"))
    for _ in range(pages - 1):
        doc.add(D.PageBreak(), D.H1("Weiter"), D.P("Mehr Text"))
    return doc


def _logo_boxes(pdf: pdfium.PdfDocument, i: int) -> list[tuple[float, float, float, float]]:
    """Pfade des Logos (kleine Vektorobjekte; die Linien von Kopf und Fuß sind breit)."""
    boxes = []
    for obj in pdf[i].get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_PATH]):
        left, bottom, right, top = obj.get_pos()
        if right - left < 80 and top - bottom < 80 and (top - bottom) > 1:
            boxes.append((left, bottom, right, top))
    return boxes


def _images(pdf: pdfium.PdfDocument) -> list[int]:
    out = []
    for i in range(len(pdf)):
        page = pdf[i]
        out.append(len(list(page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]))))
    return out


def test_every_page_has_logo_and_name(tmp_path: Path):
    out = P.render(_doc(), tmp_path / "a.pdf")
    pdf = pdfium.PdfDocument(out)
    assert len(pdf) == 3
    assert _images(pdf) == [0, 0, 0]  # das Logo ist eine Vektorzeichnung, kein Bild
    boxes = [_logo_boxes(pdf, k) for k in range(3)]
    assert len(boxes[0]) >= 2 * len(boxes[1]) > 0 and len(boxes[1]) == len(boxes[2])  # Seite 1: Titelblock + Kopfzeile
    text1 = pdf[0].get_textpage().get_text_bounded()
    assert "Portfolia" in text1 and "Portfolio- und Steuerauswertung" in text1
    assert "Portfolia" in pdf[1].get_textpage().get_text_bounded()  # rechts in der Kopfzeile
    assert pdf.get_metadata_dict().get("Author") == "Portfolia"


def test_brand_can_be_disabled(tmp_path: Path):
    pdf = pdfium.PdfDocument(P.render(_doc(brand=False, pages=2), tmp_path / "b.pdf"))
    assert _images(pdf) == [0, 0] and not _logo_boxes(pdf, 0) and not _logo_boxes(pdf, 1)
    assert "Portfolio- und Steuerauswertung" not in pdf[0].get_textpage().get_text_bounded()


def _text_in(pdf: pdfium.PdfDocument, page: int, rect: tuple[float, float, float, float]) -> str:
    left, bottom, right, top = rect
    return pdf[page].get_textpage().get_text_bounded(left=left, bottom=bottom, right=right, top=top).strip()


def test_logo_never_covers_text_and_name_is_complete(tmp_path: Path):
    doc = _doc(pages=2)
    doc.orientation = "landscape"
    pdf = pdfium.PdfDocument(P.render(doc, tmp_path / "d.pdf"))
    for i in range(len(pdf)):
        page = pdf[i]
        assert "Portfolia" in page.get_textpage().get_text_bounded()
        for box in _logo_boxes(pdf, i):
            assert _text_in(pdf, i, box) == ""  # unter und neben dem Logo steht kein Text
    # Titelblock: „Portfolia“ beginnt rechts neben dem Logo und ist vollständig lesbar
    assert any(line.strip() == "Portfolia" for line in pdf[0].get_textpage().get_text_bounded().splitlines())


def test_long_header_and_footer_do_not_collide(tmp_path: Path):
    doc = D.Doc(title="Sehr langer Titel " * 8, subtitle="Untertitel " * 6, footer="Fußnote " * 40)
    doc.add(D.H1("Titel"), D.P("Text"))
    pdf = pdfium.PdfDocument(P.render(doc, tmp_path / "e.pdf", header_right="Portfolia · 2025"))
    text = pdf[0].get_textpage().get_text_bounded()
    assert "Portfolia · 2025" in text and "Seite 1 von 1" in text  # rechte Textteile unversehrt
    assert "…" in text  # linke Textteile gekürzt statt überschrieben


def test_glyphs_missing_in_font_are_replaced(tmp_path: Path):
    doc = D.Doc(title="Test", footer="Fuß")
    doc.add(D.P("Steuern → Regelwerk ↔ Bank"))
    pdf = pdfium.PdfDocument(P.render(doc, tmp_path / "f.pdf"))
    text = pdf[0].get_textpage().get_text_bounded()
    assert "Steuern -> Regelwerk <-> Bank" in text


def test_pdf_contains_no_image_and_no_transparency(tmp_path: Path):
    """Regression „ortfolia“/„teuerreport“: Der Firefox-Viewer (Android) verlor den ersten Buchstaben eines Textes neben
    einem Bild. Das Logo wird deshalb als Vektorzeichnung ausgegeben – kein Bild, keine Transparenzmaske."""
    out = P.render(_doc(pages=2), tmp_path / "g.pdf")
    raw = out.read_bytes()
    assert b"/Subtype /Image" not in raw and b"/XObject" not in raw  # (/ProcSet nennt /ImageB nur als Vorgabe)
    assert b"/SMask" not in raw and b"/Mask" not in raw
    page1 = pdfium.PdfDocument(out)[0]
    assert page1.get_textpage().get_text_bounded().count("Portfolia") >= 2
