"""Regelwerk-Dateien („Parameterstände“): importierbare YAML-Dateien mit Parametern und Schaltern je Veranlagungsjahr.

Eine Datei ändert **nur Werte und Schalter** des Regelwerks (Freigrenzen, Pauschbeträge, Basiszins, Formularzeilen,
dokumentierte Regelschalter) – keine Rechenlogik. Sie gilt nur in ihren Gültigkeitsjahren (``valid_from`` …
``valid_to``), sonst gilt der mitgelieferte Standard. Aufbau::

    ruleset:
      name: "BMF 2022 – frühere Rechtslage"   # Pflicht
      id: bmf-2022                              # optional (sonst aus dem Namen)
      valid_from: 2022
      valid_to: 2024
      status: draft                             # draft (Entwurf, „vorläufig“) | valid (gültig)
      source: "BMF-Schreiben vom 10.05.2022, Rz. …"
      description: "…"
    rules:    {2022: {crypto: {freigrenze_23: 600}}}      # wie params.yaml
    per_year: {basiszins: {2027: 0.03}}
    forms:    {2026: {anlage_so: {fields: {so_23_gain: {line: "51"}}}}}

Ablage: ``<tax_rules_dir>/<pack>/sets/<kennung>.yaml``. Der SHA-256 der Datei steht im Bericht (Nachvollziehbarkeit).
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from app.tax.params import ParamSet, _plain, deep_merge, split_ruleset
from app.tax.schema import Num, YearMap

MAX_BYTES = 256 * 1024
MIN_YEAR, MAX_YEAR = 1990, 2100
SLUG = re.compile(r"[^a-z0-9]+")
HEAD_KEYS = {"name", "id", "valid_from", "valid_to", "status", "source", "description"}
TOP_KEYS = {"ruleset", "rules", "per_year", "forms"}
FORM_ATTRS = {"title", "fund_labels", "fields"}
FIELD_ATTRS = {"section", "label", "line", "lines"}


def slugify(text: str) -> str:
    t = text.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        t = t.replace(a, b)
    return SLUG.sub("-", t).strip("-")[:40].strip("-")


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    head: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    slug: str = ""
    sha256: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class RulesetInfo:
    slug: str
    name: str
    status: str
    valid_from: int | None
    valid_to: int | None
    source: str
    description: str
    sha256: str
    uploaded: float
    size: int
    error: str = ""

    @property
    def years(self) -> str:
        lo, hi = self.valid_from, self.valid_to
        if lo is None and hi is None:
            return "alle Jahre"
        return f"{lo if lo is not None else '…'}–{hi if hi is not None else 'offen'}"


def _is_year(k: Any) -> bool:
    return isinstance(k, int) and not isinstance(k, bool) and MIN_YEAR <= k <= MAX_YEAR


def _walk(schema: Any, node: Any, path: str, out: Validation, std: Any = None) -> None:
    """Prüft ``node`` gegen den Schema-Baum; unbekannte Schlüssel und falsche Typen sind Fehler."""
    if isinstance(schema, dict):
        if not isinstance(node, dict):
            out.errors.append(f"{path}: Abschnitt (Schlüssel: Wert) erwartet")
            return
        for k, v in node.items():
            if k not in schema:
                out.errors.append(f"{path}.{k}: unbekannter Schlüssel (erlaubt: {', '.join(sorted(map(str, schema)))})")
                continue
            _walk(schema[k], v, f"{path}.{k}", out, (std or {}).get(k) if isinstance(std, dict) else None)
        return
    if isinstance(schema, YearMap):
        if not isinstance(node, dict):
            out.errors.append(f"{path}: Tabelle Jahr → Wert erwartet")
            return
        for y, v in node.items():
            if not _is_year(y):
                out.errors.append(f"{path}.{y}: Jahr zwischen {MIN_YEAR} und {MAX_YEAR} erwartet")
                continue
            msg = schema.leaf.check(v)
            if msg:
                out.errors.append(f"{path}.{y}: {msg}")
        return
    msg = schema.check(node)
    if msg:
        out.errors.append(f"{path}: {msg}")
        return
    factor = getattr(schema, "warn_factor", None)
    if (isinstance(schema, Num) and factor and isinstance(node, (int, float)) and isinstance(std, (int, float))
            and not isinstance(std, bool) and std > 0 and node > 0
            and (node / std > factor or std / node > factor)):
        out.warnings.append(f"{path}: {node:g} weicht stark vom Standard ({std:g}) ab – bitte prüfen")


def _check_forms(forms: Any, bundled_forms: dict[str, Any], out: Validation) -> None:
    if not isinstance(forms, dict):
        out.errors.append("forms: Abschnitt erwartet")
        return
    base = (bundled_forms.get("default") or {})
    for key, per in forms.items():
        if key != "default" and not _is_year(key):
            out.errors.append(f"forms.{key}: „default“ oder ein Jahr erwartet")
            continue
        if not isinstance(per, dict):
            out.errors.append(f"forms.{key}: Abschnitt erwartet")
            continue
        for fid, spec in per.items():
            p = f"forms.{key}.{fid}"
            if fid not in base:
                out.errors.append(f"{p}: unbekanntes Formular (bekannt: {', '.join(sorted(base))})")
                continue
            if not isinstance(spec, dict):
                out.errors.append(f"{p}: Abschnitt erwartet")
                continue
            for a, v in spec.items():
                if a not in FORM_ATTRS:
                    out.errors.append(f"{p}.{a}: unbekannter Schlüssel (erlaubt: {', '.join(sorted(FORM_ATTRS))})")
                elif a == "fields":
                    _check_fields(v, base[fid].get("fields") or {}, p, out)
                elif a == "title" and not isinstance(v, str):
                    out.errors.append(f"{p}.title: Text erwartet")
                elif a == "fund_labels" and not (isinstance(v, dict) and all(isinstance(x, str) for x in v.values())):
                    out.errors.append(f"{p}.fund_labels: Zuordnung Fondsart → Text erwartet")


def _check_fields(fields: Any, known: dict[str, Any], path: str, out: Validation) -> None:
    if not isinstance(fields, dict):
        out.errors.append(f"{path}.fields: Abschnitt erwartet")
        return
    for fid, attrs in fields.items():
        p = f"{path}.fields.{fid}"
        if fid not in known:
            out.errors.append(f"{p}: unbekanntes Feld (bekannt: {', '.join(sorted(known))})")
            continue
        if not isinstance(attrs, dict):
            out.errors.append(f"{p}: Abschnitt erwartet")
            continue
        for a, v in attrs.items():
            if a not in FIELD_ATTRS:
                out.errors.append(f"{p}.{a}: unbekannter Schlüssel (erlaubt: {', '.join(sorted(FIELD_ATTRS))})")
            elif a == "line":
                if isinstance(v, bool) or (v is not None and not isinstance(v, (str, int))):
                    out.errors.append(f"{p}.line: Zeilennummer (Text) oder null erwartet")
            elif a == "lines":
                if not isinstance(v, dict) or not all(isinstance(x, (str, int)) for x in v.values()):
                    out.errors.append(f"{p}.lines: Zuordnung Fondsart → Zeile erwartet")
            elif not isinstance(v, str):
                out.errors.append(f"{p}.{a}: Text erwartet")


def validate(raw: bytes, *, schema: dict[str, Any] | None, bundled: Path, today_year: int) -> Validation:
    """Prüft eine Regelwerk-Datei: Größe, YAML, Kopf, Schlüssel, Typen, Wertebereiche, Plausibilität."""
    out = Validation()
    if not raw.strip():
        out.errors.append("Die Datei ist leer.")
        return out
    if len(raw) > MAX_BYTES:
        out.errors.append(f"Die Datei ist zu groß (höchstens {MAX_BYTES // 1024} KB).")
        return out
    out.sha256 = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        out.errors.append("Die Datei ist nicht UTF-8-codiert.")
        return out
    try:
        loaded = YAML(typ="safe", pure=True).load(io.StringIO(text))
    except Exception as e:
        out.errors.append(f"YAML-Syntaxfehler: {str(e).splitlines()[0] if str(e) else type(e).__name__}")
        return out
    if not isinstance(loaded, dict):
        out.errors.append("Die Datei muss mit Abschnitten beginnen (ruleset, rules, per_year, forms).")
        return out
    data = _plain(loaded)
    for k in data:
        if k not in TOP_KEYS:
            out.errors.append(f"{k}: unbekannter Abschnitt (erlaubt: {', '.join(sorted(TOP_KEYS))})")
    head, body = split_ruleset(data)
    out.head, out.data = head, body
    _check_head(head, out, today_year)
    std = load_bundled(bundled)
    rules = body.get("rules") or {}
    for y, node in rules.items():
        if not _is_year(y):
            out.errors.append(f"rules.{y}: Jahr zwischen {MIN_YEAR} und {MAX_YEAR} erwartet")
            continue
        if schema is not None:
            _walk(schema["rules"], node or {}, f"rules.{y}", out, _std_for(std, y))
        if isinstance(head.get("valid_to"), int) and y > head["valid_to"]:
            out.warnings.append(f"rules.{y}: liegt nach valid_to ({head['valid_to']}) und wirkt daher nicht")
    out.warnings.extend(_shadow_warnings(rules, std, head))
    if schema is not None and "per_year" in body:
        _walk(schema["per_year"], body["per_year"], "per_year", out)
    elif "per_year" in body and not isinstance(body["per_year"], dict):
        out.errors.append("per_year: Abschnitt erwartet")
    if "forms" in body:
        _check_forms(body["forms"], std.get("forms") or {}, out)
    if not any(body.get(k) for k in ("rules", "per_year", "forms")):
        out.errors.append("Die Datei enthält keine Parameter (rules, per_year oder forms).")
    out.slug = slugify(str(head.get("id") or head.get("name") or ""))
    if not out.slug and not out.errors:
        out.errors.append("ruleset.name: Name erforderlich")
    return out


def _leaves(node: Any, path: str = "") -> list[str]:
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _leaves(v, f"{path}.{k}" if path else str(k))]
    return [path]


def _shadow_warnings(rules: dict[Any, Any], std: dict[str, Any], head: dict[str, Any]) -> list[str]:
    """Ein Wert „ab Jahr X“ wird von späteren Standardwerten desselben Parameters überdeckt – das ist selten gewollt."""
    out: list[str] = []
    std_rules = {y: n for y, n in (std.get("rules") or {}).items() if _is_year(y)}
    hi = head.get("valid_to") if _is_year(head.get("valid_to")) else MAX_YEAR
    for y, node in sorted((y, n) for y, n in rules.items() if _is_year(y)):
        for leaf in _leaves(node or {}):
            later = sorted(sy for sy, sn in std_rules.items() if y < sy <= hi and leaf in _leaves(sn or {}))
            if later:
                out.append(f"rules.{y}.{leaf}: ab {later[0]} gilt wieder der Standardwert (Werte gelten „ab Jahr“, "
                           f"spätere Einträge überdecken frühere) – ggf. ebenfalls für {later[0]} eintragen")
    return out


def _std_for(std: dict[str, Any], year: int) -> dict[str, Any]:
    eff: dict[str, Any] = {}
    for y in sorted(k for k in (std.get("rules") or {}) if isinstance(k, int) and k <= year):
        eff = deep_merge(eff, std["rules"][y] or {})
    return eff


def load_bundled(bundled: Path) -> dict[str, Any]:
    from app.tax.params import load_yaml

    return load_yaml(bundled) if bundled.exists() else {}


def _check_head(head: dict[str, Any], out: Validation, today_year: int) -> None:
    if not head:
        out.errors.append("ruleset: Kopf fehlt (mindestens name)")
        return
    for k in head:
        if k not in HEAD_KEYS:
            out.errors.append(f"ruleset.{k}: unbekannter Schlüssel (erlaubt: {', '.join(sorted(HEAD_KEYS))})")
    name = head.get("name")
    if not isinstance(name, str) or not name.strip():
        out.errors.append("ruleset.name: Name erforderlich")
    elif len(name) > 80:
        out.errors.append("ruleset.name: höchstens 80 Zeichen")
    for k in ("valid_from", "valid_to"):
        v = head.get(k)
        if v is not None and not _is_year(v):
            out.errors.append(f"ruleset.{k}: Jahr zwischen {MIN_YEAR} und {MAX_YEAR} erwartet")
    lo, hi = head.get("valid_from"), head.get("valid_to")
    if _is_year(lo) and _is_year(hi) and lo > hi:
        out.errors.append("ruleset: valid_from liegt nach valid_to")
    if head.get("status", "draft") not in ("draft", "valid"):
        out.errors.append("ruleset.status: „draft“ (Entwurf) oder „valid“ (gültig) erwartet")
    if head.get("id") is not None and not (isinstance(head["id"], str) and slugify(head["id"])):
        out.errors.append("ruleset.id: Kennung aus Buchstaben/Ziffern erwartet")
    for k in ("source", "description"):
        if head.get(k) is not None and not isinstance(head[k], str):
            out.errors.append(f"ruleset.{k}: Text erwartet")
    if head.get("status") == "valid" and not str(head.get("source") or "").strip():
        out.warnings.append("ruleset.source: Status „valid“ ohne Quellenangabe – bitte die Rechtsquelle eintragen")
    if _is_year(hi) and hi > today_year + 10:
        out.warnings.append(f"ruleset.valid_to: {hi} liegt weit in der Zukunft")


class RulesetStore:
    """Dateiablage der Regelwerk-Dateien eines Regelwerks."""

    def __init__(self, base_dir: Path, pack_id: str, bundled: Path, schema: dict[str, Any] | None) -> None:
        self.base_dir, self.pack_id, self.bundled, self.schema = base_dir, pack_id, bundled, schema
        self.dir = base_dir / pack_id / "sets"

    # -- Dateien --------------------------------------------------------------------------------------
    def path(self, slug: str) -> Path | None:
        """Pfad einer vorhandenen Datei; ``None`` bei ungültiger Kennung oder fehlender Datei (kein Pfadsprung)."""
        if not slug or slugify(slug) != slug:
            return None
        p = self.dir / f"{slug}.yaml"
        return p if p.is_file() else None

    def list(self) -> list[RulesetInfo]:
        out: list[RulesetInfo] = []
        if not self.dir.is_dir():
            return out
        for p in sorted(self.dir.glob("*.yaml")):
            try:
                raw = p.read_bytes()
                head = split_ruleset(_load(raw))[0]
                err = ""
            except Exception as e:
                raw, head, err = b"", {}, str(e).splitlines()[0] if str(e) else type(e).__name__
            lo, hi = head.get("valid_from"), head.get("valid_to")
            out.append(RulesetInfo(
                slug=p.stem, name=str(head.get("name") or p.stem), valid_from=lo if _is_year(lo) else None,
                valid_to=hi if _is_year(hi) else None, status="valid" if head.get("status") == "valid" else "draft",
                source=str(head.get("source") or ""), description=str(head.get("description") or ""),
                sha256=hashlib.sha256(raw).hexdigest() if raw else "", uploaded=p.stat().st_mtime, size=len(raw),
                error=err))
        return out

    def info(self, slug: str) -> RulesetInfo | None:
        return next((i for i in self.list() if i.slug == slug), None)

    def save(self, raw: bytes, today_year: int) -> Validation:
        """Prüft und speichert; bei Fehlern wird nichts geschrieben. Gleiche Kennung ersetzt die Datei."""
        v = validate(raw, schema=self.schema, bundled=self.bundled, today_year=today_year)
        if not v.ok:
            return v
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".{v.slug}.tmp"
        tmp.write_bytes(raw)
        tmp.replace(self.dir / f"{v.slug}.yaml")
        return v

    def delete(self, slug: str) -> bool:
        p = self.path(slug)
        if p is None:
            return False
        p.unlink()
        return True

    def read(self, slug: str) -> bytes | None:
        p = self.path(slug)
        return p.read_bytes() if p else None

    # -- Vorschau (Unterschiede zum Standard) --------------------------------------------------------------
    def diff(self, slug: str, override_dir: Path | None, this_year: int, max_years: int = 40) -> list[dict[str, Any]]:
        """Unterschiede der wirksamen Parameter (ohne Datei ↔ mit Datei) je Jahr, zu Bereichen zusammengefasst."""
        p = self.path(slug)
        if p is None:
            return []
        base = ParamSet(self.pack_id, self.bundled, override_dir)
        alt = ParamSet(self.pack_id, self.bundled, override_dir, p)
        info = alt.ruleset_info()
        if info is None:
            return []
        lo = info["valid_from"] if _is_year(info["valid_from"]) else (min(base.years() or [this_year]))
        hi = min(info["valid_to"] if _is_year(info["valid_to"]) else this_year + 2, this_year + 2)
        years = list(range(lo, hi + 1))[:max_years]
        cells: dict[str, dict[int, tuple[Any, Any]]] = {}
        for y in years:
            a, b = _flat(base.for_year(y)), _flat(alt.for_year(y))
            for k in sorted(set(a) | set(b)):
                if a.get(k) != b.get(k):
                    cells.setdefault(k, {})[y] = (a.get(k), b.get(k))
        out = []
        for key, per in cells.items():
            ranges: list[dict[str, Any]] = []
            for y in sorted(per):
                old, new = per[y]
                if ranges and ranges[-1]["to"] == y - 1 and (ranges[-1]["old"], ranges[-1]["new"]) == (old, new):
                    ranges[-1]["to"] = y
                else:
                    ranges.append({"from": y, "to": y, "old": old, "new": new})
            out.append({"key": key, "ranges": ranges})
        return out


def _load(raw: bytes) -> dict[str, Any]:
    d = YAML(typ="safe", pure=True).load(io.StringIO(raw.decode("utf-8-sig")))
    if not isinstance(d, dict):
        raise ValueError("kein Mapping")
    return _plain(d)


def _flat(eff: dict[str, Any]) -> dict[str, Any]:
    """Wirksame Parameter eines Jahres als flache Zuordnung Pfad → Wert (ohne Metadaten)."""
    out: dict[str, Any] = {}

    def rec(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "_meta":
                    continue
                rec(v, f"{path}.{k}" if path else str(k))
        else:
            out[path] = node

    rec(eff, "")
    return out


def format_value(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "ja" if v else "nein"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


# -- Vorlage ------------------------------------------------------------------------------------------------
def template_text(pack_name: str, effective: dict[str, Any], year: int) -> str:
    """Startdatei für eigene Regelwerk-Dateien: Kopf, die für ``year`` wirksamen Werte und Erläuterungen."""
    cap = effective.get("capital") or {}
    cr = effective.get("crypto") or {}
    tf = cap.get("teilfreistellung") or {}

    def num(v: Any) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return str(v).lower()
        return f"{v:g}" if isinstance(v, float) else str(v)

    lines = [
        f"# Regelwerk-Datei (Parameterstand) für „{pack_name}\" – Vorlage",
        "#",
        "# Die Datei ändert nur Werte und Schalter, keine Rechenlogik. Sie gilt nur in den Jahren",
        "# valid_from … valid_to;",
        "# sonst gilt der mitgelieferte Standard. Nur geänderte Werte eintragen (alles andere bleibt Standard).",
        "# status: draft = Entwurf (Berichte tragen den Vermerk „vorläufig“) | valid = geprüft und gültig.",
        "# Beim Hochladen wird die Datei geprüft (Schlüssel, Typen, Wertebereiche) und erst dann gespeichert.",
        "",
        "ruleset:",
        '  name: "Mein Parameterstand"',
        "  # id: mein-stand            # optional, sonst aus dem Namen",
        f"  valid_from: {year}",
        f"  valid_to: {year}",
        "  status: draft",
        '  source: "z. B. BMF-Schreiben vom TT.MM.JJJJ, Rz. …"',
        '  description: "Wozu dieser Stand dient"',
        "",
        "rules:                       # gilt ab dem genannten Jahr, bis ein späteres Jahr es ändert",
        f"  {year}:",
        "    crypto:",
        f"      holding_period_years: {num(cr.get('holding_period_years'))}    # Haltefrist in Jahren",
        f"      freigrenze_23: {num(cr.get('freigrenze_23'))}            # § 23 Abs. 3 S. 5 EStG",
        f"      freigrenze_22_3: {num(cr.get('freigrenze_22_3'))}          # § 22 Nr. 3 S. 2 EStG",
        f"      exempt_after_holding: {num(cr.get('exempt_after_holding', True))}   # false: danach steuerpflichtig",
        f"      freigrenze_mode: {cr.get('freigrenze_mode', 'freigrenze')}      # freigrenze | freibetrag",
        "    capital:",
        f"      flat_rate: {num(cap.get('flat_rate'))}",
        f"      soli_rate: {num(cap.get('soli_rate'))}",
        f"      sparer_pauschbetrag_single: {num(cap.get('sparer_pauschbetrag_single'))}",
        f"      sparer_pauschbetrag_joint: {num(cap.get('sparer_pauschbetrag_joint'))}",
        f"      vp_factor: {num(cap.get('vp_factor'))}",
        f"      wht_credit_cap: {num(cap.get('wht_credit_cap'))}",
        f"      derivative_loss_cap: {num(cap.get('derivative_loss_cap'))}      # null = keine Verrechnungsgrenze",
        "      teilfreistellung:",
    ]
    lines += [f"        {k}: {num(v)}" for k, v in tf.items()]
    lines += [
        "",
        "# per_year:                  # Werte nur für das genannte Jahr",
        "#   basiszins: {2027: 0.03}  # amtlichen Wert des BMF eintragen",
        "",
        "# forms:                     # Formularzeilen (nur belegte Zeilen eintragen)",
        "#   2026:",
        "#     anlage_so: {fields: {so_23_gain: {line: \"51\"}}}",
        "",
    ]
    return "\n".join(lines)

