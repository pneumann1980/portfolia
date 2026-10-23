"""Belege-Archiv der Bankdokumente (``bank_doc``): auflisten, ansehen, einzeln oder gesammelt herunterladen.

Grundlage ist der Dokumentindex der Datenquelle (nicht die Beleg-Tabelle ``document``): So erscheinen auch Belege,
die zu keiner Buchung gehören (Depotauszug, Kosteninformation, Hauptversammlung …), und Dateien, die nur im
Ablageordner liegen.

* **Auswahl:** Konto, Jahr, Art, Suchtext (Titel, ISIN, Kennung) und Verknüpfung mit einer Buchung; Seiten à 50.
* **Ansehen/Herunterladen:** Die Datei wird nur über ``bank_doc.rel_path`` unterhalb des Ablageordners der Datenquelle
  gelesen (vorhandene Pfadprüfung), nie über einen vom Browser gelieferten Pfad. Dateinamen entstehen aus Datum, Art,
  ISIN und Kennung – nie aus dem Originalnamen.
* **ZIP:** alle Belege der aktuellen Auswahl in ``<Jahr>/<Ordner>/…`` mit ``manifest.csv`` (UTF-8 mit BOM, ``;``);
  fehlende Originale brechen den Download nicht ab, sie stehen als ``original_fehlt`` im Manifest.
  Grenzen wie beim Jahres-Download der Buchungen (``yearpack``).
* **Datenschutz:** Die PDFs enthalten Name, Anschrift und Kontonummern. Der Zugriff läuft nur über die geschützte
  Oberfläche (``no-store``); protokolliert werden Zähler, nie Dateinamen oder Inhalte.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from app.datasources import dkb as D
from app.documentimport.dkb import KINDS
from app.documentimport.yearpack import MAX_BYTES, MAX_FILES, SPOOL, Pack, safe

log = logging.getLogger(__name__)

PAGE = 50
FOLDER = {"abrechnung_kauf": "01_Abrechnungen", "abrechnung_verkauf": "01_Abrechnungen", "ertrag": "02_Ertraege",
          "kapitalmassnahme": "03_Kapitalmassnahmen", "depotuebertrag": "03_Kapitalmassnahmen",
          "depotauszug": "04_Depotauszuege", "ausfuehrungsanzeige": "05_Orderbelege",
          "auftragsbestaetigung": "05_Orderbelege", "streichung": "05_Orderbelege",
          "kosteninfo_order": "05_Orderbelege", "kosteninfo_jahr": "05_Orderbelege"}
OTHER = "06_Sonstige"
HEADER = ("datei_im_zip", "datum", "art", "wertpapier", "isin", "konto", "buchungen", "kennung", "originalname",
          "sha256", "status")
_LIKE = re.compile(r"([%_\\])")


def _meta(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def kind_label(doctype: str | None, category: str | None = None) -> str:
    if doctype and doctype in KINDS:
        return KINDS[doctype][0]
    return category or "Sonstiges"


def _where(account: str, year: int, kind: str, q: str, linked: str) -> tuple[str, list[Any]]:
    sql, par = ["b.status != 'error'"], []
    if account:
        sql.append("s.account = ?")
        par.append(account)
    if year:
        sql.append("substr(b.doc_date,1,4) = ?")
        par.append(str(year))
    if kind:
        sql.append("COALESCE(b.doctype, '') = ?")
        par.append(kind)
    if q:
        like = "%" + _LIKE.sub(r"\\\1", q.strip()[:80]) + "%"
        sql.append("(b.subject LIKE ? ESCAPE '\\' OR b.meta LIKE ? ESCAPE '\\' OR b.rel_path LIKE ? ESCAPE '\\')")
        par += [like, like, like]
    link = ("EXISTS (SELECT 1 FROM tx_document t JOIN document d ON d.id=t.document_id WHERE d.sha256=b.sha256)")
    if linked == "ja":
        sql.append(link)
    elif linked == "nein":
        sql.append("NOT " + link)
    return " AND ".join(sql), par


_BASE = ("FROM bank_doc b JOIN data_source s ON s.id=b.source_id")
_COLS = ("b.id, b.source_id, b.category, b.doctype, b.doc_date, b.subject, b.rel_path, b.sha256, b.size, b.status, "
         "b.meta, s.account AS konto, s.name AS source_name, "
         "(SELECT d.id FROM document d WHERE d.sha256=b.sha256 ORDER BY d.id LIMIT 1) AS document_id, "
         "(SELECT COUNT(DISTINCT t.tx_id) FROM tx_document t JOIN document d ON d.id=t.document_id "
         "WHERE d.sha256=b.sha256) AS n_tx")


def _names(ctx: Any) -> dict[str, str]:
    pf = ctx.portfolio()
    return {a.isin: a.name for a in pf.assets.values() if a.isin} if pf is not None else {}


def listing(ctx: Any, *, account: str = "", year: int = 0, kind: str = "", q: str = "", linked: str = "",
            page: int = 1) -> dict[str, Any]:
    db = ctx.db
    where, par = _where(account, year, kind, q, linked)
    total = int(db.scalar(f"SELECT COUNT(*) {_BASE} WHERE {where}", par, default=0) or 0)
    pages = max(1, -(-total // PAGE))
    page = max(1, min(page, pages))
    names = _names(ctx)
    rows = []
    for r in db.q(f"SELECT {_COLS} {_BASE} WHERE {where} ORDER BY b.doc_date DESC, b.id DESC LIMIT ? OFFSET ?",
                  [*par, PAGE, (page - 1) * PAGE]):
        m = _meta(r["meta"])
        isin = m.get("isin") or ""
        rows.append({**dict(r), "label": kind_label(r["doctype"], r["category"]), "isin": isin,
                     "asset": names.get(isin, ""), "ident": m.get("order_no") or m.get("belegnr")
                     or m.get("abrechnungsnr") or "", "name": file_name(dict(r), m)})
    return {"rows": rows, "total": total, "page": page, "pages": pages,
            "years": sorted({int(y["y"]) for y in db.q(
                f"SELECT DISTINCT substr(b.doc_date,1,4) AS y {_BASE} "
                "WHERE b.status != 'error' AND b.doc_date IS NOT NULL") if str(y["y"]).isdigit()}, reverse=True),
            "kinds": [(r["k"], kind_label(r["k"]), int(r["n"])) for r in db.q(
                f"SELECT COALESCE(b.doctype,'') AS k, COUNT(*) AS n {_BASE} WHERE b.status != 'error' "
                "GROUP BY COALESCE(b.doctype,'') ORDER BY n DESC")],
            "accounts": [r["a"] for r in db.q(
                f"SELECT DISTINCT s.account AS a {_BASE} ORDER BY s.account COLLATE NOCASE")]}


def file_name(row: dict[str, Any], meta: dict[str, Any] | None = None) -> str:
    """Download-Name aus Datum, Art, ISIN und Kennung (nie aus dem Originalnamen)."""
    m = meta if meta is not None else _meta(row.get("meta"))
    base = "_".join(x for x in ((row.get("doc_date") or "")[:10], kind_label(row.get("doctype")),
                                m.get("isin") or "", m.get("order_no") or m.get("belegnr") or m.get("abrechnungsnr")
                                or str(row["id"])) if x)
    return safe(base, 110) + ".pdf"


def file_of(ctx: Any, bid: int) -> tuple[dict[str, Any], Path] | None:
    """Datei eines Belegs; ``None``, wenn Eintrag oder Datei fehlen. Der Pfad kommt nur aus dem Index."""
    r = ctx.db.q1("SELECT b.*, s.name AS source_name FROM bank_doc b JOIN data_source s ON s.id=b.source_id "
                  "WHERE b.id=?", (bid,))
    if r is None:
        return None
    p = D._inside(D.root_dir(ctx, int(r["source_id"]), r["source_name"]), r["rel_path"])
    return (dict(r), p) if p is not None else None


def build_zip(ctx: Any, *, account: str = "", year: int = 0, kind: str = "", q: str = "", linked: str = "") -> Pack:
    db = ctx.db
    where, par = _where(account, year, kind, q, linked)
    rows = db.q(f"SELECT {_COLS} {_BASE} WHERE {where} ORDER BY b.doc_date, b.id", par)
    if not rows:
        return Pack(message="Keine Belege in dieser Auswahl.")
    if len(rows) > MAX_FILES:
        return Pack(message=f"{len(rows)} Belege sind zu viele für einen Download (höchstens {MAX_FILES}). "
                            "Bitte die Auswahl einschränken (Jahr, Art).")
    names = _names(ctx)
    tx_of: dict[str, list[str]] = {}
    for t in db.q("SELECT d.sha256 AS sha, t.tx_id FROM tx_document t JOIN document d ON d.id=t.document_id"):
        tx_of.setdefault(t["sha"], []).append(t["tx_id"])
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL)  # noqa: SIM115 - Pack.chunks schließt die Datei
    pack = Pack(file=spool)
    manifest = io.StringIO()
    w = csv.writer(manifest, delimiter=";", lineterminator="\r\n")
    w.writerow(HEADER)
    used: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in rows:
                row = dict(r)
                m = _meta(r["meta"])
                found = file_of(ctx, int(r["id"]))
                year_dir = (r["doc_date"] or "")[:4] or "ohne_datum"
                folder = FOLDER.get(r["doctype"] or "", OTHER)
                stem = file_name(row, m)[:-4]
                path = f"belege/{year_dir}/{folder}/{stem}.pdf"
                n = 1
                while path in used:
                    n += 1
                    path = f"belege/{year_dir}/{folder}/{stem}_{n}.pdf"
                used.add(path)
                ok = False
                if found is not None:
                    size = found[1].stat().st_size
                    total += size
                    if total > MAX_BYTES:
                        spool.close()
                        return Pack(message=f"Die Originale sind zusammen größer als {MAX_BYTES // (1024 * 1024)} MB. "
                                            "Bitte die Auswahl einschränken (Jahr, Art).")
                    zf.write(found[1], path)
                    ok = True
                    pack.files += 1
                pack.missing += 0 if ok else 1
                isin = m.get("isin") or ""
                w.writerow([path if ok else "", (r["doc_date"] or "")[:10], kind_label(r["doctype"], r["category"]),
                            names.get(isin, ""), isin, r["konto"], " ".join(sorted(set(tx_of.get(r["sha256"], [])))),
                            m.get("order_no") or m.get("belegnr") or m.get("abrechnungsnr") or "",
                            Path(r["rel_path"]).name, r["sha256"] or "", "ok" if ok else "original_fehlt"])
                pack.rows += 1
            zf.writestr("belege/manifest.csv", "﻿" + manifest.getvalue())
    except Exception:
        spool.close()
        raise
    pack.size = spool.tell()
    pack.ok = True
    pack.message = f"{pack.files} Belege" + (f", {pack.missing} Originale fehlen (siehe Manifest)"
                                             if pack.missing else "")
    log.info("Belege-Download: %s Dateien, %s Zeilen", pack.files, pack.rows)
    return pack


__all__ = ["HEADER", "PAGE", "build_zip", "file_name", "file_of", "kind_label", "listing"]
