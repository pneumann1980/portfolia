"""Dokumentimport (M25): Upload-Stapel annehmen, im Hintergrund auswerten, in den Prüf-Stapel übergeben.

Ablauf je Stapel (Fortschritt mit echten Phasen, abbrechbar):

    Dokument analysieren → Text extrahieren / OCR → Transaktionen erkennen → fehlende Daten recherchieren
    → bestehende Buchungen abgleichen (Prüf-Stapel der Import-Pipeline) → Vorschläge vorbereiten → fertig

Es wird **nichts gebucht**: Übernehmen, Verknüpfen, Auslassen, Sammelaktionen und Rückgängig laufen über den
bestehenden Prüf-Stapel (:mod:`app.csvimport`), Ergänzungen vorhandener Buchungen über die Diagnose
(:mod:`app.documentimport.amend`). Wiederholbar und idempotent: ein bereits verarbeiteter Beleg (gleicher SHA-256)
wird erkannt und nicht erneut gestaget; eine geänderte Fassung desselben Belegs verweist auf die frühere.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.documentimport import enrich as E
from app.documentimport.bridge import to_recs
from app.documentimport.evidence import FieldEvidence
from app.documentimport.extract import MAX_FILE, DocumentError, DocumentResult, sniff
from app.documentimport.profiles import DOC_LABEL, PROVIDER_LABEL, Analysis, DocTx, analyze
from app.documentimport.worker import Cancelled, extract_isolated
from app.progress import job_progress
from app.util.timeutil import iso

log = logging.getLogger(__name__)
JOB = "documents"
MAX_FILES = 20
EXT = {"pdf": "pdf", "png": "png", "jpeg": "jpg", "webp": "webp"}
_LOCK = threading.Lock()
_CANCEL: dict[str, threading.Event] = {}
_NAME_RE = re.compile(r"[^\w.\- ()äöüÄÖÜß]+")
EDITABLE = ("kind", "date", "time", "quantity", "symbol", "isin", "gross", "ccy", "fee", "fee_ccy", "value_eur",
            "txhash", "ext_id", "price", "network_fee")


def _now() -> str:
    return iso(datetime.now(UTC)) or ""


def safe_name(name: str | None) -> str:
    """Dateiname nur zur Anzeige: ohne Pfad, Steuerzeichen und Sonderzeichen, gekürzt."""
    base = os.path.basename((name or "").replace("\\", "/")) or "Beleg"
    base = _NAME_RE.sub("_", base).strip(" ._") or "Beleg"
    return base[:120]


@dataclass
class Accepted:
    stack_id: str | None
    errors: list[str] = field(default_factory=list)
    known: list[dict[str, Any]] = field(default_factory=list)  # bereits verarbeitete Belege (gleicher Inhalt)
    queued: int = 0


class DocumentService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.dir = Path(ctx.config.data_dir) / "documents"
        self.tmp = Path(ctx.config.data_dir) / "tmp" / "documents"

    # -- Speicher ------------------------------------------------------------------------------------------
    def path(self, row: Any) -> Path:
        return self.dir / row["sha256"][:2] / f"{row['sha256']}.{EXT.get(row['file_type'], 'bin')}"

    def _write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(path.suffix + ".part")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def original(self, doc_id: int) -> bytes | None:
        row = self.get(doc_id)
        if row is None or not row["stored"]:
            return None
        p = self.path(row)
        return p.read_bytes() if p.is_file() else None

    def get(self, doc_id: int) -> Any:
        return self.db.q1("SELECT * FROM document WHERE id=?", (doc_id,))

    def list(self, limit: int = 200) -> list[Any]:
        return self.db.q("SELECT * FROM document ORDER BY id DESC LIMIT ?", (limit,))

    def stack(self, sid: str) -> Any:
        return self.db.q1("SELECT * FROM document_stack WHERE id=?", (sid,))

    # -- Annahme -------------------------------------------------------------------------------------------
    def accept(self, files: list[tuple[str | None, bytes]], *, account: str = "", public: bool = False,
               reanalyze: bool = False) -> Accepted:
        """Dateien prüfen (Inhaltstyp, Größe), Duplikate erkennen, Original sichern, Stapel anlegen. Startet den
        Hintergrundlauf nicht (siehe :meth:`start`)."""
        out = Accepted(None)
        if not files:
            out.errors.append("Keine Datei ausgewählt.")
            return out
        if len(files) > MAX_FILES:
            out.errors.append(f"Höchstens {MAX_FILES} Dateien je Stapel.")
            return out
        keep = bool(self.ctx.settings.get("documents.keep_originals", True))
        sid = datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
        stamp = _now()
        rows = []
        for name, data in files:
            fname = safe_name(name)
            try:
                if len(data) > MAX_FILE:
                    raise DocumentError(f"Datei zu groß (höchstens {MAX_FILE // (1024 * 1024)} MiB).")
                kind = sniff(data)
            except DocumentError as e:
                out.errors.append(f"{fname}: {e}")
                continue
            import hashlib

            sha = hashlib.sha256(data).hexdigest()
            prev = self.db.q1("SELECT * FROM document WHERE sha256=?", (sha,))
            if prev is not None and prev["status"] in ("analysed", "staged") and not reanalyze:
                out.known.append({"id": prev["id"], "filename": prev["filename"], "batch_id": prev["batch_id"]})
                continue
            self.tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._write(self.tmp / f"{sha}.in", data)
            if keep:
                self._write(self.dir / sha[:2] / f"{sha}.{EXT[kind]}", data)
            rows.append((sha, fname, kind, len(data), 1 if keep else 0, prev))
        if not rows:
            return out
        with self.db.transaction() as c:
            c.execute("INSERT INTO document_stack(id, status, options_json, summary_json, created_at) VALUES "
                      "(?, 'queued', ?, '{}', ?)", (sid, json.dumps({"account": account[:80], "public": public}),
                                                   stamp))
            for sha, fname, kind, size, stored, prev in rows:
                if prev is not None:
                    c.execute("UPDATE document SET status='queued', stack_id=?, filename=?, stored=MAX(stored, ?), "
                              "error=NULL, updated_at=? WHERE id=?", (sid, fname, stored, stamp, prev["id"]))
                else:
                    c.execute("INSERT INTO document(sha256, filename, file_type, size, stored, status, stack_id, "
                              "created_at, updated_at) VALUES (?,?,?,?,?, 'queued', ?,?,?)",
                              (sha, fname, kind, size, stored, sid, stamp, stamp))
        out.stack_id, out.queued = sid, len(rows)
        return out

    def start(self, sid: str) -> dict[str, Any]:
        if not _LOCK.acquire(blocking=False):
            return {"error": "Ein Dokumentimport läuft bereits – bitte warten oder abbrechen."}
        ev = threading.Event()
        _CANCEL[sid] = ev

        def run() -> None:
            try:
                self.run(sid, ev)
            except Exception:  # pragma: no cover - Absicherung des Hintergrundlaufs
                log.exception("Dokumentimport %s abgebrochen", sid)
                self.db.x("UPDATE document_stack SET status='failed', finished_at=? WHERE id=?", (_now(), sid))
            finally:
                _CANCEL.pop(sid, None)
                _LOCK.release()
                self.db.close_thread_conn()

        t = threading.Thread(target=run, name=f"documents-{sid}", daemon=True)
        try:
            t.start()
        except Exception:  # pragma: no cover
            _LOCK.release()
            raise
        return {"started": True}

    def cancel(self, sid: str) -> str:
        ev = _CANCEL.get(sid)
        if ev is None:
            st = self.stack(sid)
            if st is not None and st["status"] in ("queued", "running"):
                self.db.x("UPDATE document_stack SET status='cancelled', finished_at=? WHERE id=?", (_now(), sid))
                return "Kein laufender Vorgang gefunden – Stapel als abgebrochen markiert."
            return "Es läuft kein Dokumentimport."
        ev.set()
        return "Abbruch angefordert."

    @staticmethod
    def running() -> bool:
        return _LOCK.locked()

    # -- Auswertung ----------------------------------------------------------------------------------------
    def run(self, sid: str, ev: threading.Event | None = None, *, transport: Any = None) -> dict[str, Any]:
        """Stapel auswerten (synchron; im Hintergrund über :meth:`start`)."""
        ev = ev or threading.Event()
        st = self.stack(sid)
        if st is None:
            return {"error": "Stapel nicht gefunden."}
        opts = json.loads(st["options_json"] or "{}")
        docs = self.db.q("SELECT * FROM document WHERE stack_id=? AND status='queued' ORDER BY id", (sid,))
        images = [d for d in docs if d["file_type"] != "pdf"]
        phases = ["analyze", "extract", *(["ocr"] if images else []), "detect", "research", "match", "propose"]
        prog = job_progress(self.ctx, JOB, "Dokumentimport", unit="Dokumente", phases=phases)
        self.db.x("UPDATE document_stack SET status='running' WHERE id=?", (sid,))
        prog.phase("analyze", len(docs), "Dateien prüfen")
        lang = str(self.ctx.settings.get("documents.language", "deu+eng") or "deu+eng")
        ocr_on = bool(self.ctx.settings.get("documents.ocr", True))
        results: dict[int, tuple[DocumentResult, Analysis]] = {}
        errors: list[str] = []
        try:
            for phase, group in (("extract", [d for d in docs if d["file_type"] == "pdf"]), ("ocr", images)):
                if not group:
                    continue
                prog.phase(phase, len(group), "Text extrahieren" if phase == "extract" else "OCR durchführen")
                for i, d in enumerate(group):
                    if ev.is_set():
                        raise Cancelled("Abgebrochen.")
                    prog.update(i, len(group), f"{d['filename']}")
                    try:
                        data = (self.tmp / f"{d['sha256']}.in").read_bytes()
                        res = extract_isolated(data, language=lang, ocr=ocr_on, cancel=ev)
                    except Cancelled:
                        raise
                    except (DocumentError, OSError) as e:
                        errors.append(f"{d['filename']}: {e}")
                        self.db.x("UPDATE document SET status='failed', error=?, updated_at=? WHERE id=?",
                                  (str(e)[:300], _now(), d["id"]))
                        continue
                    results[d["id"]] = (res, None)  # type: ignore[assignment]
                    prog.update(i + 1, len(group))
            prog.phase("detect", len(results), "Transaktionen erkennen")
            items: list[tuple[str, DocTx]] = []
            meta: dict[str, dict[str, Any]] = {}
            for n, (doc_id, (res, _a)) in enumerate(results.items()):
                an = analyze(res)
                results[doc_id] = (res, an)
                row = self.get(doc_id)
                self._apply_overrides(row, an)
                for tx in an.txs:
                    items.append((res.sha256, tx))
                meta[res.sha256] = {"id": doc_id, "filename": row["filename"], "sha256": res.sha256,
                                    "doc_type": an.doc_type, "provider": an.provider, "ocr": res.ocr_used,
                                    "pages": len(res.pages)}
                self._supersedes(row, an)
                self.db.x("UPDATE document SET status='analysed', pages=?, doc_type=?, provider=?, ocr=?, "
                          "analysis_json=?, updated_at=? WHERE id=?",
                          (len(res.pages), an.doc_type, an.provider, 1 if res.ocr_used else 0,
                           json.dumps(res.to_json(), ensure_ascii=False), _now(), doc_id))
                prog.update(n + 1, len(results))
            if ev.is_set():
                raise Cancelled("Abgebrochen.")
            prog.phase("research", len(items), "Fehlende Daten recherchieren")
            merged = E.merge_batch(items)
            ectx = E.Context.build(self.ctx)
            budget = E.Budget()
            public = bool(opts.get("public")) and E.public_lookup_enabled(self.ctx)
            enriched: list[tuple[str, E.Enriched]] = []
            for i, (sha, tx) in enumerate(merged):
                if ev.is_set():
                    raise Cancelled("Abgebrochen.")
                enriched.append((sha, E.enrich(ectx, tx, budget, public=public, transport=transport)))
                prog.update(i + 1, len(merged))
            prog.phase("match", len(enriched), "Bestehende Buchungen abgleichen")
            batch_ids = self._stage(sid, enriched, meta, opts.get("account") or "")
            prog.phase("propose", 1, "Vorschläge vorbereiten")
            summary = self._summary(batch_ids, errors, budget, results)
            self._store_results(results, enriched)
            with self.db.transaction() as c:
                c.execute("UPDATE document_stack SET status='done', summary_json=?, finished_at=? WHERE id=?",
                          (json.dumps(summary, ensure_ascii=False), _now(), sid))
            prog.finish(True, f"{summary['transactions']} Vorgänge aus {len(results)} Dokument(en)",
                        stack=sid, batches=batch_ids)
            return summary
        except Cancelled:
            self.db.x("UPDATE document_stack SET status='cancelled', finished_at=? WHERE id=?", (_now(), sid))
            self.db.x("UPDATE document SET status='failed', error='abgebrochen', updated_at=? WHERE stack_id=? AND "
                      "status='queued'", (_now(), sid))
            prog.finish(False, "Abgebrochen – nichts wurde in den Prüf-Stapel übernommen.", stack=sid)
            return {"cancelled": True}
        finally:
            self._cleanup(docs)

    def _cleanup(self, docs: list[Any]) -> None:
        for d in docs:
            (self.tmp / f"{d['sha256']}.in").unlink(missing_ok=True)

    def _apply_overrides(self, row: Any, an: Analysis) -> None:
        """Korrekturen des Nutzers als Belege mit Herkunft ``user`` (Vorrang vor allen anderen Quellen)."""
        ov = json.loads(row["overrides_json"] or "{}") if row is not None else {}
        for tx in an.txs:
            for name, value in (ov.get(str(tx.n)) or {}).items():
                if name not in EDITABLE:
                    continue
                if value in ("", None):
                    continue
                if name == "kind":
                    tx.kind = str(value)
                tx.evidence.append(FieldEvidence(name, str(value), "user", "user:korrektur", "Korrektur",
                                                 "belegt", "Korrektur durch den Nutzer"))
            tx.resolve()

    def _supersedes(self, row: Any, an: Analysis) -> None:
        """Geänderte Fassung: anderer Inhalt, aber dieselbe Kennung (Auftrags-/Transaktions-ID) wie ein früherer
        Beleg → Verweis; der Abgleich behandelt den Vorgang als bekannt und zeigt die Unterschiede."""
        ids = {tx.value("ext_id") for tx in an.txs if tx.value("ext_id")}
        if not ids:
            return
        for other in self.db.q("SELECT id, result_json FROM document WHERE id<>? AND result_json IS NOT NULL "
                               "ORDER BY id DESC LIMIT 500", (row["id"],)):
            try:
                res = json.loads(other["result_json"])
            except ValueError:
                continue
            o_ids = {t.get("fields", {}).get("ext_id", {}).get("value") for t in res.get("txs", [])}
            if ids & o_ids:
                self.db.x("UPDATE document SET supersedes=? WHERE id=?", (other["id"], row["id"]))
                return

    def _stage(self, sid: str, enriched: list[tuple[str, E.Enriched]], meta: dict[str, dict[str, Any]],
               account: str) -> list[int]:
        """Vorgänge je Anbieter als Prüf-Stapel der Import-Pipeline anlegen (keine Buchung)."""
        from app.csvimport.service import csv_service, rec_to_json

        groups: dict[str, list[Any]] = {}
        docs_of: dict[str, set[str]] = {}
        n = 1
        for sha, e in enriched:
            doc = meta.get(sha) or {}
            acc = account or self._account(e)
            src = f"doc:{e.tx.provider or e.tx.doc_type}"
            recs = to_recs(e, sha=sha, doc=doc, account=acc, n0=n)
            n += len(recs)
            groups.setdefault(src, []).extend(recs)
            docs_of.setdefault(src, set()).update({sha, *e.tx.sources})
        out: list[int] = []
        csv = csv_service(self.ctx)
        for src, recs in groups.items():
            key = src.split(":", 1)[1]
            label = f"Belege · {PROVIDER_LABEL.get(key) or DOC_LABEL.get(key) or key} · Stapel {sid}"
            payload = ("[" + ",".join(rec_to_json(r) for r in recs) + "]").encode()
            bid = csv.ingest(recs, source=src, profile=src, account=account or "Belege", label=label,
                             datasource_id=None, payload=payload)
            out.append(bid)
            for sha in docs_of[src]:
                self.db.x("UPDATE document SET status='staged', batch_id=?, updated_at=? WHERE sha256=?",
                          (bid, _now(), sha))
        return out

    def _account(self, e: E.Enriched) -> str:
        """Konto: vorhandene Buchung → deren Konto; Datenquelle desselben Anbieters (genau eine) → deren Konto;
        sonst Anbieter bzw. maskiertes Depot laut Beleg (im Prüf-Stapel einem Portfolia-Konto zuordnen)."""
        t = e.existing
        if t is not None:
            return t.to_account or t.from_account or "Belege"
        prov = e.tx.provider
        if prov:
            accs = {r["account"] for r in self.db.q("SELECT account FROM data_source WHERE provider=?", (prov,))}
            if len(accs) == 1:
                return next(iter(accs))
            return PROVIDER_LABEL.get(prov, prov)
        acc = e.tx.value("account")
        return f"Depot {acc}" if acc else "Belege"

    def _store_results(self, results: dict[int, tuple[DocumentResult, Analysis]],
                       enriched: list[tuple[str, E.Enriched]]) -> None:
        from app.documentimport.bridge import provenance

        by_sha: dict[str, list[dict[str, Any]]] = {}
        for sha, e in enriched:
            p = provenance(e, {})
            p["n"] = e.tx.n
            for s in [sha, *e.tx.sources]:
                by_sha.setdefault(s, []).append(p)
        for doc_id, (res, an) in results.items():
            data = {"doc_type": an.doc_type, "provider": an.provider, "convention": an.convention.evidence,
                    "warnings": [*res.warnings, *an.warnings], "txs": by_sha.get(res.sha256, []),
                    "pages": [{"n": p.number, "method": p.method, "conf": p.conf, "warnings": p.warnings}
                              for p in res.pages]}
            self.db.x("UPDATE document SET result_json=?, updated_at=? WHERE id=?",
                      (json.dumps(data, ensure_ascii=False, default=str), _now(), doc_id))

    def _summary(self, batch_ids: list[int], errors: list[str], budget: E.Budget,
                 results: dict[int, Any]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for bid in batch_ids:
            for r in self.db.q("SELECT status, COUNT(*) AS n FROM csv_row WHERE batch_id=? GROUP BY status", (bid,)):
                counts[r["status"]] = counts.get(r["status"], 0) + int(r["n"])
        return {"documents": len(results), "transactions": sum(counts.values()), "rows": counts,
                "batches": batch_ids, "errors": errors[:20], "public_requests": budget.used,
                "public_errors": budget.errors[:5]}

    # -- Korrektur und erneute Bewertung -------------------------------------------------------------------
    def correct(self, doc_id: int, n: int, values: dict[str, str]) -> dict[str, Any]:
        """Feldkorrekturen speichern und den Beleg ohne erneute OCR neu bewerten (Recherche, Abgleich)."""
        row = self.get(doc_id)
        if row is None or not row["analysis_json"]:
            return {"error": "Beleg nicht gefunden bzw. Text gelöscht – bitte erneut hochladen."}
        ov = json.loads(row["overrides_json"] or "{}")
        cur = ov.setdefault(str(n), {})
        for k, v in values.items():
            if k in EDITABLE:
                v = (v or "").strip()[:200]
                if v:
                    cur[k] = v
                else:
                    cur.pop(k, None)
        self.db.x("UPDATE document SET overrides_json=?, updated_at=? WHERE id=?",
                  (json.dumps(ov, ensure_ascii=False), _now(), doc_id))
        return self.reevaluate(doc_id)

    def reevaluate(self, doc_id: int) -> dict[str, Any]:
        """Neu bewerten: Analyse aus dem gespeicherten Text + Korrekturen, Recherche, Prüfzeilen ersetzen (nur
        unbearbeitete, nicht übernommene Zeilen dieses Belegs)."""
        from app.csvimport.service import csv_service, rec_to_json

        row = self.get(doc_id)
        if row is None or not row["analysis_json"]:
            return {"error": "Beleg nicht gefunden bzw. Text gelöscht."}
        res = DocumentResult.from_json(json.loads(row["analysis_json"]))
        an = analyze(res)
        self._apply_overrides(row, an)
        ectx = E.Context.build(self.ctx)
        budget = E.Budget(requests=0)
        enriched = [(res.sha256, E.enrich(ectx, tx, budget)) for tx in an.txs]
        meta = {"id": row["id"], "filename": row["filename"], "sha256": res.sha256, "doc_type": an.doc_type,
                "provider": an.provider, "ocr": bool(row["ocr"]), "pages": row["pages"]}
        bid = row["batch_id"]
        if bid is None or self.db.q1("SELECT 1 FROM csv_batch WHERE id=? AND status IN ('preview', 'partial')",
                                     (bid,)) is None:
            opts = {"account": ""}
            ids = self._stage(f"neu-{doc_id}", enriched, {res.sha256: meta}, opts["account"])
            self._store_results({doc_id: (res, an)}, enriched)
            return {"batches": ids}
        csv = csv_service(self.ctx)
        recs = []
        n = 1
        for sha, e in enriched:
            rs = to_recs(e, sha=sha, doc=meta, account=self._account(e), n0=n)
            n += len(rs)
            recs += rs
        keys = {r.event_key for r in recs}
        old = self.db.q("SELECT id, event_key, status, decision, tx_id FROM csv_row WHERE batch_id=?", (bid,))
        own = f"doc:{res.sha256[:16]}:"
        mine = [r for r in old if r["event_key"] in keys or (r["event_key"] or "").startswith(own)]
        if any(r["status"] in ("committed", "merged", "linked") or r["tx_id"] for r in mine):
            return {"error": "Ein Vorgang dieses Belegs ist bereits übernommen bzw. verknüpft – Änderungen an "
                             "Buchungen über „Bestehende Buchung ergänzen“ bzw. im Journal."}
        first = int(self.db.scalar("SELECT COALESCE(MAX(idx), -1) + 1 FROM csv_row WHERE batch_id=?", (bid,),
                                   default=0))
        with self.db.transaction() as c:
            for r in mine:
                c.execute("DELETE FROM csv_row WHERE id=?", (r["id"],))
            c.executemany("INSERT INTO csv_row(batch_id, idx, line, rec_json, status, event_key, event_line) "
                          "VALUES (?,?,?,?, 'new', ?,?)",
                          [(bid, first + i, r.line, rec_to_json(r), r.event_key, r.event_line)
                           for i, r in enumerate(recs)])
        csv.evaluate(bid)
        self._store_results({doc_id: (res, an)}, enriched)
        return {"batches": [bid]}

    # -- Datenschutz -----------------------------------------------------------------------------------------
    def delete_original(self, doc_id: int) -> bool:
        """Original und extrahierten Volltext löschen. Erhalten bleiben SHA-256, Dateiname, Feldbelege mit
        Herkunft (ohne Volltext) und die daraus entstandenen Prüfzeilen/Buchungen."""
        row = self.get(doc_id)
        if row is None:
            return False
        self.path(row).unlink(missing_ok=True)
        with __import__("contextlib").suppress(OSError):
            self.path(row).parent.rmdir()
        self.db.x("UPDATE document SET stored=0, analysis_json=NULL, text_deleted_at=?, updated_at=? WHERE id=?",
                  (_now(), _now(), doc_id))
        log.info("Dokument %s: Original und Volltext gelöscht", doc_id)
        return True

    def preview(self, doc_id: int, page: int = 1, box: tuple[float, float, float, float] | None = None,
                width: int = 900) -> bytes | None:
        """Belegausschnitt als PNG (Seite bzw. Box mit Rand) – nur aus dem lokal gespeicherten Original."""
        import io

        data = self.original(doc_id)
        row = self.get(doc_id)
        if data is None or row is None:
            return None
        from PIL import Image

        from app.documentimport.extract import render_page

        if row["file_type"] == "pdf":
            img = render_page(data, page, scale=2.0)
        else:
            Image.MAX_IMAGE_PIXELS = 30_000_000
            img = Image.open(io.BytesIO(data)).convert("RGB")
        if box:
            x0, y0, x1, y1 = box
            w, h = img.size
            pad_y = 0.04
            img = img.crop((0, int(max(0.0, y0 - pad_y) * h), w, int(min(1.0, y1 + pad_y) * h)))
            del x0, x1
        if img.size[0] > width:
            img = img.resize((width, max(1, int(img.size[1] * width / img.size[0]))), resample=3)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    def purge_tmp(self) -> None:
        """Zwischendateien abgebrochener Läufe entfernen (Start der App)."""
        if self.tmp.is_dir():
            shutil.rmtree(self.tmp, ignore_errors=True)


def document_service(ctx: Any) -> DocumentService:
    return DocumentService(ctx)
