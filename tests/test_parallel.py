"""P2: parallel scheduler, per-worker caps, dependencies, staged-aware bases, fresh retries, tiers."""
import json
import subprocess
import threading

import pytest
from merp_relay import ops
from merp_relay.adapters import ClaudeAdapter, CodexAdapter, execute_repo
from merp_relay.boundary import runtime_profile
from merp_relay.contracts import AgentResult, TaskRequest
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
    monkeypatch.setattr(ops, 'review_dispatch', lambda a, task, p, s: (
        AgentResult(execution_id='r', task_id=task.task_id, status='completed', summary='APPROVE: ok'), {}))
    return Ledger(tmp_path / 'relay.db'), repo


def writer(name, text, hook=None):
    def run(adapter, task, cwd, timeout, should_stop, **kw):
        if hook:
            hook(cwd)
        (cwd / name).write_text(text, encoding='utf-8')
        return AgentResult(execution_id='e', task_id=task.task_id, status='completed', summary='done'), {}
    return run


def test_independent_runs_execute_in_parallel(env, monkeypatch):
    ledger, repo = env
    barrier = threading.Barrier(2, timeout=10)  # breaks (fails the runs) if they ran one after another
    def worker(adapter, task, cwd, timeout, should_stop, **kw):
        barrier.wait()
        (cwd / f'{task.task_id}.txt').write_text('x\n', encoding='utf-8')
        return AgentResult(execution_id='e', task_id=task.task_id, status='completed', summary='done'), {}
    monkeypatch.setattr(ops, 'dispatch', worker)
    first = ops.submit(ledger, repo, 'a')
    second = ops.submit(ledger, repo, 'b')
    ops.work(ledger)
    assert ledger.status(first) == ledger.status(second) == 'needs_approval'


def test_per_worker_cap_limits_concurrency(env, monkeypatch):
    ledger, repo = env
    ledger.set_setting('per_worker', '1')
    lock, active, peak = threading.Lock(), [0], [0]
    def worker(adapter, task, cwd, timeout, should_stop, **kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        threading.Event().wait(0.3)
        with lock:
            active[0] -= 1
        return AgentResult(execution_id='e', task_id=task.task_id, status='completed', summary='nothing'), {}
    monkeypatch.setattr(ops, 'dispatch', worker)
    ids = [ops.submit(ledger, repo, f't{i}', worker='codex') for i in range(3)]
    ops.work(ledger)
    assert peak[0] == 1 and all(ledger.status(i) == 'completed' for i in ids)


def test_dependency_waits_for_apply_and_builds_on_it(env, monkeypatch):
    ledger, repo = env
    monkeypatch.setattr(ops, 'dispatch', writer('app.txt', 'v2\n'))
    first = ops.submit(ledger, repo, 'bump')
    seen = {}
    second = ops.submit(ledger, repo, 'changelog', depends_on=[first])
    ops.work(ledger)
    assert ledger.status(first) == 'needs_approval' and ledger.status(second) == 'queued'  # waits
    assert ops.approve(ledger, first)  # staged in the repo, not committed
    monkeypatch.setattr(ops, 'dispatch', writer('CHANGELOG.md', 'v2\n',
                                                hook=lambda cwd: seen.update(app=(cwd / 'app.txt').read_text())))
    ops.work(ledger)
    assert seen['app'] == 'v2\n'  # the dependent run saw the approved change
    diff = ledger.run(second)['diff']
    assert 'CHANGELOG.md' in diff and 'app.txt' not in diff  # its diff is only its own work
    assert ops.approve(ledger, second)
    assert (repo / 'app.txt').read_text() == 'v2\n' and (repo / 'CHANGELOG.md').exists()


def test_failed_or_rejected_dependency_blocks(env, monkeypatch):
    ledger, repo = env
    monkeypatch.setattr(ops, 'dispatch', writer('app.txt', 'v2\n'))
    first = ops.submit(ledger, repo, 'bump')
    second = ops.submit(ledger, repo, 'after', depends_on=[first])
    ops.work(ledger)
    ops.reject(ledger, first)
    ops.work(ledger)
    assert ledger.status(second) == 'blocked' and first in ledger.run(second)['summary']
    with pytest.raises(ValueError):
        ops.submit(ledger, repo, 'x', depends_on=['r-missing'])


def test_dependency_follows_revision(env, monkeypatch):
    ledger, repo = env
    monkeypatch.setattr(ops, 'dispatch', writer('app.txt', 'v2\n'))
    first = ops.submit(ledger, repo, 'bump')
    second = ops.submit(ledger, repo, 'after', depends_on=[first])
    ops.work(ledger)
    retry = ops.revise(ledger, first, 'use v3')
    assert ops.dependency_state(ledger, ledger.run(second)) == 'wait'
    ops.work(ledger)
    assert ops.approve(ledger, retry)
    assert ops.dependency_state(ledger, ledger.run(second)) == 'ready'


def test_fresh_revision_starts_from_current_state(env, monkeypatch):
    ledger, repo = env
    monkeypatch.setattr(ops, 'dispatch', writer('app.txt', 'v2\n'))
    stale = ops.submit(ledger, repo, 'bump')
    ops.work(ledger)
    (repo / 'app.txt').write_bytes(b'owner\n')  # repo moved on; the old diff no longer applies
    subprocess.run(['git', '-C', str(repo), 'commit', '-qam', 'owner change'], check=True)
    assert not ops.approve(ledger, stale)
    seen = {}
    monkeypatch.setattr(ops, 'dispatch', writer('app.txt', 'v3\n',
                                                hook=lambda cwd: seen.update(start=(cwd / 'app.txt').read_text())))
    redo = ops.revise(ledger, stale, 'apply to the new version', fresh=True)
    assert ledger.run(redo)['options'] == {'fresh': True}
    ops.work(ledger)
    assert seen['start'] == 'owner\n' and ops.approve(ledger, redo)


def test_owner_staged_changes_are_base_not_diff(env, monkeypatch):
    ledger, repo = env
    (repo / 'notes.txt').write_text('mine\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(repo), 'add', 'notes.txt'], check=True)
    monkeypatch.setattr(ops, 'dispatch', writer('app.txt', 'v2\n'))
    run_id = ops.submit(ledger, repo, 'bump')
    ops.work(ledger)
    diff = ledger.run(run_id)['diff']
    assert 'app.txt' in diff and 'notes.txt' not in diff
    assert ops.approve(ledger, run_id)


def test_tier_options_validated_and_passed_to_cli(env, monkeypatch, tmp_path):
    ledger, repo = env
    for bad in ({'effort': 'ultra'}, {'model': 'gpt; rm -rf'}, {'temperature': 1}):
        with pytest.raises(ValueError):
            ops.submit(ledger, repo, 'x', options=bad)
    seen = {}
    def fake_run(command, prompt, cwd, env, timeout, should_stop):
        seen.setdefault('commands', []).append(command)
        result = {'type': 'result', 'subtype': 'success', 'is_error': False,
                  'structured_output': {'summary': 'ok', 'findings': [], 'source_refs': [], 'unresolved': []}}
        return json.dumps(result), 0, None
    monkeypatch.setattr('merp_relay.adapters.run_bounded', fake_run)
    monkeypatch.setattr('merp_relay.quota.record_claude', lambda infos: None)
    monkeypatch.setattr('merp_relay.adapters.codex_executable', lambda: 'codex')
    task = TaskRequest(task_id='t', objective='o', kind='IMPLEMENT', execution_mode='repository_access',
                       completion_criteria=('c',), read_scope=('.',))
    claude, codex = ClaudeAdapter(), CodexAdapter()
    for adapter in (claude, codex):
        monkeypatch.setattr(adapter, 'probe', lambda: {'status': 'available', 'version': 'v'})
    execute_repo(claude, task, tmp_path, model='sonnet', effort='high')
    execute_repo(codex, task, tmp_path, model='gpt-6-luna', effort='low')
    claude_cmd, codex_cmd = seen['commands']
    assert claude_cmd[claude_cmd.index('--model') + 1] == 'sonnet' and claude_cmd[claude_cmd.index('--effort') + 1] == 'high'
    assert codex_cmd[codex_cmd.index('-m') + 1] == 'gpt-6-luna' and 'model_reasoning_effort="low"' in codex_cmd


def test_base_commit_names_included_approved_runs(env, monkeypatch):
    ledger, repo = env
    monkeypatch.setattr(ops, 'dispatch', writer('app.txt', 'v2\n'))
    first = ops.submit(ledger, repo, 'bump')
    ops.work(ledger)
    assert ops.approve(ledger, first)
    seen = {}
    def check(cwd):
        seen['log'] = subprocess.run(['git', '-C', str(cwd), 'log', '-1', '--format=%s'],
                                     capture_output=True, text=True).stdout
    monkeypatch.setattr(ops, 'dispatch', writer('notes.txt', 'x\n', hook=check))
    second = ops.submit(ledger, repo, 'after', depends_on=[first])
    ops.work(ledger)
    assert first in seen['log'] and 'approved runs' in seen['log']
