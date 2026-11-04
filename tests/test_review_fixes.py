"""Regressionstests zum Review „Buchungen prüfen“ (2026-11-03, Befunde R01–R14, Verbesserungen V01–V05).

Jeder Test prüft das fachlich gewünschte Verhalten (Soll) – nicht das im Review reproduzierte Fehlerbild. Vor der
jeweiligen Korrektur schlagen sie aus dem dort beschriebenen Grund fehl (Nachweis siehe
``docs/BUCHUNGEN_PRUEFEN_KORREKTUREN.md``). Alle Daten sind synthetisch; Anbieterantworten kommen aus den Fixtures.
"""

# ruff: noqa: F811  (Fixtures aus anderen Testmodulen werden importiert und als Argumente verwendet)
from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from lxml import html

from app.config import Config, Secrets
from app.csvimport import batch as BA
from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from app.diagnosis import guide as G
from tests.helpers import ASSETS, tx
from tests.test_bitpanda import api, ctx, master  # noqa: F401  (Fixtures)
from tests.test_diagnosis import PF_HEADER, client_with_import, upload
from tests.test_takeover import holdings, ids, staged, started, sync_txs


@pytest.fixture
def cfg(tmp_path):
    imp = tmp_path / "import"
    imp.mkdir()
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


@pytest.fixture
def c(cfg):
    rows = [tx("D1", "2025-01-01T10:00:00Z", "deposit", to=("Wallet K", "KAS", "10"), value="1"),
            tx("D2", "2025-01-01T10:00:00Z", "deposit", to=("Wallet K", "KAS", "10"), value="1")]
    client = client_with_import(cfg, rows, ASSETS)
    try:
        yield client
    finally:
        client.__exit__(None, None, None)


def post(c, url, values):
    flat = [("csrf_token", c.token)]
    for k, v in values.items():
        flat.extend((k, str(x)) for x in (v if isinstance(v, list) else [v]))
    return c.post(url, content=urlencode(flat), headers={"content-type": "application/x-www-form-urlencoded"},
                  follow_redirects=False)


def form_values(page: str, action: str = "/quality/assistent/antwort") -> dict[str, list[str]]:
    """Werte eines gerenderten Formulars wie ein Browser (gewählte Radios/Checkboxen, erste bzw. gewählte Option)."""
    form = html.fromstring(page).xpath(f'//form[@action="{action}"]')[0]
    out: dict[str, list[str]] = {}
    for el in form.xpath(".//input[@name] | .//select[@name] | .//textarea[@name]"):
        if el.get("disabled") is not None:
            continue
        name = el.get("name")
        if el.tag == "select":
            opt = el.xpath("./option[@selected]") or el.xpath("./option")[:1]
            out.setdefault(name, []).append(opt[0].get("value", "") if opt else "")
        elif el.get("type") in ("radio", "checkbox"):
            if el.get("checked") is not None:
                out.setdefault(name, []).append(el.get("value", "on"))
        else:
            out.setdefault(name, []).append(el.get("value", ""))
    return out


def economic_state(c) -> tuple:
    """Wirtschaftlicher Stand: aktive Buchungen, Bestände je Konto, realisierte Ergebnisse/Erträge je Jahr."""
    return ids(c), holdings(c), TK._years(ctx(c))


# ---- R04 · Übernahme: Fehler nach jeder Schreibphase hinterlässt keinen Teilzustand ---------------------------------

class InlineThread:
    def __init__(self, *, target, **kw):
        self.target = target

    def start(self):
        self.target()


@pytest.mark.parametrize("phase", ["hidden", "batch", "booked", "check"])
def test_R04_failure_after_any_write_phase_restores_the_old_state(config, master, api, monkeypatch, phase):
    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        before = economic_state(c)
        monkeypatch.setattr(TK, "_FAULTS", {phase})
        monkeypatch.setattr(TK.threading, "Thread", InlineThread)
        TK.start_apply(ctx(c), sid, a.token)  # Hintergrundlauf (hier sofort ausgeführt)
        last = TK.latest(ctx(c).db, sid)
        assert last["status"] == "staged" and "Unerwarteter Fehler" in last["message"], dict(last)
        assert "Zurückgenommen" in last["message"]
        assert economic_state(c) == before and not sync_txs(c)
        assert not (last["decision_id"] or last["action_id"] or last["batch_id"])
        assert TK.needs_recovery(ctx(c).db, sid) is None
        monkeypatch.setattr(TK, "_FAULTS", set())
        assert TK.apply(ctx(c), sid, TK.assess(ctx(c), sid).token)["errors"] == []  # danach normal übernehmbar
    finally:
        c.__exit__(None, None, None)


def test_R04_failed_compensation_keeps_references_and_offers_recovery(config, master, api, monkeypatch):
    from app.diagnosis import actions as A

    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        before = economic_state(c)
        real_undo = A.undo
        monkeypatch.setattr(TK, "_FAULTS", {"booked"})
        monkeypatch.setattr(A, "undo", lambda ctx_, d: A.Result(errors=["Sperre (synthetisch)"]))
        res = TK.apply(ctx(c), sid, a.token)
        assert res["errors"] and "Rücknahme unvollständig" in res["errors"][0]
        last = TK.latest(ctx(c).db, sid)
        assert last["status"] == "failed" and last["decision_id"] and not last["action_id"]  # Buchungen schon zurück
        assert TK.needs_recovery(ctx(c).db, sid) is not None
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Unterbrochene Übernahme zurücknehmen" in page and f"Ausblendung #{last['decision_id']}" in page
        assert TK.start_apply(ctx(c), sid, a.token)["errors"] == [TK.RECOVER_FIRST]  # kein neuer Lauf darüber
        assert TK.start_fetch(ctx(c), sid)["errors"] == [TK.RECOVER_FIRST]
        monkeypatch.setattr(A, "undo", real_undo)
        monkeypatch.setattr(TK, "_FAULTS", set())
        r = c.post(f"/settings/datasources/{sid}/uebernahme/wiederherstellen", data={"csrf_token": c.token},
                   follow_redirects=False)
        assert r.status_code == 303 and "msg=" in r.headers["location"]
        assert economic_state(c) == before and TK.latest(ctx(c).db, sid)["status"] == "staged"
        assert TK.needs_recovery(ctx(c).db, sid) is None
    finally:
        c.__exit__(None, None, None)


class ProcessDied(BaseException):
    """Simuliert einen Prozessabbruch (kein ``Exception`` – wird von keinem Fehlerpfad abgefangen)."""


def test_R04_interrupted_process_leaves_a_visible_recovery_path(config, master, api, monkeypatch):
    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        before = economic_state(c)

        def die(phase):
            if phase == "booked":
                raise ProcessDied

        monkeypatch.setattr(TK, "_fault", die)
        with pytest.raises(ProcessDied):
            TK.apply(ctx(c), sid, a.token)
        last = TK.latest(ctx(c).db, sid)
        assert last["status"] == "running" and last["decision_id"] and last["action_id"] and last["batch_id"]
        assert economic_state(c) != before  # Teilzustand gespeichert …
        v = TK.view(ctx(c).db, sid)
        assert v["recover"] and "Unterbrochene Übernahme zurücknehmen" in v["message"]  # … mit Rückweg
        assert TK.recover(ctx(c), sid)["errors"] == []
        assert economic_state(c) == before and not sync_txs(c)
        assert TK.recover(ctx(c), sid)["errors"] == ["Keine unterbrochene Übernahme gespeichert."]  # idempotent
    finally:
        c.__exit__(None, None, None)


# ---- R09 · Vorschau gilt genau für die gezeigten Buchungswerte ------------------------------------------------------

def _new_row_batch(c) -> int:
    return upload(c, "new.csv", PF_HEADER + "N1,2025-03-12T12:00:00Z,deposit,,,,,Wallet K,KAS,3.5,,,,1,new\n")


@pytest.mark.parametrize(("field", "value"), [("val_0", "999"), ("fee_0", "0.5"), ("dec_0", "skip")])
def test_R09_changed_input_invalidates_an_old_preview(c, field, value):
    cx = c.app.state.ctx
    svc = csv_service(cx)
    bid = _new_row_batch(c)
    old = BA.plan(svc, bid, "include", [0]).fingerprint()
    svc.set_rows(bid, {field: value})
    assert BA.plan(svc, bid, "include", [0]).fingerprint() != old
    res = BA.execute(svc, bid, "include", [0], token=BA.new_token(), fingerprint=old)
    assert res["errors"] and "nicht mehr aktuell" in res["errors"][0]
    assert not cx.db.q("SELECT 1 FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))


def test_R09_changed_asset_mapping_invalidates_preview_and_fresh_preview_runs_once(c):
    cx = c.app.state.ctx
    svc = csv_service(cx)
    bid = upload(c, "new.csv", PF_HEADER + "N1,2025-03-12T12:00:00Z,deposit,,,,,Wallet K,FOO,3.5,,,,1,new\n")
    svc.set_symbol("FOO", "KAS")
    svc.evaluate(bid)
    old = BA.plan(svc, bid, "include", [0]).fingerprint()
    svc.set_symbol("FOO", "BTC")  # anderes Asset nach der Vorschau
    svc.evaluate(bid)
    assert BA.execute(svc, bid, "include", [0], token=BA.new_token(), fingerprint=old)["errors"]
    fresh, token = BA.plan(svc, bid, "include", [0]).fingerprint(), BA.new_token()
    first = BA.execute(svc, bid, "include", [0], token=token, fingerprint=fresh)
    again = BA.execute(svc, bid, "include", [0], token=token, fingerprint=fresh)  # Doppelklick
    assert first["errors"] == [] and again.get("repeat") and again["action_id"] == first["action_id"]
    assert cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,)) == 1
    assert cx.db.scalar("SELECT to_asset FROM journal_tx WHERE batch_id=? AND status='active'", (bid,)) == "BTC"


def test_R09_assistant_bulk_answer_is_bound_to_the_shown_values(c, monkeypatch):
    """Karte „Neue Vorgänge“ (Sammelübernahme): die mitgesendete Vorschau gilt nur für die gezeigten Werte."""
    cx = c.app.state.ctx
    svc = csv_service(cx)
    bid = _new_row_batch(c)
    task = next(t for t in G.tasks(cx) if t.kind == "stapel")
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [task])
    shown = form_values(c.get("/quality/assistent?new=1").text)
    assert shown["a"] == ["stk:all"] and shown["x_parts"][0] != "[]"
    svc.set_rows(bid, {"val_0": "999"})  # in einem zweiten Tab geändert
    r = post(c, "/quality/assistent/antwort", shown)
    assert "nicht mehr aktuell" in parse_qs(urlsplit(r.headers["location"]).query).get("err", [""])[0]
    assert not cx.db.q("SELECT 1 FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))
    fresh = form_values(c.get("/quality/assistent?new=1").text)
    assert "err=" not in post(c, "/quality/assistent/antwort", fresh).headers["location"]
    assert cx.db.scalar("SELECT value_eur FROM journal_tx WHERE batch_id=? AND status='active'", (bid,)) == "999"


def test_R09_assistant_row_answer_is_bound_to_the_shown_values(c, monkeypatch):
    """Einzelkarte (möglicherweise schon gebucht): „zusätzlich buchen“ gilt nur für den angezeigten Stand."""
    cx = c.app.state.ctx
    svc = csv_service(cx)
    bid = upload(c, "dup.csv", PF_HEADER + "N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n")
    task = next(t for t in G.tasks(cx) if t.kind == "zeile" and t.ref.startswith(f"{bid}:"))
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [task])
    shown = form_values(c.get("/quality/assistent?new=1").text)
    assert shown["x_fp"][0]
    shown["a"] = ["row:include"]
    svc.set_rows(bid, {"val_0": "999"})
    r = post(c, "/quality/assistent/antwort", shown)
    assert "seit der Anzeige geändert" in parse_qs(urlsplit(r.headers["location"]).query).get("err", [""])[0]
    assert not cx.db.q("SELECT 1 FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))
    fresh = form_values(c.get("/quality/assistent?new=1").text)
    fresh["a"] = ["row:include"]
    assert "err=" not in post(c, "/quality/assistent/antwort", fresh).headers["location"]
    assert cx.db.scalar("SELECT value_eur FROM journal_tx WHERE batch_id=? AND status='active'", (bid,)) == "999"
