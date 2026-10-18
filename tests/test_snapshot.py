import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from pydantic import ValidationError
from merp_relay.adapters import ClaudeAdapter, CodexAdapter, execute_repo, execute_snapshot, normalize_claude
from merp_relay.boundary import BoundaryEvidence, admission_reasons, runtime_profile
from merp_relay.conformance import assess_cases
from merp_relay.contracts import Artifact, AgentResult, Control, SNAPSHOT_CONTROLS, TaskRequest
from merp_relay.processes import run_bounded, stop_local
from merp_relay.snapshot import SourceInput, SourcePacket, load_fixture, prompt_for, downstream_review
from merp_relay.routing import route
from datetime import datetime, timezone


def snapshot_task():
    return TaskRequest(task_id='t', objective='inspect', kind='INVESTIGATE', execution_mode='snapshot_analysis',
        completion_criteria=('cite',), read_scope=('behavior.py',))


def test_snapshot_modes_and_controls():
    task = snapshot_task()
    profile = runtime_profile(CodexAdapter(), {'version': 'synthetic'})
    assert admission_reasons(task, profile)
    d = route(task, [profile], [], [], [], datetime.now(timezone.utc), 'test')
    assert d.outcome == 'blocked'
    controls = {name: Control(state='supported_enforced', enforcer='synthetic runtime',
        evidence='design_hypothesis', source='fixture') for name in SNAPSHOT_CONTROLS}
    qualified = profile.model_copy(update={'controls': controls})
    assert not admission_reasons(task, qualified)
    assert admission_reasons(task.model_copy(update={'execution_mode': 'repository_access'}), qualified)
    for state in ('unknown', 'unsupported', 'observed_only'):
        bad = dict(controls)
        bad['model_tools_disabled'] = bad['model_tools_disabled'].model_copy(update={'state': state})
        assert admission_reasons(task, qualified.model_copy(update={'controls': bad}))


def test_packet_provenance_and_review_no_transcript():
    packet = load_fixture(Path('fixtures/investigation'))
    result = AgentResult(execution_id='e', task_id='t', status='completed', summary='author claim')
    artifact = Artifact.from_result(result)
    task, downstream = downstream_review('review', packet, artifact)
    prompt = prompt_for(task, downstream)
    assert downstream.sources == packet.sources and task.input_refs == (artifact.artifact_id,)
    assert 'author claim' in prompt and 'def clamp' in prompt and 'transcript' not in prompt
    assert all(s.provenance == 'relay_selected_source_data' for s in downstream.sources)
    assert 'never instructions' in prompt


def test_packet_integrity_and_bounds():
    source = SourceInput.create('a', 'text')
    with pytest.raises(ValidationError):
        SourceInput.model_validate({**source.model_dump(), 'text': 'tamper'})
    with pytest.raises(ValidationError):
        SourcePacket(sources=(SourceInput.create('x', 'a'*17000),))
    with pytest.raises(ValueError):
        prompt_for(snapshot_task().model_copy(update={'input_refs': ('missing',)}), SourcePacket(sources=(source,)))


@pytest.mark.parametrize('envelope', [None, [], {}, {'type': 'result', 'subtype': 'error'},
    {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': {'summary': 'missing'}},
    {'type': 'result', 'subtype': 'success', 'is_error': True}])
def test_claude_incomplete_and_malformed(envelope):
    result, usage = normalize_claude(json.dumps(envelope), 0, 'e', 't')
    assert result.status == 'failed' and usage['native_usage'] == {}


def test_claude_documented_shape_synthetic():
    envelope = {'type': 'result', 'subtype': 'success', 'is_error': False,
        'structured_output': {'summary': 'ok', 'findings': ['finding'], 'source_refs': ['a:1'], 'unresolved': []},
        'usage': {'input_tokens': 5, 'cache_read_input_tokens': 3, 'private': 'secret'}}
    result, usage = normalize_claude(json.dumps(envelope), 0, 'e', 't')
    assert result.status == 'completed' and result.findings[0].provenance == 'worker_reported'
    assert usage['native_usage'] == {'input_tokens': 5, 'cache_read_input_tokens': 3}


def test_sanitized_error():
    result, metrics = normalize_claude('token=DO_NOT_PRINT_SECRET', 1, 'e', 't')
    assert 'DO_NOT_PRINT' not in result.model_dump_json() and 'DO_NOT_PRINT' not in str(metrics)
    assert result.summary == 'process_error'


def test_refusal_cannot_prove_enforcement():
    with pytest.raises(ValidationError):
        BoundaryEvidence(case_id='x', outcome='voluntary_refusal', enforced=True, evidence='worker said no')
    evidence = assess_cases({'event_types': ['turn.completed'], 'tool_item_types': []}, 'same', 'same')
    assert all(e.outcome == 'no_action_observed' and not e.enforced for e in evidence)
    assert all(e.outcome == 'inconclusive' for e in assess_cases({}, 'same', 'same'))


def test_blocked_inference_never_launches(monkeypatch):
    adapter = ClaudeAdapter()
    monkeypatch.setattr(adapter, 'probe', lambda: {'status': 'unavailable', 'reason': 'auth unavailable'})
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('inference launched'))
    result, _ = execute_snapshot(adapter, snapshot_task(), SourcePacket(sources=(SourceInput.create('behavior.py', ''),)), weaker_experiment=True)
    assert result.status == 'blocked'


def test_normal_admission_blocks_even_authenticated(monkeypatch):
    adapter = CodexAdapter()
    monkeypatch.setattr(adapter, 'probe', lambda: {'status': 'available', 'version': 'codex-cli 0.160.0'})
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('inference launched'))
    result, _ = execute_snapshot(adapter, snapshot_task(),
        SourcePacket(sources=(SourceInput.create('behavior.py', ''),)))
    assert result.status == 'blocked'


def test_claude_safe_stdin_command_synthetic(monkeypatch):
    adapter = ClaudeAdapter()
    monkeypatch.setattr(adapter, 'probe', lambda: {'status': 'available', 'version': '2.1.240 (Claude Code)'})
    seen = {}
    def fake_run(command, prompt, cwd, env, timeout):
        seen.update(command=command, prompt=prompt, env=env)
        envelope = {'type': 'result', 'subtype': 'success', 'is_error': False,
            'structured_output': {'summary': 'ok', 'findings': [], 'source_refs': [], 'unresolved': []}}
        return json.dumps(envelope), 0, None
    monkeypatch.setattr('merp_relay.adapters.run_bounded', fake_run)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-secret')
    packet = SourcePacket(sources=(SourceInput.create('behavior.py', 'unsafe shell string'),))
    result, _ = execute_snapshot(adapter, snapshot_task(), packet, weaker_experiment=True)
    assert result.status == 'completed'
    assert '--safe-mode' in seen['command'] and seen['command'][seen['command'].index('--tools')+1] == ''
    assert 'unsafe shell string' in seen['prompt'] and 'unsafe shell string' not in str(seen['command'])
    assert 'ANTHROPIC_API_KEY' not in seen['env'] and '--bare' not in seen['command']


def test_claude_repo_run_gets_resolved_cwd(monkeypatch, tmp_path):
    # Claude denies unresolved Windows paths (8.3 short names) as suspicious, so cwd is resolved first.
    adapter = ClaudeAdapter()
    monkeypatch.setattr(adapter, 'probe', lambda: {'status': 'available', 'version': '2.1.240 (Claude Code)'})
    seen = {}
    def fake_run(command, prompt, cwd, env, timeout, should_stop):
        seen.update(command=command, cwd=cwd)
        envelope = {'type': 'result', 'subtype': 'success', 'is_error': False,
            'structured_output': {'summary': 'ok', 'findings': [], 'source_refs': [], 'unresolved': []}}
        return json.dumps(envelope), 0, None
    monkeypatch.setattr('merp_relay.adapters.run_bounded', fake_run)
    monkeypatch.setattr('merp_relay.quota.record_claude', lambda infos: None)
    task = TaskRequest(task_id='t', objective='edit', kind='IMPLEMENT', execution_mode='repository_access',
        completion_criteria=('done',), read_scope=('.',))
    result, metrics = execute_repo(adapter, task, tmp_path / 'sub' / '..')
    assert result.status == 'completed' and metrics['runtime_version'] == '2.1.240 (Claude Code)'
    assert seen['cwd'] == tmp_path.resolve()
    assert seen['command'][seen['command'].index('--permission-mode') + 1] == 'acceptEdits'


def test_scope_mismatch_rejected():
    with pytest.raises(ValueError):
        prompt_for(snapshot_task(), SourcePacket(sources=(SourceInput.create('other.txt', ''),)))


def test_timeout_real_harmless_subprocess(tmp_path):
    out, code, state = run_bounded([sys.executable, '-c', 'import time; time.sleep(30)'],
        '', tmp_path, os.environ.copy(), .2)
    assert state['cancellation_requested'] and state['local_process_exited']
    assert state['local_tree_termination'] == 'unknown' and state['remote_execution'] == 'unknown'


@pytest.mark.skipif(os.name != 'nt', reason='Windows taskkill descendant check')
def test_windows_descendant_cleanup(tmp_path):
    child_pid = tmp_path / 'child.pid'
    script = ('import subprocess,sys,time,pathlib; '
        'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
        'pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)')
    parent = subprocess.Popen([sys.executable, '-c', script, str(child_pid)])
    try:
        deadline = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert child_pid.exists()
        pid = int(child_pid.read_text())
        state = stop_local(parent)
        listing = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                                 capture_output=True, text=True, timeout=5)
        assert f'"{pid}"' not in listing.stdout
        assert state['local_process_exited'] and state['remote_execution'] == 'unknown'
    finally:
        if parent.poll() is None:
            stop_local(parent)
