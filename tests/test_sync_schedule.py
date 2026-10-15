"""Zeitplan der Datenquellen: das gewählte Intervall gilt auch bei unvollständigem Abruf.

Kontrollierte Uhr und ein Test-Connector, der die Historie in Etappen liefert – keine echten Wartezeiten. Geprüft:
Tagesintervall bleibt wirksam, zügige Fortsetzung nur mit ausdrücklicher Einstellung, begrenzt und mit nachweislichem
Fortschritt; „nur manuell“, Deaktivierung, Abbruch und Neustart lösen keine automatischen Läufe aus; Wartezeiten des
Anbieters zählen ab Ende des Laufs; kein Fortschritt/Stillstand führt nie zu einer Wiederholungsschleife.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from app.csvimport import model as M
from app.csvimport.model import Rec
from app.datasources import connector as K
from app.datasources import service as S
from app.datasources.service import datasource_service
from app.main import build_app
from app.util.timeutil import parse_iso
from tests.test_datasources import KEY, create_source, source

T0 = datetime(2026, 3, 1, 6, 0, tzinfo=UTC)


@pytest.fixture
def client(config):
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


class Staged(K.Connector):
    """Liefert ``total`` Vorgänge in Etappen zu ``step``; ``stuck`` simuliert fehlenden Fortschritt (Cursor bleibt),
    ``stall`` einen erkannten Stillstand, ``cost_s`` die Laufzeit einer Etappe auf der kontrollierten Uhr."""

    provider = "kraken"
    label = "Kraken (Test, in Etappen)"
    needs_credentials = True
    total: ClassVar[int] = 10
    step: ClassVar[int] = 2
    stuck: ClassVar[bool] = False
    stall: ClassVar[str | None] = None
    cost_s: ClassVar[float] = 0.0
    fail: ClassVar[BaseException | None] = None
    clock: ClassVar[Clock | None] = None
    calls: ClassVar[int] = 0

    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        secret.reveal()
        return K.CheckResult(True, "Leserechte vorhanden.")

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        Staged.calls += 1
        if Staged.clock is not None and Staged.cost_s:
            Staged.clock.advance(seconds=Staged.cost_s)
        if Staged.fail is not None:
            raise Staged.fail
        n = int((cursor or {}).get("n", 0))
        end = n if Staged.stuck else min(n + Staged.step, Staged.total)
        evs = []
        for i in range(n, end):
            t = T0 + timedelta(minutes=i)
            evs.append(K.SourceEvent(f"kraken:staged-{i}", t, [Rec(line=0, ts=t, kind=M.DEPOSIT, in_sym="EUR",
                                                            in_qty=Decimal(i + 1), label="deposit")]))
        done = end >= Staged.total
        return K.FetchResult(events=evs, cursor={"n": end}, complete=done, resume=not done, stalled=Staged.stall)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(S, "_now", c)
    Staged.clock = c
    return c


@pytest.fixture
def staged(monkeypatch, clock):
    monkeypatch.setenv(KEY, "APIKEY-1234567890:SECRET-abcdefghij")
    Staged.total, Staged.step, Staged.stuck, Staged.stall, Staged.cost_s = 10, 2, False, None, 0.0
    Staged.fail, Staged.calls = None, 0
    K.register(Staged)
    try:
        yield Staged
    finally:
        K.unregister("kraken")
        Staged.clock = None


def svc(c):
    return datasource_service(c.app.state.ctx)


def at(row_or_ds: Any, col: str) -> datetime | None:
    return parse_iso(row_or_ds[col] if isinstance(row_or_ds, dict) else row_or_ds.row[col])


def make(client, *, interval: str = "1440") -> int:
    return create_source(client, sync_interval_min=interval)


def set_catchup(client, sid: int, on: bool) -> None:
    ctx = client.app.state.ctx
    ctx.db.x("UPDATE data_source SET watch_json=? WHERE id=?", ('{"addresses": [], "catchup": true}' if on
                                                                 else '{"addresses": [], "catchup": false}', sid))


# ----------------------------------------------------------------------------------------------------

def test_daily_interval_survives_an_incomplete_fetch(client, staged, clock):
    sid = make(client)
    set_catchup(client, sid, False)
    res = svc(client).sync(sid, "schedule")
    assert res["status"] == "partial"
    ds = svc(client).get(sid)
    assert ds.backfill_pending and ds.sched["next_kind"] == "schedule" and "Aufholen aus" in ds.sched["next_reason"]
    assert at(source(client, sid), "next_run_at") == T0 + timedelta(days=1)  # nicht Start + 90 s
    clock.advance(hours=23)
    assert svc(client).due() == [] and svc(client).run_due(wait=True)["ran"] == 0 and staged.calls == 1
    clock.advance(hours=1, minutes=1)
    assert [d.id for d in svc(client).due()] == [sid]
    assert svc(client).run_due(wait=True)["ran"] == 1 and staged.calls == 2


def test_fast_continuation_needs_the_explicit_setting_and_is_bounded(client, staged, clock):
    staged.total, staged.step = 100, 1
    sid = make(client)
    set_catchup(client, sid, True)
    assert svc(client).get(sid).catchup
    svc(client).sync(sid, "schedule")  # regulärer Start
    runs = 0
    while True:
        ds = svc(client).get(sid)
        nxt = at(ds, "next_run_at")
        if ds.sched["next_kind"] != "catchup":
            break
        assert nxt == clock() + timedelta(seconds=S.BACKFILL_NEXT_S)  # ab Ende des Laufs, nicht abgelaufen
        clock.advance(seconds=S.BACKFILL_NEXT_S + 1)
        out = svc(client).run_due(wait=True)
        assert out["ran"] == 1
        runs += 1
        assert runs <= S.CATCHUP_MAX_RUNS + 1
    assert runs == S.CATCHUP_MAX_RUNS  # begrenzt: danach gilt wieder der reguläre Termin
    ds = svc(client).get(sid)
    assert "Grenze" in ds.sched["next_reason"] and at(ds, "next_run_at") >= clock() + timedelta(hours=23)
    last = [r["trigger"] for r in svc(client).runs(sid, 20)]
    assert last.count("catchup") == S.CATCHUP_MAX_RUNS and last[0] == "catchup" and last[-1] == "schedule"


def test_no_fast_continuation_without_progress(client, staged, clock):
    staged.total = 50
    sid = make(client, interval="60")
    set_catchup(client, sid, True)
    svc(client).sync(sid, "schedule")  # erste Etappe: Abrufstand rückt vor
    assert svc(client).get(sid).sched["next_kind"] == "catchup"
    staged.stuck = True
    clock.advance(seconds=S.BACKFILL_NEXT_S + 1)
    assert svc(client).run_due(wait=True)["ran"] == 1
    ds = svc(client).get(sid)
    assert ds.sched["next_kind"] == "schedule" and ds.sched["stalls"] == 1 and "kein Fortschritt" in ds.sched[
        "next_reason"]
    assert at(ds, "next_run_at") >= clock() + timedelta(minutes=59)
    for n in range(2, S.STALL_LIMIT + 1):  # wiederholt ohne Fortschritt: nur der reguläre Termin, nie eine Schleife
        clock.advance(hours=1, minutes=1)
        assert svc(client).run_due(wait=True)["ran"] == 1
        assert svc(client).get(sid).sched["stalls"] == n
    assert "wiederholt ohne Fortschritt" in svc(client).get(sid).sched["next_reason"]
    assert staged.calls == S.STALL_LIMIT + 1


def test_reported_stall_is_visible_with_its_reason(client, staged, clock):
    staged.total, staged.stall = 50, "Paginierung ohne Fortschritt (Testfall)"
    sid = make(client, interval="60")
    set_catchup(client, sid, True)
    svc(client).sync(sid, "schedule")
    ds = svc(client).get(sid)
    assert ds.sched["stalled"] and ds.sync_state[0] == "Abruf unvollständig – Stillstand erkannt"
    page = client.get(f"/settings/datasources/{sid}").text
    assert "Stillstand erkannt" in page and "Paginierung ohne Fortschritt (Testfall)" in page


def test_manual_never_starts_automatically(client, staged, clock):
    sid = make(client, interval="0")
    set_catchup(client, sid, True)  # auch mit „aufholen“: nur manuell bleibt nur manuell
    svc(client).sync(sid, "manual")
    ds = svc(client).get(sid)
    assert ds.backfill_pending and ds.row["next_run_at"] is None and "nur manuell" in ds.sched["next_reason"]
    clock.advance(days=30)
    assert svc(client).due() == [] and svc(client).run_due(wait=True)["ran"] == 0 and staged.calls == 1
    svc(client).sync(sid, "manual")  # manuell setzt am gespeicherten Fortsetzungspunkt fort
    assert staged.calls == 2 and source(client, sid)["cursor_json"] == '{"n": 4}'


def test_disabled_source_gets_no_new_runs(client, staged, clock):
    sid = make(client, interval="60")
    set_catchup(client, sid, True)
    svc(client).sync(sid, "schedule")
    assert svc(client).set_enabled(sid, False)
    assert source(client, sid)["next_run_at"] is None
    clock.advance(days=3)
    assert svc(client).due() == [] and svc(client).sync(sid, "catchup") == {"skipped": "deaktiviert"}
    assert svc(client).sync(sid, "schedule") == {"skipped": "deaktiviert"}


def test_disabling_during_a_run_prevents_the_fast_continuation(client, staged, clock, monkeypatch):
    staged.total = 50
    sid = make(client, interval="60")
    set_catchup(client, sid, True)
    orig = Staged.fetch

    def fetch_and_disable(self, cfg, secret, cursor):
        out = orig(self, cfg, secret, cursor)
        svc(client).set_enabled(sid, False)  # Nutzer deaktiviert während des Laufs
        return out

    monkeypatch.setattr(Staged, "fetch", fetch_and_disable)
    svc(client).sync(sid, "schedule")
    row = source(client, sid)
    assert row["enabled"] == 0 and row["next_run_at"] is None  # kein neu gesetzter Termin
    assert svc(client).get(sid).sched["next_kind"] is None and svc(client).due() == []


def test_cancel_prevents_an_immediate_continuation(client, staged, clock, monkeypatch):
    staged.total = 50
    sid = make(client, interval="60")
    set_catchup(client, sid, True)
    orig = Staged.fetch

    def fetch_then_cancel(self, cfg, secret, cursor):
        out = orig(self, cfg, secret, cursor)
        ev = K.current_cancel()
        assert ev is not None
        ev.set()  # Abbruch kommt, nachdem die Etappe abgerufen wurde
        return out

    monkeypatch.setattr(Staged, "fetch", fetch_then_cancel)
    svc(client).sync(sid, "schedule")
    ds = svc(client).get(sid)
    assert ds.sched["next_kind"] == "schedule" and "Abbruch" in ds.sched["next_reason"]
    assert at(ds, "next_run_at") == T0 + timedelta(hours=1)
    clock.advance(minutes=5)
    assert svc(client).due() == []


def test_cancelled_fetch_keeps_the_regular_schedule(client, staged, clock):
    staged.fail = K.ConnectorError("cancelled", K.CANCEL_TEXT)
    sid = make(client, interval="60")
    set_catchup(client, sid, True)
    out = svc(client).sync(sid, "schedule")
    assert out["cancelled"] and at(source(client, sid), "next_run_at") == T0 + timedelta(hours=1)


def test_restart_respects_the_stored_schedule(client, staged, clock, monkeypatch):
    sid = make(client)  # täglich
    orig = Staged.fetch

    def crash(self, cfg, secret, cursor):
        raise SystemExit("Container wird beendet")  # kein Exception-Zweig: der Lauf endet ohne Abschluss

    monkeypatch.setattr(Staged, "fetch", crash)
    with pytest.raises(SystemExit):
        svc(client).sync(sid, "schedule")
    monkeypatch.setattr(Staged, "fetch", orig)
    # nach dem Neustart: Termin steht bereits beim Start des abgebrochenen Laufs fest (Start + Intervall)
    assert at(source(client, sid), "next_run_at") == T0 + timedelta(days=1)
    clock.advance(minutes=10)
    assert svc(client).due() == [] and svc(client).run_due(wait=True)["ran"] == 0


def test_provider_wait_counts_from_the_end_of_a_long_run(client, staged, clock):
    staged.cost_s = 3 * 3600  # der Lauf dauert 3 Stunden, danach verlangt der Anbieter 1 Stunde Wartezeit
    staged.fail = K.ConnectorError("rate_limit", "Zu viele Anfragen.", retry_after_s=3600)
    sid = make(client, interval="60")
    svc(client).sync(sid, "schedule")
    row = source(client, sid)
    end = T0 + timedelta(hours=3)
    assert at(row, "last_run_at") == T0 and at(row, "next_run_at") == end + timedelta(hours=1)
    assert at(row, "next_run_at") > clock()  # nie ein bereits abgelaufener Folgetermin
    assert "Wartezeit" in svc(client).get(sid).sched["next_reason"]


def test_next_run_after_a_long_run_is_never_in_the_past(client, staged, clock):
    staged.cost_s = 2 * 3600  # Intervall 60 min, Lauf 2 h
    sid = make(client, interval="60")
    set_catchup(client, sid, False)
    svc(client).sync(sid, "schedule")
    nxt = at(source(client, sid), "next_run_at")
    assert nxt >= clock() + timedelta(seconds=S.MIN_GAP_S) and svc(client).due() == []


def test_error_never_triggers_a_quick_retry_even_with_catchup(client, staged, clock):
    staged.total = 50
    sid = make(client, interval="1440")
    set_catchup(client, sid, True)
    svc(client).sync(sid, "schedule")  # Etappe 1 (catchup folgt)
    assert svc(client).get(sid).sched["next_kind"] == "catchup"
    staged.fail = K.ConnectorError("unavailable", "Anbieter nicht erreichbar.")
    clock.advance(seconds=S.BACKFILL_NEXT_S + 1)
    assert svc(client).run_due(wait=True)["ran"] == 1
    ds = svc(client).get(sid)
    assert ds.sched["next_kind"] == "schedule" and ds.backfill_pending
    assert at(ds, "next_run_at") >= clock() + timedelta(hours=23)  # nicht 10 min, nicht 90 s


def test_manual_background_rounds_stop_without_progress_or_when_disabled(client, staged, clock):
    sid = make(client, interval="60")
    staged.total = 50
    svc(client).sync(sid, "manual")  # erste Etappe legt den Abrufstand an
    staged.stuck, staged.calls = True, 0
    ev = S._acquire(sid)
    assert ev is not None
    svc(client)._background(sid, "manual", ev)  # gibt die Sperre selbst frei
    assert staged.calls == 1  # kein Fortschritt → keine weiteren Etappen im selben Lauf
    staged.stuck, staged.calls = False, 0
    ev = S._acquire(sid)
    svc(client)._background(sid, "manual", ev)
    assert 1 < staged.calls <= S.MAX_ROUNDS and svc(client).get(sid).sched["next_kind"] != "catchup"
    # deaktiviert während des Laufs: keine weitere Etappe
    staged.total, staged.calls = 500, 0
    ev = S._acquire(sid)
    orig = svc(client)._sync

    def sync_then_disable(sid_, trigger, background=False):
        out = orig(sid_, trigger, background=background)
        svc(client).set_enabled(sid_, False)
        return out

    s = svc(client)
    s._sync = sync_then_disable  # type: ignore[method-assign]
    s._background(sid, "manual", ev)
    assert staged.calls == 1


def test_repeated_runs_do_not_double_book(client, staged, clock):
    staged.total, staged.step = 6, 2
    sid = make(client, interval="60")
    for _ in range(5):
        svc(client).sync(sid, "manual")
    keys = [r["event_key"] for r in client.app.state.ctx.db.q(
        "SELECT r.event_key FROM csv_row r JOIN csv_batch b ON b.id=r.batch_id WHERE b.datasource_id=?", (sid,))]
    assert len(keys) == 6 == len(set(keys))
    assert staged.total == 6 and not svc(client).get(sid).backfill_pending


def test_catchup_setting_defaults(client, staged):
    assert K.connector_for("kraken").catchup_default is True  # Börsen-Anbindungen: bisheriges Verhalten
    from app.datasources.chains import kaspa as KA
    assert KA.KaspaConnector.catchup_default is False  # Wallets: nur mit ausdrücklicher Einstellung
