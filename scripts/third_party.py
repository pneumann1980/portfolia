"""Python-Laufzeitabhängigkeiten (direkt und transitiv) mit Version und Lizenz als Markdown-Tabelle.

Grundlage für den Abschnitt „Python-Pakete“ in THIRD_PARTY_NOTICES.md. Aufruf in einer Umgebung, in der
genau die Pakete aus requirements.txt installiert sind (Entwicklungs-venv oder Container)::

    python scripts/third_party.py [requirements.txt]
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement

# Pakete mit uneinheitlichen oder fehlenden Lizenzangaben in den Metadaten (geprüft an der Lizenzdatei)
SPDX = {
    "beautifulsoup4": "MIT",
    "defusedxml": "PSF-2.0",
    "jinja2": "BSD-3-Clause",
    "multitasking": "Apache-2.0",
    "pandas": "BSD-3-Clause",
    "peewee": "MIT",
    "protobuf": "BSD-3-Clause",
    "python-dateutil": "Apache-2.0 AND BSD-3-Clause",
    "reportlab": "BSD-3-Clause",
}
CLASSIFIERS = {
    "MIT License": "MIT",
    "BSD License": "BSD",
    "Apache Software License": "Apache-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def closure(roots: list[str]) -> dict[str, md.Distribution]:
    env = {**default_environment(), "extra": ""}
    seen: dict[str, md.Distribution] = {}
    stack = list(roots)
    while stack:
        name = stack.pop()
        if _norm(name) in seen:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            print(f"nicht installiert: {name}", file=sys.stderr)
            continue
        seen[_norm(name)] = dist
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is None or req.marker.evaluate(env):
                stack.append(req.name)
    return seen


def license_of(key: str, dist: md.Distribution) -> str:
    if key in SPDX:
        return SPDX[key]
    meta = dist.metadata
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    text = (meta.get("License") or "").strip()
    if text and len(text) <= 40 and "\n" not in text:
        return text
    cls = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    return " / ".join(CLASSIFIERS.get(c, c) for c in cls) or "siehe Lizenzdatei"


def main() -> None:
    req_file = Path(sys.argv[1] if len(sys.argv) > 1 else "requirements.txt")
    roots = [ln.split("==")[0].strip() for ln in req_file.read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    print("| Paket | Version | Lizenz |")
    print("|---|---|---|")
    for key, dist in sorted(closure(roots).items()):
        print(f"| {dist.metadata['Name']} | {dist.version} | {license_of(key, dist)} |")


if __name__ == "__main__":
    main()
