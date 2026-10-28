"""Jedes eingebaute CSV-Profil muss mit beliebigen Zellwerten umgehen: Zeilenfehler statt Ausnahme.

Die Profile wurden bislang nur mit echten Beispielzeilen geprüft (Abdeckung 60 %). Hier läuft jedes Profil mit jeder
seiner Kennspalten-Kombinationen über Müll-, Grenz- und Leerwerte – ein Abbruch (500 beim Hochladen) wäre ein Fehler.
"""

from __future__ import annotations

import itertools

import pytest

from app.csvimport.profiles import BUILTIN, ParseOptions, ParseResult, detect
from app.csvimport.reader import Table, unique_keys

VALUES = ["", " ", "abc", "0", "-3", "1,5", "1.234,56", "2025-01-02", "2025-01-02 10:00:00", "02.01.2025 10:00",
          "2025-13-45 99:99", "1e309", "NaN", "BTC", "EUR", "buy", "sell", "send", "receive", "x" * 300, "ß",
          "0.00000001"]


def table(header: list[str], rows: list[list[str]]) -> Table:
    return Table(header=header, keys=unique_keys(header), rows=rows, lines=list(range(2, 2 + len(rows))), delimiter=",",
                 encoding="utf-8", header_line=1)


def cases():
    for p in BUILTIN:
        for i, sg in enumerate(p.signatures or (frozenset(),)):
            yield pytest.param(p, sg, id=f"{p.id}-{i}")


@pytest.mark.parametrize(("profile", "signature"), list(cases()))
def test_profile_never_raises_on_garbage_cells(profile, signature):
    header = sorted(signature) or ["date", "type", "amount"]
    n = len(header)
    # jede Zelle durchläuft jeden Wert (Versatz je Spalte), dazu Zeilen mit zu wenigen Zellen und komplett leere
    rows = [[VALUES[(r + c) % len(VALUES)] for c in range(n)] for r in range(len(VALUES))]
    rows += [[], [""] * n, ["x"] * max(1, n - 1)]
    rows += [list(t) for t in itertools.islice(itertools.product(VALUES[:6], repeat=min(n, 3)), 60)]
    for decimal in (".", ","):
        res = profile.parse(table(header, rows), ParseOptions(decimal=decimal, account="Test"))
        assert isinstance(res, ParseResult)
        assert res.rows_read >= 0 and all(isinstance(ln, int) for ln, _m in res.errors)


@pytest.mark.parametrize(("profile", "signature"), list(cases()))
def test_signature_detection_is_stable(profile, signature):
    keys = {*unique_keys(sorted(signature)), "extra spalte"}
    found = detect(sorted(keys))
    assert found is not None and found.score(keys) >= profile.score(keys)  # nie ein schlechteres Profil als dieses


def test_empty_file_and_header_only():
    for p in BUILTIN:
        header = sorted(p.signatures[0]) if p.signatures else ["a"]
        res = p.parse(table(header, []), ParseOptions())
        assert res.recs == [] and res.rows_read == 0
