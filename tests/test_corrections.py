"""Correction regressions: provider interfaces are mocked; subprocesses are harmless Python."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from merp_relay.adapters import ClaudeAdapter, CodexAdapter, execute_snapshot, normalize
from merp_relay.boundary import admission_reasons, runtime_profile
from merp_relay.contracts import Control, SNAPSHOT_CONTROLS, TaskRequest
from merp_relay.eligibility import task_profile_eligibility
from merp_relay.processes import run_bounded, stop_local
from merp_relay.routing import route
from merp_relay.snapshot import SourceInput, SourcePacket


def task(**changes):
    values = dict(task_id='t', objective='inspect', kind='INVESTIGATE', execution_mode='snapshot_analysis',
                  completion_criteria=('cite',), read_scope=('source',))
    values.update(changes)
    return TaskRequest(**values)


def qualified_profile():
    profile = runtime_profile(CodexAdapter(), {'version': 'codex-cli 0.160.0'})
    controls = {name: Control(state='supported_enforced', enforcer='synthetic fixture',
        evidence='design_hypothesis', source='unit test', validation_basis='synthetic test only')
        for name in SNAPSHOT_CONTROLS}
    return profile.model_copy(update={'controls': controls})


@pytest.mark.parametrize('changes', [{}, {'capabilities': frozenset({'missing'})},
    {'tools': frozenset({'shell'})}, {'execution_mode': 'repository_access'},
    {'boundary_requirements': frozenset({'extra_control'})}])
def test_router_admission_agree(changes):
    request, profile = task(**changes), qualified_profile()
    reasons = admission_reasons(request, profile)
    decision = route(request, [profile], [], [], [], datetime.now(timezone.utc), 'test')
    assert bool(reasons) == (decision.outcome == 'blocked')
    assert tuple(reasons) == decision.excluded.get(profile.resource_id, ())
    assert tuple(reasons) == task_profile_eligibility(request, profile).reasons


@pytest.mark.parametrize('weaker', [False, True])
@pytest.mark.parametrize('changes', [{'capabilities': frozenset({'missing'})},
    {'tools': frozenset({'shell'})}, {'execution_mode': 'repository_access'}])
def test_incompatible_dispatch_never_runs(monkeypatch, changes, weaker):
    adapter = CodexAdapter()
    monkeypatch.setattr(adapter, 'probe', lambda: {'status': 'available', 'version': 'codex-cli 0.160.0'})
    monkeypatch.setattr('merp_relay.boundary.runtime_profile', lambda *args: qualified_profile())
    monkeypatch.setattr('merp_relay.adapters.run_bounded', lambda *a: pytest.fail('runner reached'))
    result, _ = execute_snapshot(adapter, task(**changes),
        SourcePacket(sources=(SourceInput.create('source', ''),)), weaker_experiment=weaker)
    assert result.status == 'blocked'


def test_repository_mode_does_not_require_snapshot_controls():
    profile = qualified_profile().model_copy(update={'execution_modes': frozenset({'repository_access'}), 'controls': {}})
    assert not admission_reasons(task(execution_mode='repository_access'), profile)


OUTPUT = json.dumps({'summary': 'ok', 'findings': [], 'source_refs': [], 'unresolved': []})


def event(lifecycle, text=OUTPUT):
    return {'type': lifecycle, 'item': {'type': 'agent_message', 'text': text}}


END = {'type': 'turn.completed'}


@pytest.mark.parametrize('events,completed', [
    ([event('item.started'), END], False),
    ([event('item.updated'), END], False),
    ([event('item.started', '{'), event('item.updated', 'not json'), event('item.completed'), END], True),
    ([event('item.completed')], False),
    ([event('item.completed'), {'type': 'turn.failed'}, END], False),
    ([event('item.completed'), {'type': 'error'}, END], False),
    ([event('item.completed', '{'), END], False),
    ([event('item.completed'), event('item.completed'), END], False),
    ([event('item.completed', '{'), event('item.completed'), END], False),
    ([END, event('item.completed')], False),
    ([event('item.completed'), END, END], False),
    ([event('item.completed'), END], True),
])
def test_codex_lifecycle(events, completed):
    result, _ = normalize('\n'.join(json.dumps(e) for e in events), 0, 'e', 't')
    assert (result.status == 'completed') is completed


VALID_AUTH = {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty'}


@pytest.mark.parametrize('stdout,version_code,auth_code,version,available', [
    ('not-json', 0, 0, '2.1.240 (Claude Code)', False),
    ('[]', 0, 0, '2.1.240 (Claude Code)', False),
    ('null', 0, 0, '2.1.240 (Claude Code)', False),
    ('{}', 0, 0, '2.1.240 (Claude Code)', False),
    (json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}), 0, 0, '2.1.240 (Claude Code)', False),
    (json.dumps({**VALID_AUTH, 'loggedIn': 'true'}), 0, 0, '2.1.240 (Claude Code)', False),
    (json.dumps({**VALID_AUTH, 'authMethod': []}), 0, 0, '2.1.240 (Claude Code)', False),
    (json.dumps({**VALID_AUTH, 'apiProvider': 1}), 0, 0, '2.1.240 (Claude Code)', False),
    (json.dumps(VALID_AUTH), 1, 0, '2.1.240 (Claude Code)', False),
    (json.dumps(VALID_AUTH), 0, 1, '2.1.240 (Claude Code)', False),
    (json.dumps(VALID_AUTH), 0, 0, 'unknown-version', False),
    (json.dumps(VALID_AUTH), 0, 0, '2.2.0 (Claude Code)', True),
    (json.dumps({**VALID_AUTH, 'authMethod': 'api_key'}), 0, 0, '2.1.240 (Claude Code)', False),
    (json.dumps({**VALID_AUTH, 'apiProvider': 'thirdParty'}), 0, 0, '2.1.240 (Claude Code)', False),
    (json.dumps(VALID_AUTH), 0, 0, '2.1.240 (Claude Code)', True),
])
def test_claude_probe_validation(monkeypatch, stdout, version_code, auth_code, version, available):
    responses = iter([SimpleNamespace(returncode=version_code, stdout=version),
                      SimpleNamespace(returncode=auth_code, stdout=stdout)])
    monkeypatch.setattr('shutil.which', lambda name: 'synthetic-cli')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: next(responses))
    probe = ClaudeAdapter().probe()
    assert (probe['status'] == 'available') is available
    assert stdout not in probe.get('reason', '')


@pytest.mark.parametrize('adapter', [ClaudeAdapter(), CodexAdapter()])
@pytest.mark.parametrize('probe', [{}, {'version': 'unknown'},
    {'version': '2.1.240 (Claude Code)'},
    {'status': 'available', 'version': '2.1.240 (Claude Code)', 'subscription_authenticated': True},
    {'status': 'available', 'version': 'codex-cli 0.160.0', 'subscription_authenticated': True}])
def test_documentation_never_qualifies_runtime_controls(adapter, probe):
    profile = runtime_profile(adapter, probe)
    for control in profile.controls.values():
        assert control.state == 'unknown'
        assert control.runtime_version == probe.get('version')
        assert control.validation_basis and control.source
    assert admission_reasons(task(), profile)


def test_claude_support_separate_from_qualification():
    profile = runtime_profile(ClaudeAdapter(), {'version': '2.1.240 (Claude Code)'})
    assert profile.controls['model_tools_disabled'].interface_support == 'documented_supported'
    assert profile.controls['model_tools_disabled'].state == 'unknown'
    other = runtime_profile(ClaudeAdapter(), {'version': 'future'})
    assert other.controls['model_tools_disabled'].interface_support == 'unknown'


@pytest.mark.parametrize('adapter', [CodexAdapter(), ClaudeAdapter()])
def test_public_cancel_unsupported(adapter):
    outcome = adapter.cancel()
    assert not outcome['supported'] and not outcome['cancellation_requested']
    assert outcome['local_process_exited'] is None and outcome['remote_execution'] == 'unknown'


def test_no_active_parent_requires_no_request(tmp_path):
    p = subprocess.Popen([sys.executable, '-c', 'pass'])
    p.wait(timeout=5)
    outcome = stop_local(p)
    assert not outcome['cancellation_requested'] and outcome['local_process_exited']
    assert outcome['local_tree_termination'] == 'unknown'


def test_cleanup_errors_are_bounded_and_not_success(monkeypatch):
    class Unstoppable:
        pid = 123
        def poll(self): return None
        def kill(self): raise OSError('SENSITIVE diagnostic')
        def wait(self, timeout): raise subprocess.TimeoutExpired('synthetic', timeout)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(OSError('SENSITIVE')))
    state = stop_local(Unstoppable())
    assert state['cancellation_requested'] and not state['local_process_exited']
    assert 'SENSITIVE' not in str(state) and state['remote_execution'] == 'unknown'


@pytest.mark.parametrize('exited,expected', [(False, 'failed'), (True, 'cancelled')])
def test_dispatch_timeout_exit_truth(monkeypatch, exited, expected):
    adapter = CodexAdapter()
    monkeypatch.setattr(adapter, 'probe', lambda: {'status': 'available', 'version': 'codex-cli 0.160.0'})
    state = {'cancellation_requested': True, 'local_process_exited': exited,
             'local_tree_termination': 'unknown', 'remote_execution': 'unknown'}
    monkeypatch.setattr('merp_relay.adapters.run_bounded', lambda *args: ('', None, state))
    result, metrics = execute_snapshot(adapter, task(), SourcePacket(sources=(SourceInput.create('source', ''),)),
                                       weaker_experiment=True)
    assert result.status == expected and metrics['cancellation'] == state


def test_stdin_and_stderr_handling_real_python(tmp_path):
    script = 'import sys; print(sys.stdin.read()); print("PRIVATE", file=sys.stderr)'
    out, code, state = run_bounded([sys.executable, '-c', script], 'source packet',
                                   tmp_path, os.environ.copy(), 5)
    assert code == 0 and state is None and out.strip() == 'source packet'
    assert 'PRIVATE' not in out
