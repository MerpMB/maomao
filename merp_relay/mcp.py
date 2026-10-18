"""Relay as an MCP server (stdio, newline-delimited JSON-RPC) so a lead agent CLI can drive the workers.

Works with Codex (`[mcp_servers.relay]`) and Claude Code (`.mcp.json`). Stdlib only.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from . import ops
from .ledger import Ledger
from .server import pools, run_summary

DONE = ('needs_approval', 'completed', 'applied', 'rejected', 'failed', 'cancelled', 'blocked', 'revised')
MAX_DIFF = 20000
MAX_FILES = 20
# Paths whose changes always need the owner, whatever the reviewer said.
SENSITIVE = re.compile(r'(^|/)(\.env[^/]*|\.github/|\.gitlab-ci|secrets?[^/]*|credentials?[^/]*|[^/]*\.(pem|key|pfx))',
                       re.IGNORECASE)


def _review(ledger, run_id):
    last = [e for e in ledger.events(run_id) if e['type'] in ('reviewed', 'review_skipped')][-1:]
    if not last:
        return None
    data = last[0]['data']
    if last[0]['type'] == 'review_skipped':
        return {'verdict': 'skipped', 'reason': data.get('reason')}
    result = data.get('result') or {}
    summary = result.get('summary', '')
    verdict = 'approve' if summary.startswith('APPROVE:') else 'changes' if summary.startswith('CHANGES:') else 'none'
    return {'verdict': verdict if result.get('status') == 'completed' else result.get('status'),
            'reviewer': data.get('reviewer'), 'summary': summary,
            'findings': [f['text'] for f in result.get('findings', [])], 'unresolved': result.get('unresolved', [])}


def approval_blockers(ledger, run):
    """Why the lead may not approve alone. Empty list = allowed by policy."""
    reasons = []
    review = _review(ledger, run['id'])
    if not review or review['verdict'] != 'approve':
        reasons.append(f"cross-vendor review is not APPROVE ({review['verdict'] if review else 'missing'})")
    diff = run.get('diff') or ''
    files = re.findall(r'^diff --git a/(\S+) b/', diff, re.MULTILINE)
    if len(files) > MAX_FILES:
        reasons.append(f'{len(files)} files changed (limit {MAX_FILES})')
    if 'deleted file mode' in diff:
        reasons.append('deletes files')
    sensitive = sorted({f for f in files if SENSITIVE.search(f)})
    if sensitive:
        reasons.append('touches sensitive paths: ' + ', '.join(sensitive))
    return reasons


def _arg(args, name, kind, required=True, default=None, limit=None):
    value = args.get(name, default)
    if value is None and not required:
        return None
    if not isinstance(value, kind) or (isinstance(value, str) and not value.strip()):
        raise ValueError(f'{name} must be a non-empty {kind.__name__}')
    if limit and isinstance(value, str) and len(value) > limit:
        raise ValueError(f'{name} exceeds {limit} characters')
    return value


def t_workers(ledger, args):
    if args.get('refresh') is True:
        from .quota import refresh
        ledger.event(None, 'quota_refresh', **(refresh() or {'note': 'refresh already running'}))
    profiles = ops.available_profiles()
    return {'workers': [{'id': k, 'runtime_version': p.runtime_version} for k, (_, p) in profiles.items()],
            'models': ops.models(), 'quota': pools(), 'preference': ops.PREFERENCE,
            'slots': int(ledger.setting('slots') or ops.SLOTS),
            'per_worker': int(ledger.setting('per_worker') or ops.PER_WORKER),
            'runner_active': ops.runner_active(), 'routing_paused': ops.paused(ledger)}


def t_dispatch(ledger, args):
    worker = args.get('worker')
    if worker not in (None, 'codex', 'claude'):
        raise ValueError('worker must be codex or claude (omit to let quota routing choose)')
    repo = _arg(args, 'repo', str)
    if not Path(repo).is_dir():
        raise ValueError('repo must be an existing directory')
    timeout = args.get('timeout', 900)
    if type(timeout) is not int or not 30 <= timeout <= 3600:
        raise ValueError('timeout must be 30-3600 seconds')
    kind = args.get('kind', 'IMPLEMENT')
    if kind not in ('IMPLEMENT', 'INVESTIGATE', 'REVIEW'):
        raise ValueError('unknown kind')
    depends_on = args.get('depends_on') or []
    if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
        raise ValueError('depends_on must be a list of run ids')
    run_id = ops.submit(ledger, repo, _arg(args, 'objective', str, limit=4000).strip(), kind,
                        args.get('criteria') or (), timeout, worker=worker,
                        options={'model': args.get('model'), 'effort': args.get('effort')}, depends_on=depends_on,
                        project=args.get('project'))
    return {'run_id': run_id, 'status': 'queued',
            'note': 'starts after dependencies are applied' if depends_on else None}


def t_runs(ledger, args):
    status = args.get('status')
    runs = [run_summary(r) for r in ledger.runs(int(args.get('limit', 20)))]
    return {'runs': [r for r in runs if not status or r['status'] == status]}


def t_report(ledger, args):
    run = ledger.run(_arg(args, 'run_id', str))
    if not run:
        raise ValueError('unknown run')
    diff = run['diff'] or ''
    result = (run['artifact'] or {}).get('result') or {}
    waiting = ops.dependency_state(ledger, run) if run['status'] == 'queued' and run.get('depends_on') else None
    return {**run_summary(run), 'parent': run.get('parent'), 'requested_worker': run.get('worker'),
            'options': run.get('options'), 'depends_on': run.get('depends_on'), 'dependency_state': waiting,
            'route': (run['routing'] or {}).get('rationale'),
            'worker_result': {k: result.get(k) for k in ('status', 'summary', 'unresolved')} | {
                'findings': [f['text'] for f in result.get('findings', [])],
                'provenance': 'worker_reported'} if result else None,
            'review': _review(ledger, run['id']),
            'approval_blockers': approval_blockers(ledger, run) if run['status'] == 'needs_approval' else None,
            'diff': diff[:MAX_DIFF], 'diff_truncated': len(diff) > MAX_DIFF}


def t_wait(ledger, args):
    ids = args.get('run_ids')
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise ValueError('run_ids must be a non-empty list')
    timeout = min(max(int(args.get('timeout', 45)), 1), 600)
    deadline = time.monotonic() + timeout
    while True:
        states = {i: ledger.status(i) for i in ids}
        if all(s in DONE or s is None for s in states.values()) or time.monotonic() >= deadline:
            return {'statuses': states, 'all_done': all(s in DONE for s in states.values()),
                    'hint': None if all(s in DONE for s in states.values()) else 'still running; call wait again'}
        time.sleep(2)


def t_approve(ledger, args):
    run_id, reason = _arg(args, 'run_id', str), _arg(args, 'reason', str, limit=2000)
    run = ledger.run(run_id)
    if not run or run['status'] != 'needs_approval':
        raise ValueError(f"not awaiting approval (status {run['status'] if run else 'unknown'})")
    blockers = approval_blockers(ledger, run)
    if blockers:
        return {'approved': False, 'policy': 'ask the owner (ask_owner) or revise', 'blockers': blockers}
    ledger.event(run_id, 'decision', by='lead', action='approve', reason=reason)
    ok = ops.approve(ledger, run_id)
    return {'approved': ok, 'status': ledger.status(run_id),
            'note': 'applied to the working tree and staged; the owner commits' if ok else 'apply failed; see report'}


def t_reject(ledger, args):
    run_id, reason = _arg(args, 'run_id', str), _arg(args, 'reason', str, limit=2000)
    ledger.event(run_id, 'decision', by='lead', action='reject', reason=reason)
    return {'rejected': ops.reject(ledger, run_id), 'status': ledger.status(run_id)}


def t_revise(ledger, args):
    new_id = ops.revise(ledger, _arg(args, 'run_id', str), _arg(args, 'feedback', str, limit=2000),
                        fresh=args.get('fresh') is True)
    if not new_id:
        raise ValueError('only a run awaiting approval can be revised')
    return {'run_id': new_id, 'status': 'queued'}


def t_cancel(ledger, args):
    run_id = _arg(args, 'run_id', str)
    return {'cancelled': ops.cancel(ledger, run_id), 'status': ledger.status(run_id)}


def hq_dir():
    """RelayHQ: MERP_HQ, else the lead's working directory (the CLIs start servers there)."""
    return Path(os.environ.get('MERP_HQ') or Path.cwd())


def t_checkpoint(ledger, args):
    """Commit RelayHQ for the lead; agent sandboxes usually protect .git from the lead itself."""
    message = _arg(args, 'message', str, limit=200).strip()
    hq = hq_dir()
    if not (hq / 'AGENTS.md').exists() or not (hq / '.git').exists():
        raise ValueError(f'{hq} is not a RelayHQ git repo (set MERP_HQ)')
    run = lambda *a: subprocess.run(['git', '-C', str(hq), *a], capture_output=True, timeout=60)
    run('add', '-A')
    commit = run('-c', 'user.name=Relay lead', '-c', 'user.email=lead@relay.invalid', 'commit', '-qm', message)
    if commit.returncode:
        out = (commit.stdout + commit.stderr).decode(errors='replace')
        if 'nothing to commit' in out:
            return {'committed': False, 'note': 'nothing to commit'}
        raise RuntimeError('commit failed: ' + out.strip()[:300])
    return {'committed': True, 'commit': run('rev-parse', '--short', 'HEAD').stdout.decode().strip()}


def t_ask_owner(ledger, args):
    qid = ledger.ask(_arg(args, 'question', str, limit=4000), asked_by=args.get('asked_by') or 'lead',
                     project=args.get('project'), run_id=args.get('run_id'))
    return {'question_id': qid, 'status': 'open',
            'note': 'shown in the dashboard inbox; check with inbox(question_id) later'}


def t_inbox(ledger, args):
    qid = args.get('question_id')
    if qid:
        q = ledger.question(qid)
        if not q:
            raise ValueError('unknown question')
        return q
    return {'questions': ledger.questions(status=args.get('status'), limit=20)}


S = {'type': 'string'}
TOOLS = {
    'workers': (t_workers, 'List available workers, live quota per pool, runner and routing state. '
                'refresh=true first makes one tiny call per CLI for fresh quota (costs a little quota).',
                {'refresh': {'type': 'boolean'}}),
    'dispatch': (t_dispatch, 'Queue a task for a worker in a disposable git worktree of `repo` (starts from HEAD plus '
                 'staged changes). The diff is cross-vendor reviewed automatically. Omit `worker` to route by live '
                 'quota. `model`/`effort` pick the tier (see workers().models); `depends_on` waits until those runs '
                 'are applied. Independent runs execute in parallel.',
                 {'objective': S, 'repo': S, 'kind': {'type': 'string', 'enum': ['IMPLEMENT', 'INVESTIGATE', 'REVIEW']},
                  'worker': {'type': 'string', 'enum': ['codex', 'claude']}, 'model': S,
                  'effort': {'type': 'string', 'enum': list(ops.EFFORTS)}, 'timeout': {'type': 'integer'},
                  'depends_on': {'type': 'array', 'items': S}, 'project': S,
                  'criteria': {'type': 'array', 'items': S}}, ['objective', 'repo']),
    'runs': (t_runs, 'Recent runs (optionally filter by status).', {'status': S, 'limit': {'type': 'integer'}}),
    'report': (t_report, 'Full report for one run: worker result, cross-vendor review, approval blockers, diff.',
               {'run_id': S}, ['run_id']),
    'wait': (t_wait, 'Block until the runs finish or `timeout` seconds pass (default 45, max 600).',
             {'run_ids': {'type': 'array', 'items': S}, 'timeout': {'type': 'integer'}}, ['run_ids']),
    'approve': (t_approve, 'Apply a diff awaiting approval (staged, never committed). Refused by policy unless the '
                'review is APPROVE and no risky change is involved; then ask the owner.',
                {'run_id': S, 'reason': S}, ['run_id', 'reason']),
    'reject': (t_reject, 'Reject a diff awaiting approval.', {'run_id': S, 'reason': S}, ['run_id', 'reason']),
    'revise': (t_revise, 'Send a diff back with feedback; a new run continues from it. fresh=true redoes the task '
               'from the current repo state instead (use when approve reports the diff no longer applies).',
               {'run_id': S, 'feedback': S, 'fresh': {'type': 'boolean'}}, ['run_id', 'feedback']),
    'cancel': (t_cancel, 'Cancel a queued run or stop a running one.', {'run_id': S}, ['run_id']),
    'ask_owner': (t_ask_owner, 'Ask the owner a question; it appears in the dashboard inbox. Non-blocking.',
                  {'question': S, 'project': S, 'run_id': S}, ['question']),
    'checkpoint': (t_checkpoint, 'Commit all RelayHQ changes (journal, state, briefs) with a message.',
                   {'message': S}, ['message']),
    'inbox': (t_inbox, 'Read owner answers: one question by id, or the list (status open|answered).',
              {'question_id': S, 'status': S}),
}


def tool_list():
    return [{'name': name, 'description': spec[1],
             'inputSchema': {'type': 'object', 'properties': spec[2], 'required': spec[3] if len(spec) > 3 else []}}
            for name, spec in TOOLS.items()]


def handle(message, ledger):
    """One JSON-RPC message in, one response (or None for notifications) out."""
    method, mid = message.get('method'), message.get('id')
    if mid is None:
        return None
    if method == 'initialize':
        version = (message.get('params') or {}).get('protocolVersion') or '2025-06-18'
        result = {'protocolVersion': version, 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'merp-relay', 'version': '0.2.0'},
                  'instructions': 'Merp Relay: dispatch coding tasks to subscription workers, read reports, '
                                  'approve within policy, ask the owner when needed.'}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': tool_list()}
    elif method == 'tools/call':
        params = message.get('params') or {}
        spec = TOOLS.get(params.get('name'))
        if not spec:
            return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': 'unknown tool'}}
        try:
            payload, is_error = spec[0](ledger, params.get('arguments') or {}), False
        except Exception as e:  # a bad call must never take the server down
            payload, is_error = {'error': f'{type(e).__name__}: {e}'}, True
        result = {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False, default=str)}],
                  'isError': is_error}
    else:
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'method not found: {method}'}}
    return {'jsonrpc': '2.0', 'id': mid, 'result': result}


def main():
    ledger = Ledger()
    if (hq_dir() / 'AGENTS.md').exists():  # tell the dashboard where RelayHQ is, for project traces
        ledger.set_setting('hq_path', str(hq_dir().resolve()))
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # stdout belongs to the protocol; stray prints (runner) go to stderr
    # The lead's dispatches must run even without the dashboard; the lock keeps it to one runner.
    threading.Thread(target=ops.run_forever, args=(Ledger,), daemon=True).start()
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            response = handle(json.loads(line), ledger)
        except (ValueError, AttributeError):
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False).encode('utf-8') + b'\n')
            out.flush()


if __name__ == '__main__':
    main()
