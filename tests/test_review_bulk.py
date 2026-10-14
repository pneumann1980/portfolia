"""Zentrale Prüfung und Sammelverarbeitung (Financial Integrity, Phase 2) – Ende-zu-Ende über HTTP.

Szenario (synthetisch): kuratierter Import mit Einzahlung und Transfer (Koinly-Format), Bitpanda-Abruf mit denselben
Vorgängen (zwei sichere Verknüpfungen) und ein zweiter Datenquellen-Stapel (Kraken-Testconnector) mit einer sicheren
neuen Einzahlung und einem manuell zu prüfenden Vorgang.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from app.csvimport import queue as Q
from app.csvimport import review as RV
from app.csvimport.service import csv_service
from tests.test_csvimport import journal, post
from tests.test_datasources import batch_of as ds_batch_of
from tests.test_datasources import create_source, deposit, fake, sync  # noqa: F401
from tests.test_importcheck import api, client, scenario  # noqa: F401
from tests.test_review_workflow import unknown


def ctx(c):
    return c.app.state.ctx


def setup(c, config, api, fake) -> tuple[int, int, int]:  # noqa: F811
    """Stapel A (Bitpanda, 2 sichere Verknüpfungen), Stapel B (Kraken: 1 sichere neue Buchung, 1 manuell)."""
    bid_a, _rows = scenario(c, config, api, "extra")
    sid = create_source(c)
    fake.events = [deposit("kraken:L1", "2024-05-30T09:00:00", "EUR", "1000"),
                   unknown("kraken:U1", "2024-06-03T10:00:00")]
    bid_b = ds_batch_of(sync(c, sid))
    return bid_a, bid_b, sid


def keys(html: str, checked: bool | None = None) -> list[str]:
    out = []
    for m in re.finditer(r'<input type="checkbox" name="on" value="([^"]+)"[^>]*?(checked)?>', html):
        if checked is None or bool(m.group(2)) == checked:
            out.append(m.group(1))
    return out


def preview_form(html: str) -> dict[str, Any]:
    form: dict[str, Any] = {"part": re.findall(r'name="part" value="(\d+)"', html)}
    for k, v in re.findall(r'name="((?:token|fp|idx)_\d+)" value="([^"]*)"', html):
        form[k] = v
    return form


def post_multi(c, url, data: dict[str, Any]):
    flat = [("csrf_token", c.token)]
    for k, v in data.items():
        for x in (v if isinstance(v, list) else [v]):
            flat.append((k, x))
    return c.post(url, content=urlencode(flat), follow_redirects=False,
                  headers={"content-type": "application/x-www-form-urlencoded"})


def test_safe_tab_preselects_only_links_and_keeps_deselection(client, config, api, fake):  # noqa: F811
    c = client
    bid_a, bid_b, _sid = setup(c, config, api, fake)
    html = c.get("/journal/pruefung?tab=sicher").text
    allk = keys(html)
    on = keys(html, checked=True)
    assert len(allk) == 3 and len(on) == 2  # neue Buchung (Kraken) nicht vorausgewählt
    assert all(k.startswith(f"{bid_a}:") for k in on) and any(k.startswith(f"{bid_b}:") for k in set(allk) - set(on))
    assert "neu buchen" in html and "verknüpfen (bucht nichts)" in html
    # eine Verknüpfung abwählen – bleibt gespeichert
    r = post_multi(c, "/journal/pruefung/auswahl", {"shown": allk, "on": on[:1], "op": "save"})
    assert r.status_code == 303
    html2 = c.get("/journal/pruefung?tab=sicher").text
    assert keys(html2, checked=True) == on[:1]
    # Vorauswahl wiederherstellen
    post_multi(c, "/journal/pruefung/auswahl", {"op": "safe"})
    assert keys(c.get("/journal/pruefung?tab=sicher").text, checked=True) == on


def test_bulk_preview_confirm_and_execute_across_batches(client, config, api, fake):  # noqa: F811
    c = client
    bid_a, bid_b, sid = setup(c, config, api, fake)
    n_tx = len(journal(c))
    r = post_multi(c, "/journal/pruefung/auswahl", {"op": "all"})  # ausdrücklich auch die neue Buchung
    assert r.status_code == 303
    r = post_multi(c, "/journal/pruefung/auswahl", {"shown": [], "op": "preview"})
    assert r.status_code == 303 and "/journal/pruefung/vorschau" in r.headers["location"]
    html = c.get(r.headers["location"]).text
    assert "2</b><span>verknüpfen (bucht nichts)" in html and "1</b><span>neu buchen" in html
    assert "Wirkung auf die Bestände" in html and "FIFO und Steuer" in html and "EUR" in html
    assert len(journal(c)) == n_tx  # Vorschau bucht nichts
    form = preview_form(html)
    assert sorted(form["part"]) == sorted([str(bid_a), str(bid_b)])
    # ohne Bestätigung: nichts ausgeführt
    r = post_multi(c, "/journal/pruefung/ausfuehren", form)
    assert r.status_code == 303 and "err=confirm" in r.headers["location"] and len(journal(c)) == n_tx
    r = post_multi(c, "/journal/pruefung/ausfuehren", {**form, "confirm": "1"})
    assert r.status_code == 200 and "2 von 2 ausgeführt" in r.text
    assert len(journal(c)) == n_tx + 1  # nur die neue Einzahlung gebucht, Verknüpfungen buchen nichts
    db = ctx(c).db
    assert db.scalar("SELECT COUNT(*) FROM tx_link WHERE status='active'") == 2
    assert csv_service(ctx(c)).batch(bid_a)["status"] == "done"
    assert csv_service(ctx(c)).batch(bid_b)["status"] == "partial"  # manueller Fall bleibt offen (Teilabschluss)
    s = RV.source_summary(db, sid)
    assert s.open == 1 and s.to_review == 1
    # erneutes Absenden derselben Vorschau: keine zweite Ausführung (Token)
    r = post_multi(c, "/journal/pruefung/ausfuehren", {**form, "confirm": "1"})
    assert "bereits ausgeführt" in r.text and len(journal(c)) == n_tx + 1
    assert db.scalar("SELECT COUNT(*) FROM import_action") == 2


def test_changed_batch_is_not_executed_others_are(client, config, api, fake):  # noqa: F811
    c = client
    bid_a, _bid_b, _sid = setup(c, config, api, fake)
    post_multi(c, "/journal/pruefung/auswahl", {"op": "all"})
    html = c.get("/journal/pruefung/vorschau").text
    form = preview_form(html)
    # nach der Vorschau: in Stapel A eine Zeile zurückstellen (eigene Entscheidung hat Vorrang)
    rc = next(rc for rc in csv_service(ctx(c)).rows(bid_a) if RV.row_state(rc) == "sicher")
    assert post(c, f"/journal/csv/{bid_a}/rows", **{f"dec_{rc.idx}": "defer"}).status_code == 303
    n_tx = len(journal(c))
    r = post_multi(c, "/journal/pruefung/ausfuehren", {**form, "confirm": "1"})
    assert "1 von 2 ausgeführt" in r.text and "nicht mehr aktuell" in r.text
    assert len(journal(c)) == n_tx + 1  # Stapel B ausgeführt
    assert ctx(c).db.scalar("SELECT COUNT(*) FROM tx_link WHERE status='active'") == 0  # Stapel A unverändert
    assert {x.idx: x.decision for x in csv_service(ctx(c)).rows(bid_a)}[rc.idx] == "defer"


def test_manual_variants_skip_defer_reset_from_queue(client, config, api, fake):  # noqa: F811
    c = client
    _bid_a, bid_b, sid = setup(c, config, api, fake)
    html = c.get(f"/journal/pruefung?source={sid}").text
    assert "Fehlende Angabe ergänzen" in html and "Zurückstellen" in html and "Nicht übernehmen" in html
    it = next(i for i in Q.build(ctx(c).db, "offen", source=sid).all_items if i.event == "kraken:U1")
    ev = f"{it.batch_id}:{','.join(map(str, it.idxs))}"
    r = post(c, "/journal/pruefung/entscheiden", event=ev, op="defer", source=str(sid))
    assert r.status_code == 303 and "msg=decided" in r.headers["location"]
    it = next(i for i in Q.build(ctx(c).db, "offen", source=sid).all_items if i.event == "kraken:U1")
    assert it.state == "zurueckgestellt" and any(v["op"] == "reset" for v in it.variants() if "op" in v)
    post(c, "/journal/pruefung/entscheiden", event=ev, op="reset")
    assert next(i for i in Q.build(ctx(c).db, "offen", source=sid).all_items
                if i.event == "kraken:U1").state == "offen"
    post(c, "/journal/pruefung/entscheiden", event=ev, op="skip")
    assert not [i for i in Q.build(ctx(c).db, "offen", source=sid).all_items if i.event == "kraken:U1"]
    assert RV.source_summary(ctx(c).db, sid).to_review == 0
    # protokolliert und rückgängig zu machen
    aid = ctx(c).db.scalar("SELECT MAX(id) FROM import_action WHERE batch_id=?", (bid_b,))
    assert post(c, f"/journal/csv/{bid_b}/actions/{aid}/undo").status_code == 303
    assert RV.source_summary(ctx(c).db, sid).to_review == 1
    # Entscheidung für einen erledigten Vorgang: verständliche Meldung, keine Änderung
    post(c, "/journal/pruefung/entscheiden", event=ev, op="skip")
    r = post(c, "/journal/pruefung/entscheiden", event=ev, op="skip")
    assert "msg=decide_failed" in r.headers["location"]


def test_queue_filters_narrow_results(client, config, api, fake):  # noqa: F811
    c = client
    _bid_a, _bid_b, sid = setup(c, config, api, fake)
    db = ctx(c).db
    total = Q.build(db, "sicher").total
    assert total == 3
    assert Q.build(db, "sicher", flt=Q.Filter(source=sid)).total == 1
    assert Q.build(db, "sicher", flt=Q.Filter(evidence="neu")).total == 1
    assert Q.build(db, "sicher", flt=Q.Filter(account="Bitpanda")).total == 2
    assert Q.build(db, "sicher", flt=Q.Filter(frm="2024-05-01")).total == 1
    assert Q.build(db, "sicher", flt=Q.Filter(to="2024-03-31")).total == 2
    assert Q.build(db, "offen", flt=Q.Filter(kind="deposit")).total == 1
    flt = Q.Filter.parse({"frm": "kaputt", "source": "x", "to": "2024-13-99"})
    assert (flt.frm, flt.source) == ("", None)
    page = c.get(f"/journal/pruefung?tab=sicher&source={sid}&evidence=neu").text
    assert "Filter (2 aktiv)" in page and len(keys(page)) == 1
