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


# ---- R13 · Antworten mit Seitenwechsel: Navigation und „Später“ brauchen nur den Aufgabenbezug --------------------

def _page_form(c) -> dict[str, list[str]]:
    shown = form_values(c.get("/quality/assistent?new=1").text)
    assert shown["typ"] == ["seite"]
    return shown


def test_R13_rendered_page_task_opens_its_page(c):
    cx = c.app.state.ctx
    task = next(t for t in G.tasks(cx) if t.kind == "seite")
    shown = _page_form(c)
    assert shown["a"] == ["go:open"] and shown["kid"] == [task.kid]  # empfohlene Antwort ist vorausgewählt
    r = post(c, "/quality/assistent/antwort", shown)
    loc = r.headers["location"]
    assert r.status_code == 303 and loc.startswith(task.href) and parse_qs(urlsplit(loc).query)["kid"] == [task.kid]
    assert c.get(loc).status_code == 200


def test_R13_rendered_page_task_can_be_postponed(c):
    cx = c.app.state.ctx
    task = next(t for t in G.tasks(cx) if t.kind == "seite")
    shown = _page_form(c)
    shown["a"] = ["skip"]
    r = post(c, "/quality/assistent/antwort", shown)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert "err" not in q and q["skip"] == [task.kid]
    assert f'value="{task.kid}"' not in c.get(r.headers["location"]).text  # für diesen Durchgang übersprungen


def test_R13_case_downgraded_to_page_task_still_opens(c, monkeypatch):
    """Ein Klärungsfall mit vorbereiteter Lösung, dessen Lösung nicht (mehr) direkt anwendbar ist, wird zur
    Seitenaufgabe zurückgestuft – auch dann führt die empfohlene Antwort zur Seite."""
    from app.diagnosis import workplan as WP

    real = WP.build

    def build(*a, **kw):
        plan = real(*a, **kw)
        for it in plan.items:
            if it.kind == "fall":
                it.cta = "Lösung prüfen"
        return plan

    monkeypatch.setattr(WP, "build", build)
    cx = c.app.state.ctx
    task = next(t for t in G.tasks(cx) if t.kind == "fall")
    shown = _page_form(c)  # gerendert als „seite“ (Rückstufung in _case_detail)
    r = post(c, "/quality/assistent/antwort", shown)
    assert r.headers["location"].startswith(task.href) and "err=" not in r.headers["location"]


def test_R13_navigation_without_task_reference_is_rejected_visibly(c):
    r = post(c, "/quality/assistent/antwort", {"typ": "seite", "a": "go:open", "kid": ""})
    assert "nicht mehr" in parse_qs(urlsplit(r.headers["location"]).query)["err"][0]
    r = post(c, "/quality/assistent/antwort", {"typ": "seite", "a": "go:open", "kid": "unbekannt"})
    assert "nicht mehr" in parse_qs(urlsplit(r.headers["location"]).query)["err"][0]


# ---- R14 · Lange Ereigniskennungen öffnen genau ihren Vorgang ------------------------------------------------------

def _solana_batch(c, sigs: list[str]) -> tuple[int, list[str]]:
    from datetime import UTC, datetime
    from decimal import Decimal
    from types import SimpleNamespace

    from app.csvimport.model import Rec
    from app.datasources import service as DS
    from app.datasources.connector import FetchResult
    from app.datasources.wallet import event as chain_event

    cx = c.app.state.ctx
    evs = []
    for i, sig in enumerate(sigs):
        ts = datetime(2025, 3, 12, 12, i, tzinfo=UTC)
        rec = Rec(line=1, ts=ts, kind="deposit", in_sym="BTC", in_qty=Decimal("0.003") + i, account="Wallet SOL",
                  value=Decimal("150"), value_ccy="EUR")
        evs.append(chain_event("solana", sig, "7" * 44, ts, [rec], "Synthetische Einzahlung"))
    recs = DS.datasource_service(cx)._normalize(
        SimpleNamespace(is_wallet=True, provider="solana", account="Wallet SOL"), FetchResult(events=evs))
    bid = csv_service(cx).ingest(recs, source="sync:solana", profile="sync:solana", account="Wallet SOL",
                                 label="Synthetische Solana-Historie", datasource_id=None, payload=b"[]",
                                 options={"cutoff": ""})
    return bid, [e.event_key for e in evs]


def _shown_rows(page: str) -> int:
    return page.count('data-label="Datum"')


def test_R14_long_event_id_opens_exactly_its_row(c):
    bid, (key,) = _solana_batch(c, ["5" * 88])
    assert len(key) > 80
    assert BA.Filters.parse({"ev": key}).ev == key
    page = c.get(f"/journal/csv/{bid}?" + urlencode({"ev": key})).text
    assert _shown_rows(page) == 1 and "Keine Zeilen" not in page and "Gezeigt wird ein einzelner Vorgang" in page


def test_R14_ids_with_same_80_char_prefix_stay_distinguishable(c):
    bid, keys = _solana_batch(c, ["5" * 87 + "A", "5" * 87 + "B"])
    assert keys[0][:80] == keys[1][:80] and keys[0] != keys[1]
    rows = csv_service(c.app.state.ctx).rows(bid)
    for key in keys:
        flt = BA.Filters.parse({"ev": key})
        assert [rc.rec.event_key for rc in rows if flt.test(rc)] == [key]
        assert _shown_rows(c.get(f"/journal/csv/{bid}?" + urlencode({"ev": key})).text) == 1
    # über die Formular-Rundreise (versteckte Felder „_ev“) bleibt die vollständige ID erhalten
    assert BA.Filters.parse({"_ev": keys[1]}).ev == keys[1]


def test_R14_short_and_row_ids_open_their_row(c):
    cx = c.app.state.ctx
    bid = _new_row_batch(c)
    rc = csv_service(cx).rows(bid)[0]
    ev = rc.rec.event_key or f"#{rc.idx}"
    assert _shown_rows(c.get(f"/journal/csv/{bid}?" + urlencode({"ev": ev})).text) == 1


@pytest.mark.parametrize("bad", ["solana:" + "5" * 300, "solana:abc def", "x\u0000y"])
def test_R14_invalid_event_id_is_rejected_not_truncated(c, bad):
    bid, _ = _solana_batch(c, ["5" * 88])
    flt = BA.Filters.parse({"ev": bad})
    assert flt.ev == "" and flt.rejected
    page = c.get(f"/journal/csv/{bid}?" + urlencode({"ev": bad})).text
    assert "Vorgangskennung im Link ist ungültig" in page


# ---- V01 · Aus der Aufgabe direkt zum Einzelvorgang, mit Rückweg ---------------------------------------------------

def test_V01_row_task_jumps_to_its_single_event_and_keeps_the_way_back(c, monkeypatch):
    cx = c.app.state.ctx
    bid = upload(c, "inv.csv", PF_HEADER + "N1,2025-03-12T12:00:00Z,deposit,,,,,Wallet K,ZZZUNBEKANNT,3.5,,,,,x\n")
    task = next(t for t in G.tasks(cx) if t.kind == "zeile" and t.ref.startswith(f"{bid}:"))
    ev = task.ref.partition(":")[2]
    assert parse_qs(urlsplit(task.href).query)["ev"] == [ev] and task.href.endswith("#zeilen")
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [task])
    shown = form_values(c.get("/quality/assistent?new=1").text)
    shown["a"] = ["go:open"]
    loc = post(c, "/quality/assistent/antwort", shown).headers["location"]
    q = parse_qs(urlsplit(loc).query)
    assert q["ev"] == [ev] and q["kid"] == [task.kid]
    page = c.get(loc).text
    assert _shown_rows(page) == 1 and "Zurück zu Buchungen prüfen" in page
    assert f'name="kid" value="{task.kid}"' in page and f'name="ev" value="{ev}"' in page  # Filterformular


# ---- R01 · Leere Mehrfachauswahl bleibt leer · V05 · Fehler behalten Aufgabe, Antwort und Eingaben --------------

def _question_task(c, prefix: str):
    from app.diagnosis.engine import report_for

    cx = c.app.state.ctx
    f = next(f for f in report_for(cx).findings if f.key.startswith(prefix))
    return f, G.Task(key="befund:" + f.id, theme="bestand", kind="frage", heading="", finding_id=f.id, ref=f.id)


def test_R01_unchecking_every_pair_is_rejected_and_stays_empty(c, monkeypatch):
    cx = c.app.state.ctx
    _, t = _question_task(c, "ident|")
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [t])
    shown = form_values(c.get("/quality/assistent?new=1").text)
    field = "ap.opt:hide_second.pairs"
    assert shown[field] == ["D1|D2"]  # Vorgabe angehakt
    shown["a"] = ["opt:hide_second"]
    del shown[field]  # Browser schickt abgewählte Checkboxen nicht
    before, state = cx.db.scalar("SELECT COUNT(*) FROM diag_decision"), economic_state(c)
    r = post(c, "/quality/assistent/antwort", shown)
    assert r.status_code == 200 and "Bitte mindestens einen Eintrag auswählen." in r.text
    assert "Das ändert sich mit deiner Antwort" not in r.text
    again = form_values(r.text)
    assert field not in again  # bleibt leer, keine Vorgabe eingesetzt
    assert again["a"] == ["opt:hide_second"] and again["kid"] == [t.kid]  # V05: Aufgabe und Antwort bleiben
    assert cx.db.scalar("SELECT COUNT(*) FROM diag_decision") == before and economic_state(c) == state


def test_V05_redirected_error_keeps_task_and_answer(c, monkeypatch):
    """Fehler, die über eine Weiterleitung gemeldet werden (z. B. Vorschau veraltet), zeigen dieselbe Aufgabe mit
    derselben gewählten Antwort – nicht die nächste Aufgabe."""
    cx = c.app.state.ctx
    svc = csv_service(cx)
    bid = _new_row_batch(c)
    stack = next(t for t in G.tasks(cx) if t.kind == "stapel")
    other = next(t for t in G.tasks(cx) if t.kind == "seite")
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [other, stack])
    page = c.get("/quality/assistent?new=1&" + urlencode({"skip": other.kid})).text
    shown = form_values(page)
    assert shown["kid"] == [stack.kid]
    svc.set_rows(bid, {"val_0": "999"})
    loc = post(c, "/quality/assistent/antwort", shown).headers["location"]
    q = parse_qs(urlsplit(loc).query)
    assert q["cur"] == [stack.kid] and q["ch"] == ["stk:all"]
    c.cookies.delete("dq_skip")  # auch ohne Überspringen-Cookie (andere Aufgabe stünde vorn): dieselbe Aufgabe
    page = c.get(loc).text
    assert "nicht mehr aktuell" in page and form_values(page)["kid"] == [stack.kid]


# ---- R11 · Eingaben gelten nur für die gewählte Antwort -----------------------------------------------------------

from tests.test_chainscan_diag import client as chain_client  # noqa: E402,F401  (Fixture)
from tests.test_chainscan_diag import holdings as chain_holding  # noqa: E402


def _chain_form(chain_client, monkeypatch):
    cx = chain_client.app.state.ctx
    _, f = chain_holding(cx)
    t = G.Task(key="befund:" + f.id, theme="bestand", kind="frage", heading="", finding_id=f.id, ref=f.id)
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [t])
    return cx, form_values(chain_client.get("/quality/assistent?new=1").text)


def test_R11_inputs_of_the_unchosen_answer_have_no_effect(chain_client, monkeypatch):
    c = chain_client
    cx, shown = _chain_form(c, monkeypatch)
    assert "ap.opt:chain_steps.tag" in shown and "ap.opt:adjust.tag" in shown and "p_tag" not in shown
    shown["ap.opt:chain_steps.tag"] = ["reward"]  # erst beim Hauptvorschlag „Belohnung“ gewählt …
    shown["a"] = ["opt:adjust"]  # … dann zur Ausgleichsbuchung „Zugang ohne Ertrag“ gewechselt
    shown["ap.opt:adjust.tag"] = [""]
    shown["ap.opt:adjust.value"] = ["123"]
    r = post(c, "/quality/assistent/antwort", shown)
    assert r.status_code == 200 and "Das ändert sich mit deiner Antwort" in r.text
    assert "Zugang ohne Ertrag" in r.text and "Deine Angaben" in r.text  # Einordnung sichtbar
    confirm = form_values(r.text)
    assert confirm["ap.opt:adjust.tag"] == [""] and not any(k.startswith("ap.opt:chain_steps") for k in confirm)
    done = post(c, "/quality/assistent/antwort", confirm)
    assert "done=" in done.headers["location"], done.headers["location"]
    tag = cx.db.scalar("SELECT tag FROM journal_tx WHERE source='diagnose' AND status='active' ORDER BY id DESC "
                       "LIMIT 1")
    assert tag in ("", None)  # kein steuerpflichtiger Ertrag


def test_R11_scalar_input_with_several_values_is_rejected(chain_client, monkeypatch):
    c = chain_client
    cx, shown = _chain_form(c, monkeypatch)
    shown["a"] = ["opt:adjust"]
    shown["ap.opt:adjust.tag"] = ["", "reward"]
    shown["ap.opt:adjust.value"] = ["123"]
    before = cx.db.scalar("SELECT COUNT(*) FROM diag_decision")
    r = post(c, "/quality/assistent/antwort", shown)
    assert r.status_code == 200 and "mehrere Werte" in r.text
    assert cx.db.scalar("SELECT COUNT(*) FROM diag_decision") == before


def test_R11_legacy_shared_field_names_are_ignored(chain_client, monkeypatch):
    """Alte, antwortübergreifende Feldnamen (p_tag) wirken nicht mehr – nur der Namensraum der Antwort zählt."""
    c = chain_client
    _, shown = _chain_form(c, monkeypatch)
    shown["a"] = ["opt:adjust"]
    shown["ap.opt:adjust.tag"] = [""]
    shown["ap.opt:adjust.value"] = ["123"]
    shown["p_tag"] = ["reward"]
    r = post(c, "/quality/assistent/antwort", shown)
    assert form_values(r.text)["ap.opt:adjust.tag"] == [""]


# ---- R03 · Vorschau prüft denselben vollständigen Plan wie die Übernahme ----------------------------------------

def _refusing_rows():
    return [tx("K1", "2024-02-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "500"), value="500"),
            tx("K2", "2024-02-02T09:01:00Z", "buy", frm=("Bitpanda", "EUR", "250"), to=("Bitpanda", "BTC", "0.0055"),
               value="251.5"),
            tx("K3", "2024-02-12T12:00:00Z", "withdrawal", frm=("Bitpanda", "BTC", "0.001"), value="55"),
            tx("L1", "2024-02-12T12:30:00Z", "deposit", to=("Ledger", "BTC", "0.001"), value="55")]


def test_R03_preview_shows_the_blocker_before_any_write_and_offers_a_decision(config, master, api):
    from app.datasources import service as DS

    c, sid = started(config, api, rows=_refusing_rows())
    try:
        state = economic_state(c)
        a = staged(c, sid)
        assert not a.ok and [x.key for x in a.blockers] == ["rows"]  # vor jeder Änderung als blockiert erkennbar
        assert "Einzelentscheidung" in a.blockers[0].label and "withdrawal" in a.blockers[0].detail
        assert economic_state(c) == state and not sync_txs(c)  # der Probelauf ändert nichts
        assert not DS.datasource_service(ctx(c)).pending_batches(sid)
        res = TK.apply(ctx(c), sid, a.token)  # wird ohne Schreiben abgelehnt
        assert res["errors"] and economic_state(c) == state
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Jetzt entscheiden" in page and "In den Prüfstapel abrufen" not in page
        calls = len(api.calls) if hasattr(api, "calls") else None
        r = post(c, f"/settings/datasources/{sid}/uebernahme/pruefstapel", {})
        loc = r.headers["location"]
        assert r.status_code == 303 and loc.startswith("/journal/csv/") and "ev=" in loc
        if calls is not None:
            assert len(api.calls) == calls  # kein neuer Abruf beim Anbieter
        detail = c.get(loc).text
        assert detail.count('data-label="Datum"') == 1
        # Entscheidung, die auch im Übernahmekontext gilt: Vorgang dauerhaft ignorieren → Plan wird neu bewertet
        bid = int(urlsplit(loc).path.rsplit("/", 1)[1])
        svc = csv_service(ctx(c))
        (rc,) = [x for x in svc.rows(bid) if x.open]
        assert svc.set_ignored(bid, rc.rec.event_key, True, "Test")
        a2 = TK.assess(ctx(c), sid)
        assert "rows" not in [x.key for x in a2.blockers]  # nicht mehr „Einzelentscheidung“ …
        assert not a2.ok and economic_state(c) == state  # … aber ohne die Auszahlung stimmt der Bestand nicht
        # erneut anfordern legt nichts doppelt ab
        again = post(c, f"/settings/datasources/{sid}/uebernahme/pruefstapel", {})
        assert again.status_code == 303
        assert ctx(c).db.scalar("SELECT COUNT(*) FROM csv_row WHERE event_key=?", (rc.rec.event_key,)) == 1
    finally:
        c.__exit__(None, None, None)


def test_R03_probe_is_cached_per_preview_and_data_state(config, master, api, monkeypatch):
    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        assert a.ok and any(x.key == "probe" and x.ok for x in a.checks)
        runs = []
        real = TK._apply_steps
        monkeypatch.setattr(TK, "_apply_steps", lambda *x, **k: runs.append(1) or real(*x, **k))
        TK.assess(ctx(c), sid)
        assert runs == []  # gleicher Stand: kein zweiter Probelauf
        state = economic_state(c)
        assert TK.apply(ctx(c), sid, a.token)["errors"] == []
        assert runs == [1] and economic_state(c) != state  # nur die echte Übernahme
    finally:
        c.__exit__(None, None, None)


# ---- R10 · Unerklärte Zählungsdifferenz blockiert ----------------------------------------------------------------

def _bump_count(c, sid, delta: int, skipped: dict | None = None):
    import json

    row = TK.latest(ctx(c).db, sid)
    meta = json.loads(row["meta_json"])
    meta["coverage"]["operations"] += delta
    if skipped is not None:
        meta["skipped"] = skipped
    ctx(c).db.x("UPDATE ds_takeover SET meta_json=? WHERE id=?", (json.dumps(meta), row["id"]))


def test_R10_unexplained_count_gap_blocks(config, master, api):
    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        assert a.ok and a.counts["state"] == "ok"
        _bump_count(c, sid, +1)
        a = TK.assess(ctx(c), sid)
        assert a.counts["gap"] == 1 and not a.ok and [x.key for x in a.blockers] == ["count"]
        state = economic_state(c)
        assert TK.apply(ctx(c), sid, a.token)["errors"] and economic_state(c) == state
    finally:
        c.__exit__(None, None, None)


def test_R10_documented_skips_and_multiline_events_are_counted_correctly(config, master, api):
    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        assert a.lines >= a.events  # Mehrzeilenereignisse zählen als ein Vorgang
        _bump_count(c, sid, +2, {"Bitpanda: interne Umbuchung zwischen Bitpanda-Wallets": 2})
        a = TK.assess(ctx(c), sid)
        assert a.counts["state"] == "ok" and a.ok, a.blockers  # begründete Auslassungen erklären die Differenz
    finally:
        c.__exit__(None, None, None)


def test_R10_unknown_count_is_unknown_not_complete(config, master, api):
    import json

    c, sid = started(config, api)
    try:
        staged(c, sid)
        row = TK.latest(ctx(c).db, sid)
        meta = json.loads(row["meta_json"])
        meta["coverage"].pop("operations", None)
        ctx(c).db.x("UPDATE ds_takeover SET meta_json=? WHERE id=?", (json.dumps(meta), row["id"]))
        a = TK.assess(ctx(c), sid)
        chk = next(x for x in a.checks if x.key == "count")
        assert a.counts["state"] == "unknown" and not chk.ok and not chk.blocking and "unbekannt" in chk.detail
    finally:
        c.__exit__(None, None, None)


# ---- R02 · Ungeklärte Transfers haben einen ausführbaren Weg -----------------------------------------------------

from tests.test_transfer_ambiguity import T0, T1, line, pairs, sheet  # noqa: E402


def _xfer_task(c, bid, monkeypatch):
    cx = c.app.state.ctx
    task = next(t for t in G.tasks(cx) if t.kind == "zeile" and t.ref.startswith(f"{bid}:")
                and any(rc.pair_ref for rc in csv_service(cx).rows(bid)
                        if (rc.rec.event_key or f"#{rc.idx}") == t.ref.partition(":")[2]))
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [task])
    return task, form_values(c.get("/quality/assistent?new=1").text)


def _xfer_sheet(c):
    return pairs(c, sheet(line(T0, "withdrawal", wallet="Börse U", qty="1"),
                          line(T1, "deposit", wallet="Wallet W", qty="0.999")))


def test_R02_unclear_transfer_offers_only_executable_answers(c, monkeypatch):
    out = _xfer_sheet(c)
    assert all(r.transfer_unclear for r in out["rows"])
    task, _ = _xfer_task(c, out["bid"], monkeypatch)
    ids_ = [a.id for a in G.detail(c.app.state.ctx, task).answers]
    assert "row:include" not in ids_  # wäre an der Schutzprüfung gescheitert
    assert {"row:pair", "row:nopair", "go:open", "row:defer"} <= set(ids_)


def test_R02_confirming_the_transfer_books_both_sides_together(c, monkeypatch):
    cx = c.app.state.ctx
    out = _xfer_sheet(c)
    _task, shown = _xfer_task(c, out["bid"], monkeypatch)
    shown["a"] = ["row:pair"]
    loc = post(c, "/quality/assistent/antwort", shown).headers["location"]
    assert "err=" not in loc, loc
    rows = csv_service(cx).rows(out["bid"])
    assert all(not rc.open for rc in rows) and any(rc.status == "merged" or rc.status == "committed" for rc in rows)
    n = cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (out["bid"],))
    assert n == 1  # ein Transfer, keine zwei Einzelbuchungen


def test_R02_rejecting_the_transfer_books_this_side_alone(c, monkeypatch):
    cx = c.app.state.ctx
    out = _xfer_sheet(c)
    _, shown = _xfer_task(c, out["bid"], monkeypatch)
    shown["a"] = ["row:nopair"]
    loc = post(c, "/quality/assistent/antwort", shown).headers["location"]
    assert "err=" not in loc, loc
    rows = csv_service(cx).rows(out["bid"])
    booked = [rc for rc in rows if not rc.open]
    left = [rc for rc in rows if rc.open]
    assert len(booked) == 1 and booked[0].status == "committed" and booked[0].pair_ok == 0
    assert len(left) == 1 and not left[0].transfer_unclear  # die Gegenseite bleibt als eigene, klare Frage offen


def test_R02_deferring_and_detail_view_are_complete_paths(c, monkeypatch):
    cx = c.app.state.ctx
    out = _xfer_sheet(c)
    _, shown = _xfer_task(c, out["bid"], monkeypatch)
    opened = dict(shown, a=["go:open"])
    loc = post(c, "/quality/assistent/antwort", opened).headers["location"]
    assert loc.startswith(f"/journal/csv/{out['bid']}?ev=")
    page = c.get(loc).text
    assert 'name="pair_' in page  # Bestätigen/Ablehnen in der Detailansicht
    shown["a"] = ["row:defer"]
    assert "err=" not in post(c, "/quality/assistent/antwort", shown).headers["location"]
    assert any(rc.decision == "defer" for rc in csv_service(cx).rows(out["bid"]))


# ---- R05 · Hintergrundlauf endet nie ohne sichtbares Ergebnis -----------------------------------------------------

def test_R05_busy_source_is_answered_immediately(config, master, api):
    from app.datasources import service as S

    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        ev = S._acquire(sid)
        assert ev is not None
        try:
            res = TK.start_apply(ctx(c), sid, a.token)
        finally:
            S._release(sid)
        assert res["errors"] and "läuft bereits" in res["errors"][0] and "erneut übernehmen" in res["errors"][0]
        assert not TK.busy(sid)
    finally:
        c.__exit__(None, None, None)


def test_R05_returned_error_of_the_background_run_is_persisted(config, master, api, monkeypatch):
    from app.datasources import service as S

    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        monkeypatch.setattr(TK.threading, "Thread", InlineThread)
        monkeypatch.setattr(TK, "apply", lambda *x, **k: {"errors": [S.BUSY_TEXT]})  # Sperre erst im Lauf belegt
        assert TK.start_apply(ctx(c), sid, a.token)["errors"] == []  # gestartet ≠ übernommen
        last = TK.latest(ctx(c).db, sid)
        assert last["status"] == "staged" and last["message"].startswith("Übernahme nicht ausgeführt")
        assert S.BUSY_TEXT in last["message"]
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Übernahme nicht ausgeführt" in page
    finally:
        c.__exit__(None, None, None)


def test_R05_stale_preview_and_missing_fetch_end_with_explanation(config, master, api, monkeypatch):
    c, sid = started(config, api)
    try:
        assert "Kein gespeicherter Vollabruf" in TK.start_apply(ctx(c), sid, "x")["errors"][0]
        staged(c, sid)
        monkeypatch.setattr(TK.threading, "Thread", InlineThread)
        assert TK.start_apply(ctx(c), sid, "veraltet")["errors"] == []
        last = TK.latest(ctx(c).db, sid)
        assert "nicht mehr aktuell" in last["message"] and "neu öffnen" in last["message"]
    finally:
        c.__exit__(None, None, None)


# ---- R06 · Wiederholtes Absenden erhält die Ablehnungsgründe -------------------------------------------------------

def test_R06_resubmitting_an_old_preview_keeps_the_row_blockers(config, master, api, monkeypatch):
    import json

    c, sid = started(config, api, rows=_refusing_rows())
    try:
        staged(c, sid)
        monkeypatch.setattr(TK, "probe", lambda ctx, sid, a, run=True: {"ok": True})  # nur der echte Lauf erkennt
        old = TK.assess(ctx(c), sid)
        assert old.ok
        first = TK.apply(ctx(c), sid, old.token)
        assert first["errors"] and "Einzelentscheidung" in first["errors"][0]
        state = economic_state(c)
        rows_doc = json.loads(TK.latest(ctx(c).db, sid)["result_json"])
        for _ in range(2):  # alte Vorschau aus einem zweiten Tab, zweimal
            assert TK.apply(ctx(c), sid, old.token)["errors"]
            doc = json.loads(TK.latest(ctx(c).db, sid)["result_json"])
            assert doc["rows"] == rows_doc["rows"] and doc["dv"] == rows_doc["dv"]
            a = TK.assess(ctx(c), sid)
            assert not a.ok and [x.key for x in a.blockers] == ["rows"]
        assert economic_state(c) == state
    finally:
        c.__exit__(None, None, None)


# ---- R07 · Verweigerte Rücknahme im Protokoll zeigt Grund und Rückweg · V02 · gemeinsames Protokoll -------------

def test_R07_takeover_part_in_protocol_points_to_the_takeover_and_explains_refusal(config, master, api):
    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        assert TK.apply(ctx(c), sid, a.token)["errors"] == []
        did = ctx(c).db.scalar("SELECT decision_id FROM ds_takeover WHERE source_id=? AND status='applied'", (sid,))
        page = c.get("/quality/assistent/protokoll").text
        assert f"/settings/datasources/{sid}/uebernahme" in page and "nur gemeinsam zurücknehmbar" in page
        assert f'name="d" value="{did}"' not in page  # kein wirkungsloser Knopf
        state = economic_state(c)
        r = post(c, "/quality/assistent/rueckgaengig", {"d": str(did), "back": "protokoll"})
        q = parse_qs(urlsplit(r.headers["location"]).query)
        assert r.headers["location"].startswith("/quality/assistent/protokoll") and "Quellenübernahme" in q["err"][0]
        assert economic_state(c) == state
        assert "Quellenübernahme" in c.get(r.headers["location"]).text
    finally:
        c.__exit__(None, None, None)


def test_V02_protocol_lists_batch_actions_and_takeovers_with_their_undo_path(c):
    cx = c.app.state.ctx
    svc = csv_service(cx)
    bid = _new_row_batch(c)
    p = BA.plan(svc, bid, "include", [0])
    act = BA.execute(svc, bid, "include", [0], token=BA.new_token(), fingerprint=p.fingerprint())["action_id"]
    page = c.get("/quality/assistent/protokoll").text
    assert "Weitere Änderungen" in page and f'name="acts" value="{act}"' in page
    r = post(c, "/quality/assistent/rueckgaengig", {"acts": str(act), "back": "protokoll"})
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert "wieder offen" in q["msg"][0]
    assert not cx.db.q("SELECT 1 FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))


# ---- V04 · Teilfortschritt wird gemeldet --------------------------------------------------------------------------

def test_V04_partial_progress_of_a_case_is_reported(c, monkeypatch):
    from app.diagnosis import clarify as K

    msg = "1 Korrektur gespeichert und nachgeprüft – 1 Problem dieses Falls bleibt offen."
    monkeypatch.setattr(K, "apply_hypothesis", lambda *a, **k: K.Result(ok=True, message=msg, remaining=1, token="t1"))
    r = post(c, "/quality/assistent/antwort", {"typ": "fall", "ref": "kg-x", "kid": "abcdef01", "a": "case:apply"})
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["msg"] == [msg]


# ---- R08 · Durchgang mit deutlich mehr als 60 übersprungenen Aufgaben -------------------------------------------

def test_R08_skipping_far_more_than_60_tasks_never_oscillates(c, monkeypatch):
    tasks = [G.Task(key=f"probe:{n}", theme="bestand", kind="seite", heading=f"Probe {n}", ref=str(n),
                    question=f"Frage Nummer {n:03d}?") for n in range(130)]
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: tasks)
    page = c.get("/quality/assistent?new=1").text
    seen = []
    for n in range(len(tasks)):
        cur = form_values(page)["kid"][0]
        assert cur not in seen  # nie eine schon übersprungene Aufgabe erneut
        seen.append(cur)
        page = c.get("/quality/assistent?" + urlencode({"skip": cur})).text
        if n == 69:  # mitten im Durchgang: Browser hält nur den Bezug, Fortsetzen ohne Parameter bleibt dort
            ref = c.cookies.get("dq_skip") or ""
            assert G.is_pass(ref) and len(ref) < 30
            assert form_values(c.get("/quality/assistent").text)["kid"] == [tasks[70].kid]
    assert seen == [t.kid for t in tasks]
    assert "130 übersprungen" in page and "Alles geprüft" not in page
    assert form_values(c.get("/quality/assistent?new=1").text)["kid"] == [tasks[0].kid]  # gezielt erneut öffnen


def test_R08_old_cookie_lists_are_still_read(c, monkeypatch):
    tasks = [G.Task(key=f"probe:{n}", theme="bestand", kind="seite", heading=f"Probe {n}", ref=str(n),
                    question=f"Frage {n}?") for n in range(3)]
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: tasks)
    c.cookies.set("dq_skip", ",".join(t.kid for t in tasks[:2]))
    assert form_values(c.get("/quality/assistent").text)["kid"] == [tasks[2].kid]


# ---- R12 · „Alles geprüft“ nur, wenn der fachliche Stand es trägt -----------------------------------------------

def test_R12_deferred_work_is_reported_not_called_everything_checked(c):
    from app.diagnosis import actions as A
    from app.diagnosis import clarify as K
    from app.diagnosis.engine import report_for

    cx = c.app.state.ctx
    bid = _new_row_batch(c)
    for f in report_for(cx).findings:
        if f.parent is None:
            A.mark(cx, f.id, "defer")
    for case in K.stored_cases(cx.db):
        K.defer(cx, case["key"], "später")
    csv_service(cx).set_rows(bid, {"dec_0": "defer"})
    assert G.tasks(cx) == []
    rest = G.rest_overview(cx)
    assert rest["import_deferred"] == 1 and rest["cases_deferred"] >= 1
    page = c.get("/quality/assistent?new=1").text
    assert "Alles geprüft" not in page and "Für diesen Durchgang fertig" in page
    assert "1 Vorgang zurückgestellt" in page and "zurückgestellt" in page


def test_R12_everything_checked_only_without_open_work(c, monkeypatch):
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [])
    monkeypatch.setattr(G, "rest_overview", lambda ctx: dict.fromkeys(
        ("import_deferred", "import_open", "findings_deferred", "cases_deferred", "cases_waiting"), 0))
    assert "Alles geprüft" in c.get("/quality/assistent?new=1").text


# ---- V03 · Abgelehnte Quellenpräferenz gezielt erneut öffnen ------------------------------------------------------

def test_V03_declined_source_preference_is_visible_and_reopenable(config, master, api):
    from app.diagnosis import actions as A
    from app.diagnosis.engine import report_for

    c, sid = started(config, api)
    try:
        cx = ctx(c)
        f = next(f for f in report_for(cx).findings if f.parent is None)
        A.mark(cx, f.id, "defer")  # anderer Prüfstand bleibt unberührt
        G.decline_takeover(cx, sid)
        page = c.get("/quality/assistent/protokoll").text
        assert "Quellenvorrang für" in page and "abgelehnt am" in page and f"/vorrang/{sid}/wieder" in page
        r = post(c, f"/quality/assistent/vorrang/{sid}/wieder", {})
        assert "erscheint wieder" in parse_qs(urlsplit(r.headers["location"]).query)["msg"][0]
        assert sid not in (cx.db.get_state(G.DECLINED_KEY) or [])
        assert f.id in A.active_marks(cx.db)  # „Weiß ich nicht“ bleibt bestehen
        assert "nicht mehr" in parse_qs(urlsplit(post(c, f"/quality/assistent/vorrang/{sid}/wieder", {})
                                                 .headers["location"]).query)["err"][0]
    finally:
        c.__exit__(None, None, None)
