"""Fehler dürfen nicht still verschluckt werden (Befund M3): Obergrenze für stille Handler + Schutzprüfungen."""

from __future__ import annotations

import logging
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import silent_excepts

# Geprüfter Bestand: Fehler wird in Status/Ergebnis festgehalten (Preisabrufe, Anbieter-Abrufe, Regelwerke) oder die
# Protokollierung selbst darf nie werfen. Die Zahl darf nur sinken; neue stille Handler sind hier zu begründen.
BASELINE = {
    "app/datasources/service.py": 2,
    "app/diagnosis/actions.py": 1,
    "app/diagnosis/integrity.py": 1,
    "app/diagnosis/references.py": 1,
    "app/logging_setup.py": 4,
    "app/prices/market.py": 2,
    "app/prices/service.py": 9,
    "app/prices/yahoo.py": 2,
    "app/tax/rulesets.py": 2,
    "app/util/http.py": 1,
}


def test_silent_exception_handlers_do_not_grow():
    found = Counter(f for f, _ in silent_excepts.find())
    grown = {f: (n, BASELINE.get(f, 0)) for f, n in found.items() if n > BASELINE.get(f, 0)}
    assert not grown, f"neue stille Ausnahmebehandler (Ist, erlaubt): {grown} – app.db.fallback nutzen oder neu werfen"


def test_fallback_logs_a_warning_and_quiet_logs_debug(caplog):
    from app.db import fallback

    with caplog.at_level(logging.DEBUG, logger="app.db"):
        fallback("test_ort", ValueError("kaputt"))
        fallback("test_leise", KeyError("x"), quiet=True)
    recs = {r.getMessage().split(" nicht")[0]: r.levelno for r in caplog.records}
    assert recs["test_ort"] == logging.WARNING and recs["test_leise"] == logging.DEBUG


class BrokenDb:
    def q1(self, *a, **k):
        raise sqlite3.OperationalError("x")


def test_assert_version_aborts_when_the_version_cannot_be_read():
    """Die Schutzprüfung darf bei einem Lesefehler nicht entfallen (vorher: stilles ``return``)."""
    from app.diagnosis import actions as A

    class Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        A.assert_version(Broken(), "1|2|3")
    with pytest.raises(sqlite3.OperationalError):
        A.data_version(BrokenDb())


def test_assert_version_still_detects_changes(db):
    from app.diagnosis import actions as A

    v = A.data_version(db)
    A.assert_version(db.conn, v)  # unverändert: kein Fehler
    A.assert_version(db.conn, "")  # ohne Vergleichswert wie bisher kein Vergleich
    with pytest.raises(A.Conflict):
        A.assert_version(db.conn, "ganz|anders")


@pytest.mark.parametrize("table,call", [
    ("asset_merge", lambda db: __import__("app.assetchange.merge", fromlist=["x"]).active_map(db)),
    ("account_merge", lambda db: __import__("app.assetchange.account_merge", fromlist=["x"]).active_map(db)),
    ("account_remove", lambda db: __import__("app.assetchange.account_remove", fromlist=["x"]).hidden_ids(db)),
    ("diag_decision", lambda db: __import__("app.diagnosis.actions", fromlist=["x"]).decisions(db)),
    ("asset_change", lambda db: __import__("app.assetchange.service", fromlist=["x"]).apply_changes(db, {})),
    ("tax_file", lambda db: __import__("app.fullexport", fromlist=["x"])._taxdata_export(db, {})),
])
def test_data_helpers_raise_instead_of_returning_empty_results(db, table, call):
    """Ein echter Datenbankfehler darf keine leeren Ergebnisse liefern (Zusammenführungen, Entscheidungen …)."""
    db.x(f"DROP TABLE {table}")
    with pytest.raises(sqlite3.OperationalError):
        call(db)


def test_informational_reads_fall_back_but_log(db, caplog):
    from app.assetchange import merge

    db.x("DROP TABLE asset_merge")
    with caplog.at_level(logging.WARNING, logger="app.db"):
        assert merge.history(db) == []
    assert any("history" in r.getMessage() for r in caplog.records)
