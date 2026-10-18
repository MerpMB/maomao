"""Serial worktree runner and operator CLI. Nothing reaches a repository without `approve`."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from .adapters import ClaudeAdapter, CodexAdapter, execute_repo, execute_snapshot
from .boundary import runtime_profile
from .contracts import AgentResult, Artifact, TaskRequest
from .ledger import Ledger
from .quota import STATE, claude_observation, codex_observation
from .routing import route
from .snapshot import SourceInput, SourcePacket

PREFERENCE = ['codex', 'claude']
WORKTREES = STATE / 'worktrees'
ACTIVE = ('running', 'cancel_requested')
REVIEW_OBJECTIVE = ('Review proposed.diff against the operator objective in objective.txt and the author worker\'s '
    'claims. proposed.diff is the complete change against the repository base commit, including any earlier '
    'attempts named in the objective; files shown as new did not exist at that base. '
    'Report concrete defects, missed requirements, scope creep and risks, with file references. '
    'Start the summary with "APPROVE:" if it can be applied as is, otherwise with "CHANGES:".')


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


def review_dispatch(adapter, task, packet, should_stop):
    return execute_snapshot(adapter, task, packet, weaker_experiment=True, timeout=300, should_stop=should_stop)


def submit(ledger, repo, objective, kind='IMPLEMENT', criteria=(), timeout=900, parent=None, worker=None):
    repo = Path(repo).resolve()
    git(repo, 'rev-parse', 'HEAD')  # must be a git repository with a commit
    task = TaskRequest(task_id='t-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'), objective=objective,
        kind=kind, execution_mode='repository_access', read_scope=('.',),
        completion_criteria=tuple(criteria) or ('Objective met with changes limited to the worktree',))
    return ledger.submit(task, repo, timeout, parent, worker)


def review(ledger, run_id, objective, artifact, diff, profiles, author):
    """Second opinion from another vendor, tool-free. Skipped with a reason; it never blocks approval."""
    try:
        # shortcut: diffs over the 16 KiB snapshot packet are not reviewed; chunk them if that becomes common.
        packet = SourcePacket(sources=(SourceInput.create('objective.txt', objective),
                                       SourceInput.create('proposed.diff', diff)), artifacts=(artifact,))
    except ValueError:
        return ledger.event(run_id, 'review_skipped', reason='diff too large for the 16 KiB review packet')
    task = TaskRequest(task_id=f'{run_id}-review', objective=REVIEW_OBJECTIVE, kind='REVIEW',
        execution_mode='snapshot_analysis', input_refs=(artifact.artifact_id,),
        completion_criteria=('Verdict prefix, concrete findings with file references',),
        read_scope=('objective.txt', 'proposed.diff'))
    reviewers = {k: v for k, v in profiles.items() if k != author}
    decision = route(task, [p for _, p in reviewers.values()], observations(), [], PREFERENCE,
                     datetime.now(timezone.utc), 'ops-review', contained=True)
    if not decision.selected:
        return ledger.event(run_id, 'review_skipped', reason='no other vendor available: ' + decision.rationale)
    ledger.event(run_id, 'review_started', reviewer=decision.selected)
    result, metrics = review_dispatch(reviewers[decision.selected][0], task, packet,
                                      lambda: ledger.status(run_id) == 'cancel_requested')
    ledger.event(run_id, 'reviewed', reviewer=decision.selected, result=result.model_dump(mode='json'),
                 metrics=metrics)


def process(ledger, run_id):
    """Run one queued task in a fresh worktree; capture its diff and remove the worktree."""
    if not ledger.transition(run_id, ['queued'], 'running'):
        return ledger.status(run_id)
    run = ledger.run(run_id)
    task, repo = TaskRequest.model_validate(run['task']), Path(run['repo'])
    profiles = available_profiles()
    obs = observations()
    # A lead-chosen worker narrows the candidates; quota and eligibility still apply.
    candidates = {k: v for k, v in profiles.items() if not run.get('worker') or k == run['worker']}
    decision = route(task, [p for _, p in candidates.values()], obs, [], PREFERENCE,
                     datetime.now(timezone.utc), 'ops-default', contained=True)
    ledger.update(run_id, routing=decision.model_dump(mode='json'), resource_id=decision.selected)
    ledger.event(run_id, 'routed', selected=decision.selected, rationale=decision.rationale,
                 excluded=decision.excluded)
    if not decision.selected:
        ledger.transition(run_id, ACTIVE, 'blocked', summary=decision.rationale)
        return 'blocked'
    parent = ledger.run(run['parent']) if run.get('parent') else None
    base = parent['base_commit'] if parent else git(repo, 'rev-parse', 'HEAD').strip()
    worktree = WORKTREES / run_id
    ledger.update(run_id, base_commit=base)
    diff = ''
    try:
        git(repo, 'worktree', 'add', '--detach', str(worktree), base)
        if parent:  # fix round: start from the previous attempt so the new diff is cumulative
            git(worktree, 'apply', '--index', '--binary', '-', stdin=parent['diff'])
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
    artifact = Artifact.from_result(result)
    if status == 'needs_approval':
        try:
            review(ledger, run_id, task.objective, artifact, diff, profiles, decision.selected)
        except Exception as e:  # a broken review must not lose the diff
            ledger.event(run_id, 'review_skipped', reason=f'review error: {type(e).__name__}')
        if ledger.status(run_id) == 'cancel_requested':
            status = 'cancelled'
    ledger.transition(run_id, ACTIVE, status, diff=diff,
                      artifact=artifact.model_dump(mode='json'), summary=result.summary)
    return status


def recover(ledger):
    """A run left active by a dead runner is failed; its remote execution state is unknown."""
    for run_id in ledger.with_status(*ACTIVE):
        ledger.transition(run_id, ACTIVE, 'failed', summary='runner exited during run; remote state unknown')
        shutil.rmtree(WORKTREES / run_id, ignore_errors=True)
        git(ledger.run(run_id)['repo'], 'worktree', 'prune', check=False)


class RunnerLock:
    """One runner per machine across processes; the OS releases the lock if its holder dies."""
    def __init__(self, path=STATE / 'runner.lock'):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, 'a+b')

    def acquire(self):
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def release(self):
        if os.name == 'nt':
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_UN)


def runner_active():
    probe = RunnerLock()
    if probe.acquire():
        probe.release()
        return False
    return True


def run_forever(ledger_factory, retry=15):
    """Become the runner when nobody else is (dashboard and MCP server both call this)."""
    lock = RunnerLock()
    while not lock.acquire():
        time.sleep(retry)
    work(ledger_factory(), watch=True)


def paused(ledger):
    return ledger.setting('paused') == '1'


def work(ledger, watch=False):
    """Serial loop. Pause stops new pickups; a run already in flight finishes or is cancelled.
    Callers hold RunnerLock (run_forever, `ops work`), so recover() never fails another runner's run."""
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


def revise(ledger, run_id, feedback):
    """Fix round: supersede a diff awaiting approval with a new run that continues from it."""
    run = ledger.run(run_id)
    if not run or not feedback.strip() or not ledger.transition(run_id, ['needs_approval'], 'revised'):
        return None
    task = run['task']
    objective = (f"{task['objective']}\n\nOperator feedback on the previous attempt ({run_id}), whose changes are "
                 f"already in this worktree:\n{feedback.strip()}")
    new_id = submit(ledger, run['repo'], objective, task['kind'], task['completion_criteria'], run['timeout'],
                    parent=run_id)
    ledger.event(run_id, 'revision_queued', next=new_id, feedback=feedback.strip())
    return new_id


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
    r = sub.add_parser('revise')
    r.add_argument('run_id')
    r.add_argument('feedback')
    args = parser.parse_args(argv)
    ledger = Ledger()
    if args.command == 'submit':
        print(submit(ledger, args.repo, args.objective, args.kind, args.criteria, args.timeout))
    elif args.command == 'work':
        if not RunnerLock().acquire():
            sys.exit('another runner is active (dashboard or MCP server)')
        work(ledger, args.watch)
    elif args.command == 'list':
        for r in ledger.runs():
            print(f"{r['id']}  {r['status']:15} {r['resource_id'] or '-':7} {r['task']['kind']:11} "
                  f"{r['task']['objective'][:60]}")
    elif args.command == 'revise':
        new_id = revise(ledger, args.run_id, args.feedback)
        print(new_id or f'not allowed in status {ledger.status(args.run_id)}')
        sys.exit(0 if new_id else 1)
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
