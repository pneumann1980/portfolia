"""Lasttest des Dokumentimports (M25) – nur synthetische Belege, temporäres Verzeichnis, keine Netzwerkzugriffe.

    python scripts/bench_documents.py [scale]

Misst: Extraktion eines Einzelbelegs (isolierter Kindprozess), eines 10-seitigen Kontoauszugs (200 Zeilen), eines
Screenshots und eines gescannten PDFs (OCR), einen Stapel aus 20 Belegen bis in den Prüf-Stapel gegen ein Portfolio
mit > 10.000 Buchungen (``tests.synthetic``, ``scale`` = 3), die Prüfansicht und „neu bewerten“; dazu den
Spitzenspeicher des Hauptprozesses und der Analyse-Kindprozesse.
"""

from __future__ import annotations

import os
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.config import Config, Secrets
from app.documentimport.extract import ocr_available
from app.documentimport.profiles import analyze
from app.documentimport.service import document_service
from app.documentimport.worker import extract_isolated
from app.main import build_app
from tests.docfixtures import scanned_pdf, screenshot, text_pdf
from tests.synthetic import write_zip

BANK = ["Musterbank AG", "Wertpapierabrechnung Kauf", "Depot  1234567890",
        "Handelstag  02.01.2025  Handelszeit  09:15:03", "ISIN  DE0007164600  Stück  10", "SAP SE Inhaber-Aktien",
        "Kurs  123,45 EUR  Kurswert  1.234,50 EUR", "Provision  4,90 EUR", "Ausmachender Betrag  1.239,40 EUR",
        "Auftragsnummer  A-778899"]


def rss_mb(who: int = resource.RUSAGE_SELF) -> float:
    return resource.getrusage(who).ru_maxrss / 1024


def timed(label: str, fn, results: dict):
    t = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t
    results[label] = dt
    print(f"{label:<58}{dt:8.3f}s")
    return out


def statement(pages: int = 10, rows: int = 20) -> bytes:
    out, n = [], 0
    for p in range(pages):
        pg = ["Krypto-Börse Kontoauszug", "Datum  Typ  Asset  Menge  Preis (EUR)  Betrag (EUR)  Gebühr (EUR)"]
        for i in range(rows):
            n += 1
            pg.append(f"{(n % 28) + 1:02d}.{(p % 9) + 1:02d}.2024  Kauf  ETH  0,0{i + 1}  2.000,00  "
                      f"{20 * (i + 1)},00  0,10")
        out.append(pg)
    return text_pdf(*out)


def bank(i: int) -> bytes:
    lines = list(BANK)
    lines[-1] = f"Auftragsnummer  A-{100000 + i}"
    lines[4] = f"ISIN  DE0007164600  Stück  {10 + i}"
    lines[6] = f"Kurs  123,45 EUR  Kurswert  {(10 + i) * 123.45:,.2f} EUR".replace(",", "X").replace(".", ",") \
        .replace("X", ".")
    del lines[8]  # ausmachender Betrag entfällt (Kurswert variiert)
    return text_pdf(lines)


def main() -> int:
    scale = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    base = Path(tempfile.mkdtemp(prefix="portfolia-bench-docs-"))
    results: dict[str, float] = {}
    ocr = ocr_available()
    try:
        single = text_pdf(BANK)
        res = timed("Einzelbeleg (PDF, isolierter Kindprozess)", lambda: extract_isolated(single, ocr=False),
                    results)
        print(f"  {len(res.lines())} Zeilen, {len(analyze(res).txs)} Vorgang")
        st = statement()
        res = timed("Kontoauszug 10 Seiten / 200 Zeilen (Extraktion)", lambda: extract_isolated(st, ocr=False),
                    results)
        an = timed("Kontoauszug 10 Seiten (Analyse, Validierung)", lambda: analyze(res), results)
        print(f"  {len(res.pages)} Seiten, {len(an.txs)} Vorgänge, PDF {len(st) // 1024} KB")
        if ocr:
            shot = screenshot(["Bitpanda", "Kauf  BTC", "Menge  0,00512345 BTC", "Preis  45.123,40 €",
                               "Betrag  231,18 €", "Gebühr  1,49 €", "Datum  03.02.2025 14:22"])
            r2 = timed("Screenshot 900 px (OCR)", lambda: extract_isolated(shot), results)
            print(f"  OCR-Konfidenz {r2.pages[0].conf:.0f} %")
            scan = scanned_pdf(BANK)
            timed("Gescanntes PDF, 1 Seite (Rendern + OCR)", lambda: extract_isolated(scan), results)
        else:
            print("  Tesseract fehlt – OCR-Messungen übersprungen")
        (base / "import").mkdir(parents=True)
        zpath, ntx, nassets = timed("Synthetisches Portfolio erzeugen", lambda: write_zip(
            base / "import" / "gross.zip", scale=scale), results)
        old = time.time() - 3600
        os.utime(zpath, (old, old))
        print(f"  {ntx} Buchungen, {nassets} Assets")
        cfg = Config(data_dir=base / "data", import_dir=base / "import", demo_mode=True, scheduler_enabled=False,
                     startup_jobs=False, log_format="text", log_level="WARNING", secrets=Secrets())
        app = build_app(cfg, start_scheduler=False)
        with TestClient(app) as c:
            ctx = app.state.ctx
            from app.importer.loader import import_file

            timed("Portfolio importieren", lambda: import_file(ctx.db, zpath, ctx.engine_options()), results)
            ctx.invalidate_data()
            timed("Portfolio laden (Ledger)", ctx.portfolio, results)
            svc = document_service(ctx)
            files = [(f"beleg-{i:02d}.pdf", bank(i)) for i in range(18)] + [("auszug.pdf", st)]
            if ocr:
                files.append(("shot.png", shot))
            acc = svc.accept(files)
            summ = timed(f"Stapel {len(files)} Belege bis Prüf-Stapel (ohne Buchung)", lambda: svc.run(acc.stack_id),
                         results)
            print(f"  {summ['documents']} Belege, {summ['transactions']} Vorgänge, Zeilen {summ['rows']}")
            did = ctx.db.scalar("SELECT id FROM document WHERE filename='beleg-00.pdf'", default=None)
            timed("Prüfansicht eines Belegs (HTTP)", lambda: c.get(f"/journal/documents/{did}"), results)
            timed("Neu bewerten (ohne OCR)", lambda: svc.reevaluate(int(did)), results)
            timed("Belegübersicht (HTTP)", lambda: c.get("/journal/documents"), results)
        print(f"{'Spitzenspeicher Hauptprozess (RSS)':<58}{rss_mb():8.0f} MB")
        print(f"{'Spitzenspeicher Analyse-Kindprozess (RSS)':<58}{rss_mb(resource.RUSAGE_CHILDREN):8.0f} MB")
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
