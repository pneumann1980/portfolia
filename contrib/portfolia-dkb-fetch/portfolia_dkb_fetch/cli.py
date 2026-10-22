"""Kommandozeile und Ablauf des Abrufs. Schnittstelle: siehe README (JSON-Zeilen auf stdout)."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from portfolia_dkb_fetch import __version__

PAUSE = 0.5  # Sekunden zwischen Downloads (schonend für die Bank)
MFA_TIMEOUT = 50  # die Bibliothek wartet höchstens so lange auf die Freigabe in der App
_SAFE = re.compile(r"[^\w.\- ]+", re.UNICODE)


class Emitter:
    """Schreibt Ereignisse als JSON-Zeilen; Ausgaben der Bibliothek (print) gehen auf stderr."""

    def __init__(self, out: TextIO) -> None:
        self.out = out

    def __call__(self, event: str, **fields: Any) -> None:
        self.out.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")
        self.out.flush()


def safe_part(text: Any, fallback: str) -> str:
    """Verzeichnisname aus Angaben der Bank: kein Pfadtrenner, keine Punkt-Segmente, begrenzte Länge."""
    s = _SAFE.sub("_", str(text or "")).strip(" ._")
    return (s or fallback)[:80]


def classify(message: str) -> str:
    m = message.lower()
    if "2fa" in m and ("cancel" in m or "chancel" in m):
        return "2fa_canceled"
    if "2fa" in m or "mfa" in m or "confirm" in m:
        return "2fa_timeout" if "time" in m or "fail" in m else "login"
    if "login" in m or "password" in m or "credential" in m:
        return "login"
    if any(x in m for x in ("connection", "timed out", "timeout", "resolve", "ssl", "max retries")):
        return "network"
    if any(x in m for x in ("unexpected", "format", "keyerror", "could not fetch", "attribute", "not supported")):
        return "changed"
    return "other"


def _sha256(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def versions() -> dict[str, Any]:
    libs: dict[str, str] = {}
    try:
        import dkb_robo

        libs["dkb_robo"] = str(getattr(dkb_robo, "__version__", "?"))
    except Exception:
        libs["dkb_robo"] = "fehlt"
    return {"version": __version__, "libs": libs}


def run(out_dir: Path, creds: dict[str, Any], known: set[str], emit: Emitter, stop: dict[str, bool]) -> int:
    try:
        import dkb_robo
        from dkb_robo import DKBRobo
        from dkb_robo.postbox import PostBox
    except Exception as e:
        emit("error", kind="dependency", message=f"dkb-robo ist nicht installiert: {type(e).__name__}")
        return 2
    emit("hello", fetcher=__version__, dkb_robo=str(getattr(dkb_robo, "__version__", "?")))

    try:  # die Bibliothek druckt die Aufforderung nur; so erfährt Portfolia, dass jetzt die App freigeben muss
        from dkb_robo.authentication import APPAuthentication

        orig = APPAuthentication._print

        def announce(self: Any, devicename: Any) -> Any:
            emit("awaiting_2fa", timeout_s=MFA_TIMEOUT)
            return orig(self, devicename)

        APPAuthentication._print = announce  # type: ignore[method-assign]
    except Exception as e:
        sys.stderr.write(f"Hinweis zur Freigabe nicht verfügbar: {type(e).__name__}\n")

    user, password = str(creds.get("user") or ""), str(creds.get("password") or "")
    if not user or not password:
        emit("error", kind="login", message="Benutzername und Passwort fehlen.")
        return 2
    mfa = creds.get("mfa_device") or None
    stats = {"downloaded": 0, "existing": 0, "failed": 0}
    emit("login")
    try:
        with contextlib.redirect_stdout(sys.stderr), DKBRobo(dkb_user=user, dkb_password=password, chip_tan=False,
                                                              mfa_device=mfa, debug=False) as dkb:
            emit("logged_in", accounts=len(getattr(dkb, "account_dic", {}) or {}))
            items = PostBox(client=dkb.wrapper.client).fetch_items()
            todo = [(i, it) for i, it in items.items() if i not in known]
            emit("listed", total=len(items), new=len(todo))
            try:
                lookup = dkb._accounts_by_id()
            except Exception:
                lookup = {}
            for doc_id, item in todo:
                if stop.get("stop"):
                    break
                try:
                    rel = _fetch_one(dkb.wrapper.client, item, out_dir, lookup)
                except Exception as e:
                    stats["failed"] += 1
                    emit("document_error", id=str(doc_id), message=_clean(str(e), password, user))
                    continue
                path = out_dir / rel["rel"]
                sha, size = _sha256(path)
                existed = rel.pop("existed")
                stats["existing" if existed else "downloaded"] += 1
                emit("document", id=str(doc_id), size=size, sha256=sha,
                     status="exists" if existed else "downloaded", **rel)
                if not existed:
                    time.sleep(PAUSE)
        emit("done", cancelled=bool(stop.get("stop")), **stats)
        return 0
    except Exception as e:
        msg = _clean(str(e) or type(e).__name__, password, user)
        emit("error", kind=classify(msg), message=f"{type(e).__name__}: {msg}"[:300], fetcher=__version__)
        return 2


def _clean(text: str, *secrets: str) -> str:
    for s in secrets:
        if s and len(s) >= 3:
            text = text.replace(s, "***")
    return text


def _fetch_one(client: Any, item: Any, out_dir: Path, lookup: dict[str, str]) -> dict[str, Any]:
    """Ein Dokument laden. Pfad: <Kategorie>/<Konto>/<Datum>_<Name>; nie außerhalb von ``out_dir``. Es wird nichts als
    gelesen markiert."""
    category = safe_part(item.category(), "Sonstiges")
    account = safe_part(item.account(card_lookup=lookup), "ohne-Konto")
    date = item.date()
    name = safe_part(f"{date}_{item.filename()}", f"{date}_dokument.pdf")
    target = out_dir / category / account / name
    if not str(target.resolve()).startswith(str(out_dir.resolve()) + "/"):
        raise ValueError("Ungültiger Pfad")
    existed = target.exists()
    checksum_ok = True
    if not existed:
        item.download(client, target)
        if not target.exists():  # die Bibliothek benennt Dateien mit abweichender Prüfsumme um
            alt = target.with_name(target.name + ".checksum_mismatch")
            if alt.exists():
                target, checksum_ok = alt, False
            else:
                raise OSError("Datei wurde nicht angelegt")
    msg = getattr(item, "message", None)
    return {"rel": target.relative_to(out_dir).as_posix(), "category": category, "account": account, "date": date,
            "doctype": getattr(msg, "documentType", None), "subject": str(item.subject() or "")[:200],
            "checksum_ok": checksum_ok, "existed": existed}


def main(argv: list[str] | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    ap = argparse.ArgumentParser(prog="portfolia_dkb_fetch")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--known", type=Path)
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)
    out = stdout or sys.stdout
    if args.version:
        out.write(json.dumps(versions()) + "\n")
        return 0
    emit = Emitter(out)
    if args.out is None:
        emit("error", kind="other", message="--out fehlt")
        return 2
    try:
        creds = json.loads((stdin or sys.stdin).readline() or "{}")
    except ValueError:
        emit("error", kind="other", message="Zugangsdaten nicht lesbar")
        return 2
    known: set[str] = set()
    if args.known is not None:
        try:
            known = {str(x) for x in json.loads(args.known.read_text())}
        except (OSError, ValueError):
            known = set()
    args.out.mkdir(parents=True, exist_ok=True)
    stop = {"stop": False}
    with contextlib.suppress(ValueError, OSError):  # Abbruch durch Portfolia (SIGTERM): sauber abmelden
        signal.signal(signal.SIGTERM, lambda *_: stop.update(stop=True))
    return run(args.out.resolve(), creds, known, emit, stop)
