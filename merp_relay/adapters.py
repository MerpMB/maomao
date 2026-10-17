"""Bounded snapshot adapters with fail-closed admission and sanitized normalization.

This is not an isolation boundary for real repositories.
"""
import json
import os
import shutil
import subprocess
import tempfile
from functools import cache
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
    candidates, completed_turns = 0, 0
    for line in text.splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                malformed = True
                continue
            events.append(event)
            item = event.get('item')
            if event.get('type') == 'turn.completed':
                completed_turns += 1
                if candidates != 1:
                    malformed = True
            if event.get('type') == 'item.completed' and isinstance(item, dict) and item.get('type') == 'agent_message':
                candidates += 1
                if completed_turns:
                    malformed = True
                output = WorkerOutput.model_validate_json(item.get('text', ''))
        except (ValueError, TypeError, ValidationError):
            malformed = True
    failed_event = any(e.get('type') in ('error', 'turn.failed') for e in events)
    if returncode or malformed or failed_event or output is None or completed_turns != 1 or candidates != 1:
        return AgentResult(execution_id=execution_id, task_id=task_id, status='failed',
            summary=classify_error(events, returncode), unresolved=('No verified structured completion',)), events
    result = AgentResult(execution_id=execution_id, task_id=task_id, status='completed',
        summary=output.summary, findings=tuple(Claim(text=f, source_refs=tuple(output.source_refs),
        provenance='worker_reported') for f in output.findings), unresolved=tuple(output.unresolved))
    return result, events


@cache
def codex_executable():
    """PATH first, else the CLI bundled in the Codex desktop app (its path changes per update)."""
    found = os.environ.get('MERP_CODEX') or shutil.which('codex')
    if found or os.name != 'nt':
        return found
    try:
        # WindowsApps cannot be listed, but the package reports its own install location.
        location = subprocess.run(['powershell', '-NoProfile', '-Command',
            '(Get-AppxPackage OpenAI.Codex).InstallLocation'], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    bundled = Path(location.strip(), 'app', 'resources', 'codex.exe') if location.strip() else None
    return str(bundled) if bundled and bundled.exists() else None


class CodexAdapter:
    def probe(self):
        executable = codex_executable()
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
        return unsupported_cancellation()

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
            if version.returncode != 0 or auth.returncode != 0 or not version.stdout.strip().endswith('(Claude Code)'):
                raise ValueError('failed or unsupported probe')
            status = json.loads(auth.stdout)
            if not isinstance(status, dict) or type(status.get('loggedIn')) is not bool or \
                    not isinstance(status.get('authMethod'), str) or not isinstance(status.get('apiProvider'), str):
                raise ValueError('invalid auth status shape')
            subscription = status['loggedIn'] and status['authMethod'] == 'claude.ai' and status['apiProvider'] == 'firstParty'
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
        return unsupported_cancellation()


def unsupported_cancellation():
    """Synchronous dispatch owns no externally cancellable execution handle."""
    return {'supported': False, 'active_process': 'not_exposed', 'cancellation_requested': False,
            'local_process_exited': None, 'local_tree_termination': 'unknown', 'remote_execution': 'unknown',
            'reason': 'Public cancellation unsupported; runner timeout cleanup only'}


def normalize_claude(text, returncode, execution_id, task_id):
    """stream-json: last `result` line is the envelope; `rate_limit_event` lines carry quota."""
    rate_limits = []
    try:
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        rate_limits += [e['rate_limit_info'] for e in lines if isinstance(e, dict)
                       and e.get('type') == 'rate_limit_event' and isinstance(e.get('rate_limit_info'), dict)]
        envelope = lines[-1] if lines else None
        if not isinstance(envelope, dict) or envelope.get('type') != 'result' or \
                envelope.get('subtype') != 'success' or envelope.get('is_error') is not False or returncode:
            raise ValueError('incomplete result')
        output = WorkerOutput.model_validate(envelope.get('structured_output'))
        result = AgentResult(execution_id=execution_id, task_id=task_id, status='completed',
            summary=output.summary, findings=tuple(Claim(text=f, source_refs=tuple(output.source_refs),
            provenance='worker_reported') for f in output.findings), unresolved=tuple(output.unresolved))
        usage = envelope.get('usage', {})
        counters = {k: v for k, v in usage.items() if type(v) is int and v >= 0} if isinstance(usage, dict) else {}
        return result, {'native_usage': counters, 'accounting_scope': 'runtime result envelope',
                        'rate_limits': rate_limits}
    except (ValueError, TypeError, ValidationError):
        return AgentResult(execution_id=execution_id, task_id=task_id, status='failed',
            # Only a typed rejection counts as exhaustion, never a generic failure.
            summary='exhausted' if any(r.get('status') == 'rejected' for r in rate_limits) else
                    'process_error' if returncode else 'malformed_or_incomplete_output'), {
            'native_usage': {}, 'rate_limits': rate_limits}


def execute_snapshot(adapter, task, packet, *, weaker_experiment=False, timeout=60, workdir=None):
    """Explicit weaker experiment never implies normal snapshot admission."""
    from .boundary import runtime_profile
    from .eligibility import task_profile_eligibility
    probe = adapter.probe()
    identity = str(uuid4())
    eligibility = task_profile_eligibility(task, runtime_profile(adapter, probe))
    blocked = list(eligibility.reasons)
    if probe.get('status') != 'available' or eligibility.incompatibilities or (blocked and not weaker_experiment):
        return AgentResult(execution_id=identity, task_id=task.task_id, status='blocked',
            summary='; '.join(blocked) if blocked else probe.get('reason', 'runtime unavailable')), {
            'admission': blocked, 'native_usage': {}}
    prompt = prompt_for(task, packet)
    # Versions are recorded, not pinned: a changed CLI interface fails closed in normalization.
    is_claude = isinstance(adapter, ClaudeAdapter)
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
                '--no-session-persistence', '--output-format', 'stream-json', '--verbose',
                '--json-schema', schema.read_text()]
        else:
            command = [codex_executable(), 'exec', '--ignore-user-config', '--ignore-rules',
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
                   'weaker_experiment': weaker_experiment, 'cancellation': cancellation,
                   'runtime_version': probe.get('version')}
        if cancellation:
            stopped = cancellation.get('local_process_exited') is True
            return AgentResult(execution_id=identity, task_id=task.task_id,
                status='cancelled' if stopped else 'failed',
                summary='local timeout; parent exit observed; remote execution unknown' if stopped else
                        'local timeout; parent exit unverified; remote execution unknown'), metrics
        if is_claude:
            result, telemetry = normalize_claude(stdout, code, identity, task.task_id)
            metrics.update(telemetry)
            from .quota import record_claude
            record_claude(telemetry.get('rate_limits'))
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
