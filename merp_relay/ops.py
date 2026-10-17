"""Serial worktree runner and operator CLI. Nothing reaches a repository without `approve`."""
import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from .adapters import ClaudeAdapter, CodexAdapter, execute_repo
from .boundary import runtime_profile
from .contracts import AgentResult, Artifact, TaskRequest
from .ledger import Ledger
from .quota import STATE, claude_observation, codex_observation
from .routing import route

PREFERENCE = ['codex', 'claude']
WORKTREES = STATE / 'worktrees'
ACTIVE = ('running', 'cancel_requested')


def git(repo, *args, stdin=None, check=True):
    # Bytes, not text mode: Windows text pipes would rewrite LF as CRLF inside patches.
    # shortcut: diffs are stored as UTF-8 text; non-UTF-8 file contents would be mangled.
    p = subprocess.run(['git', '-C', str(repo), *args], input=stdin.encode() if stdin else None,
                       capture_output=True, timeout=120)
    if check and p.returncode:
        raise RuntimeError(f'git {args[0]} failed: {p.stderr.decode(errors="replace").strip()[:300]}')
    return p.stdout.decode('utf-8', errors='replace')


def available_profiles():
    """resource_id -> (adapter, repo-capable profile) for runtimes with a verified subscription login."""
    found = {}
    for adapter in (CodexAdapter(), ClaudeAdapter()):
        probe = adapter.probe()
        if probe.get('status') == 'available':
            profile = runtime_profile(adapter, probe)
            found[profile.resource_id] = (adapter, profile.model_copy(update={
                'execution_modes': frozenset({'snapshot_analysis', 'repository_access'})}))
    return found


def observations():
    return [o for o, _ in (claude_observation(), codex_observation()) if o]


def dispatch(adapter, task, cwd, timeout, should_stop):
    return execute_repo(adapter, task, cwd, timeout=timeout, should_stop=should_stop)


def submit(ledger, repo, objective, kind='IMPLEMENT', criteria=(), timeout=900):
    repo = Path(repo).resolve()
    git(repo, 'rev-parse', 'HEAD')  # must be a git repository with a commit
    task = TaskRequest(task_id='t-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'), objective=objective,
        kind=kind, execution_mode='repository_access', read_scope=('.',),
        completion_criteria=tuple(criteria) or ('Objective met with changes limited to the worktree',))
    return ledger.submit(task, repo, timeout)


def process(ledger, run_id):
    """Run one queued task in a fresh worktree; capture its diff and remove the worktree."""
    if not ledger.transition(run_id, ['queued'], 'running'):
        return ledger.status(run_id)
    run = ledger.run(run_id)
    task, repo = TaskRequest.model_validate(run['task']), Path(run['repo'])
    profiles = available_profiles()
    obs = observations()
    decision = route(task, [p for _, p in profiles.values()], obs, [], PREFERENCE,
                     datetime.now(timezone.utc), 'ops-default', contained=True)
    ledger.update(run_id, routing=decision.model_dump(mode='json'), resource_id=decision.selected)
    ledger.event(run_id, 'routed', selected=decision.selected, rationale=decision.rationale,
                 excluded=decision.excluded)
    if not decision.selected:
        ledger.transition(run_id, ACTIVE, 'blocked', summary=decision.rationale)
        return 'blocked'
    base = git(repo, 'rev-parse', 'HEAD').strip()
    worktree = WORKTREES / run_id
    ledger.update(run_id, base_commit=base)
    diff = ''
    try:
        git(repo, 'worktree', 'add', '--detach', str(worktree), base)
        result, metrics = dispatch(profiles[decision.selected][0], task, worktree, run['timeout'],
                                   lambda: ledger.status(run_id) == 'cancel_requested')
        if result.status == 'completed':
            git(worktree, 'add', '-A')
            diff = git(worktree, 'diff', '--cached', '--binary', base)
    except Exception as e:  # keep the ledger truthful even when git or the adapter breaks
        result, metrics = AgentResult(execution_id='none', task_id=task.task_id, status='failed',
                                      summary=f'runner error: {type(e).__name__}'), {}
    finally:
        git(repo, 'worktree', 'remove', '--force', str(worktree), check=False)
        shutil.rmtree(worktree, ignore_errors=True)
    status = 'needs_approval' if result.status == 'completed' and diff else result.status
    ledger.event(run_id, 'worker_finished', result=result.status, metrics=metrics)
    ledger.transition(run_id, ACTIVE, status, diff=diff,
                      artifact=Artifact.from_result(result).model_dump(mode='json'), summary=result.summary)
    return status


def recover(ledger):
    """A run left active by a dead runner is failed; its remote execution state is unknown."""
    for run_id in ledger.with_status(*ACTIVE):
        ledger.transition(run_id, ACTIVE, 'failed', summary='runner exited during run; remote state unknown')
        shutil.rmtree(WORKTREES / run_id, ignore_errors=True)
        git(ledger.run(run_id)['repo'], 'worktree', 'prune', check=False)


def paused(ledger):
    return ledger.setting('paused') == '1'


def work(ledger, watch=False):
    """Serial loop. Pause stops new pickups; a run already in flight finishes or is cancelled."""
    recover(ledger)
    while True:
        queued = [] if paused(ledger) else ledger.with_status('queued')
        for run_id in queued[:1]:  # re-check pause between runs
            try:
                print(run_id, process(ledger, run_id), flush=True)
            except Exception as e:  # e.g. the repository vanished; the loop must survive
                ledger.transition(run_id, ['queued', *ACTIVE], 'failed', summary=f'runner error: {type(e).__name__}')
        if not watch and not queued:
            return
        if not queued:
            time.sleep(2)


def approve(ledger, run_id):
    """Apply the stored diff staged but uncommitted. False if it no longer applies.

    --index patches the normalized index blob and writes files through git's own line-ending
    conversion; it also refuses files the operator has changed since, instead of merging them.
    """
    run = ledger.run(run_id)
    if not run or run['status'] != 'needs_approval':
        return False
    try:
        git(run['repo'], 'apply', '--check', '--index', '--binary', '-', stdin=run['diff'])
    except RuntimeError as e:
        ledger.event(run_id, 'apply_failed', reason=str(e))
        return False
    # Claim the transition first so a concurrent approve cannot apply twice.
    if not ledger.transition(run_id, ['needs_approval'], 'applying'):
        return False
    try:
        git(run['repo'], 'apply', '--index', '--binary', '-', stdin=run['diff'])
    except RuntimeError as e:
        ledger.transition(run_id, ['applying'], 'needs_approval')
        ledger.event(run_id, 'apply_failed', reason=str(e))
        return False
    return ledger.transition(run_id, ['applying'], 'applied')


def reject(ledger, run_id):
    return ledger.transition(run_id, ['needs_approval'], 'rejected')


def cancel(ledger, run_id):
    return ledger.transition(run_id, ['queued'], 'cancelled', summary='cancelled before start') or \
        ledger.transition(run_id, ['running'], 'cancel_requested')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m merp_relay.ops')
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('submit')
    s.add_argument('objective')
    s.add_argument('--repo', default='.')
    s.add_argument('--kind', default='IMPLEMENT', choices=['IMPLEMENT', 'INVESTIGATE', 'REVIEW'])
    s.add_argument('--criteria', action='append', default=[])
    s.add_argument('--timeout', type=int, default=900)
    w = sub.add_parser('work')
    w.add_argument('--watch', action='store_true')
    sub.add_parser('list')
    for name in ('show', 'approve', 'reject', 'cancel'):
        sub.add_parser(name).add_argument('run_id')
    args = parser.parse_args(argv)
    ledger = Ledger()
    if args.command == 'submit':
        print(submit(ledger, args.repo, args.objective, args.kind, args.criteria, args.timeout))
    elif args.command == 'work':
        work(ledger, args.watch)
    elif args.command == 'list':
        for r in ledger.runs():
            print(f"{r['id']}  {r['status']:15} {r['resource_id'] or '-':7} {r['task']['kind']:11} "
                  f"{r['task']['objective'][:60]}")
    elif args.command == 'show':
        run = ledger.run(args.run_id)
        if not run:
            sys.exit('unknown run')
        print(json.dumps({k: v for k, v in run.items() if k != 'diff'}, indent=2))
        print(run['diff'] or '(no diff)')
    else:
        ok = {'approve': approve, 'reject': reject, 'cancel': cancel}[args.command](ledger, args.run_id)
        print('ok' if ok else f'not allowed in status {ledger.status(args.run_id)}')
        sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
