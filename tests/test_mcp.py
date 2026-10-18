"""Relay MCP server: protocol, lead tools and the approval policy guard. Workers and reviewers are fakes."""
import json
import os
import subprocess
import sys

import pytest
from merp_relay import mcp, ops
from merp_relay.adapters import ClaudeAdapter, CodexAdapter
from merp_relay.boundary import runtime_profile
from merp_relay.contracts import AgentResult
from merp_relay.ledger import Ledger


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    for args in (['init', '-q'], ['config', 'user.email', 't@example.invalid'], ['config', 'user.name', 't']):
        subprocess.run(['git', '-C', str(repo), *args], check=True)
    (repo / 'app.txt').write_bytes(b'v1\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'init'], check=True)
    modes = {'execution_modes': frozenset({'snapshot_analysis', 'repository_access'})}
    profiles = {'codex': ('codex-adapter', runtime_profile(CodexAdapter(), {'version': 'c'}).model_copy(update=modes)),
                'claude': ('claude-adapter', runtime_profile(ClaudeAdapter(), {'version': 'x (Claude Code)'})
                           .model_copy(update=modes))}
    monkeypatch.setattr(ops, 'WORKTREES', tmp_path / 'worktrees')
    monkeypatch.setattr(ops, 'available_profiles', lambda: profiles)
    monkeypatch.setattr(ops, 'observations', lambda: [])
    monkeypatch.setattr(mcp, 'pools', lambda: [])
    return Ledger(tmp_path / 'relay.db'), repo


def call(ledger, name, **arguments):
    response = mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': name, 'arguments': arguments}}, ledger)
    result = response['result']
    return json.loads(result['content'][0]['text']), result['isError']


def worker(files):
    def run(adapter, task, cwd, timeout, should_stop):
        for name, text in files.items():
            (cwd / name).parent.mkdir(parents=True, exist_ok=True)
            (cwd / name).write_text(text, encoding='utf-8')
        return AgentResult(execution_id='e', task_id=task.task_id, status='completed', summary='done'), {}
    return run


def reviewer(summary):
    return lambda adapter, task, packet, should_stop: (
        AgentResult(execution_id='r', task_id=task.task_id, status='completed', summary=summary), {})


def test_protocol_basics(env):
    ledger, _ = env
    init = mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                       'params': {'protocolVersion': '2025-03-26'}}, ledger)
    assert init['result']['protocolVersion'] == '2025-03-26' and 'tools' in init['result']['capabilities']
    assert mcp.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, ledger) is None
    names = {t['name'] for t in mcp.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, ledger)['result']['tools']}
    assert {'workers', 'dispatch', 'report', 'wait', 'approve', 'ask_owner', 'inbox'} <= names
    assert mcp.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'nope'}, ledger)['error']['code'] == -32601
    payload, is_error = call(ledger, 'runs', limit=None)  # bad argument types return errors, not crashes
    assert is_error and 'error' in payload


def test_lead_dispatch_wait_report_approve(env, monkeypatch):
    ledger, repo = env
    monkeypatch.setattr(ops, 'dispatch', worker({'app.txt': 'v2\n'}))
    monkeypatch.setattr(ops, 'review_dispatch', reviewer('APPROVE: correct'))
    queued, _ = call(ledger, 'dispatch', objective='bump', repo=str(repo), worker='claude')
    ops.process(ledger, queued['run_id'])
    waited, _ = call(ledger, 'wait', run_ids=[queued['run_id']], timeout=1)
    assert waited['all_done'] and waited['statuses'][queued['run_id']] == 'needs_approval'
    report, _ = call(ledger, 'report', run_id=queued['run_id'])
    assert report['resource_id'] == 'claude' and report['requested_worker'] == 'claude'
    assert report['review']['verdict'] == 'approve' and report['review']['reviewer'] == 'codex'
    assert report['approval_blockers'] == [] and '+v2' in report['diff']
    assert call(ledger, 'approve', run_id=queued['run_id'], reason='')[1]  # a reason is required
    approved, _ = call(ledger, 'approve', run_id=queued['run_id'], reason='review approved, one-line change')
    assert approved['approved'] and approved['status'] == 'applied'
    assert (repo / 'app.txt').read_text() == 'v2\n'
    decision = [e for e in ledger.events(queued['run_id']) if e['type'] == 'decision'][0]['data']
    assert decision == {'by': 'lead', 'action': 'approve', 'reason': 'review approved, one-line change'}


@pytest.mark.parametrize('files,review,blocker', [
    ({'app.txt': 'v2\n'}, 'CHANGES: missing tests', 'not APPROVE'),
    ({'app.txt': 'v2\n', '.env': 'TOKEN=x\n'}, 'APPROVE: fine', 'sensitive'),
    ({'.github/workflows/ci.yml': 'on: push\n'}, 'APPROVE: fine', 'sensitive'),
    ({f'f{i}.txt': 'x\n' for i in range(21)}, 'APPROVE: fine', 'files changed'),
])
def test_policy_sends_risky_approvals_to_owner(env, monkeypatch, files, review, blocker):
    ledger, repo = env
    monkeypatch.setattr(ops, 'dispatch', worker(files))
    monkeypatch.setattr(ops, 'review_dispatch', reviewer(review))
    run_id = call(ledger, 'dispatch', objective='change', repo=str(repo))[0]['run_id']
    ops.process(ledger, run_id)
    result, _ = call(ledger, 'approve', run_id=run_id, reason='looks fine')
    assert not result['approved'] and any(blocker in b for b in result['blockers'])
    assert ledger.status(run_id) == 'needs_approval' and (repo / 'app.txt').read_text() == 'v1\n'


def test_deleted_files_need_owner(env, monkeypatch):
    ledger, repo = env
    def delete(adapter, task, cwd, timeout, should_stop):
        (cwd / 'app.txt').unlink()
        return AgentResult(execution_id='e', task_id=task.task_id, status='completed', summary='removed'), {}
    monkeypatch.setattr(ops, 'dispatch', delete)
    monkeypatch.setattr(ops, 'review_dispatch', reviewer('APPROVE: fine'))
    run_id = call(ledger, 'dispatch', objective='remove', repo=str(repo))[0]['run_id']
    ops.process(ledger, run_id)
    assert 'deletes files' in call(ledger, 'approve', run_id=run_id, reason='ok')[0]['blockers']


def test_ask_owner_round_trip(env):
    ledger, _ = env
    asked, _ = call(ledger, 'ask_owner', question='Use Postgres or SQLite?', project='demo')
    assert call(ledger, 'inbox', question_id=asked['question_id'])[0]['status'] == 'open'
    assert ledger.answer(asked['question_id'], 'SQLite for now') and not ledger.answer(asked['question_id'], 'again')
    answered = call(ledger, 'inbox', question_id=asked['question_id'])[0]
    assert answered['status'] == 'answered' and answered['answer'] == 'SQLite for now'


def test_dispatch_validation(env):
    ledger, repo = env
    assert call(ledger, 'dispatch', objective='x', repo=str(repo), worker='grok')[1]
    assert call(ledger, 'dispatch', objective='x', repo=str(repo / 'missing'))[1]
    assert call(ledger, 'dispatch', objective='', repo=str(repo))[1]


def test_runner_lock_is_exclusive(tmp_path):
    first, second = ops.RunnerLock(tmp_path / 'r.lock'), ops.RunnerLock(tmp_path / 'r.lock')
    assert first.acquire() and not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_stdio_server_speaks_json_lines(tmp_path):
    env = {**os.environ, 'LOCALAPPDATA': str(tmp_path)}  # isolated ledger and runner lock
    proc = subprocess.Popen([sys.executable, '-m', 'merp_relay.mcp'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    try:
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
        proc.stdin.write(b''.join(json.dumps(m).encode() + b'\n' for m in messages))
        proc.stdin.flush()
        first, second = json.loads(proc.stdout.readline()), json.loads(proc.stdout.readline())
        assert first['id'] == 1 and first['result']['serverInfo']['name'] == 'merp-relay'
        assert second['id'] == 2 and len(second['result']['tools']) == len(mcp.TOOLS)
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_checkpoint_commits_relayhq(env, tmp_path, monkeypatch):
    ledger, _ = env
    hq = tmp_path / 'hq'
    hq.mkdir()
    (hq / 'AGENTS.md').write_text('lead\n')
    subprocess.run(['git', '-C', str(hq), 'init', '-q'], check=True)
    monkeypatch.setenv('MERP_HQ', str(hq))
    first, is_error = call(ledger, 'checkpoint', message='demo: first')
    assert not is_error and first['committed']
    assert call(ledger, 'checkpoint', message='demo: again')[0] == {'committed': False, 'note': 'nothing to commit'}
    monkeypatch.setenv('MERP_HQ', str(tmp_path))
    assert call(ledger, 'checkpoint', message='x')[1]  # not a RelayHQ


def test_dispatch_with_tier_and_dependency(env):
    ledger, repo = env
    first = call(ledger, 'dispatch', objective='a', repo=str(repo))[0]['run_id']
    queued, is_error = call(ledger, 'dispatch', objective='b', repo=str(repo), worker='codex',
                            model='gpt-6-luna', effort='low', depends_on=[first])
    assert not is_error and queued['note'].startswith('starts after')
    report = call(ledger, 'report', run_id=queued['run_id'])[0]
    assert report['options'] == {'model': 'gpt-6-luna', 'effort': 'low'} and report['depends_on'] == [first]
    assert report['dependency_state'] == 'wait'
    assert call(ledger, 'dispatch', objective='c', repo=str(repo), effort='ultra')[1]
    assert call(ledger, 'dispatch', objective='c', repo=str(repo), depends_on='r-x')[1]
    workers = call(ledger, 'workers')[0]
    assert workers['slots'] == ops.SLOTS and 'efforts' in workers['models']
