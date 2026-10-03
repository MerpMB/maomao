"""Bounded snapshot adapters with fail-closed admission and sanitized normalization.

This is not an isolation boundary for real repositories.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError
from .contracts import AgentResult, Claim, TaskRequest
from .processes import run_bounded
from .snapshot import SourcePacket, SourceInput, prompt_for


class WorkerOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    summary: str
    findings: list[str]
    source_refs: list[str]
    unresolved: list[str]


def classify_error(events, returncode):
    # Only explicit machine codes count. Fixture coverage, not observed quota events.
    codes = []
    for event in events:
        error = event.get('error')
        if isinstance(error, dict):
            codes.append(error.get('code'))
    if 'usage_limit_reached' in codes:
        return 'exhausted'
    if 'authentication_required' in codes:
        return 'authentication'
    return 'process_error' if returncode else 'malformed_output'


def normalize(text, returncode, execution_id, task_id):
    events, output = [], None
    malformed = False
    for line in text.splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                malformed = True
                continue
            events.append(event)
            item = event.get('item')
            if isinstance(item, dict) and item.get('type') == 'agent_message':
                output = WorkerOutput.model_validate_json(item.get('text', ''))
        except (ValueError, TypeError, ValidationError):
            malformed = True
    failed_event = any(e.get('type') in ('error', 'turn.failed') for e in events)
    completed = any(e.get('type') == 'turn.completed' for e in events)
    if returncode or malformed or failed_event or output is None or not completed:
        return AgentResult(execution_id=execution_id, task_id=task_id, status='failed',
            summary=classify_error(events, returncode), unresolved=('No verified structured completion',)), events
    result = AgentResult(execution_id=execution_id, task_id=task_id, status='completed',
        summary=output.summary, findings=tuple(Claim(text=f, source_refs=tuple(output.source_refs),
        provenance='worker_reported') for f in output.findings), unresolved=tuple(output.unresolved))
    return result, events


class CodexAdapter:
    def __init__(self):
        self.process = None

    def probe(self):
        executable = shutil.which('codex')
        if not executable:
            return {'status': 'unavailable', 'reason': 'CLI missing'}
        try:
            p = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=10)
            auth = subprocess.run([executable, 'login', 'status'], capture_output=True, text=True, timeout=10)
            subscription = auth.returncode == 0 and 'Logged in using ChatGPT' in (auth.stdout + auth.stderr)
            return {'status': 'available' if p.returncode == 0 and subscription else 'unavailable',
                    'subscription_authenticated': subscription,
                    'version': p.stdout.strip(), 'boundary': 'disposable_snapshot_only'}
        except (OSError, subprocess.TimeoutExpired) as e:
            return {'status': 'unavailable', 'reason': type(e).__name__}

    def cancel(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
        return {'local_stop_requested': True, 'remote_stopped': 'unknown',
                'descendant_processes_stopped': 'unknown'}

    def execute(self, task: TaskRequest, snapshot: str, *, disposable=False, timeout=45):
        if not disposable or task.execution_mode != 'snapshot_analysis' or task.tools:
            raise ValueError('adapter accepts explicit disposable snapshot mode only')
        packet = SourcePacket(sources=(SourceInput.create(
            'legacy-snapshot', snapshot),))
        task = task.model_copy(update={'read_scope': ('legacy-snapshot',)})
        result, metrics = execute_snapshot(self, task, packet, weaker_experiment=True, timeout=timeout)
        return result, []



class ClaudeAdapter:
    """Supported snapshot interface; inference stays blocked without subscription auth."""
    def probe(self):
        executable = shutil.which('claude')
        if not executable:
            return {'status': 'unavailable', 'reason': 'CLI missing'}
        try:
            version = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=10)
            auth = subprocess.run([executable, 'auth', 'status'], capture_output=True, text=True, timeout=10)
            status = json.loads(auth.stdout)
            subscription = status.get('loggedIn') is True and status.get('authMethod') == 'claude.ai'
            return {'status': 'available' if subscription else 'unavailable',
                'version': version.stdout.strip(), 'subscription_authenticated': subscription,
                'reason': 'subscription status verified' if subscription else 'supported subscription auth unavailable'}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {'status': 'unavailable', 'reason': 'probe failed; details suppressed'}

    def execute(self, task, packet=None, **kwargs):
        if packet is not None:
            return execute_snapshot(self, task, packet, **kwargs)
        return AgentResult(execution_id=str(uuid4()), task_id=task.task_id,
            status='blocked', summary=self.probe()['reason']), []

    def cancel(self):
        return {'local_stop_requested': False, 'remote_stopped': 'unknown'}


def normalize_claude(text, returncode, execution_id, task_id):
    """Documented result envelope; fixture-tested until authenticated execution exists."""
    try:
        envelope = json.loads(text)
        if not isinstance(envelope, dict) or envelope.get('type') != 'result' or \
                envelope.get('subtype') != 'success' or envelope.get('is_error') is not False or returncode:
            raise ValueError('incomplete result')
        output = WorkerOutput.model_validate(envelope.get('structured_output'))
        result = AgentResult(execution_id=execution_id, task_id=task_id, status='completed',
            summary=output.summary, findings=tuple(Claim(text=f, source_refs=tuple(output.source_refs),
            provenance='worker_reported') for f in output.findings), unresolved=tuple(output.unresolved))
        usage = envelope.get('usage', {})
        counters = {k: v for k, v in usage.items() if type(v) is int and v >= 0} if isinstance(usage, dict) else {}
        return result, {'native_usage': counters, 'accounting_scope': 'runtime result envelope'}
    except (ValueError, TypeError, ValidationError):
        return AgentResult(execution_id=execution_id, task_id=task_id, status='failed',
            summary='process_error' if returncode else 'malformed_or_incomplete_output'), {'native_usage': {}}


def execute_snapshot(adapter, task, packet, *, weaker_experiment=False, timeout=45, workdir=None):
    """Explicit weaker experiment never implies normal snapshot admission."""
    from .boundary import runtime_profile, admission_reasons
    prompt = prompt_for(task, packet)
    probe = adapter.probe()
    identity = str(uuid4())
    blocked = admission_reasons(task, runtime_profile(adapter, probe))
    if probe['status'] != 'available' or (blocked and not weaker_experiment):
        return AgentResult(execution_id=identity, task_id=task.task_id, status='blocked',
            summary='; '.join(blocked) if blocked else probe.get('reason', 'runtime unavailable')), {
            'admission': blocked, 'native_usage': {}}
    # Pin to interfaces actually inspected. Upgrade requires revalidation.
    is_claude = isinstance(adapter, ClaudeAdapter)
    version_matches = probe.get('version') == ('2.1.240 (Claude Code)' if is_claude else 'codex-cli 0.160.0')
    if not version_matches:
        return AgentResult(execution_id=identity, task_id=task.task_id, status='blocked',
            summary='runtime version requires interface revalidation'), {'native_usage': {}}
    env = os.environ.copy()
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix='merp-snapshot-') as temp:
        schema = Path(temp) / 'schema.json'
        schema.write_text(json.dumps(WorkerOutput.model_json_schema()), encoding='utf-8')
        if is_claude:
            command = [shutil.which('claude'), '-p', '--safe-mode', '--tools', '',
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--no-chrome',
                '--disable-slash-commands', '--permission-mode', 'dontAsk',
                '--no-session-persistence', '--output-format', 'json', '--json-schema', schema.read_text()]
        else:
            command = [shutil.which('codex'), 'exec', '--ignore-user-config', '--ignore-rules',
                '--ephemeral', '--skip-git-repo-check', '--sandbox', 'read-only', '--json',
                '--disable', 'shell_tool', '--disable', 'unified_exec',
                '-c', 'web_search="disabled"', '-c', 'approval_policy="never"',
                '--output-schema', str(schema), '-']
        try:
            stdout, code, cancellation = run_bounded(command, prompt, workdir or temp, env, timeout)
        except OSError:
            return AgentResult(execution_id=identity, task_id=task.task_id, status='blocked',
                summary='runtime launch failed; diagnostics suppressed'), {'native_usage': {}}
        metrics = {'packet_bytes': len(packet.model_dump_json().encode()),
                   'prompt_bytes': len(prompt.encode()), 'admission': blocked,
                   'weaker_experiment': weaker_experiment, 'cancellation': cancellation}
        if cancellation:
            return AgentResult(execution_id=identity, task_id=task.task_id, status='cancelled',
                summary='local timeout; remote execution unknown'), metrics
        if is_claude:
            result, telemetry = normalize_claude(stdout, code, identity, task.task_id)
            metrics.update(telemetry)
        else:
            result, events = normalize(stdout, code, identity, task.task_id)
            metrics['native_usage'] = {}
            metrics['event_types'] = [e.get('type') for e in events]
            metrics['tool_item_types'] = sorted({e['item']['type'] for e in events
                if isinstance(e.get('item'), dict) and e['item'].get('type') not in ('agent_message', 'reasoning')
                and isinstance(e['item'].get('type'), str)})
            for e in events:
                if e.get('type') == 'turn.completed' and isinstance(e.get('usage'), dict):
                    metrics['native_usage'] = {k: v for k, v in e['usage'].items() if type(v) is int and v >= 0}
        return result, metrics
