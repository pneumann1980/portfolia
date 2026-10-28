"""Findet „stille“ Ausnahmebehandler: ``except Exception`` (oder ohne Typ) ohne Logeintrag und ohne erneutes ``raise``.

Aufruf: ``python scripts/silent_excepts.py [--all]`` – ohne ``--all`` nur die Zahl und die Verteilung je Datei.
Wird auch von ``tests/test_no_silent_except.py`` genutzt (Obergrenze, darf nur sinken).
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}


def _broad(h: ast.ExceptHandler) -> bool:
    t = h.type
    if t is None:
        return True
    names = [t] if not isinstance(t, ast.Tuple) else list(t.elts)
    return any(isinstance(n, ast.Name) and n.id in ("Exception", "BaseException") for n in names)


def _handles(h: ast.ExceptHandler) -> bool:
    """Protokolliert der Handler oder wirft er neu?"""
    for node in ast.walk(ast.Module(body=h.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LOG_METHODS:
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "fallback":
            return True  # app.db.fallback: Ersatzwert mit Logeintrag
    return False


def find(root: Path = ROOT) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for path in sorted((root / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _broad(node) and not _handles(node):
                out.append((str(path.relative_to(root)), node.lineno))
    return out


if __name__ == "__main__":
    found = find()
    print(f"{len(found)} stille Ausnahmebehandler")
    if "--all" in sys.argv:
        for f, ln in found:
            print(f"{f}:{ln}")
    else:
        for f, n in Counter(f for f, _ in found).most_common(15):
            print(f"{n:3d}  {f}")
