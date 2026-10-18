"""P3 project trace: RelayHQ files joined with ledger runs, reviews, decisions and questions."""
import json
from datetime import datetime

from merp_relay import trace
from merp_relay.contracts import TaskRequest
from merp_relay.ledger import Ledger


def task(objective):
    return TaskRequest(task_id='t', objective=objective, kind='IMPLEMENT', execution_mode='repository_access',
                       completion_criteria=('c',), read_scope=('.',))


def make_hq(tmp_path, journal_text):
    hq = tmp_path / 'hq'
    for name in ('p1', 'other', '_template'):
        (hq / 'projects' / name).mkdir(parents=True)
    (hq / 'projects' / 'p1' / 'brief.md').write_text('# p1 brief\n', encoding='utf-8')
    (hq / 'projects' / 'p1' / 'state.md').write_text('# p1 state\n', encoding='utf-8')
    (hq / 'journal').mkdir()
    (hq / 'journal' / '2026-10-18.md').write_text(journal_text, encoding='utf-8-sig')  # leads may write a BOM
    return hq


def test_trace_joins_journal_and_ledger(tmp_path):
    ledger = Ledger(tmp_path / 'relay.db')
    tagged = ledger.submit(task('tagged run'), tmp_path, project='p1', options={'effort': 'low'})
    mentioned = ledger.submit(task('legacy run without project'), tmp_path)
    unrelated = ledger.submit(task('someone else'), tmp_path, project='other')
    ledger.event(tagged, 'reviewed', reviewer='claude', result={'summary': 'APPROVE: fine'})
    ledger.event(tagged, 'decision', by='lead', action='approve', reason='review approved')
    ledger.transition(tagged, ['queued'], 'applied')
    qid = ledger.ask('Unicode or ASCII?', asked_by='lead', project='p1', run_id=mentioned)
    ledger.answer(qid, 'Unicode')
    hq = make_hq(tmp_path, f'## 09:00 p1 — started\n- dispatched {mentioned}: legacy\n\n'
                            f'## 09:05 other — not ours\n- {unrelated}\n')
    assert trace.projects(hq) == ['other', 'p1']
    t = trace.trace(ledger, hq, 'p1')
    kinds = [item['kind'] for item in t['timeline']]
    assert {'journal', 'dispatch', 'review', 'decision', 'status', 'question', 'answer'} <= set(kinds)
    assert all(item.get('run') != unrelated for item in t['timeline'])
    journal = [i for i in t['timeline'] if i['kind'] == 'journal']
    assert len(journal) == 1 and journal[0]['runs'] == [mentioned] and journal[0]['title'] == 'started'
    review = [i for i in t['timeline'] if i['kind'] == 'review'][0]
    assert review['verdict'] == 'approve'
    dispatch = [i for i in t['timeline'] if i['kind'] == 'dispatch' and i['run'] == tagged][0]
    assert 'effort low' in dispatch['text']
    times = [datetime.fromisoformat(i['at']) for i in t['timeline']]
    assert times == sorted(times)
    assert t['brief'] == '# p1 brief\n' and t['run_counts'] == {'applied': 1, 'queued': 1}
    assert trace.trace(ledger, hq, '_template') is None and trace.trace(ledger, hq, 'nope') is None


def test_projects_without_hq_is_empty(tmp_path):
    assert trace.projects(tmp_path / 'missing') == []
