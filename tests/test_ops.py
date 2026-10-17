"""Worktree runner on a scratch repository; workers are fakes, git and subprocesses are real."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from merp_relay import ops
from merp_relay.adapters import CodexAdapter, normalize
from merp_relay.boundary import runtime_profile
from merp_relay.contracts import AgentResult, TaskRequest
from merp_relay.ledger import Ledger
from merp_relay.processes import run_bounded
from merp_relay.routing import route


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    for args in (['init', '-q'], ['config', 'user.email', 't@example.invalid'], ['config', 'user.name', 't']):
        subprocess.run(['git', '-C', str(repo), *args], check=True)
    (repo / 'app.txt').write_bytes(b'v1\n')  # LF on disk: Windows text pipes once broke these patches
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'init'], check=True)
    profile = runtime_profile(CodexAdapter(), {'version': 'codex-cli test'}).model_copy(
        update={'execution_modes': frozenset({'repository_access'})})
    monkeypatch.setattr(ops, 'WORKTREES', tmp_path / 'worktrees')
    monkeypatch.setattr(ops, 'available_profiles', lambda: {'codex': ('fake-adapter', profile)})
    monkeypatch.setattr(ops, 'observations', lambda: [])
    return Ledger(tmp_path / 'relay.db'), repo


def edit_worker(content='v2\n'):
    def worker(adapter, task, cwd, timeout, should_stop):
        (cwd / 'app.txt').write_text(content, encoding='utf-8')
        (cwd / 'new.txt').write_text('added\n', encoding='utf-8')
        return AgentResult(execution_id='e', task_id=task.task_id, status='completed', summary='edited'), {}
    return worker


def test_diff_needs_approval_then_applies_uncommitted(env, monkeypatch):
    ledger, repo = env
    monkeypatch.setattr(ops, 'dispatch', edit_worker())
    run_id = ops.submit(ledger, repo, 'bump version')
    assert ops.process(ledger, run_id) == 'needs_approval'
    run = ledger.run(run_id)
    assert '+v2' in run['diff'] and 'new.txt' in run['diff'] and run['resource_id'] == 'codex'
    assert (repo / 'app.txt').read_text() == 'v1\n' and not (ops.WORKTREES / run_id).exists()
    assert ops.approve(ledger, run_id) and ledger.status(run_id) == 'applied'
    assert (repo / 'app.txt').read_text() == 'v2\n' and (repo / 'new.txt').exists()
    head = subprocess.run(['git', '-C', str(repo), 'log', '--oneline'], capture_output=True, text=True).stdout
    assert len(head.splitlines()) == 1  # applied to the working tree, never committed
    assert not ops.approve(ledger, run_id)  # a second approve cannot apply twice


def test_reject_leaves_repository_untouched(env, monkeypatch):
    ledger, repo = env
    monkeypatch.setattr(ops, 'dispatch', edit_worker())
    run_id = ops.submit(ledger, repo, 'bump version')
    ops.process(ledger, run_id)
    assert ops.reject(ledger, run_id) and ledger.status(run_id) == 'rejected'
    assert (repo / 'app.txt').read_text() == 'v1\n' and not (repo / 'new.txt').exists()
    assert not ops.approve(ledger, run_id)


def test_conflicting_approve_keeps_waiting(env, monkeypatch):
    ledger, repo = env
    monkeypatch.setattr(ops, 'dispatch', edit_worker())
    run_id = ops.submit(ledger, repo, 'bump version')
    ops.process(ledger, run_id)
    (repo / 'app.txt').write_text('operator edit\n', encoding='utf-8')
    assert not ops.approve(ledger, run_id) and ledger.status(run_id) == 'needs_approval'
    assert any(e['type'] == 'apply_failed' for e in ledger.events(run_id))


def test_operator_cancel_stops_running_worker(env, monkeypatch):
    ledger, repo = env
    seen = {}
    def slow_worker(adapter, task, cwd, timeout, should_stop):
        ops.cancel(ledger, run_id)  # as the dashboard/CLI would, from outside the worker
        _, _, seen['cancellation'] = run_bounded([sys.executable, '-c', 'import time; time.sleep(30)'],
                                                 '', cwd, os.environ.copy(), 30, should_stop)
        return AgentResult(execution_id='e', task_id=task.task_id, status='cancelled', summary='stopped'), {}
    monkeypatch.setattr(ops, 'dispatch', slow_worker)
    run_id = ops.submit(ledger, repo, 'long task')
    assert ops.process(ledger, run_id) == 'cancelled'
    assert seen['cancellation']['reason'] == 'operator' and seen['cancellation']['local_process_exited']
    assert [e['type'] for e in ledger.events(run_id)].count('cancel_requested') == 1


def test_queued_cancel_and_no_resource_block(env, monkeypatch):
    ledger, repo = env
    first = ops.submit(ledger, repo, 'never runs')
    assert ops.cancel(ledger, first) and ledger.status(first) == 'cancelled'
    monkeypatch.setattr(ops, 'available_profiles', lambda: {})
    second = ops.submit(ledger, repo, 'no worker')
    assert ops.process(ledger, second) == 'blocked'


def test_worker_crash_is_recorded_and_worktree_removed(env, monkeypatch):
    ledger, repo = env
    def broken(*args):
        raise OSError('synthetic')
    monkeypatch.setattr(ops, 'dispatch', broken)
    run_id = ops.submit(ledger, repo, 'crash')
    assert ops.process(ledger, run_id) == 'failed' and not (ops.WORKTREES / run_id).exists()


def test_recover_fails_orphaned_runs(env):
    ledger, repo = env
    run_id = ops.submit(ledger, repo, 'orphan')
    ledger.transition(run_id, ['queued'], 'running')
    ops.recover(ledger)
    assert ledger.status(run_id) == 'failed'


def test_contained_route_waives_only_controls():
    task = TaskRequest(task_id='t', objective='o', kind='IMPLEMENT', execution_mode='repository_access',
                       completion_criteria=('c',), read_scope=('.',), boundary_requirements=frozenset({'x'}))
    profile = runtime_profile(CodexAdapter(), {}).model_copy(
        update={'execution_modes': frozenset({'repository_access'})})
    now = datetime.now(timezone.utc)
    assert route(task, [profile], [], [], [], now, 'c').outcome == 'blocked'
    assert route(task, [profile], [], [], [], now, 'c', contained=True).selected == 'codex'
    wrong_mode = task.model_copy(update={'execution_mode': 'snapshot_analysis'})
    repo_only = profile.model_copy(update={'execution_modes': frozenset({'repository_access'})})
    assert route(wrong_mode, [repo_only], [], [], [], now, 'c', contained=True).outcome == 'blocked'


def test_repo_normalization_uses_last_agent_message():
    final = json.dumps({'summary': 'done', 'findings': [], 'source_refs': [], 'unresolved': []})
    events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Looking at files...'}},
              {'type': 'item.completed', 'item': {'type': 'file_change'}},
              {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': final}},
              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}]
    text = '\n'.join(map(json.dumps, events))
    assert normalize(text, 0, 'e', 't', single_message=False)[0].status == 'completed'
    assert normalize(text, 0, 'e', 't')[0].status == 'failed'  # snapshot mode stays strict
