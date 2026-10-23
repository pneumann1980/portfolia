"""DKB-Wertpapierdokumente per ZIP-Upload: Hintergrundlauf mit Fortschritt, Restzeit, Abbruch und Fortsetzen.

Ablauf (alles in der Oberfläche, kein Kommandozeilenweg):

1. **Annahme:** Das ZIP wird als Zwischendatei (Rechte 0600) in ``<data>/tmp`` abgelegt und geprüft (Signatur, Größe,
   Mitgliederzahl, Kompressionsverhältnis, Verschlüsselung). Die Datei bleibt nur bis zum Ende des Laufs bestehen –
   bei Abbruch oder Fehler, damit „Fortsetzen“ möglich ist.
2. **ZIP entpacken (Phase ``zip_unpack``):** Jedes PDF-Mitglied wird im Speicher gelesen und per SHA-256 mit dem Index
   (``bank_doc``) und dem Archiv selbst verglichen. Bekanntes wird als Dublette gezählt und nicht erneut verarbeitet.
3. **Dokumente erkennen (Phase ``zip_eval``):** Text-Extraktion im isolierten Kindprozess (kein OCR), Klassifikation
   nach Inhalt (:mod:`app.documentimport.dkb`). Erkannte Dokumente werden unter selbst gebautem Pfad im Ordner der
   Datenquelle abgelegt und im Index ``bank_doc`` mit Status ``classified`` geführt; **nicht erkannte werden gezählt,
   aufgelistet und nicht abgelegt**. Gebucht oder vorgeschlagen wird in dieser Stufe nichts.
4. **Abrechnungen zuordnen (Phase ``zip_match``):** Kauf-/Verkaufsabrechnungen gehen durch den Dokumentimport
   (DKB-Profil mit Rechenprobe). Gehört eine Abrechnung zu einer **vorhandenen Buchung** (Konto, Asset, Menge,
   Datum ± 3 Tage), wird sie nur verknüpft und als Ergänzung vorgeschlagen (Gebühr, Kurswert, Uhrzeit) – es
   entsteht keine neue Buchung. Alle anderen kommen als Vorschlag in den Prüf-Stapel, bei nicht bestandener
   Rechenprobe als „ungeklärt“.
5. **Bericht (Phase ``zip_report``):** Zähler je Dokumentart, Dubletten, übersprungene und nicht lesbare Dateien.

Fehler einzelner Dateien beenden den Lauf nicht. Je Datenquelle läuft höchstens ein Vorgang (Abruf, Auswertung oder
ZIP); ein zweiter wird verständlich abgewiesen.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

from app.datasources import dkb as D
from app.datasources import dkb_zip as Z
from app.documentimport.dkb import GROUP_LABEL, KINDS, classify
from app.documentimport.extract import DocumentError
from app.documentimport.worker import Cancelled, extract_isolated
from app.progress import Progress, job_sink

log = logging.getLogger(__name__)

JOB = "dkb_zip:{sid}"
SETTING_RATES = "dkb_zip.eta_rates"
# Startschätzung (Bytes je Sekunde); nach dem ersten Lauf ersetzen gemessene Werte (Einstellung ``dkb_zip.eta_rates``)
DEFAULT_RATES = {"zip_unpack": 20e6, "zip_eval": 300e3, "zip_match": 3.0}
TRADE_KINDS = ("abrechnung_kauf", "abrechnung_verkauf")  # Phase 2: werden ausgewertet; weitere Arten folgen
KEEP_TMP_DAYS = 7
SUB_LABEL = {"storno": "Storno", "entgelt": "Entgeltbelastung", "reverse_split": "Zusammenlegung (Reverse Split)",
             "split": "Aktiensplit", "gratis": "Gratisausgabe", "neubuchung": "Neubuchung nach Storno",
             "umbenennung": "Namensänderung", "wertlos": "Wertlose Titel (Ausbuchung)",
             "liquidation": "Liquidation", "optionsfrist": "Ablauf der Optionsfrist", "insolvenz": "Insolvenzverfahren",
             "umtausch": "Umtausch/Fusion", "spinoff": "Abspaltung", "bezugsrecht": "Bezugsrecht",
             "dividende": "Dividende", "kapitalrueckzahlung": "Kapitalrückzahlung",
             "ersatzlose_ausbuchung": "Ersatzlose Ausbuchung", "": "Unterart nicht erkannt"}


def tmp_dir(ctx: Any) -> Path:
    p = Path(ctx.config.data_dir) / "tmp"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


UPLOAD_MAX = 100 * 1024 * 1024  # Obergrenze des Uploads (Anfragegrenze der App: security.UPLOAD_LIMITS)


def save_upload(ctx: Any, stream: BinaryIO) -> Path:
    """Upload in eine Zwischendatei (0600) kopieren – mit harter Obergrenze."""
    _cleanup(ctx)
    path = tmp_dir(ctx) / f"dkbzip-{int(time.time())}-{os.urandom(4).hex()}.zip"
    size = 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := stream.read(1 << 20):
                size += len(chunk)
                if size > UPLOAD_MAX:
                    raise Z.ZipRejected(f"Archiv zu groß (höchstens {UPLOAD_MAX // (1024 * 1024)} MiB je Upload – bei "
                                        "mehr Dokumenten bitte in mehrere ZIP-Dateien aufteilen).")
                out.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _cleanup(ctx: Any) -> None:
    """Zwischendateien alter, nie fortgesetzter Läufe entfernen."""
    limit = time.time() - KEEP_TMP_DAYS * 86400
    for p in tmp_dir(ctx).glob("dkbzip-*.zip"):
        with contextlib.suppress(OSError):
            if p.stat().st_mtime < limit:
                p.unlink()


def _summary_new(filename: str, zip_tmp: Path, lineage: list[int]) -> dict[str, Any]:
    return {"kind": "zip", "filename": filename[:160], "zip_tmp": str(zip_tmp), "lineage": lineage, "members": 0,
            "stored": 0, "by_kind": {}, "by_sub": {}, "depots": {}, "duplicate_known": [], "duplicate_in_zip": [],
            "skipped": [], "unrecognized": [], "unreadable": [], "notes": [], "resumable": False}


def _load(db: Any, rid: int) -> dict[str, Any]:
    r = db.q1("SELECT summary_json FROM bank_run WHERE id=?", (rid,))
    try:
        return json.loads(r["summary_json"] or "{}") if r else {}
    except ValueError:
        return {}


def runs(db: Any, sid: int, limit: int = 8) -> list[dict[str, Any]]:
    out = []
    for r in db.q("SELECT * FROM bank_run WHERE source_id=? ORDER BY id DESC LIMIT 40", (sid,)):
        s = _load(db, r["id"])
        if s.get("kind") != "zip":
            continue
        d = dict(r)
        d["summary"] = s
        d["label"] = D.STATUS_LABEL.get(r["status"], r["status"])
        out.append(d)
        if len(out) >= limit:
            break
    return out


def view(ctx: Any, sid: int, rid: int) -> dict[str, Any] | None:
    """Lauf für die Anzeige: Zustand, Fortschritt (laufend), Bericht (beendet)."""
    from app.progress import view as progress_view

    r = ctx.db.q1("SELECT * FROM bank_run WHERE id=? AND source_id=?", (rid, sid))
    if r is None:
        return None
    s = _load(ctx.db, rid)
    if s.get("kind") != "zip":
        return None
    d = dict(r)
    d["summary"] = s
    live = d["status"] in D.ACTIVE
    if live and sid not in D._RUNNING:  # Neustart der App o. Ä.: Lauf ist nicht mehr aktiv
        d["status"], d["message"] = "error", "Unterbrochen (Neustart der App) – mit „Fortsetzen“ weiterführen."
        s["resumable"] = bool(s.get("zip_tmp")) and Path(str(s["zip_tmp"])).exists()
        live = False
    d["label"] = D.STATUS_LABEL.get(d["status"], d["status"])
    d["active"] = live
    d["resumable"] = (not live and d["status"] in ("cancelled", "error") and bool(s.get("zip_tmp"))
                      and Path(str(s["zip_tmp"])).exists())
    row = ctx.db.q1("SELECT progress_json FROM job_status WHERE job=?", (JOB.format(sid=sid),))
    p: dict[str, Any] | None = None
    if row is not None and row["progress_json"]:
        with contextlib.suppress(ValueError):
            p = json.loads(row["progress_json"])
    d["progress"] = progress_view(p) if p and (live or p.get("rid") == rid) else None
    d["report"] = report(s)
    return d


def report(s: dict[str, Any]) -> dict[str, Any]:
    """Bericht aus den Zählern: Dokumentarten nach Gruppen, Unterarten der Kapitalmaßnahmen/Erträge, Listen."""
    groups: dict[str, dict[str, Any]] = {}
    for kind, n in sorted((s.get("by_kind") or {}).items(), key=lambda kv: list(KINDS).index(kv[0])
                          if kv[0] in KINDS else 99):
        label, grp = KINDS.get(kind, (kind, "info"))
        g = groups.setdefault(grp, {"label": GROUP_LABEL.get(grp, grp), "total": 0, "rows": []})
        g["total"] += n
        g["rows"].append({"kind": kind, "label": label, "count": n})
    subs = [{"label": SUB_LABEL.get(k.split(":", 1)[1], k), "kind": KINDS.get(k.split(":", 1)[0], (k,))[0],
             "count": n} for k, n in sorted((s.get("by_sub") or {}).items())]
    return {"groups": list(groups.values()), "subs": subs, "stored": s.get("stored", 0),
            "depots": [{"depot": "…" + k[-4:], "count": n} for k, n in sorted((s.get("depots") or {}).items())],
            "duplicate_known": s.get("duplicate_known") or [], "duplicate_in_zip": s.get("duplicate_in_zip") or [],
            "skipped": s.get("skipped") or [], "unrecognized": s.get("unrecognized") or [],
            "unreadable": s.get("unreadable") or [], "notes": s.get("notes") or [], "members": s.get("members", 0),
            "trades": s.get("trades") or {},
            "duration": s.get("duration_text") or ""}


def report_csv(s: dict[str, Any]) -> str:
    """Bericht als CSV (Zähler und Listen, keine Dateiinhalte)."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["Bereich", "Eintrag", "Anzahl/Grund"])
    for kind, n in sorted((s.get("by_kind") or {}).items()):
        w.writerow(["Dokumentart", KINDS.get(kind, (kind,))[0], n])
    for k, n in sorted((s.get("by_sub") or {}).items()):
        w.writerow(["Unterart", k, n])
    for key, title in (("duplicate_known", "bereits vorhanden"), ("duplicate_in_zip", "doppelt im Archiv"),
                       ("skipped", "übersprungen"), ("unrecognized", "nicht erkannt"),
                       ("unreadable", "nicht lesbar")):
        for it in s.get(key) or []:
            w.writerow([title, it.get("name", ""), it.get("reason", "")])
    return buf.getvalue()


# -- Start / Abbruch / Fortsetzen ----------------------------------------------------------------------------------

def start(ctx: Any, sid: int, zip_path: Path, filename: str, resume_of: int | None = None) -> dict[str, Any]:
    """Lauf starten. Prüft das Archiv sofort (verständliche Meldung statt späterem Fehler)."""
    from app.datasources.service import datasource_service

    ds = datasource_service(ctx).get(sid)
    if ds is None or not D.is_dkb(ds):
        zip_path.unlink(missing_ok=True)
        return {"errors": ["Für diese Datenquelle ist kein DKB-Import vorgesehen."]}
    with D._LOCK:
        if sid in D._RUNNING:
            if resume_of is None:
                zip_path.unlink(missing_ok=True)
            return {"errors": ["Für diese Datenquelle läuft bereits ein Vorgang – bitte den Fortschritt abwarten "
                               "oder abbrechen."]}
        D._RUNNING[sid] = D.Handle()
    try:
        try:
            with Z.open_zip(zip_path) as zf:
                mem, _skipped = Z.members(zf)
        except Z.ZipRejected as e:
            zip_path.unlink(missing_ok=True)
            D._RUNNING.pop(sid, None)
            return {"errors": [str(e)]}
        base = _summary_new(filename, zip_path, [])
        if resume_of is not None:
            old = _load(ctx.db, resume_of)
            for k in ("by_kind", "by_sub", "depots", "stored"):
                base[k] = old.get(k, base[k])
            base["lineage"] = [*old.get("lineage", []), resume_of]
            base["resumed_from"] = resume_of
        base["members"] = len(mem)
        rid = int(ctx.db.x("INSERT INTO bank_run(source_id, status, message, total, started_at, summary_json) VALUES "
                           "(?, 'unpacking', 'ZIP wird gelesen …', ?, ?, ?)",
                           (sid, len(mem), D._now(), json.dumps(base, ensure_ascii=False))).lastrowid)  # type: ignore[arg-type]
    except Exception:
        D._RUNNING.pop(sid, None)
        raise
    threading.Thread(target=_work, args=(ctx, sid, rid, zip_path, ds.account, ds.name, base),
                     name=f"dkb-zip-{sid}", daemon=True).start()
    return {"run_id": rid, "errors": []}


def resume(ctx: Any, sid: int, rid: int) -> dict[str, Any]:
    s = _load(ctx.db, rid)
    path = Path(str(s.get("zip_tmp") or ""))
    r = ctx.db.q1("SELECT status FROM bank_run WHERE id=? AND source_id=?", (rid, sid))
    if r is None or s.get("kind") != "zip" or not s.get("zip_tmp") or not path.is_file():
        return {"errors": ["Dieser Lauf kann nicht fortgesetzt werden (Zwischendatei nicht mehr vorhanden). Bitte das "
                           "ZIP erneut hochladen – bereits abgelegte Dokumente werden übersprungen."]}
    if r["status"] in D.ACTIVE and sid in D._RUNNING:
        return {"errors": ["Der Lauf ist noch aktiv."]}
    return start(ctx, sid, path, str(s.get("filename") or "Archiv.zip"), resume_of=rid)


def discard(ctx: Any, sid: int, rid: int) -> bool:
    """Zwischendatei eines nicht fortgesetzten Laufs löschen."""
    s = _load(ctx.db, rid)
    p = s.get("zip_tmp")
    r = ctx.db.q1("SELECT status FROM bank_run WHERE id=? AND source_id=?", (rid, sid))
    if not p or r is None or r["status"] in D.ACTIVE:
        return False
    Path(str(p)).unlink(missing_ok=True)
    s["resumable"], s["zip_tmp"] = False, ""
    D._upd(ctx.db, rid, summary_json=json.dumps(s, ensure_ascii=False, default=str))
    return True


# -- Lauf ----------------------------------------------------------------------------------------------------------

def _rates(ctx: Any) -> dict[str, float]:
    try:
        stored = ctx.settings.get(SETTING_RATES, {}) or {}
        return {**DEFAULT_RATES, **{k: float(v) for k, v in stored.items() if k in DEFAULT_RATES and float(v) > 0}}
    except (TypeError, ValueError, AttributeError):
        return dict(DEFAULT_RATES)


def _add(d: dict[str, int], key: str) -> None:
    d[key] = d.get(key, 0) + 1


def _match(ctx: Any, sid: int, rid: int, account: str, root: Path, s: dict[str, Any], prog: Progress,
           handle: D.Handle, run_ids: tuple[int, ...]) -> None:
    """Kauf-/Verkaufsabrechnungen dieses Laufs durch den Dokumentimport schicken und das Ergebnis erfassen."""
    n = ctx.db.scalar(f"SELECT COUNT(*) FROM bank_doc WHERE source_id=? AND status='classified' AND doctype IN "
                      f"({','.join('?' * len(TRADE_KINDS))}) AND run_id IN ({','.join('?' * len(run_ids))})",
                      (sid, *TRADE_KINDS, *run_ids), default=0)
    if n:
        prog.phase("zip_match", total=n, text="Abrechnungen zuordnen")
        D._upd(ctx.db, rid, status="classifying", message=f"{n} Abrechnungen werden zugeordnet …", total=n, done=0)
        ev = D.evaluate_pending(ctx, sid, account, root, handle.cancel, status="classified", doctypes=TRADE_KINDS,
                                run_ids=run_ids, busy_wait_s=180,
                                on_step=lambda d, t: (prog.update(d, t, text=f"Abrechnung {d} von {t}", work=d),
                                                      D._upd(ctx.db, rid, done=d)))
        if ev.get("error"):
            s["notes"].append(str(ev["error"]))
    s["trades"] = trades(ctx, sid, run_ids)


def trades(ctx: Any, sid: int, run_ids: tuple[int, ...]) -> dict[str, list[dict[str, Any]]]:
    """Ergebnis der Auswertung je Abrechnung: ``matched`` (vorhandener Buchung zugeordnet, Beleg verknüpft), ``new``
    (Vorschlag im Prüf-Stapel) und ``unclear`` (Rechenprobe/Pflichtangaben – nicht buchbar)."""
    from app.documentimport import txdocs

    out: dict[str, list[dict[str, Any]]] = {"matched": [], "new": [], "unclear": [], "failed": []}
    marks = ",".join("?" * len(run_ids))
    rows = ctx.db.q(
        f"SELECT b.subject, b.doc_date, b.status, b.note, d.id AS doc, d.batch_id, d.result_json FROM bank_doc b "
        f"LEFT JOIN document d ON d.sha256=b.sha256 WHERE b.source_id=? "
        f"AND b.doctype IN ({','.join('?' * len(TRADE_KINDS))}) AND b.run_id IN ({marks}) "
        "AND b.status IN ('evaluated', 'error') ORDER BY b.doc_date, b.id",
        (sid, *TRADE_KINDS, *run_ids))
    for r in rows:
        item = {"doc": r["doc"], "name": r["subject"], "date": r["doc_date"]}
        if r["status"] == "error" or not r["result_json"]:
            out["failed"].append({**item, "reason": r["note"] or "nicht auswertbar"})
            continue
        try:
            txs = json.loads(r["result_json"]).get("txs") or []
        except ValueError:
            txs = []
        for t in txs:
            if t.get("linked") and t.get("existing"):
                txdocs.link_document(ctx.db, t["existing"], int(r["doc"]), "abrechnung", "auto")
                out["matched"].append({**item, "n": t.get("n", 1), "tx": t["existing"]})
            elif t.get("kind") in ("buy", "sell"):
                out["new"].append({**item, "n": t.get("n", 1), "batch": r["batch_id"]})
            else:
                why = "; ".join(w for w in (t.get("warnings") or []) if str(w).startswith("Rechenprobe")) or \
                    "; ".join(str(w) for w in (t.get("warnings") or [])[:2]) or "Pflichtangaben fehlen"
                out["unclear"].append({**item, "n": t.get("n", 1), "reason": why[:400], "batch": r["batch_id"]})
    for k in out:
        out[k] = out[k][:500]
    return out


def _work(ctx: Any, sid: int, rid: int, path: Path, account: str, name: str, s: dict[str, Any]) -> None:
    db = ctx.db
    handle = D._RUNNING[sid]
    root = D.root_dir(ctx, sid, name)
    prog: Progress | None = None
    stop = threading.Event()
    status, message = "error", "Der ZIP-Import ist fehlgeschlagen."
    last_flush = [0.0]

    def flush(force: bool = False, **fields: Any) -> None:
        now = time.monotonic()
        if not force and now - last_flush[0] < 1.5:
            return
        last_flush[0] = now
        D._upd(db, rid, summary_json=json.dumps(s, ensure_ascii=False, default=str), **fields)

    try:
        rates = _rates(ctx)
        prog = Progress(job_sink(ctx, JOB.format(sid=sid)), "DKB-Wertpapierdokumente (ZIP)", source=name,
                        unit="Dokumente", phases=["zip_unpack", "zip_eval", "zip_match", "zip_report"], eta=True)
        prog.extra.update(href=f"/settings/datasources/{sid}/dkb/zip/{rid}", rid=rid)
        prog.touch()

        def beat() -> None:
            while not stop.wait(5):
                prog.touch()

        threading.Thread(target=beat, name=f"dkb-zip-beat-{sid}", daemon=True).start()
        root.mkdir(parents=True, exist_ok=True)
        lineage = set(s.get("lineage") or [])
        with Z.open_zip(path) as zf:
            mem, skipped = Z.members(zf)
            s["skipped"] = [{"name": k.name, "reason": k.reason} for k in skipped]
            total_bytes = sum(m.size for m in mem)
            prog.expect("zip_unpack", total_bytes, rates["zip_unpack"]).expect("zip_eval", total_bytes,
                                                                                 rates["zip_eval"])
            prog.expect("zip_match", max(1, round(len(mem) * 0.2)), rates["zip_match"])
            # -- Phase 1: lesen, Prüfsumme, Dubletten -------------------------------------------------------
            prog.phase("zip_unpack", total=len(mem), text="Archiv wird gelesen", work_total=total_bytes)
            D._upd(db, rid, status="unpacking", message=f"{len(mem)} PDF-Dateien im Archiv.", total=len(mem), done=0)
            cand: list[tuple[Z.Member, str]] = []
            seen: set[str] = set()
            s["duplicate_known"], s["duplicate_in_zip"], s["unreadable"], s["unrecognized"] = [], [], [], []
            done_bytes = 0
            for i, m in enumerate(mem):
                if handle.cancel.is_set():
                    break
                try:
                    data = Z.read_member(zf, m)
                except Z.ZipRejected as e:
                    s["unreadable"].append({"name": m.name, "reason": str(e)})
                else:
                    sha = Z.sha256(data)
                    row = db.q1("SELECT run_id FROM bank_doc WHERE source_id=? AND sha256=? LIMIT 1", (sid, sha))
                    if sha in seen:
                        s["duplicate_in_zip"].append({"name": m.name, "reason": "gleicher Inhalt wie eine andere Datei "
                                                                                  "im Archiv"})
                    elif row is not None and row["run_id"] in lineage:
                        seen.add(sha)  # in einem früheren Teil dieses Laufs bereits abgelegt
                    elif row is not None:
                        seen.add(sha)
                        s["duplicate_known"].append({"name": m.name, "reason": "bereits im Ordner/Index vorhanden"})
                    else:
                        seen.add(sha)
                        cand.append((m, sha))
                done_bytes += m.size
                prog.update(i + 1, text=f"Archiv lesen: {i + 1} von {len(mem)}", work=done_bytes)
                flush(done=i + 1, message=f"Archiv lesen: {i + 1} von {len(mem)}")
            # -- Phase 2: erkennen und ablegen -------------------------------------------------------------
            new_bytes = sum(m.size for m, _ in cand)
            prog.phase("zip_eval", total=len(cand), text="Dokumente erkennen", work_total=new_bytes)
            D._upd(db, rid, status="classifying", message=f"{len(cand)} neue Dokumente werden erkannt …",
                   total=len(cand), done=0)
            done_bytes = 0
            for j, (m, sha) in enumerate(cand):
                if handle.cancel.is_set():
                    break
                try:
                    data = Z.read_member(zf, m)
                    res = extract_isolated(data, ocr=False, cancel=handle.cancel)
                    c = classify([ln.text for ln in res.lines()], m.name)
                    if c is None:
                        s["unrecognized"].append({"name": m.name, "reason": "Dokumentart nicht erkannt"})
                    else:
                        placed = Z.place(root, c, data)
                        cur = db.x(
                            "INSERT OR IGNORE INTO bank_doc(source_id, doc_id, category, doctype, account, doc_date, "
                            "subject, rel_path, sha256, size, checksum_ok, status, note, run_id, fetched_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,1,'classified',?,?,?)",
                            (sid, f"zip:{sha}", Z.CATEGORY, c.kind, c.depot, c.doc_date, c.subject(), placed.rel, sha,
                             len(data), "; ".join(c.notes)[:300] or None, rid, D._now()))
                        if cur.rowcount:
                            s["stored"] += 1
                            _add(s["by_kind"], c.kind)
                            if c.kind in ("kapitalmassnahme", "ertrag", "depotuebertrag"):
                                _add(s["by_sub"], f"{c.kind}:{c.sub}")
                            _add(s["depots"], c.depot or "unbekannt")
                        prog.text = f"{c.label} {c.doc_date[8:10]}.{c.doc_date[5:7]}.{c.doc_date[:4]}"[:200]
                except Cancelled:
                    break
                except (DocumentError, Z.ZipRejected) as e:
                    s["unreadable"].append({"name": m.name, "reason": str(e)[:200]})
                except OSError as e:
                    s["unreadable"].append({"name": m.name, "reason": f"Ablage fehlgeschlagen ({type(e).__name__})"})
                done_bytes += m.size
                prog.update(j + 1, text=prog.text, work=done_bytes)
                flush(done=j + 1, message=f"Dokument {j + 1} von {len(cand)}: {prog.text}")
        # -- Abrechnungen auswerten und vorhandenen Buchungen zuordnen -------------------------------------
        if not handle.cancel.is_set():
            _match(ctx, sid, rid, account, root, s, prog, handle, (*lineage, rid))
        # -- Bericht ----------------------------------------------------------------------------------------
        cancelled = handle.cancel.is_set()
        prog.phase("zip_report", total=1, text="Bericht wird erstellt")
        if len(s["depots"]) > 1:
            s["notes"].append("Das Archiv enthält Dokumente mehrerer Depots – vorgesehen ist ein Depot je Datenquelle.")
        if "unbekannt" in s["depots"]:
            s["notes"].append("Bei einigen Dokumenten war die Depotnummer nicht lesbar.")
        s["duration_text"] = ""
        if cancelled:
            status = "cancelled"
            message = (f"Abgebrochen – {s['stored']} Dokumente sind abgelegt. „Fortsetzen“ liest den Rest des "
                       "Archivs.")
            s["resumable"] = True
        else:
            status = "partial" if s["unreadable"] else "ok"
            tr = s.get("trades") or {}
            message = (f"{s['stored']} Dokumente abgelegt, {len(s['unrecognized'])} nicht erkannt, "
                       f"{len(s['duplicate_known']) + len(s['duplicate_in_zip'])} Dubletten, "
                       f"{len(s['skipped'])} übersprungen" + (f", {len(s['unreadable'])} nicht lesbar" if
                                                             s["unreadable"] else "") + "."
                       + (f" Abrechnungen: {len(tr.get('matched', []))} vorhandenen Buchungen zugeordnet, "
                          f"{len(tr.get('new', []))} neu im Prüf-Stapel, {len(tr.get('unclear', []))} ungeklärt."
                          if tr else ""))
            s["resumable"] = False
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            s["zip_tmp"] = ""
            with contextlib.suppress(Exception):
                ctx.settings.set(SETTING_RATES, {**_rates(ctx), **prog.measured_rates()})
        prog.update(1, 1)
        prog.finish(not cancelled, message[:200])
        s["duration_text"] = prog.extra.get("duration_text", "")
    except Z.ZipRejected as e:
        status, message = "error", str(e)
        s["resumable"] = path.is_file()
    except Exception as e:  # pragma: no cover - Absicherung des Hintergrundlaufs
        log.exception("DKB-ZIP-Import %s fehlgeschlagen", sid)
        status, message = "error", f"Der ZIP-Import ist fehlgeschlagen ({type(e).__name__}). Mit „Fortsetzen“ erneut " \
                                    "versuchen."
        s["resumable"] = path.is_file()
    finally:
        stop.set()
        if status == "error" and prog is not None and not prog.finished:
            prog.finish(False, message[:200])
        D._upd(db, rid, status=status, message=message[:500], finished_at=D._now(),
               summary_json=json.dumps(s, ensure_ascii=False, default=str))
        D._RUNNING.pop(sid, None)
        db.close_thread_conn()
