"""Datenquellen laufen unabhängig: ein langsamer Abruf (z. B. Wallet mit Ratenlimit) blockiert andere Quellen nicht,
und jeder laufende Abruf lässt sich abbrechen – ohne Abrufstand oder Daten zu verändern. Fakes ohne Netz."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources import connector as K
from app.datasources import service as S
from app.datasources.service import datasource_service
from tests.test_datasources import KEY, client, create_source, deposit, fake, source  # noqa: F401


class SlowFake(K.Connector):
    """Abruf, der bis zur Freigabe läuft und dabei Fortschritt meldet (Prüfstelle für den Abbruch)."""

    provider = "coinbase"
    label = "Coinbase (Test, langsam)"
    needs_credentials = True
    started: ClassVar[threading.Event] = threading.Event()
    release: ClassVar[threading.Event] = threading.Event()
    calls: ClassVar[int] = 0

    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        return K.CheckResult(True, "ok")

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        SlowFake.calls += 1
        SlowFake.started.set()
        n = 0
        while not SlowFake.release.is_set():
            n += 1
            self.report("Abruf", n, None, "warte auf Anbieter")
            time.sleep(0.01)
        return K.FetchResult(events=[deposit("coinbase:C1", "2024-03-02T10:00:00", "EUR", "5")],
                             cursor={"n": 1}, complete=True)


@pytest.fixture
def slow(monkeypatch, fake):  # noqa: F811
    monkeypatch.setenv("PORTFOLIA_DS_COINBASE", "APIKEY-1234567890:SECRET-abcdefghij")
    SlowFake.started, SlowFake.release, SlowFake.calls = threading.Event(), threading.Event(), 0
    K.register(SlowFake)
    try:
        yield SlowFake
    finally:
        SlowFake.release.set()
        K.unregister("coinbase")


def wait_until(cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def two_sources(c) -> tuple[int, int]:
    a = create_source(c, provider="coinbase", name="Coinbase", account="Coinbase",
                      credential_ref="PORTFOLIA_DS_COINBASE")
    b = create_source(c)
    return a, b


def test_slow_source_does_not_block_other_sources(client, fake, slow):  # noqa: F811
    c = client
    svc = datasource_service(c.app.state.ctx)
    a, b = two_sources(c)
    fake.events = [deposit("kraken:L1", "2024-03-01T10:00:00", "EUR", "1000")]
    assert svc.start_sync(a)["started"] and slow.started.wait(5)
    assert S.is_busy(a) and svc.get(a).progress["running"]
    res = svc.sync(b)  # andere Quelle läuft sofort
    assert not res.get("error"), res
    assert source(c, b)["status"] == "synced" and not S.is_busy(b)
    assert svc.sync(a).get("busy") and svc.start_sync(a).get("busy")  # dieselbe Quelle nie doppelt
    slow.release.set()
    assert wait_until(lambda: not S.is_busy(a))
    assert source(c, a)["status"] == "synced" and slow.calls == 1


def test_cancel_running_sync_keeps_cursor_and_state(client, fake, slow):  # noqa: F811
    c = client
    svc = datasource_service(c.app.state.ctx)
    a, _b = two_sources(c)
    before = source(c, a)
    assert svc.start_sync(a)["started"] and slow.started.wait(5)
    r = c.post(f"/settings/datasources/{a}/cancel", data={"csrf_token": c.token, "back": "detail"},
               follow_redirects=False)
    assert r.status_code == 303 and f"/settings/datasources/{a}" in r.headers["location"]
    assert wait_until(lambda: not S.is_busy(a))
    row = source(c, a)
    p = svc.get(a).progress
    assert p["running"] is False and p["cancelled"] and not p["ok"] and "abgebrochen" in p["result"]
    assert row["cursor_json"] == before["cursor_json"]  # Abrufstand unverändert
    assert row["status"] != "error" and not row["last_error"]  # Abbruch ist kein Fehler der Quelle
    run = svc.runs(a)[0]
    assert run["status"] == "error" and "abgebrochen" in run["message"]
    assert not c.app.state.ctx.db.scalar("SELECT COUNT(*) FROM csv_row")  # nichts abgelegt
    # erneuter Lauf funktioniert normal
    slow.release.set()
    assert svc.sync(a).get("status") == "synced"


def test_cancel_clears_orphaned_running_display(client, fake):  # noqa: F811
    c = client
    svc = datasource_service(c.app.state.ctx)
    sid = create_source(c)
    svc._set_progress(sid, {"running": True, "stage": "Abruf", "done": 0, "total": None, "text": "",
                            "started_at": datetime.now(UTC).isoformat()}, force=True)
    assert svc.get(sid).progress["running"] and not S.is_busy(sid)  # z. B. nach Neustart des Containers
    assert "zurückgesetzt" in svc.cancel(sid)
    assert not svc.get(sid).progress["running"]
    assert svc.cancel(sid) == "Es läuft kein Abruf."


def test_scheduled_runs_start_independently(client, fake, slow):  # noqa: F811
    c = client
    svc = datasource_service(c.app.state.ctx)
    a, b = two_sources(c)
    fake.events = [deposit("kraken:L1", "2024-03-01T10:00:00", "EUR", "1000")]
    soon = datetime.now(UTC) + timedelta(seconds=5)
    assert {d.id for d in svc.due(soon)} == {a, b}
    t0 = time.monotonic()
    res = svc.run_due(wait=False)  # Zeitplan: nur starten
    assert time.monotonic() - t0 < 5 and res["ran"] == 2
    assert wait_until(lambda: source(c, b)["status"] == "synced")  # Börse fertig, während das Wallet noch läuft
    assert S.is_busy(a)
    assert svc.run_due(wait=False)["results"].get("Coinbase") in (None, "läuft bereits")
    svc.cancel(a)
    assert wait_until(lambda: not S.is_busy(a))


def test_batch_skips_source_running_separately_and_can_be_cancelled(client, fake, slow):  # noqa: F811
    c = client
    svc = datasource_service(c.app.state.ctx)
    a, b = two_sources(c)
    assert svc.start_sync(a)["started"] and slow.started.wait(5)
    assert svc.start_sync_many([a, b], "Test")["started"]
    assert wait_until(lambda: not svc.batch_progress().get("running"))
    st = svc.batch_progress()
    assert any("läuft bereits separat" in e for e in st.get("errors", []))
    assert source(c, b)["status"] == "synced"
    assert "Es läuft keine" in svc.cancel_many()
    svc.cancel(a)
    assert wait_until(lambda: not S.is_busy(a))


def test_http_client_stops_on_cancel_even_while_waiting():
    ev = threading.Event()
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        ev.set()  # Nutzer bricht während der Antwort ab
        return httpx.Response(429, headers={"retry-after": "30"})

    ep = next(iter(CH.ENDPOINTS.values()))
    with K.cancel_scope(ev):
        http = CH.ChainHttp(ep, key="k" * 32 if ep.key_required else None, transport=httpx.MockTransport(handler),
                            network=ep.networks[0] if getattr(ep, "networks", None) else None)
    t0 = time.monotonic()
    with pytest.raises(K.ConnectorError) as e:
        http.get("", what="Test")
    assert e.value.kind == "cancelled" and len(calls) == 1 and time.monotonic() - t0 < 5
    # ohne Abbruchsignal: normales Verhalten
    assert K.current_cancel() is None
    K.check_cancel()
