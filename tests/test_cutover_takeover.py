"""0.100.0 Stufe 4: Die Quellenübernahme darf das festgeschriebene Archiv nicht berühren."""

# ruff: noqa: F811  (Fixtures aus test_bitpanda werden importiert und als Argumente verwendet)
from __future__ import annotations

from app.datasources import takeover as TK
from tests.test_bitpanda import api, ctx, master  # noqa: F401  (Fixtures)
from tests.test_takeover import ids, staged, started


def _freeze(c, cutoff: str, utc: str) -> None:
    x = ctx(c)
    x.db.x("INSERT INTO cutover(cutoff, cutoff_utc, status, options_json, fingerprint, created_at) "
           "VALUES (?,?,?,?,?,?)", (cutoff, utc, "active", "{}", "x", "2026-01-01T00:00:00Z"))
    x.invalidate_data()


def test_takeover_is_blocked_when_it_would_replace_archive_bookings(config, master, api):
    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        assert a.ok and not [x for x in a.checks if x.key == "cutover"]  # ohne Stichtag keine zusätzliche Prüfung
        _freeze(c, "2024-03-01", "2024-02-29T23:00:00Z")  # alle Buchungen (Februar 2024) liegen davor
        before = ids(c)
        a = TK.assess(ctx(c), sid)
        chk = next(x for x in a.checks if x.key == "cutover")
        assert not chk.ok and "vor dem Stichtag" in chk.detail and not a.ok
        res = TK.apply(ctx(c), sid, a.token)
        assert res["errors"] and ids(c) == before  # nichts verändert
    finally:
        c.__exit__(None, None, None)


def test_takeover_after_the_cutoff_is_not_blocked_by_the_check(config, master, api):
    c, sid = started(config, api)
    try:
        staged(c, sid)
        _freeze(c, "2024-02-01", "2024-01-31T23:00:00Z")  # Stichtag vor allen Buchungen: nichts im Archiv
        a = TK.assess(ctx(c), sid)
        chk = next(x for x in a.checks if x.key == "cutover")
        assert chk.ok and "keine Buchung" in chk.detail
    finally:
        c.__exit__(None, None, None)
