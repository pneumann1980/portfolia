"""Independent desired-behavior checks for Portfolia bc612c0 (0.91.0). Synthetic data only."""
from __future__ import annotations

import json
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from lxml import html

from app.csvimport import batch as BA
from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from app.journal.service import journal_service
from tests.conftest import config, _no_env_leak  # noqa: F401
from tests.test_bitpanda import api, ctx, master  # noqa: F401
from tests.test_diagnosis import PF_HEADER, upload
from tests.test_nachpruefung import _all_state, _journal_deposits, _row, _without_counterpart
from tests.test_review_090 import _edit_target, _rows
from tests.test_review_fixes import c, cfg, form_values, post  # noqa: F401
from tests.test_takeover import staged, started


def _duplicate(c):
    cx = ctx(c)
    bid = upload(c, 'dup.csv', PF_HEADER + 'N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n')
    svc = csv_service(cx)
    svc.evaluate(bid)
    rc = next(rc for rc in svc.rows(bid) if (rc.match or {}).get('target'))
    BA.set_selection(cx.db, bid, {rc.idx})
    return cx, svc, bid, rc


def test_N10_old_detail_preview_rejects_changed_link_target(c):
    cx, svc, bid, rc = _duplicate(c)
    page = c.get(f'/journal/csv/{bid}/preview?action=link&force=1')
    action = f'/journal/csv/{bid}/execute'
    old = form_values(page.text, action)
    target = str(rc.match['target'])
    assert not _edit_target(cx, target, '7').errors
    res = post(c, action, old)
    links = [dict(r) for r in cx.db.q('SELECT * FROM tx_link WHERE batch_id=?', (bid,))]
    assert res.status_code == 409 and not links, {'http': res.status_code, 'location': res.headers.get('location'),
                                                'links': links}


def test_N10_old_detail_row_button_rejects_changed_link_target(c):
    cx, svc, bid, rc = _duplicate(c)
    page = c.get(f'/journal/csv/{bid}')
    action = f'/journal/csv/{bid}/row-action'
    old = form_values(page.text, f'/journal/csv/{bid}/rows')
    assert html.fromstring(page.text).xpath(f'//button[@formaction="{action}" and @value="link:{rc.idx}"]')
    old['row_action'] = [f'link:{rc.idx}']
    target = str(rc.match['target'])
    assert not _edit_target(cx, target, '7').errors
    res = post(c, action, old)
    links = [dict(r) for r in cx.db.q('SELECT * FROM tx_link WHERE batch_id=?', (bid,))]
    assert not links, {'http': res.status_code, 'location': res.headers.get('location'), 'links': links}


def _confirmed_journal_transfer(c, decision):
    cx = ctx(c)
    fund = journal_service(cx).save({'kind': 'expert', 'date': '2025-07-01', 'time': '10:00',
                                    'type': 'deposit', 'to_account': 'Sender', 'to_asset': 'BTC',
                                    'to_qty': '0.01', 'value_eur': '550'})
    assert not fund.errors, fund.errors
    saved = journal_service(cx).save({'kind': 'expert', 'date': '2025-07-12', 'time': '14:30',
                                     'type': 'deposit', 'to_account': 'Ledger', 'to_asset': 'BTC',
                                     'to_qty': '0.001', 'value_eur': '55'})
    assert not saved.errors, saved.errors
    target = saved.tx_ids[0]
    bid = upload(c, 'out.csv', PF_HEADER +
                 'OUT,2025-07-12T12:00:00Z,withdrawal,,Sender,BTC,0.001,,,,,,,55,out\n')
    svc = csv_service(cx)
    svc.evaluate(bid)
    rows = svc.rows(bid)
    assert [(r.status, r.pair_ref) for r in rows], rows
    rc = next(r for r in rows if r.pair_ref == f'j:{target}')
    action = f'/journal/csv/{bid}/rows'
    shown = form_values(c.get(f'/journal/csv/{bid}').text, action)
    shown[f'pair_{rc.idx}'] = [decision]
    shown[f'dec_{rc.idx}'] = ['include']
    assert post(c, action, shown).status_code == 303
    BA.set_selection(cx.db, bid, {rc.idx})
    svc.evaluate(bid)
    rc = next(r for r in svc.rows(bid) if r.idx == rc.idx)
    assert rc.pair_ok == int(decision)
    return cx, svc, bid, rc


@pytest.mark.parametrize('decision', ['1', '0'])
def test_N11_confirmed_journal_transfer_is_executable_in_detail_preview(c, decision):
    cx, svc, bid, rc = _confirmed_journal_transfer(c, decision)
    p = BA.plan(svc, bid, 'include', {rc.idx}, True)
    assert p.todo, {'confirmed': decision, 'reason': [it.note for it in p.items],
                    'preview_http': c.get(f'/journal/csv/{bid}/preview?action=include&force=1').status_code}


def test_N12_takeover_rejects_journal_partner_edit_after_assessment(config, master, api, monkeypatch):
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        (target,) = _journal_deposits(client, ('2024-02-12T12:30:00Z', 'Ledger', '0.001'))
        a = staged(client, sid)
        assert TK.decide(cx, sid, _row(a)['key'], 'pair', a.token)['errors'] == []
        ready = TK.assess(cx, sid)
        assert ready.ok
        real = BA.execute
        did = []
        enter, changed = threading.Event(), threading.Event()
        errors = []

        def other_editor():
            try:
                assert enter.wait(8)
                js = journal_service(cx)
                form = js.form_data(cx.db.q1('SELECT * FROM journal_tx WHERE tx_id=?', (target,)))
                form['value_eur'] = '777'
                saved = js.save(form, target)
                assert not saved.errors, saved.errors
                did.append(True)
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                changed.set()
                cx.db.close_thread_conn()

        def edit_then_apply(svc, *args, **kwargs):
            if not getattr(TK._SANDBOX, 'on', False) and not did:
                enter.set()
                assert changed.wait(8), 'Journal writer unexpectedly blocked'
                assert not errors, errors
            return real(svc, *args, **kwargs)

        monkeypatch.setattr(BA, 'execute', edit_then_apply)
        worker = threading.Thread(target=other_editor)
        worker.start()
        res = TK.apply(cx, sid, ready.token)
        worker.join(8)
        assert did and not errors, errors
        target_now = dict(cx.db.q1('SELECT tx_id, value_eur, status FROM journal_tx WHERE tx_id=?', (target,)))
        assert target_now['value_eur'] == '777'
        assert res['errors'], {'check': res.get('result', {}).get('check'), 'target': target_now,
                               'errors': res['errors']}
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize('path', ['preview', 'row'])
def test_control_unchanged_detail_link_and_undo_work(c, path):
    cx, svc, bid, rc = _duplicate(c)
    if path == 'preview':
        action = f'/journal/csv/{bid}/execute'
        fields = form_values(c.get(f'/journal/csv/{bid}/preview?action=link&force=1').text, action)
    else:
        action = f'/journal/csv/{bid}/row-action'
        fields = form_values(c.get(f'/journal/csv/{bid}').text, f'/journal/csv/{bid}/rows')
        fields['row_action'] = [f'link:{rc.idx}']
    res = post(c, action, fields)
    assert res.status_code == 303
    assert cx.db.scalar('SELECT COUNT(*) FROM tx_link WHERE batch_id=?', (bid,)) == 1
    aid = cx.db.scalar('SELECT id FROM import_action WHERE batch_id=?', (bid,))
    undone = post(c, f'/journal/csv/{bid}/actions/{aid}/undo', {})
    assert undone.status_code == 303
    assert cx.db.scalar('SELECT COUNT(*) FROM tx_link WHERE batch_id=? AND status="active"', (bid,)) == 0


@pytest.mark.parametrize('decision', ['1', '0'])
def test_control_legacy_commit_can_complete_the_confirmed_journal_path(c, decision):
    cx, svc, bid, rc = _confirmed_journal_transfer(c, decision)
    before = dict(cx.ledger().balances)
    res = post(c, f'/journal/csv/{bid}/commit', {})
    assert res.status_code == 303 and 'msg=commit' in res.headers.get('location', '')
    assert svc.rows(bid)[0].status in ('merged', 'committed')
    assert cx.db.scalar('SELECT COUNT(*) FROM journal_tx WHERE source="transfer" AND status="active"') == int(decision)
    back = post(c, f'/journal/csv/{bid}/revert', {})
    assert back.status_code == 303
    cx.invalidate_data()
    assert {k: v for k, v in cx.ledger().balances.items() if v} == {k: v for k, v in before.items() if v}


def test_control_standalone_review_include_has_full_apply_undo_path(config, master, api):
    client, sid = started(config, api)
    try:
        cx = ctx(client)
        before = _all_state(client)
        assert not TK.run_fetch(cx, sid)['errors']
        saved = TK.latest(cx.db, sid)
        events = json.loads(saved['events_json'])
        events[0]['review'] = 'Synthetic fully representable record requiring explicit confirmation'
        cx.db.x('UPDATE ds_takeover SET events_json=? WHERE id=?', (json.dumps(events), saved['id']))
        a = TK.assess(cx, sid)
        problem = _row(a)
        assert problem['options'] == ['include', 'skip']
        assert not TK.decide(cx, sid, problem['key'], 'include', a.token)['errors']
        ready = TK.assess(cx, sid)
        assert ready.ok
        assert not TK.apply(cx, sid, ready.token)['errors']
        assert not TK.undo(cx, sid)['errors']
        cx.invalidate_data()
        assert _all_state(client) == before
    finally:
        client.__exit__(None, None, None)
