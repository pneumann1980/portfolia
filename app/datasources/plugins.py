"""Zusatzprogramme (Plugins), die Portfolia bei Bedarf in eine **eigene Python-Umgebung** nachinstalliert.

Grund: Der DKB-Abruf nutzt die Bibliothek ``dkb-robo`` (GPL-3.0). Portfolia steht unter MIT;
GPL-Code gehört deshalb weder
in den Quelltext noch in das Image. Der „Fetcher“ (``portfolia-dkb-fetch``, GPL-3.0, Verzeichnis ``contrib/``) ist ein
eigenes Programm, das nur über eine Zeilenschnittstelle (JSON auf stdout) und Dateien mit Portfolia
spricht – installiert
wird es erst auf Knopfdruck beim Nutzer, in ``<DATA_DIR>/plugins/<name>/venv`` (bleibt über Image-Updates erhalten,
Entfernen löscht den Ordner rückstandsfrei).

Ablauf der Installation: neue Umgebung (``venv``) → ``pip install`` mit festen Versionen der Abhängigkeiten
(``constraints``) → Prüfung (``--version`` liefert JSON) → erst dann ersetzt die neue die alte Umgebung. Fehlschläge
lassen die bisherige Installation unberührt. Der Unterprozess bekommt eine bereinigte Umgebung
(keine Schlüssel der App).

Quelle: ``PORTFOLIA_DKB_FETCH_SPEC`` (pip-Anforderung, z. B. eine Wheel-Datei, ein eigenes Repository oder PyPI);
Standard ist das Verzeichnis ``contrib/portfolia-dkb-fetch`` dieses Repositorys in der Version der laufenden App.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import REVISION, __version__

log = logging.getLogger(__name__)

REPO = "pneumann1980/portfolia"
INSTALL_TIMEOUT = 900  # Sekunden für venv + pip (langsame Leitung, Wheels bauen)
VERIFY_TIMEOUT = 60
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
              "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
              "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_CERT", "PIP_TRUSTED_HOST", "PIP_PROXY")
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Plugin:
    id: str
    label: str
    module: str  # ``python -m <module>``
    pins: tuple[str, ...]  # feste Versionen der Abhängigkeiten (constraints)
    spec_env: str

    def default_spec(self) -> str:
        """Standardquelle: exakt der Stand dieses Images (Commit), sonst das Versions-Tag der laufenden App."""
        ref = REVISION if len(REVISION) >= 7 else f"refs/tags/v{__version__}"
        return (f"portfolia-dkb-fetch @ https://github.com/{REPO}/archive/{ref}.tar.gz"
                "#subdirectory=contrib/portfolia-dkb-fetch")

    def spec(self) -> str:
        return (os.environ.get(self.spec_env) or "").strip() or self.default_spec()


PLUGINS: dict[str, Plugin] = {
    "dkb": Plugin("dkb", "DKB-Abruf (dkb-robo)", "portfolia_dkb_fetch", ("dkb-robo==0.31.2",),
                  "PORTFOLIA_DKB_FETCH_SPEC"),
}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def plugin_dir(config: Any, pid: str) -> Path:
    return Path(config.plugins_dir) / pid


def venv_python(config: Any, pid: str) -> Path:
    return plugin_dir(config, pid) / "venv" / "bin" / "python"


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Umgebung für Unterprozesse: nur, was Python, pip und Netzwerk brauchen – nie Schlüssel oder
    Master-Key der App."""
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env.update(extra or {})
    return env


def manifest(config: Any, pid: str) -> dict[str, Any]:
    try:
        return json.loads((plugin_dir(config, pid) / "manifest.json").read_text())
    except (OSError, ValueError):
        return {}


def installed(config: Any, pid: str) -> bool:
    return venv_python(config, pid).exists() and bool(manifest(config, pid).get("version"))


def status(config: Any, pid: str) -> dict[str, Any]:
    """Anzeige: installiert?, Version(en), Quelle, Zeitpunkt, laufende Installation."""
    p = PLUGINS[pid]
    m = manifest(config, pid)
    return {"id": pid, "label": p.label, "installed": installed(config, pid), "version": m.get("version"),
            "libs": m.get("libs") or {}, "spec": m.get("spec") or p.spec(), "installed_at": m.get("installed_at"),
            "python": m.get("python"), "busy": _LOCK.locked(), "spec_default": p.spec()}


def command(config: Any, pid: str) -> list[str]:
    """Aufruf des installierten Plugins (``python -m <modul>``)."""
    return [str(venv_python(config, pid)), "-m", PLUGINS[pid].module]


def _run(cmd: list[str], timeout: int, cwd: Path | None = None) -> tuple[int, str]:
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=clean_env(), cwd=cwd,  # noqa: S603 - feste Aufrufe (venv, pip, Plugin), keine Shell
                            check=False)
    except subprocess.TimeoutExpired:
        return 124, f"Zeitüberschreitung nach {timeout} s"
    except OSError as e:
        return 127, str(e)
    return cp.returncode, ((cp.stdout or "") + (cp.stderr or "")).strip()


def _tail(text: str, n: int = 12) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[:900]


def install(config: Any, pid: str, progress: Any = None) -> dict[str, Any]:
    """Plugin einrichten bzw. aktualisieren (synchron; die Oberfläche startet es im Hintergrund). Rückgabe:
    ``{"ok": bool, "message": str, "version": …}``; bei Fehlern bleibt eine bestehende Installation unverändert."""
    plug = PLUGINS[pid]
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "message": "Es läuft bereits eine Installation."}
    root = plugin_dir(config, pid)
    new = root / "venv.new"
    try:
        def step(text: str) -> None:
            log.info("Plugin %s: %s", pid, text)
            if progress is not None:
                progress(text)

        root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(new, ignore_errors=True)
        step("Python-Umgebung anlegen")
        rc, out = _run([sys.executable, "-m", "venv", str(new)], 180)
        if rc:
            shutil.rmtree(new, ignore_errors=True)
            return {"ok": False, "message": f"Python-Umgebung konnte nicht angelegt werden: {_tail(out)}"}
        cons = root / "constraints.txt"
        cons.write_text("\n".join(plug.pins) + "\n")
        spec = plug.spec()
        step("Pakete installieren (pip)")
        rc, out = _run([str(new / "bin" / "python"), "-m", "pip", "install", "--no-input", "--no-cache-dir",
                        "--disable-pip-version-check", "-c", str(cons), spec], INSTALL_TIMEOUT, cwd=root)
        if rc:
            shutil.rmtree(new, ignore_errors=True)
            hint = (" Quelle prüfen (Umgebungsvariable " + plug.spec_env + ") und Netzwerkzugang zu PyPI/GitHub."
                    if "404" in out or "Could not" in out or "No matching" in out or "connection" in out.lower()
                    else "")
            return {"ok": False, "message": f"Installation fehlgeschlagen: {_tail(out)}.{hint}"}
        step("Installation prüfen")
        rc, out = _run([str(new / "bin" / "python"), "-m", plug.module, "--version"], VERIFY_TIMEOUT)
        info: dict[str, Any] = {}
        if rc == 0:
            try:
                info = json.loads(out.splitlines()[-1])
            except (ValueError, IndexError):
                info = {}
        if rc or not info.get("version"):
            shutil.rmtree(new, ignore_errors=True)
            return {"ok": False, "message": f"Prüfung nach der Installation fehlgeschlagen: {_tail(out)}"}
        old = root / "venv"
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        new.rename(old)
        data = {"version": info["version"], "libs": info.get("libs") or {}, "spec": spec, "installed_at": _now(),
                "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"}
        (root / "manifest.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
        return {"ok": True, "message": f"Eingerichtet: Version {info['version']}.", "version": info["version"]}
    except Exception as e:  # pragma: no cover - Absicherung
        log.exception("Plugin %s: Installation fehlgeschlagen", pid)
        shutil.rmtree(new, ignore_errors=True)
        return {"ok": False, "message": f"Installation fehlgeschlagen ({type(e).__name__})."}
    finally:
        _LOCK.release()


def start_install(config: Any, pid: str) -> bool:
    """Installation im Hintergrund starten; ``False``, wenn bereits eine läuft."""
    if _LOCK.locked():
        return False
    result: dict[str, Any] = {}
    _LAST[pid] = {"running": True, "message": "Wird eingerichtet …", "ok": None}

    def work() -> None:
        try:
            result.update(install(config, pid, lambda t: _LAST[pid].update(message=t + " …")))
        finally:
            _LAST[pid] = {"running": False, "message": result.get("message", "Abgebrochen."),
                          "ok": bool(result.get("ok"))}

    threading.Thread(target=work, name=f"plugin-install-{pid}", daemon=True).start()
    return True


_LAST: dict[str, dict[str, Any]] = {}


def last(pid: str) -> dict[str, Any]:
    """Stand der letzten bzw. laufenden Installation dieses Prozesses (für die Anzeige)."""
    return _LAST.get(pid) or {}


def remove(config: Any, pid: str) -> bool:
    if _LOCK.locked():
        return False
    shutil.rmtree(plugin_dir(config, pid), ignore_errors=True)
    _LAST.pop(pid, None)
    return True
