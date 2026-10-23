"""Sammeldownload der Belege eines Jahres (ZIP mit ``manifest.csv``) – Auswahl über ``tx_document``.

* **Auswahl:** alle Belege, die mit einer Buchung verknüpft sind, deren Datum im Jahr liegt; zusätzlich der Ordner
  ``ohne_buchung/`` für Belege des Jahres ohne Verknüpfung (Depotauszug, Hauptversammlung, Kosteninformation ohne
  Treffer …).
* **Ordner:** ``01_Abrechnungen``, ``02_Ertraege``, ``03_Kapitalmassnahmen``, ``04_Depotauszuege``, ``05_Orderbelege``,
  ``06_Sonstige``. Dateinamen werden aus Datum, Art, ISIN und Kennung gebaut (nie aus dem Originalnamen), bereinigt und
  bei Kollisionen mit ``_2``, ``_3`` aufgelöst.
* **Manifest:** UTF-8 mit BOM, Semikolon, Dezimalkomma; je Verknüpfung eine Zeile (Buchung, Beleg, Rolle,
  Gebührenquelle *belegt/geschätzt*, Datei im ZIP, Originalname, SHA-256, Status ``ok``/``original_fehlt``). Ein
  fehlendes Original bricht den Download nicht ab.
* **Grenzen:** höchstens ``MAX_FILES`` Dateien und ``MAX_BYTES`` Originale je Download – darüber eine verständliche
  Meldung. Die ZIP entsteht in einer temporären Datei (bis 50 MB im Speicher), die Originale werden nacheinander
  gelesen.
* **Datenschutz:** Die PDFs enthalten Name, Anschrift und Kontonummern. Der Download läuft nur über die geschützte
  Oberfläche; protokolliert werden nur Jahr und Anzahl, keine Dateiinhalte oder -namen.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, BinaryIO

from app.documentimport import txdocs

log = logging.getLogger(__name__)

MAX_FILES = 2000
MAX_BYTES = 500 * 1024 * 1024
SPOOL = 50 * 1024 * 1024
FOLDER = {"abrechnung": "01_Abrechnungen", "ertrag": "02_Ertraege", "kapitalmassnahme": "03_Kapitalmassnahmen",
          "depotauszug": "04_Depotauszuege", "ausfuehrung": "05_Orderbelege", "order": "05_Orderbelege",
          "kosteninformation": "05_Orderbelege"}
OTHER = "06_Sonstige"
NO_TX = "ohne_buchung"
ROLE_ORDER = ("abrechnung", "ertrag", "kapitalmassnahme", "depotauszug", "ausfuehrung", "order", "kosteninformation")
KIND_NAME = {"abrechnung_kauf": "Kauf", "abrechnung_verkauf": "Verkauf", "ertrag": "Ertrag",
             "kapitalmassnahme": "Kapitalmassnahme", "depotuebertrag": "Ausbuchung", "depotauszug": "Depotauszug",
             "ausfuehrungsanzeige": "Ausfuehrung", "auftragsbestaetigung": "Auftragsbestaetigung",
             "streichung": "Streichung", "kosteninfo_order": "Kosteninformation"}
HEADER = ("buchungs_id", "datum", "vorgang", "asset", "isin", "menge", "kurswert_eur", "gebuehr_eur", "gebuehr_quelle",
          "beleg_rolle", "datei_im_zip", "originalname", "sha256", "status")


@dataclass
class Pack:
    ok: bool = False
    message: str = ""
    file: BinaryIO | None = None
    size: int = 0
    files: int = 0
    rows: int = 0
    missing: int = 0
    errors: list[str] = field(default_factory=list)

    def chunks(self, size: int = 1 << 20) -> Iterator[bytes]:
        assert self.file is not None
        try:
            self.file.seek(0)
            while chunk := self.file.read(size):
                yield chunk
        finally:
            self.file.close()


def safe(text: str, limit: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("._-")[:limit] or "x"


def _de(v: Any) -> str:
    """Dezimalkomma für deutsche Tabellenprogramme."""
    return "" if v in (None, "") else str(v).replace(".", ",")


def _eur(v: Any) -> str:
    """Geldbetrag mit zwei Nachkommastellen und Dezimalkomma."""
    return "" if v in (None, "") else _de(f"{Decimal(str(v)):.2f}")


def _meta(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def _vorgang(t: Any) -> str:
    if t.type == "corporate_action":
        return f"Kapitalmassnahme {t.tag or ''}".strip()
    return f"{t.type} {t.tag}".strip() if t.tag else t.type


def _asset_of(t: Any) -> str:
    if t.type == "buy" or (t.type in ("deposit", "corporate_action") and t.to_asset):
        return t.to_asset or ""
    return t.from_asset if t.from_asset and t.from_asset != "EUR" else (t.related_asset or t.to_asset or "")


def _qty_of(t: Any) -> Any:
    return t.to_qty if t.type in ("buy", "deposit") or (t.type == "corporate_action" and t.to_qty) else t.from_qty


def _fee_source(t: Any, roles: set[str]) -> str:
    if t.fee_qty is None:
        return ""
    if "Kosteninformation" in (t.note or "") and "geschätzt" in (t.note or ""):
        return "geschaetzt"
    return "belegt" if "abrechnung" in roles else ""


def build(ctx: Any, year: int) -> Pack:
    """ZIP mit den Belegen des Jahres bauen. ``Pack.file`` ist eine temporäre Datei (nach dem Streamen geschlossen)."""
    from app.documentimport.service import EXT, document_service

    pf = ctx.portfolio()
    if pf is None:
        return Pack(message="Noch keine Buchungen vorhanden.")
    svc = document_service(ctx)
    db = ctx.db
    txs = {t.tx_id: t for t in pf.txs if t.date.year == year}
    links = db.q("SELECT t.tx_id, t.role, d.id, d.filename, d.stored, d.sha256, d.file_type FROM tx_document t "
                 "JOIN document d ON d.id=t.document_id")
    info = {r["sha256"]: r for r in db.q(
        "SELECT sha256, doctype, doc_date, meta FROM bank_doc WHERE sha256 IS NOT NULL")}
    docs: dict[int, dict[str, Any]] = {}
    rows: list[tuple[Any, Any, str]] = []  # (tx, doc row, role)
    roles_of: dict[str, set[str]] = {}
    for r in links:
        if r["tx_id"] in txs:
            roles_of.setdefault(r["tx_id"], set()).add(r["role"])
    for r in links:
        t = txs.get(r["tx_id"])
        if t is None:
            continue
        docs.setdefault(int(r["id"]), r)
        rows.append((t, r, r["role"]))
    linked_ids = {r["id"] for r in links}
    loose: list[Any] = []
    for r in db.q("SELECT d.id, d.filename, d.stored, d.sha256, d.file_type, d.created_at FROM document d"):
        if r["id"] in linked_ids:
            continue
        bd = info.get(r["sha256"])
        day = (bd["doc_date"] if bd and bd["doc_date"] else (r["created_at"] or ""))[:4]
        if day == str(year):
            loose.append(r)
            docs.setdefault(int(r["id"]), r)
    if not docs:
        return Pack(message=f"Für {year} gibt es keine verknüpften Belege. Belege werden bei der Zuordnung einer "
                            "Abrechnung oder beim Übernehmen aus dem Prüf-Stapel mit der Buchung verknüpft.")
    if len(docs) > MAX_FILES:
        return Pack(message=f"{len(docs)} Belege für {year} sind zu viele für einen Download (höchstens {MAX_FILES}). "
                            "Bitte den Filter einschränken.")
    total = 0
    for r in docs.values():
        if r["stored"]:
            p = svc.path(svc.get(int(r["id"])))
            total += p.stat().st_size if p.is_file() else 0
    if total > MAX_BYTES:
        return Pack(message=f"Die Originale für {year} sind zusammen {total // (1024 * 1024)} MB groß (höchstens "
                            f"{MAX_BYTES // (1024 * 1024)} MB). Bitte den Filter einschränken.")

    names: dict[int, str] = {}
    used: set[str] = set()

    def name_for(r: Any, role: str | None) -> str:
        bd = info.get(r["sha256"])
        meta = _meta(bd["meta"]) if bd else {}
        kind = KIND_NAME.get(bd["doctype"], "Beleg") if bd else "Beleg"
        day = (bd["doc_date"] if bd and bd["doc_date"] else "")[:10]
        ident = meta.get("order_no") or meta.get("belegnr") or meta.get("abrechnungsnr") or str(r["id"])
        base = "_".join(x for x in (day, kind, meta.get("isin") or "", str(ident)) if x)
        folder = NO_TX if role is None else FOLDER.get(role, OTHER)
        ext = EXT.get(r["file_type"], "bin")
        stem = safe(base, 110)
        cand = f"belege-{year}/{folder}/{stem}.{ext}"
        n = 1
        while cand in used:
            n += 1
            cand = f"belege-{year}/{folder}/{stem}_{n}.{ext}"
        used.add(cand)
        return cand

    # je Beleg genau eine Datei: Ordner nach der wichtigsten Rolle
    def rank(role: str) -> int:
        return ROLE_ORDER.index(role) if role in ROLE_ORDER else len(ROLE_ORDER)

    best: dict[int, str] = {}
    for _t, r, role in rows:
        did = int(r["id"])
        if did not in best or rank(role) < rank(best[did]):
            best[did] = role
    for did, r in docs.items():
        names[did] = name_for(r, best.get(did))

    def isin_of(aid: str) -> str:
        a = pf.assets.get(aid)
        return (a.isin or "") if a is not None else ""

    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL)  # noqa: SIM115 - Pack.chunks schließt die Datei
    pack = Pack(file=spool)
    manifest = io.StringIO()
    w = csv.writer(manifest, delimiter=";", lineterminator="\r\n")
    w.writerow(HEADER)
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
            written: set[int] = set()
            for did, r in docs.items():
                data = svc.original(did) if r["stored"] else None
                if data is not None:
                    zf.writestr(names[did], data)
                    written.add(did)
                    pack.files += 1
            for t, r, role in sorted(rows, key=lambda x: (x[0].date, x[0].tx_id)):
                did = int(r["id"])
                ok = did in written
                pack.missing += 0 if ok else 1
                aid = _asset_of(t)
                w.writerow([t.tx_id, t.date.isoformat(), _vorgang(t), aid, isin_of(aid), _de(_qty_of(t)),
                            _eur(t.value_eur), _eur(t.fee_eur if t.fee_eur is not None else t.fee_qty),
                            _fee_source(t, roles_of.get(t.tx_id, set())), txdocs.ROLE_LABEL.get(role, role),
                            names[did] if ok else "", r["filename"], r["sha256"] or "",
                            "ok" if ok else "original_fehlt"])
                pack.rows += 1
            for r in loose:
                did = int(r["id"])
                ok = did in written
                pack.missing += 0 if ok else 1
                w.writerow(["", "", "", "", "", "", "", "", "", "ohne Buchung", names[did] if ok else "",
                            r["filename"], r["sha256"] or "", "ok" if ok else "original_fehlt"])
                pack.rows += 1
            zf.writestr(f"belege-{year}/manifest.csv", "\ufeff" + manifest.getvalue())
    except Exception:
        spool.close()
        raise
    pack.size = spool.tell()
    pack.ok = True
    pack.message = (f"{pack.files} Belege für {year}" + (f", {pack.missing} Originale fehlen (siehe Manifest)"
                                                         if pack.missing else ""))
    log.info("Belege-Download %s: %s Dateien, %s Zeilen", year, pack.files, pack.rows)
    return pack


__all__ = ["Pack", "build", "safe"]
