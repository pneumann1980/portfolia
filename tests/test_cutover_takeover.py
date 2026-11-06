"""0.100.0 Stufe 4: Die Quellenübernahme darf das festgeschriebene Archiv nicht berühren."""

# ruff: noqa: F811  (Fixtures aus test_bitpanda werden importiert und als Argumente verwendet)
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.datasources import takeover as TK
from tests.test_bitpanda import api, ctx, master  # noqa: F401  (Fixtures)
from tests.test_takeover import ids, staged, started

D = Decimal


def _freeze(c, cutoff: str, utc: str) -> None:
    x = ctx(c)
    x.db.x("INSERT INTO cutover(cutoff, cutoff_utc, status, options_json, fingerprint, created_at) "
           "VALUES (?,?,?,?,?,?)", (cutoff, utc, "active", "{}", "x", "2026-01-01T00:00:00Z"))
    x.invalidate_data()


def test_takeover_replaces_only_from_the_cutoff_and_keeps_the_archive(config, master, api):
    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        assert a.ok and not [x for x in a.checks if x.key == "cutover"]  # ohne Stichtag keine zusätzliche Prüfung
        _freeze(c, "2024-03-01", "2024-02-29T23:00:00Z")  # alle Buchungen (Februar 2024) liegen davor
        before = ids(c)
        a = TK.assess(ctx(c), sid)
        chk = next(x for x in a.checks if x.key == "cutover")
        assert chk.ok and a.hide == [] and a.lines == 0 and a.archived_events == 3  # Vorgänge davor: nicht gebucht
        assert len(a.kept) == 5 and a.window_from is not None  # Archivbuchungen bleiben
        assert next(x for x in a.checks if x.key == "count").ok  # Zählung: Archiv gilt als bewusst ausgelassen
        # der Bestand der Quelle weicht vom Archiv ab (Gebühr fehlt): die Übernahme ist gesperrt, bis ausgeglichen ist
        assert not a.ok and any(x.key == "balance" for x in a.blockers)
        assert ids(c) == before
        # Ausgleich zum Stichtag auf den Bestand der Quelle (Referenz nach dem Stichtag), danach geht die Übernahme
        x = ctx(c)
        x.db.x("INSERT INTO reference_balance(account, asset_id, qty, as_of, source, created_at) VALUES (?,?,?,?,?,?)",
               ("Bitpanda", "BTC", "0.00445", "2024-04-01", "statement", "2026-01-01"))
        x.invalidate_overlay()
        from app.cutover import core as CO
        from app.cutover import settle as ST

        m = CO.models(x, date(2024, 3, 1))[0]
        (r,) = [p for p in ST.plan(x, m)["rows"] if p["asset"] == "BTC"]
        assert r["kind"] == "remove" and r["qty"] == D("0.00005")
        assert not ST.apply(x, date(2024, 3, 1), {r["id"]})["errors"]
        a = TK.assess(ctx(c), sid)
        assert a.ok, [(z.label, z.detail) for z in a.blockers]
        res = TK.apply(ctx(c), sid, a.token)
        assert res["errors"] == [], res
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
