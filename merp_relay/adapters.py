"""Bounded snapshot-only Codex spike and explicitly blocked Claude integration.

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
            return {'status': 'available' if p.returncode == 0 else 'unavailable',
                    'version': p.stdout.strip(), 'boundary': 'disposable_snapshot_only'}
        except (OSError, subprocess.TimeoutExpired) as e:
            return {'status': 'unavailable', 'reason': type(e).__name__}

    def cancel(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
        return {'local_stop_requested': True, 'remote_stopped': 'unknown',
                'descendant_processes_stopped': 'unknown'}

    def execute(self, task: TaskRequest, snapshot: str, *, disposable=False, timeout=45):
        if not disposable or task.boundary_requirements or task.tools:
            raise ValueError('M0 adapter accepts only tool-free disposable snapshots')
        if not 0 < timeout <= 60 or len(snapshot.encode()) > 16384:
            raise ValueError('spike bounds exceeded')
        execution_id = str(uuid4())
        prompt = ('Investigate only the supplied source snapshot. Never invoke tools, shell, tests, '
                  'network or other agents. Return concise findings, references and unresolved questions.\n'
                  + task.model_dump_json() + '\nSOURCE SNAPSHOT:\n' + snapshot)
        executable = shutil.which('codex')
        if not executable:
            return AgentResult(execution_id=execution_id, task_id=task.task_id,
                               status='blocked', summary='CLI missing'), []
        env = os.environ.copy()
        for key in ('OPENAI_API_KEY', 'CODEX_API_KEY'):
            env.pop(key, None)
        with tempfile.TemporaryDirectory(prefix='merp-disposable-') as work:
            schema = Path(work) / 'schema.json'
            schema.write_text(json.dumps(WorkerOutput.model_json_schema()), encoding='utf-8')
            command = [executable, 'exec', '--ignore-user-config', '--ignore-rules',
                '--ephemeral', '--skip-git-repo-check', '--sandbox', 'read-only', '--json',
                '--disable', 'shell_tool', '--disable', 'unified_exec',
                '-c', 'web_search="disabled"', '-c', 'approval_policy="never"',
                '--output-schema', str(schema), '-']
            try:
                self.process = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
                try:
                    stdout, stderr = self.process.communicate(prompt, timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.cancel()
                    self.process.communicate(timeout=5)
                    return AgentResult(execution_id=execution_id, task_id=task.task_id,
                        status='cancelled', summary='Local timeout; remote cancellation unknown'), []
                # Raw stderr may contain sensitive runtime diagnostics; never persist it.
                return normalize(stdout, self.process.returncode, execution_id, task.task_id)
            except OSError as e:
                return AgentResult(execution_id=execution_id, task_id=task.task_id,
                    status='blocked', summary=f'Runtime launch failed: {type(e).__name__}'), []


class ClaudeAdapter:
    """Blocked until local subscription authentication and boundary are verified."""
    def probe(self):
        return {'status': 'unavailable', 'version': '2.1.240',
                'reason': '2026-10-03 supported auth status: loggedIn=false; re-probe required'}

    def execute(self, task, **kwargs):
        return AgentResult(execution_id=str(uuid4()), task_id=task.task_id,
            status='blocked', summary=self.probe()['reason']), []

    def cancel(self):
        return {'local_stop_requested': False, 'remote_stopped': 'unknown'}
