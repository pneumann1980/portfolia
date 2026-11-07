"""Abnahme der Buchungsprüfung (2026-11): „Alles geprüft“ im Assistenten nur nach vollständiger, erfolgreicher Prüfung
(I10/I15) – ein fehlgeschlagener Prüflauf oder eine fehlgeschlagene Integritätsprüfung gilt nicht als erledigt."""

# ruff: noqa: F811
from __future__ import annotations

import pytest

from app.diagnosis import checkrun as CR
from app.diagnosis import guide as G
from tests.test_review_fixes import c, cfg  # noqa: F401

NOTHING = dict.fromkeys(("import_deferred", "import_open", "findings_deferred", "cases_deferred", "cases_waiting"), 0)


@pytest.fixture
def empty(c, monkeypatch):
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [])
    monkeypatch.setattr(G, "rest_overview", lambda ctx: dict(NOTHING))
    return c


def test_everything_checked_after_a_successful_run(empty):
    cx = empty.app.state.ctx
    cx.db.set_state(CR.STATE_KEY, {"ok": True, "finished_at": "2026-11-07T10:00:00Z"})
    assert "Alles geprüft" in empty.get("/quality/assistent?new=1").text


def test_failed_check_run_is_not_reported_as_everything_checked(empty):
    cx = empty.app.state.ctx
    cx.db.set_state(CR.STATE_KEY, {"ok": False, "error": "1 von 3 Prüfstapeln nicht neu bewertet.",
                                   "finished_at": "2026-11-07T10:00:00Z", "last_ok_at": "2026-11-06T10:00:00Z"})
    page = empty.get("/quality/assistent?new=1").text
    assert "Alles geprüft" not in page and "Prüfung nicht vollständig" in page and "1 von 3 Prüfstapeln" in page


def test_failed_integrity_check_is_not_reported_as_everything_checked(empty):
    from app.diagnosis import integrity as I

    cx = empty.app.state.ctx
    I.save(cx.db, I.Run(started_at="2026-11-07T10:00:00Z", ok=False, error="ValueError: kaputt"))
    page = empty.get("/quality/assistent?new=1").text
    assert "Alles geprüft" not in page and "Prüfung nicht vollständig" in page


# ---- automatische Nachprüfung: übersprungener Lauf wird nachgeholt, wenn die Daten neuer sind (I03/I11) ----

class _Sched:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def debounce(self, name: str, delay_s: float, **_kw) -> bool:
        self.calls.append((name, delay_s))
        return True

    def shutdown(self, *_a, **_kw) -> None:
        return None


def test_skipped_autocheck_is_rescheduled_when_data_changed(c, monkeypatch):
    from app.diagnosis import autocheck as AC

    cx = c.app.state.ctx
    assert AC.job_run(cx).get("ok")  # frischer Lauf: Integritätsergebnis passt zum Datenstand
    sched = _Sched()
    monkeypatch.setattr(cx, "scheduler", sched)
    assert AC.job_run(cx) == {"skipped": "letzter Lauf zu jung"} and sched.calls == []  # unverändert
    cx.settings.set("ledger.unmatched_transfers_as_flows", False)  # fachliche Änderung direkt nach dem Lauf
    assert AC.job_run(cx) == {"skipped": "letzter Lauf zu jung"}
    assert sched.calls and sched.calls[-1][0] == AC.JOB and 0 < sched.calls[-1][1] <= AC.MIN_GAP_S + AC.DEBOUNCE_S


def test_start_page_shows_an_incomplete_run_over_http(c):
    """PR #3 über den HTTP-Weg: die Startseite „Prüfen“ zeigt den fehlgeschlagenen Lauf und die letzte erfolgreiche
    Berechnung, nicht „Alles geprüft“."""
    cx = c.app.state.ctx
    cx.db.set_state(CR.STATE_KEY, {"ok": False, "error": "Integritätsprüfung veraltet",
                                   "finished_at": "2026-11-07T10:00:00Z", "last_ok_at": "2026-11-06T10:00:00Z"})
    page = c.get("/quality/plan").text
    assert "Prüfung nicht vollständig" in page and "Integritätsprüfung veraltet" in page
    assert "Letzte erfolgreiche Berechnung" in page and "Alles geprüft" not in page
