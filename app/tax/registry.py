"""Registry der Steuer-Regelwerke.

Regelwerke liegen als Pakete unter ``app/tax/packs/<id>/`` (``__init__.py`` mit ``PACK = <Klasse>`` und
``params.yaml``). Neue Länder kommen als weiteres Paket hinzu; Parameter-Updates innerhalb eines Landes
erfolgen über die YAML-Datei (mitgeliefert) oder ``/data/tax_rules/<id>.yaml`` (lokal).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import threading
from pathlib import Path

from app.tax.base import RulePack
from app.tax.params import ParamSet

log = logging.getLogger(__name__)
PACKS_DIR = Path(__file__).resolve().parent / "packs"

_lock = threading.Lock()
_classes: dict[str, type[RulePack]] = {}
_instances: dict[tuple[str, str], RulePack] = {}


def _discover() -> None:
    if _classes:
        return
    import app.tax.packs as packs_pkg

    for mod in pkgutil.iter_modules(packs_pkg.__path__):
        if not mod.ispkg:
            continue
        try:
            m = importlib.import_module(f"app.tax.packs.{mod.name}")
        except Exception as e:  # defektes Regelwerk darf die App nicht blockieren
            log.error("Steuer-Regelwerk %s nicht ladbar: %s", mod.name, e)
            continue
        cls = getattr(m, "PACK", None)
        if cls is not None and issubclass(cls, RulePack) and cls.id:
            _classes[cls.id] = cls


def available() -> list[type[RulePack]]:
    with _lock:
        _discover()
        return sorted(_classes.values(), key=lambda c: (c.country is None, c.name))


def get(pack_id: str, override_dir: Path | None = None) -> RulePack | None:
    with _lock:
        _discover()
        cls = _classes.get(pack_id)
        if cls is None:
            return None
        key = (pack_id, str(override_dir) if override_dir else "")
        inst = _instances.get(key)
        if inst is None:
            params = ParamSet(pack_id, PACKS_DIR / pack_id / "params.yaml", override_dir)
            inst = cls(params)
            _instances[key] = inst
        return inst


def resolve(setting: str | None, tz: str | None = None, override_dir: Path | None = None) -> RulePack | None:
    """``auto`` wählt anhand der Zeitzone (Europe/Berlin → de), sonst das neutrale Regelwerk."""
    pack_id = (setting or "auto").strip().lower()
    if pack_id == "auto":
        pack_id = "de" if (tz or "").startswith("Europe/Berlin") or not tz else "neutral"
    return get(pack_id, override_dir) or get("neutral", override_dir)
