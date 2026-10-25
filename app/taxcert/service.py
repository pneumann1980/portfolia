"""Steuerbescheinigungen ablegen und bereitstellen (Einstellung ``tax.certs`` + Originaldatei unter ``tax/certs``).

Je Steuerjahr höchstens eine Jahressteuerbescheinigung und eine Erträgnisaufstellung; ein erneuter Upload derselben Art
**ersetzt** die frühere Fassung (die Datei wird gelöscht), gleicher Inhalt (SHA-256) wird nur gemeldet. Die Werte
stehen als Text-Beträge in den Einstellungen (Teil von Export und Wiederherstellung); die Originaldatei bleibt als
Beleg im Datenordner und kann geöffnet oder gelöscht werden. Es entstehen nie Buchungen.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.documentimport.extract import DocumentError
from app.documentimport.worker import extract_isolated
from app.taxcert import parse as P
from app.util.timeutil import iso

log = logging.getLogger(__name__)
KEY = "tax.certs"
MAX_SIZE = 10 * 1024 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _s(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _s(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_s(x) for x in v]
    return v


class TaxCertService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    @property
    def base(self) -> Path:
        return self.ctx.config.tax_data_dir / "certs"

    # -- Lesen ---------------------------------------------------------------------------------------------
    def all(self) -> dict[str, dict[str, Any]]:
        v = self.ctx.settings.get(KEY)
        return dict(v) if isinstance(v, dict) else {}

    def for_tax(self) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for y, entry in self.all().items():
            try:
                out[int(y)] = entry
            except (TypeError, ValueError):
                continue
        return out

    def years(self) -> list[int]:
        return sorted(self.for_tax(), reverse=True)

    def file_path(self, year: int, kind: str) -> tuple[Path, str] | None:
        e = (self.for_tax().get(year) or {}).get(kind)
        if not e:
            return None
        base = self.base.resolve()
        path = (base / str(year) / str(e.get("file", ""))).resolve()
        if base not in path.parents or not path.is_file():
            return None
        return path, str(e.get("filename") or path.name)

    def checks(self, year: int) -> list[str]:
        """Abweichungen zwischen Bescheinigung und Erträgnisaufstellung desselben Jahres."""
        entry = self.for_tax().get(year) or {}
        b, e = entry.get(P.KIND_BESCHEINIGUNG), entry.get(P.KIND_ERTRAEGNIS)
        out: list[str] = []
        if not (b and e):
            return out
        bv = {k: Decimal(v) for k, v in (b.get("values") or {}).items()}
        ev = {k: Decimal(v) for k, v in (e.get("hints") or {}).items()}
        pairs = [("Höhe der Kapitalerträge", bv.get("capital_total"),
                  Decimal(e["capital_total"]) if e.get("capital_total") else None),
                 ("Sparer-Pauschbetrag", bv.get("pauschbetrag"), ev.get("pauschbetrag")),
                 ("Kapitalertragsteuer", bv.get("kest"), ev.get("kest")),
                 ("Solidaritätszuschlag", bv.get("soli"), ev.get("soli"))]
        for label, x, y in pairs:
            if x is not None and y is not None and abs(x - y) > Decimal("0.01"):
                out.append(f"{label}: Bescheinigung {x:.2f} € ≠ Erträgnisaufstellung {y:.2f} €")
        return out

    # -- Aufnehmen -------------------------------------------------------------------------------------------
    def add(self, data: bytes, filename: str) -> dict[str, Any]:
        """Datei lesen, Art erkennen, Werte prüfen und ablegen. Rückgabe: ok, message, year, kind, warnings …"""
        name = _SAFE.sub("_", os.path.basename(filename or "beleg.pdf"))[:100] or "beleg.pdf"
        res: dict[str, Any] = {"ok": False, "file": filename, "warnings": []}
        if len(data) > MAX_SIZE:
            res["message"] = f"Datei zu groß (höchstens {MAX_SIZE // 1024 // 1024} MB)."
            return res
        try:
            doc = extract_isolated(data, ocr=False, plain=True)
        except DocumentError as e:
            res["message"] = str(e)
            return res
        pages = [[ln.text for ln in p.lines] for p in doc.pages]
        kind = P.classify(pages)
        if kind is None:
            res["message"] = ("Dokumentart nicht erkannt – erwartet: Jahressteuerbescheinigung oder "
                              "Erträgnisaufstellung („Erläuterungen zur Steuerbescheinigung“) im PDF-Format.")
            return res
        parsed: Any = P.parse_bescheinigung(pages) if kind == P.KIND_BESCHEINIGUNG else P.parse_ertraegnis(pages)
        if not parsed.ok:
            res["message"] = f"{P.KIND_LABEL[kind]} nicht lesbar: " + "; ".join(parsed.notes or ["Layout unbekannt"])
            return res
        year = int(parsed.year)
        sha = hashlib.sha256(data).hexdigest()
        res.update(year=year, kind=kind, label=P.KIND_LABEL[kind])
        certs = self.all()
        entry = dict(certs.get(str(year)) or {})
        old = entry.get(kind)
        if old and old.get("sha256") == sha:
            res.update(ok=True, duplicate=True, message="Bereits vorhanden – unverändert.")
            return res
        folder = self.base / str(year)
        folder.mkdir(parents=True, exist_ok=True)
        fname = f"{kind}-{sha[:12]}.pdf"
        fd = os.open(folder / fname, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        item: dict[str, Any] = {"file": fname, "filename": name, "sha256": sha, "uploaded": iso(datetime.now(UTC)),
                                "issuer": parsed.issuer, "notes": list(parsed.notes)}
        if kind == P.KIND_BESCHEINIGUNG:
            item.update(values=_s(parsed.values), extra_lines=list(parsed.extra_lines))
        else:
            item.update(totals=_s(parsed.totals), capital_total=_s(parsed.capital_total),
                        carry_out=_s(parsed.carry_out), carry_in=_s(parsed.carry_in()), hints=_s(parsed.hints),
                        column_probe=parsed.column_probe,
                        events=[{"kind": e.kind, "date": e.date.isoformat(), "name": e.name, "isin": e.isin,
                                 "amounts": _s(e.amounts), "pot": e.pot} for e in parsed.events])
        entry[kind] = item
        certs[str(year)] = entry
        if old and old.get("file") and old["file"] != fname:
            with_path = folder / str(old["file"])
            if with_path.is_file() and with_path.parent == folder:
                with_path.unlink(missing_ok=True)
        self.ctx.settings.set(KEY, certs)
        res.update(ok=True, replaced=bool(old),
                   message="Frühere Fassung ersetzt." if old else "Gelesen und abgelegt.")
        res["warnings"] = list(parsed.notes) + [f"Weitere Angaben „Zeile {z} Anlage KAP“ wurden nicht ausgewertet."
                                                for z in getattr(parsed, "extra_lines", [])] + self.checks(year)
        if kind == P.KIND_BESCHEINIGUNG:
            res["joint_enabled"] = self._maybe_joint(year, parsed.values.get("pauschbetrag"))
        log.info("Steuerbescheinigung %s %s aufgenommen", year, kind)
        return res

    def _maybe_joint(self, year: int, pauschbetrag: Decimal | None) -> bool:
        """Weist die Bank mehr als den Einzel-Pauschbetrag aus, kann es nur Zusammenveranlagung sein → Option setzen."""
        if pauschbetrag is None:
            return False
        from app.tax.service import tax_service

        svc = tax_service(self.ctx)
        pack = svc.pack()
        try:
            single = Decimal(str(pack.p(year)["capital"]["sparer_pauschbetrag_single"]))
        except (KeyError, AttributeError):
            return False
        if pauschbetrag > single and not svc.options(pack, year).get("joint"):
            svc.save_options(pack, year, {"joint": True})
            return True
        return False

    def remove(self, year: int, kind: str) -> bool:
        certs = self.all()
        entry = dict(certs.get(str(year)) or {})
        item = entry.pop(kind, None)
        if item is None:
            return False
        folder = (self.base / str(year)).resolve()
        p = (folder / str(item.get("file", ""))).resolve()
        if folder in p.parents and p.is_file():
            p.unlink(missing_ok=True)
        if entry:
            certs[str(year)] = entry
        else:
            certs.pop(str(year), None)
        self.ctx.settings.set(KEY, certs)
        return True


def taxcert_service(ctx: Any) -> TaxCertService:
    return TaxCertService(ctx)
