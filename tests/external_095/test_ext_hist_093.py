"""Independent desired-behavior checks for Portfolia 0.93.0 / ecf69ff."""
from __future__ import annotations

import pytest

from app.csvimport.service import ref_revs
from app.datasources import takeover as TK
from app.diagnosis import actions as A
from app.journal import service as J
from tests.conftest import config, _no_env_leak  # noqa: F401
from tests.test_bitpanda import api, ctx, master  # noqa: F401
from tests.test_nachpruefung import _all_state, _journal_deposits, _row, _without_counterpart
from tests.test_review_091 import _change_journal
from tests.test_review_092 import _run_editor
from tests.test_takeover import staged


@pytest.mark.parametrize('edit', ['none', 'selected', 'alternative'])
def test_all_offered_transfer_partners_stay_bound_until_commit(config, master, api, monkeypatch, edit):
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        targets = _journal_deposits(client, ('2024-02-12T12:30:00Z', 'Ledger A', '0.001'),
                                    ('2024-02-12T12:45:00Z', 'Ledger B', '0.001'))
        before = _all_state(client)
        a = staged(client, sid)
        problem = _row(a)
        assert problem['partner']['kind'] == 'alts'
        assert set(problem['partner']['revs']) == set(targets)
        assert not TK.decide(cx, sid, problem['key'], 'nopair', a.token)['errors']
        ready = TK.assess(cx, sid)
        assert ready.ok
        real = TK._apply_decisions
        changed = []

        def apply_then_change(context, csv, bid, assessment):
            result = real(context, csv, bid, assessment)
            if not getattr(TK._SANDBOX, 'on', False) and not changed and edit != 'none':
                rc = next(r for r in csv.rows(bid) if r.rec.ext_id == problem['key'])
                selected = rc.pair_ref[2:]
                assert selected in targets and len(rc.pair_alts) == 1
                target = selected if edit == 'selected' else next(t for t in targets if t != selected)
                _run_editor(cx, lambda: _change_journal(cx, target, 'value'))
                changed.append(target)
            return result

        monkeypatch.setattr(TK, '_apply_decisions', apply_then_change)
        out = TK.apply(cx, sid, ready.token)
        if edit == 'none':
            assert not out['errors']
            assert not TK.undo(cx, sid)['errors']
            cx.invalidate_data()
            assert _all_state(client) == before
            return
        assert changed
        assert cx.db.scalar('SELECT value_eur FROM journal_tx WHERE tx_id=?', (changed[0],)) == '777'
        assert out['errors'], {'edited': edit, 'changed': changed[0],
                               'exact': out.get('result',{}).get('check',{}).get('exact'), 'errors': out['errors']}
        last = TK.latest(cx.db,sid)
        assert last['status'] == 'staged' and not last['decision_id'] and not last['batch_id']
        assert not cx.db.scalar('SELECT COUNT(*) FROM journal_tx WHERE source="sync:bitpanda" AND status="active"')
    finally:
        client.__exit__(None,None,None)


@pytest.mark.parametrize('same_second', [False, True])
def test_takeover_preview_refreshes_partner_revision_after_journal_edit(config, master, api, monkeypatch, same_second):
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        (target,) = _journal_deposits(client, ('2024-02-12T12:30:00Z', 'Ledger', '0.001'))
        a = staged(client, sid)
        problem = _row(a)
        assert problem['partner']['kind'] == 'j'
        before = A.data_version(cx.db)
        stamp = cx.db.scalar('SELECT MAX(updated_at) FROM journal_tx')
        # Scheduling only: a journal update in the same timestamp second, or a later one.
        if not same_second:
            from app.util.timeutil import parse_iso, iso
            from datetime import timedelta
            stamp = iso(parse_iso(stamp) + timedelta(seconds=2))
        monkeypatch.setattr(J, '_now', lambda: stamp)
        _run_editor(cx, lambda: _change_journal(cx, target, 'value'))
        actual_rev = ref_revs(cx.db.conn,[target])[target]
        assert actual_rev != problem['partner']['rev']
        collision = A.data_version(cx.db) == before
        fresh = TK.assess(cx, sid)
        shown = next(p for p in fresh.problems if p.get('kind') == 'row' and (p.get('partner') or {}).get('tx') == target)
        base = _all_state(client)
        stored = TK.decide(cx, sid, shown['key'], 'pair', fresh.token)
        assert not stored['errors'], stored
        ready = TK.assess(cx, sid)
        retries = 0
        # Diagnose whether the normal remove/re-decide path can recover without another data change.
        if same_second and not ready.ok:
            for _ in range(2):
                for d in ready.stale:
                    if d.get('key'):
                        assert not TK.undecide(cx,sid,d['key'],ready.rid,d['stamp'])['errors']
                nxt = TK.assess(cx,sid)
                offer = next(p for p in nxt.problems if p.get('kind') == 'row' and (p.get('partner') or {}).get('tx') == target)
                assert not TK.decide(cx,sid,offer['key'],'pair',nxt.token)['errors']
                ready = TK.assess(cx,sid)
                retries += 1
        assert ready.ok, {'same_second': same_second, 'version_collision': collision, 'retries': retries,
                          'stale': [d['why'] for d in ready.stale]}
        assert shown['partner']['rev'] == actual_rev
        assert not TK.apply(cx, sid, ready.token)['errors']
        assert not TK.undo(cx, sid)['errors']
        cx.invalidate_data()
        assert _all_state(client) == base
    finally:
        client.__exit__(None,None,None)
