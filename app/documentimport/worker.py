"""Isolierte Extraktion: eigener Prozess mit Speicher-, CPU- und Zeitlimit für nicht vertrauenswürdige Dateien.

Ein präpariertes PDF oder Bild kann einen Parser blockieren oder viel Speicher belegen. Deshalb läuft die Extraktion
in einem Kindprozess (``python -I -m app.documentimport.worker``) mit ``RLIMIT_AS``/``RLIMIT_CPU`` und einer
Gesamtzeitgrenze; der Webprozess bleibt davon unberührt. Die Datei wird über eine temporäre Datei mit Rechten 0600
übergeben (kein Inhalt in Kommandozeile oder Logs), das Ergebnis als JSON auf stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.documentimport.extract import DocumentError, DocumentResult, extract_document

MEM_LIMIT = int(os.environ.get("PORTFOLIA_DOC_MEM_MB", "1024")) * 1024 * 1024
CPU_LIMIT = int(os.environ.get("PORTFOLIA_DOC_CPU_S", "300"))
WALL_LIMIT = int(os.environ.get("PORTFOLIA_DOC_TIMEOUT_S", "240"))
ROOT = Path(__file__).resolve().parents[2]
# isolierter Interpreter (-I: keine Umgebungsvariablen, kein Benutzer-site) – Paketpfad ausdrücklich setzen
_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); from app.documentimport.worker import main; "
         "raise SystemExit(main(sys.argv[2:]))")


def _limit() -> None:  # pragma: no cover - läuft im Kindprozess
    import resource

    for res, val in ((resource.RLIMIT_AS, MEM_LIMIT), (resource.RLIMIT_CPU, CPU_LIMIT)):
        try:
            _soft, hard = resource.getrlimit(res)
            new = val if hard == resource.RLIM_INFINITY else min(val, hard)
            resource.setrlimit(res, (new, hard))
        except (ValueError, OSError):
            pass


class Cancelled(DocumentError):
    """Vom Nutzer abgebrochen."""


def extract_isolated(data: bytes, *, language: str = "deu+eng", ocr: bool = True,
                     timeout: int | None = None, cancel: Any = None) -> DocumentResult:
    """Extraktion im Kindprozess. Fehler (Limit, Zeitüberschreitung, Absturz) → :class:`DocumentError`;
    ``cancel`` (``threading.Event``) beendet den Kindprozess sofort."""
    import time
    with tempfile.TemporaryDirectory(prefix="portfolia-doc-") as d:
        path = Path(d) / "in.bin"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "TESSDATA_PREFIX", "TZ", "LANG", "HOME",
                                                             "PORTFOLIA_DOC_MEM_MB", "PORTFOLIA_DOC_CPU_S")}
        env["OMP_THREAD_LIMIT"] = "1"
        proc = subprocess.Popen(  # noqa: S603 - fester Aufruf ohne Shell
            [sys.executable, "-I", "-c", _BOOT, str(ROOT), str(path), language, "1" if ocr else "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(ROOT),
            preexec_fn=_limit if os.name == "posix" else None)
        end = time.monotonic() + (timeout or WALL_LIMIT)
        stdout = b""
        while True:
            try:
                stdout, _err = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.communicate()
                    raise Cancelled("Abgebrochen.") from None
                if time.monotonic() > end:
                    proc.kill()
                    proc.communicate()
                    raise DocumentError("Zeitlimit der Dokumentanalyse überschritten.") from None
    try:
        out = json.loads(stdout.decode("utf-8") or "{}")
    except ValueError:
        out = {}
    if proc.returncode != 0 or not out:
        if out.get("error"):
            raise DocumentError(out["error"])
        raise DocumentError("Dokumentanalyse abgebrochen (Speicher- oder Zeitlimit bzw. beschädigte Datei).")
    if out.get("error"):
        raise DocumentError(out["error"])
    return DocumentResult.from_json(out["result"])


def main(argv: list[str]) -> int:  # pragma: no cover - Kindprozess
    path, language, ocr = argv[0], argv[1], argv[2] == "1"
    try:
        data = Path(path).read_bytes()
        res = extract_document(data, language=language, ocr=ocr)
        payload: dict[str, Any] = {"result": res.to_json()}
    except DocumentError as e:
        payload = {"error": str(e)}
    except MemoryError:
        payload = {"error": "Speicherlimit der Dokumentanalyse überschritten."}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
