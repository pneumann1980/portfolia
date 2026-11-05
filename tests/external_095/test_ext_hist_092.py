"""Independent desired-behavior checks for Portfolia 0.92.0 / 023f11d."""
from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from app.csvimport import batch as BA
from app.csvimport import model as M
from app.csvimport.service import csv_service, rec_to_json
from app.datasources import takeover as TK
from app.util.timeutil import parse_iso
from tests.conftest import config, _no_env_leak  # noqa: F401
from tests.test_bitpanda import api, ctx, master  # noqa: F401
from tests.test_diagnosis import PF_HEADER, upload
from tests.test_review_090 import _edit_target
from tests.test_review_fixes import c, cfg, form_values, post  # noqa: F401
from tests.test_review_091 import _duplicate, _journal_transfer
from tests.test_review_091 import _change_journal
from tests.test_nachpruefung import _journal_deposits, _row, _without_counterpart
from tests.test_takeover import staged


def _run_editor(cx, fn):
    failures = []

    def run():
        try:
            fn()
        except BaseException as e:
            failures.append(repr(e))
        finally:
            cx.db.close_thread_conn()

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(8)
    assert not worker.is_alive() and not failures, failures


@pytest.mark.parametrize('later', [False, True])
def test_N13_rejected_row_action_preserves_other_tabs_later_inputs(c, monkeypatch, later):
    cx = ctx(c)
    bid = upload(c, 'mix.csv', PF_HEADER +
                 'N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n')
    svc = csv_service(cx)
    other_rec = M.Rec(line=3, ts=parse_iso('2025-03-12T12:00:00Z'), kind=M.TRADE, account='Other',
                      out_sym='ETH', out_qty=Decimal('8'), in_sym='BNB', in_qty=Decimal('1'), ext_id='synthetic:other')
    recs = [svc.rows(bid)[0].rec, other_rec]
    payload = ('[' + ','.join(rec_to_json(r) for r in recs) + ']').encode()
    bid = svc.ingest(recs, source='csv:portfolia', profile='portfolia', account='',
                     label='Synthetic two-row review', datasource_id=None, payload=payload, options={'cutoff': ''})
    svc.evaluate(bid)
    mine, other = svc.rows(bid)
    assert other.missing_value
    target = str(mine.match['target'])
    page = c.get(f'/journal/csv/{bid}').text
    assert f'name="val_{other.idx}"' in page  # a real editable field in the second tab
    fields = form_values(page, f'/journal/csv/{bid}/rows')
    fields['row_action'] = [f'link:{mine.idx}']
    real_tx, real_restore = BA._execute_tx, svc.restore_inputs
    done = []

    def change_dependency_then_tx(*args, **kwargs):
        def edit():
            assert not _edit_target(cx, target, '7').errors
        _run_editor(cx, edit)
        return real_tx(*args, **kwargs)

    def other_input_then_restore(batch_id, snap):
        if later:
            def edit():
                svc.set_rows(bid, {f'dec_{other.idx}': 'defer', f'val_{other.idx}': '123'})
            _run_editor(cx, edit)
            saved = cx.db.q1('SELECT decision,value_in FROM csv_row WHERE batch_id=? AND idx=?', (bid, other.idx))
            assert tuple(saved) == ('defer', '123')
        done.append(True)
        return real_restore(batch_id, snap)

    monkeypatch.setattr(BA, '_execute_tx', change_dependency_then_tx)
    monkeypatch.setattr(svc, 'restore_inputs', other_input_then_restore)
    res = post(c, f'/journal/csv/{bid}/row-action', fields)
    assert res.status_code == 200 and 'geändert' in res.text and done
    assert not cx.db.scalar('SELECT COUNT(*) FROM tx_link WHERE batch_id=?', (bid,))
    current = cx.db.q1('SELECT decision,value_in FROM csv_row WHERE batch_id=? AND idx=?', (bid, other.idx))
    expected = ('defer', '123') if later else (None, None)
    assert tuple(current) == expected, {'later_input': expected, 'after_rejected_action': tuple(current)}


def test_control_old_row_button_does_not_link_after_new_source_identity(c):
    cx, svc, bid, mine = _duplicate(c)
    target = str(mine.match['target'])
    fields = form_values(c.get(f'/journal/csv/{bid}').text, f'/journal/csv/{bid}/rows')
    fields['row_action'] = [f'link:{mine.idx}']
    # A later record of the same source identity is booked on a different date, through real services.
    second = upload(c, 'later.csv', PF_HEADER +
                    'N1,2025-03-12T12:00:00Z,deposit,,,,,Wallet K,KAS,3.5,,,,1,later source version\n')
    assert second != bid
    rows = svc.rows(second)
    assert rows[0].status == 'new', [(r.status, r.match) for r in rows]
    assert not svc.commit(second)['errors']
    booked = cx.db.scalar('SELECT tx_id FROM journal_tx WHERE batch_id=? AND status="active"', (second,))
    assert booked and booked != target
    assert not cx.db.scalar('SELECT COUNT(*) FROM tx_link WHERE batch_id=? AND status="active"', (bid,))
    res = post(c, f'/journal/csv/{bid}/row-action', fields)
    links = [dict(r) for r in cx.db.q('SELECT tx_id FROM tx_link WHERE batch_id=? AND status="active"', (bid,))]
    assert not links, {'originally_shown_target': target, 'new_target': booked, 'linked': links,
                       'http': res.status_code, 'location': res.headers.get('location')}


@pytest.mark.parametrize('choice', ['pair', 'nopair'])
def test_N14_takeover_does_not_rebind_stored_decision_to_changed_partner(config, master, api, monkeypatch, choice):
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        (target,) = _journal_deposits(client, ('2024-02-12T12:30:00Z', 'Ledger', '0.001'))
        a = staged(client, sid)
        problem = _row(a)
        assert problem['partner']['kind'] == 'j'
        assert not TK.decide(cx, sid, problem['key'], choice, a.token)['errors']
        ready = TK.assess(cx, sid)
        assert ready.ok
        real = TK._apply_decisions
        did = []

        def edit_then_set(context, csv, bid, assessment):
            if not getattr(TK._SANDBOX, 'on', False) and not did:
                _run_editor(cx, lambda: _change_journal(cx, target, 'value'))
                did.append(True)
            return real(context, csv, bid, assessment)

        monkeypatch.setattr(TK, '_apply_decisions', edit_then_set)
        result = TK.apply(cx, sid, ready.token)
        assert did
        partner = dict(cx.db.q1('SELECT status,value_eur FROM journal_tx WHERE tx_id=?', (target,)))
        assert partner['value_eur'] == '777'
        booked = [dict(r) for r in cx.db.q('SELECT type,to_account,from_account FROM journal_tx WHERE batch_id=? AND status="active"',
                                          (TK.latest(cx.db,sid)['batch_id'],))]
        assert result['errors'], {'choice': choice, 'exact': result.get('result',{}).get('check',{}).get('exact'),
                                 'partner_value': partner['value_eur'],
                                 'source_types': [r['type'] for r in booked], 'errors': result['errors']}
    finally:
        client.__exit__(None,None,None)


@pytest.mark.parametrize('omit', [False, True])
def test_N14_transfer_form_missing_revision_cannot_confirm_changed_partner(c, omit):
    from lxml import html

    cx, svc, bid, rc, target = _journal_transfer(c, '1', save=False)
    svc.set_rows(bid, {f'dec_{rc.idx}': 'include'})
    page = c.get(f'/journal/csv/{bid}').text
    form = html.fromstring(page).xpath(f'//form[.//select[@name="pair_{rc.idx}"]]')[0]
    fields = {el.get('name'): [el.get('value') or ''] for el in form.xpath('.//input[@name]')}
    assert fields[f'pairrev_{rc.idx}']
    if omit:  # same user-visible decision form as 0.91, without the newly introduced hidden field
        del fields[f'pairrev_{rc.idx}']
    _change_journal(cx, target, 'value')
    fields[f'pair_{rc.idx}'] = ['1']
    res = post(c, f'/journal/csv/{bid}/rows', fields)
    svc.evaluate(bid)
    now = next(r for r in svc.rows(bid) if r.idx == rc.idx)
    commit_http = None
    if omit and now.pair_ok == 1:
        # The old page also contains this button; no new comparison is rendered before committing.
        commit_http = post(c, f'/journal/csv/{bid}/commit', {}).status_code
    target_now = dict(cx.db.q1('SELECT value_eur,status FROM journal_tx WHERE tx_id=?', (target,)))
    assert now.pair_ok is None, {'missing_revision': omit, 'pair_ok': now.pair_ok,
                                 'value': target_now['value_eur'], 'status': target_now['status'],
                                 'commit_http': commit_http}
