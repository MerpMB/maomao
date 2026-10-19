"""Owner usage caps: a worker over its 5-hour cap (or with unknown usage) gets no new runs and no reviews."""
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from merp_relay import ops
from merp_relay.adapters import ClaudeAdapter, CodexAdapter
from merp_relay.boundary import runtime_profile
from merp_relay.contracts import AgentResult
from merp_relay.ledger import Ledger
from merp_relay.quota import _observation


def observed(used, age_minutes=1):
    at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    windows = [{'name': '300m', 'used': used, 'resets_at': at + timedelta(hours=4)},
               {'name': '10080m', 'used': 0.5, 'resets_at': at + timedelta(days=3)}]
    return _observation('codex-local', 'fixture', at, windows, False), windows


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
    reviewers = []
    monkeypatch.setattr(ops, 'review_dispatch', lambda a, task, p, s: (reviewers.append(a), (
        AgentResult(execution_id='r', task_id=task.task_id, status='completed', summary='APPROVE: ok'), {}))[1])
    usage = {'codex': observed(0.2)}
    monkeypatch.setitem(ops.CAP_READERS, 'codex', lambda: usage['codex'])
    monkeypatch.setitem(ops.CAP_READERS, 'claude', lambda: (None, []))
    ledger = Ledger(tmp_path / 'relay.db')
    ledger.set_setting('cap.codex', '0.6')
    return ledger, repo, usage, reviewers


def worked(names):
    def run(adapter, task, cwd, timeout, should_stop, **kw):
        names.append(adapter)
        (cwd / f'{task.task_id}.txt').write_text('x\n', encoding='utf-8')
        return AgentResult(execution_id='e', task_id=task.task_id, status='completed', summary='done'), {}
    return run


def test_cap_reasons_cover_over_cap_and_unknown_usage(env):
    ledger, _, usage, _ = env
    assert ops.capped(ledger, fresh=True) == {}
    usage['codex'] = observed(0.6)
    assert 'reached 60%' in ops.capped(ledger, fresh=True)['codex']
    usage['codex'] = (None, [])
    assert 'unknown' in ops.capped(ledger, fresh=True)['codex']
    usage['codex'] = observed(0.1, age_minutes=600)  # expired observation: usage unknown
    assert 'unknown' in ops.capped(ledger, fresh=True)['codex']
    ledger.set_setting('cap.codex', '')
    assert ops.capped(ledger, fresh=True) == {}


def test_capped_codex_routes_to_claude_and_a_pinned_run_waits(env, monkeypatch):
    ledger, repo, usage, reviewers = env
    usage['codex'] = observed(0.75)
    ops.capped(ledger, fresh=True)
    names = []
    monkeypatch.setattr(ops, 'dispatch', worked(names))
    free = ops.submit(ledger, repo, 'any worker')
    ops.work(ledger)
    assert ledger.status(free) == 'needs_approval' and names == ['claude-adapter']
    assert reviewers == []  # the only other reviewer is codex, which is capped
    skipped = [e for e in ledger.events(free) if e['type'] == 'review_skipped']
    assert skipped and 'owner cap' in skipped[0]['data']['reason']
    pinned = ops.submit(ledger, repo, 'codex only', worker='codex')
    stop = {'n': 0}
    def sleep(_):  # let the scheduler loop twice, then lift the cap so it can finish
        stop['n'] += 1
        if stop['n'] == 2:
            usage['codex'] = observed(0.3)
            ops.capped(ledger, fresh=True)
    monkeypatch.setattr(ops.time, 'sleep', sleep)
    ops.work(ledger, watch=False)
    waits = [e for e in ledger.events(pinned) if e['type'] == 'waiting']
    assert len(waits) == 1 and 'reached 60%' in waits[0]['data']['reason']
