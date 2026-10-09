"""Synthetische Testbelege (M25): PDFs mit eingebettetem Text, gescannte PDFs (nur Bild) und Screenshots.

Alle Inhalte sind erfunden; Sollwerte legt der jeweilige Test unabhängig fest. Der PDF-Schreiber ist bewusst minimal
(Helvetica, WinAnsi, eine Textzeile je Anweisung) und braucht keine Zusatzbibliothek.
"""

from __future__ import annotations

import io
import zlib
from collections.abc import Iterable, Sequence

Line = tuple[float, float, str] | tuple[float, float, str, float]  # x, y (von oben, pt), Text[, Schriftgröße]
A4 = (595.0, 842.0)


def _esc(s: str) -> bytes:
    b = s.encode("cp1252", errors="replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def pdf(pages: Sequence[Iterable[Line]], *, size: tuple[float, float] = A4, images: dict[int, bytes] | None = None,
        title: str = "Synthetischer Testbeleg") -> bytes:
    """PDF mit Textseiten; ``images`` = {Seitenindex: JPEG-Bytes} legt statt Text ein ganzseitiges Bild ab (Scan)."""
    w, h = size
    objs: list[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids: list[int] = []
    pages_id_placeholder = len(objs) + 1
    add(b"")  # Platzhalter für /Pages
    for i, lines in enumerate(pages):
        res = f"<< /Font << /F1 {font} 0 R >>".encode()
        if images and i in images:
            jpg = images[i]
            from PIL import Image

            iw, ih = Image.open(io.BytesIO(jpg)).size
            img = add(f"<< /Type /XObject /Subtype /Image /Width {iw} /Height {ih} /ColorSpace /DeviceRGB "
                      f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpg)} >>\nstream\n".encode() + jpg
                      + b"\nendstream")
            res += f" /XObject << /Im1 {img} 0 R >>".encode()
            dh = w * ih / iw  # Seitenbreite, Seitenverhältnis des Scans, oben ausgerichtet
            content = f"q {w} 0 0 {dh:.2f} 0 {h - dh:.2f} cm /Im1 Do Q".encode()
        else:
            parts = []
            for ln in lines:
                x, y, text = ln[0], ln[1], ln[2]
                fs = ln[3] if len(ln) > 3 else 10  # type: ignore[misc]
                parts.append(b"BT /F1 %d Tf %.2f %.2f Td (" % (fs, x, h - y) + _esc(text) + b") Tj ET")
            content = b"\n".join(parts)
        res += b" >>"
        comp = zlib.compress(content)
        cid = add(f"<< /Length {len(comp)} /Filter /FlateDecode >>\nstream\n".encode() + comp + b"\nendstream")
        pid = add(f"<< /Type /Page /Parent {pages_id_placeholder} 0 R /MediaBox [0 0 {w} {h}] "
                  f"/Contents {cid} 0 R /Resources ".encode() + res + b" >>")
        page_ids.append(pid)
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs[pages_id_placeholder - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    info = add(b"<< /Title (" + _esc(title) + b") /Producer (Portfolia Tests) >>")
    catalog = add(f"<< /Type /Catalog /Pages {pages_id_placeholder} 0 R >>".encode())
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n"
              f"%%EOF\n".encode())
    return out.getvalue()


def text_pdf(*pages: list[str], x: float = 56, top: float = 64, step: float = 16, size: float = 10) -> bytes:
    """Bequemer Aufruf: je Seite eine Liste von Zeilen (``"  "`` trennt Spalten, die mit Abstand gesetzt werden)."""
    out = []
    for lines in pages:
        items: list[Line] = []
        for i, line in enumerate(lines):
            y = top + i * step
            cols = line.split("  ") if "  " in line else [line]
            if len(cols) > 1:
                cx = x
                for c in cols:
                    items.append((cx, y, c.strip(), size))
                    cx += max(70.0, len(c.strip()) * size * 0.55 + 24)
            else:
                items.append((x, y, line, size))
        out.append(items)
    return pdf(out)


def _font(px: int) -> object:
    from PIL import ImageFont

    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    return ImageFont.load_default(size=px)


def screenshot(lines: list[str], *, px: int = 22, width: int = 900, dark: bool = False, fmt: str = "PNG",
               quality: int = 90, pad: int = 24, line_gap: float = 1.7, scale: float = 1.0) -> bytes:
    """Screenshot-ähnliches Bild (App-Ansicht) mit Textzeilen; ``"  "`` setzt Spalten mit Abstand."""
    from PIL import Image, ImageDraw

    font = _font(px)
    height = int(pad * 2 + len(lines) * px * line_gap)
    bg, fg = ((24, 26, 30), (230, 232, 235)) if dark else ((255, 255, 255), (20, 20, 20))
    img = Image.new("RGB", (width, height), bg)
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        y = pad + i * px * line_gap
        if "  " in line:
            cols = [c.strip() for c in line.split("  ") if c.strip()]
            colw = (width - 2 * pad) / max(len(cols), 1)
            for j, c in enumerate(cols):
                d.text((pad + j * colw, y), c, fill=fg, font=font)
        else:
            d.text((pad, y), line, fill=fg, font=font)
    if scale != 1.0:
        img = img.resize((int(img.size[0] * scale), int(img.size[1] * scale)), resample=3)
    buf = io.BytesIO()
    if fmt.upper() in ("JPEG", "JPG"):
        img.save(buf, format="JPEG", quality=quality)
    elif fmt.upper() == "WEBP":
        img.save(buf, format="WEBP", quality=quality)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue()


def scanned_pdf(lines: list[str], *, px: int = 26) -> bytes:
    """„Gescanntes“ PDF: eine Seite nur als Bild (JPEG), ohne eingebetteten Text."""
    jpg = screenshot(lines, px=px, width=1240, fmt="JPEG", quality=85, pad=60)
    return pdf([[]], images={0: jpg})
