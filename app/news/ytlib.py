"""YouTube-Transkripte über die Bibliothek ``youtube-transcript-api`` – gekapselt und in der App aktualisierbar.

Die Bibliothek liest Untertitel so, wie der YouTube-Player sie lädt (keine offizielle Schnittstelle). Ändert YouTube
etwas, hilft nur eine neue Version. Deshalb:

* **Kapselung:** Portfolia nutzt nur :func:`fetch` (öffentliche API ``YouTubeTranscriptApi().list()`` →
  ``Transcript.fetch()``); Fehler werden über ihre Klassennamen eingeordnet (stabil über Versionen hinweg):
  ``none`` (keine Untertitel), ``unavailable`` (Video privat/gelöscht/altersbeschränkt), ``blocked`` (YouTube hält
  den Abruf für einen Bot – Pause), ``broken`` (Seitenaufbau geändert – Bibliothek aktualisieren), ``network``.
* **Update per Knopf** (:func:`update`): die neueste Version derselben Hauptversion von PyPI, ausschließlich als
  fertiges Wheel (``--only-binary=:all:`` – dabei läuft kein Installationscode), ohne Abhängigkeiten (``--no-deps``;
  ``requests`` und ``defusedxml`` bringt das Image mit), in einen eigenen Ordner unter ``/data/pylib``. Erst nach einem
  Selbsttest in einem getrennten Prozess wird umgeschaltet; die mitgelieferte Version bleibt als Rückfall
  (:func:`reset`). Eine neue Hauptversion (z. B. 2.x) wird nicht automatisch übernommen – deren API kann sich ändern.
* **Zeitlimit:** Die Bibliothek ruft ohne Zeitlimit ab; sie bekommt eine Verbindung mit festem Zeitlimit.

Übertragen wird nur die Video-Kennung an youtube.com (wie beim Abruf der Kanal-Feeds).
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PKG = "youtube-transcript-api"
MOD = "youtube_transcript_api"
MAJOR = 1  # Hauptversion, deren API Portfolia kennt
PYPI_URL = "https://pypi.org/pypi/youtube-transcript-api/json"
TIMEOUT_S = 20.0
PIP_TIMEOUT_S = 180
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {}

KIND_NONE, KIND_UNAVAILABLE, KIND_BLOCKED, KIND_BROKEN, KIND_NETWORK = "none", "unavailable", "blocked", "broken", \
    "network"
_ERRORS = {
    "TranscriptsDisabled": KIND_NONE, "NoTranscriptFound": KIND_NONE,
    "VideoUnavailable": KIND_UNAVAILABLE, "VideoUnplayable": KIND_UNAVAILABLE, "AgeRestricted": KIND_UNAVAILABLE,
    "InvalidVideoId": KIND_UNAVAILABLE,
    "RequestBlocked": KIND_BLOCKED, "IpBlocked": KIND_BLOCKED,
}
# Selbsttest einer neuen Version (getrennter Prozess): importieren, öffentliche API und Fehlerklassen vorhanden
_SELFTEST = (
    "import sys; sys.path.insert(0, sys.argv[1]); import youtube_transcript_api as y; "
    "assert y.__file__.startswith(sys.argv[1]), y.__file__; "
    "assert callable(getattr(y.YouTubeTranscriptApi(), 'list', None)); "
    "[getattr(y, n) for n in ('TranscriptsDisabled', 'NoTranscriptFound', 'RequestBlocked', 'VideoUnavailable')]; "
    "print('ok')"
)


class TranscriptError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


@dataclass
class Fetched:
    segments: list[tuple[float, str]]  # (Startsekunde, Text)
    language: str
    generated: bool  # automatisch erzeugte Untertitel
    version: str  # Version der Bibliothek


# ----------------------------------------------------------------------------------------------------
# Laden (mitgeliefert oder aktualisiert)
# ----------------------------------------------------------------------------------------------------

def lib_dir(config: Any) -> Path:
    return Path(config.data_dir) / "pylib" / MOD


def _state_file(config: Any) -> Path:
    return lib_dir(config) / "active.json"


def active(config: Any) -> dict[str, Any] | None:
    """Aktualisierte Version (``{"version", "dir", "at"}``) oder ``None`` (mitgelieferte Version)."""
    try:
        raw = json.loads(_state_file(config).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("dir") or not Path(raw["dir"], MOD).is_dir():
        return None
    return raw


def bundled_version() -> str | None:
    try:
        return importlib.metadata.version(PKG)
    except importlib.metadata.PackageNotFoundError:
        return None


def _purge() -> None:
    for name in [m for m in sys.modules if m == MOD or m.startswith(MOD + ".")]:
        del sys.modules[name]


def module(config: Any) -> Any:
    """Die Bibliothek – aus ``/data/pylib`` (aktualisiert) bzw. die mitgelieferte. ``ImportError``: nicht verfügbar."""
    with _LOCK:
        act = active(config)
        key = act["dir"] if act else "bundled"
        if _CACHE.get("key") == key:
            return _CACHE["mod"]
        _purge()
        if act:
            sys.path.insert(0, act["dir"])
            try:
                mod = importlib.import_module(MOD)
            finally:
                with contextlib.suppress(ValueError):
                    sys.path.remove(act["dir"])
            if not str(Path(mod.__file__ or "")).startswith(act["dir"]):  # pragma: no cover - Schutz
                raise ImportError("aktualisierte Version nicht geladen")
        else:
            mod = importlib.import_module(MOD)
        _CACHE.update(key=key, mod=mod, version=act["version"] if act else bundled_version())
        return mod


def info(config: Any) -> dict[str, Any]:
    """Anzeige: laufende Version, Herkunft, mitgelieferte Version."""
    act = active(config)
    return {"version": act["version"] if act else bundled_version(), "updated": bool(act),
            "updated_at": (act or {}).get("at"), "bundled": bundled_version(),
            "available": bool(act) or importlib.util.find_spec(MOD) is not None}


# ----------------------------------------------------------------------------------------------------
# Abruf
# ----------------------------------------------------------------------------------------------------

def _session() -> Any:
    import requests

    class _TimeoutSession(requests.Session):
        def request(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            kwargs.setdefault("timeout", TIMEOUT_S)
            return super().request(*args, **kwargs)

    return _TimeoutSession()


def _classify(e: Exception) -> TranscriptError:
    name = type(e).__name__
    if name in _ERRORS:
        return TranscriptError(_ERRORS[name], name)
    if type(e).__module__.startswith(("requests", "urllib3")) or isinstance(e, (ConnectionError, TimeoutError)):
        return TranscriptError(KIND_NETWORK, name)
    return TranscriptError(KIND_BROKEN, f"{name}: {e}"[:200])


def fetch(config: Any, video_id: str, languages: list[str]) -> Fetched:
    """Untertitel eines Videos: je bevorzugter Sprache zuerst manuell erstellte, dann automatisch erzeugte; sonst die
    erste vorhandene Spur. ``TranscriptError`` mit eingeordneter Art bei jedem Fehler."""
    try:
        mod = module(config)
    except ImportError as e:
        raise TranscriptError(KIND_BROKEN, f"Bibliothek nicht verfügbar: {e}") from e
    try:
        tracks = list(mod.YouTubeTranscriptApi(http_client=_session()).list(video_id))
        if not tracks:
            raise TranscriptError(KIND_NONE, "keine Spur")
        chosen = next((t for lang in languages for gen in (False, True) for t in tracks
                       if t.language_code.split("-")[0] == lang and bool(t.is_generated) == gen), None)
        chosen = chosen or next((t for t in tracks if not t.is_generated), tracks[0])
        snippets = chosen.fetch()
        segments = [(float(s.start), str(s.text)) for s in snippets if str(s.text or "").strip()]
    except TranscriptError:
        raise
    except Exception as e:  # Fehlerklassen der Bibliothek bzw. von requests
        raise _classify(e) from e
    if not segments:
        raise TranscriptError(KIND_NONE, "leere Spur")
    return Fetched(segments, chosen.language_code, bool(chosen.is_generated), str(_CACHE.get("version") or "?"))


# ----------------------------------------------------------------------------------------------------
# Aktualisieren (Knopf in den Einstellungen)
# ----------------------------------------------------------------------------------------------------

def latest(http: Any) -> str:
    """Neueste veröffentlichte Version laut PyPI."""
    r = http.get(PYPI_URL, timeout=15.0)
    r.raise_for_status()
    v = str((r.json().get("info") or {}).get("version") or "")
    if not _VERSION.match(v):
        raise ValueError(f"unerwartete Versionsangabe: {v[:40]}")
    return v


def _key(v: str | None) -> tuple[int, ...]:
    m = _VERSION.match(v or "")
    return tuple(int(x) for x in m.groups()) if m else (0,)


def update(config: Any, http: Any, version: str | None = None) -> dict[str, Any]:
    """Neueste Version derselben Hauptversion laden, prüfen und aktivieren. Rückgabe: ``{"ok", "version", "error"}``."""
    try:
        target = version or latest(http)
    except Exception as e:
        log.warning("Transkript-Bibliothek: PyPI nicht erreichbar (%s: %s)", type(e).__name__, e)
        return {"ok": False, "error": f"PyPI nicht erreichbar: {type(e).__name__}"}
    if not _VERSION.match(target):
        return {"ok": False, "error": "Ungültige Versionsangabe."}
    if _key(target)[0] != MAJOR:
        return {"ok": False, "version": target,
                "error": f"Neue Hauptversion {target} – die Schnittstelle kann sich geändert haben; sie kommt erst mit "
                         "einem geprüften Portfolia-Release."}
    current = (active(config) or {}).get("version") or bundled_version()
    if current and _key(target) <= _key(current):
        return {"ok": True, "version": current, "unchanged": True}
    base = lib_dir(config)
    base.mkdir(parents=True, exist_ok=True)
    staging = base / f".staging-{target}"
    shutil.rmtree(staging, ignore_errors=True)
    cmd = [sys.executable, "-m", "pip", "install", "--no-deps", "--only-binary=:all:", "--no-compile",
           "--disable-pip-version-check", "--no-input", "--target", str(staging), f"{PKG}=={target}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_S,  # noqa: S603
                           env={**os.environ, "PIP_NO_CACHE_DIR": "1"}, check=False)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[-1:] or ["pip fehlgeschlagen"])
        t = subprocess.run([sys.executable, "-c", _SELFTEST, str(staging)], capture_output=True, text=True,  # noqa: S603
                           timeout=60, check=False)
        if t.returncode != 0 or "ok" not in t.stdout:
            raise RuntimeError(["Selbsttest fehlgeschlagen: " + (t.stderr.strip().splitlines() or ["?"])[-1]])
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        shutil.rmtree(staging, ignore_errors=True)
        detail = e.args[0][0] if isinstance(e, RuntimeError) and e.args and isinstance(e.args[0], list) else str(e)
        log.warning("Transkript-Bibliothek %s nicht übernommen: %s", target, detail)
        return {"ok": False, "version": target, "error": str(detail)[:300]}
    final = base / target
    shutil.rmtree(final, ignore_errors=True)
    staging.rename(final)
    state = {"version": target, "dir": str(final), "at": datetime.now(UTC).isoformat(timespec="seconds")}
    tmp = _state_file(config).with_suffix(".tmp")
    tmp.write_text(json.dumps(state), "utf-8")
    os.replace(tmp, _state_file(config))
    with _LOCK:
        _CACHE.clear()
    for old in base.iterdir():  # ältere Versionen aufräumen (die aktive bleibt)
        if old.is_dir() and old.name != target:
            shutil.rmtree(old, ignore_errors=True)
    log.info("Transkript-Bibliothek aktualisiert auf %s", target)
    return {"ok": True, "version": target}


def reset(config: Any) -> bool:
    """Zur mitgelieferten Version zurückkehren (der geladene Ordner wird entfernt)."""
    act = active(config)
    with contextlib.suppress(FileNotFoundError):
        _state_file(config).unlink()
    if act:
        shutil.rmtree(act["dir"], ignore_errors=True)
    with _LOCK:
        _CACHE.clear()
    return act is not None
