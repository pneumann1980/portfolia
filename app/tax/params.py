"""Steuerparameter je Regelwerk und Veranlagungsjahr (YAML, mitgeliefert + lokale Overrides).

Aufbau einer Parameterdatei::

    pack: {id: de, version: "2026.1", reviewed_through: 2026}
    rules:            # Werte gelten ab dem Jahr, bis ein späteres Jahr sie ändert (kumulativ)
      2018: {crypto: {freigrenze_23: 600}, ...}
      2024: {crypto: {freigrenze_23: 1000}}
    per_year:         # Werte nur für genau dieses Jahr (nicht fortgeschrieben), z. B. Basiszins
      basiszins: {2023: 0.0255, 2024: 0.0229}
    forms:            # Formularfelder (Bezeichnungen, optional Zeilen) – je Jahr überschreibbar
      default: {...}
      2025: {...}

Ein Override unter ``/data/tax_rules/<pack>.yaml`` hat dieselbe Struktur und wird tief gemischt; so lassen
sich z. B. neue Freigrenzen, ein neuer Basiszins oder verifizierte Formularzeilen ohne Update eintragen.

Zusätzlich kann eine **Regelwerk-Datei** („Parameterstand“, ``/data/tax_rules/<pack>/sets/<kennung>.yaml``, verwaltet
über :mod:`app.tax.rulesets`) gewählt werden. Sie trägt einen Kopf ``ruleset:`` (Name, gültige Jahre, Status ``draft`` /
``valid``, Quelle) und gilt nur für Veranlagungsjahre innerhalb ``valid_from``…``valid_to``; für alle anderen Jahre
bleibt der Standard maßgeblich. Entwürfe (``draft``) gelten als „vorläufig“.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

log = logging.getLogger(__name__)


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _plain(obj: Any) -> Any:
    """ruamel-Objekte in einfache Python-Typen wandeln (Schlüssel als str, Jahre als int)."""
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            key: Any = k
            if isinstance(k, str) and k.isdigit():
                key = int(k)
            out[key] = _plain(v)
        return out
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj


def load_yaml(path: Path) -> dict[str, Any]:
    yaml = YAML(typ="safe", pure=True)
    with path.open(encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Parameterdatei muss ein Mapping enthalten")
    return _plain(data)


def _check_numbers(obj: Any, path: str = "") -> None:
    """Grobe Plausibilisierung: Zahlenfelder dürfen keine Strings sein (Tippfehler wie '1.000')."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _check_numbers(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, str) and path.split(".")[-1] in NUMERIC_KEYS:
        raise ValueError(f"{path}: Zahl erwartet, gefunden „{obj}“")


NUMERIC_KEYS = frozenset({
    "holding_period_years", "freigrenze_23", "freigrenze_22_3", "flat_rate", "soli_rate",
    "sparer_pauschbetrag_single", "sparer_pauschbetrag_joint", "vp_factor", "wht_credit_cap",
})


RULESET_SECTIONS = ("rules", "per_year", "forms")


def split_ruleset(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Regelwerk-Datei → (Kopf ``ruleset``, Parameter-Teil mit rules/per_year/forms)."""
    head = raw.get("ruleset") if isinstance(raw.get("ruleset"), dict) else {}
    return dict(head), {k: raw[k] for k in RULESET_SECTIONS if isinstance(raw.get(k), dict)}


class ParamSet:
    def __init__(self, pack_id: str, bundled: Path, override_dir: Path | None = None,
                 ruleset: Path | None = None) -> None:
        self.pack_id = pack_id
        self.bundled = bundled
        self.override_path = (override_dir / f"{pack_id}.yaml") if override_dir else None
        self.ruleset_path = ruleset
        self.override_error: str | None = None
        self.override_active = False
        self.ruleset_error: str | None = None
        self.ruleset_head: dict[str, Any] = {}
        self.ruleset_sha = ""
        self._mtime: tuple[float, float, float] | None = None
        self._data: dict[str, Any] = {}
        self._merged: dict[str, Any] | None = None
        self.reload()

    # -- Laden --------------------------------------------------------------------------------------
    def _mtimes(self) -> tuple[float, float, float]:
        def mt(p: Path | None) -> float:
            return p.stat().st_mtime if p is not None and p.exists() else 0.0

        return mt(self.bundled), mt(self.override_path), mt(self.ruleset_path)

    def reload(self) -> None:
        data = load_yaml(self.bundled) if self.bundled.exists() else {}
        self.override_error = None
        self.override_active = False
        if self.override_path is not None and self.override_path.exists():
            try:
                over = load_yaml(self.override_path)
                _check_numbers(over)
                data = deep_merge(data, over)
                self.override_active = True
            except Exception as e:  # Syntax-/Strukturfehler: Override ignorieren, sichtbar melden
                self.override_error = f"{self.override_path.name}: {e}"
                log.warning("Steuerparameter-Override ignoriert: %s", self.override_error)
        self._data = data
        self._merged = None
        self.ruleset_error, self.ruleset_head, self.ruleset_sha = None, {}, ""
        if self.ruleset_path is not None:
            try:
                raw_bytes = self.ruleset_path.read_bytes()
                raw = load_yaml(self.ruleset_path)
                _check_numbers(raw)
                head, body = split_ruleset(raw)
                self.ruleset_head = head
                self.ruleset_sha = hashlib.sha256(raw_bytes).hexdigest()
                self._merged = deep_merge(data, body)
            except Exception as e:  # Datei fehlt/defekt: Standard gilt, Fehler sichtbar
                self.ruleset_error = f"{self.ruleset_path.name}: {e}"
                log.warning("Regelwerk-Datei ignoriert: %s", self.ruleset_error)
        self._mtime = self._mtimes()

    def _fresh(self) -> dict[str, Any]:
        if self._mtime != self._mtimes():
            self.reload()
        return self._data

    # -- Regelwerk-Datei (Parameterstand) -------------------------------------------------------------
    def ruleset_applies(self, year: int) -> bool:
        self._fresh()
        if self._merged is None:
            return False
        h = self.ruleset_head
        lo, hi = h.get("valid_from"), h.get("valid_to")
        return (not isinstance(lo, int) or year >= lo) and (not isinstance(hi, int) or year <= hi)

    def ruleset_info(self, year: int | None = None) -> dict[str, Any] | None:
        """Angaben zur gewählten Regelwerk-Datei (für Bericht und Oberfläche); ``None`` ohne aktive Datei."""
        self._fresh()
        if self._merged is None:
            return None
        h = self.ruleset_head
        return {"slug": self.ruleset_path.stem if self.ruleset_path else "", "name": str(h.get("name") or ""),
                "status": "draft" if str(h.get("status") or "draft") != "valid" else "valid",
                "valid_from": h.get("valid_from"), "valid_to": h.get("valid_to"), "source": str(h.get("source") or ""),
                "sha256": self.ruleset_sha,
                "applies": None if year is None else self.ruleset_applies(year)}

    def _data_for(self, year: int) -> dict[str, Any]:
        d = self._fresh()
        return self._merged if (self._merged is not None and self.ruleset_applies(year)) else d

    # -- Abfragen ------------------------------------------------------------------------------------
    @property
    def version(self) -> str:
        v = str(self._fresh().get("pack", {}).get("version", "0"))
        if self.override_active:
            v += "+lokal"
        if self._merged is not None and self.ruleset_path is not None:
            v += f"+{self.ruleset_path.stem}"
        return v

    @property
    def meta(self) -> dict[str, Any]:
        return dict(self._fresh().get("pack", {}))

    def years(self) -> list[int]:
        d = self._merged if self._merged is not None else self._fresh()
        rule_years = [y for y in d.get("rules", {}) if isinstance(y, int)]
        if not rule_years:
            return []
        last = max(int(d.get("pack", {}).get("reviewed_through", max(rule_years))), max(rule_years))
        return list(range(min(rule_years), last + 1))

    def reviewed(self, year: int) -> bool:
        d = self._data_for(year)
        rt = d.get("pack", {}).get("reviewed_through")
        return rt is None or year <= int(rt)

    def for_year(self, year: int) -> dict[str, Any]:
        d = self._data_for(year)
        eff: dict[str, Any] = {}
        for y in sorted(k for k in d.get("rules", {}) if isinstance(k, int)):
            if y <= year:
                eff = deep_merge(eff, d["rules"][y] or {})
        for key, table in (d.get("per_year") or {}).items():
            eff[key] = (table or {}).get(year)
        forms = d.get("forms") or {}
        eff["forms"] = deep_merge(forms.get("default") or {}, forms.get(year) or {})
        info = self.ruleset_info(year)
        applied = info if info and info["applies"] else None
        eff["_meta"] = {"year": year, "reviewed": self.reviewed(year), "version": self.version, "ruleset": applied}
        return eff

    def fingerprint(self, year: int) -> str:
        blob = json.dumps(self.for_year(year), sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]
