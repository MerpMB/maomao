"""Real subscription quota from local runtime output; missing or stale data stays unknown."""
import json
import os
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .contracts import ResourceObservation

FRESH = timedelta(minutes=30)
BASIS = 'max_window_used_fraction'
# shortcut: append-only JSONL cache, replaced by the M2 sqlite ledger.
STATE = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'merp-relay'
CLAUDE_CACHE = STATE / 'claude-rate-limits.jsonl'
CODEX_SESSIONS = Path.home() / '.codex' / 'sessions'


def _ts(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc)


def _observation(pool, source, observed, windows, exhausted):
    """windows: [{'name', 'used' (0..1 or None), 'resets_at' (datetime or None)}]."""
    future = [w['resets_at'] for w in windows if w['resets_at'] and w['resets_at'] > observed]
    expires = min([observed + FRESH, *future])
    used = [w['used'] for w in windows if w['used'] is not None]
    hit = [w['resets_at'] for w in windows if w.get('exhausted') and w['resets_at']]
    return ResourceObservation(
        observation_id=f'{pool}:{observed.isoformat()}', pool_id=pool, source=source,
        evidence='locally_observed', status='exhausted' if exhausted else 'available',
        observed_at=observed, expires_at=expires, reset_at=min(hit or future) if hit or future else None,
        headroom=None if exhausted or not used else max(0.0, min(1.0, 1 - max(used))),
        accounting_basis=None if exhausted or not used else BASIS)


def _tail_lines(path, limit=1024 * 1024):
    with open(path, 'rb') as f:
        f.seek(max(0, path.stat().st_size - limit))
        return f.read().decode('utf-8', errors='replace').splitlines()


def codex_observation(sessions=CODEX_SESSIONS):
    """Newest `token_count.rate_limits` reading across recent Codex rollout logs.

    Newest reading, not newest file: a long-lived desktop thread keeps touching an old log.
    """
    files = sorted(sessions.rglob('rollout-*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)
    best = (None, [])
    for path in files[:8]:
        found = _codex_latest(path)
        if found[0] and (best[0] is None or found[0].observed_at > best[0].observed_at):
            best = found
    return best


def _codex_latest(path):
    for line in reversed(_tail_lines(path)):
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
            limits = event['payload']['rate_limits']
            observed = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
            windows = [{'name': f"{limits[k]['window_minutes']}m", 'used': limits[k]['used_percent'] / 100,
                        'resets_at': _ts(limits[k]['resets_at']),
                        'exhausted': limits[k]['used_percent'] >= 100}
                       for k in ('primary', 'secondary') if isinstance(limits.get(k), dict)]
        except (ValueError, KeyError, TypeError):
            continue
        if windows:
            exhausted = bool(limits.get('rate_limit_reached_type')) or any(w['exhausted'] for w in windows)
            return _observation('codex-local', 'codex rollout token_count', observed, windows, exhausted), windows
    return None, []


def record_claude(infos, now=None):
    """Persist `rate_limit_event.rate_limit_info` objects seen in Claude stream-json output."""
    if not infos:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with open(CLAUDE_CACHE, 'a', encoding='utf-8') as f:
        for info in infos:
            f.write(json.dumps({'observed_at': stamp, 'info': info}) + '\n')


def claude_observation(cache=CLAUDE_CACHE):
    """Claude reports only the windows it chooses to (usually the binding one)."""
    if not cache.exists():
        return None, []
    latest = {}
    for line in _tail_lines(cache):
        try:
            row = json.loads(line)
            info = row['info']
            # 2.1.302+ reports every window in `unifiedWindows`; older versions only the binding one.
            for name, window in (info.get('unifiedWindows') or {}).items():
                if isinstance(window, dict):
                    status = info.get('status') if name == info.get('rateLimitType') else 'allowed'
                    latest[name] = {'observed_at': row['observed_at'],
                                    'info': {**window, 'rateLimitType': name, 'status': status}}
            latest[info['rateLimitType']] = {**row, 'info': {**latest.get(info['rateLimitType'], {}).get('info', {}),
                                                             **{k: v for k, v in info.items() if k != 'unifiedWindows'}}}
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    if not latest:
        return None, []
    windows = []
    for name, row in sorted(latest.items()):
        info = row['info']
        used = info.get('utilization')
        windows.append({'name': name, 'used': used if isinstance(used, (int, float)) else None,
                        'resets_at': _ts(info['resetsAt']) if isinstance(info.get('resetsAt'), (int, float)) else None,
                        'exhausted': info.get('status') == 'rejected', 'status': info.get('status')})
    observed = max(datetime.fromisoformat(r['observed_at']) for r in latest.values())
    for w in windows:
        if w['resets_at'] and w['resets_at'] <= observed:  # window has reset since that reading
            w.update(used=None, exhausted=False, status='reset since last seen')
    exhausted = any(w['exhausted'] for w in windows)
    return _observation('claude-local', 'claude stream-json rate_limit_event', observed, windows, exhausted), windows


_refreshing = threading.Lock()


def refresh(timeout=90):
    """Ask both CLIs for a fresh reading with one tiny call each (Claude via Haiku: cheapest).

    The readings come back through the normal sources: Claude's rate_limit_event and Codex's rollout log.
    Returns per-CLI outcome; None if a refresh is already running.
    """
    from .adapters import codex_executable, subscription_env
    import shutil
    if not _refreshing.acquire(blocking=False):
        return None
    outcome = {}
    try:
        with tempfile.TemporaryDirectory(prefix='merp-quota-') as cwd:
            calls = {
                'claude': [shutil.which('claude'), '-p', '--model', 'haiku', '--tools', '', '--safe-mode',
                           '--no-session-persistence', '--output-format', 'stream-json', '--verbose'],
                'codex': [codex_executable(), 'exec', '--skip-git-repo-check', '--sandbox', 'read-only',
                          '-c', 'model_reasoning_effort="low"', '--json', '-'],
            }
            for name, command in calls.items():
                if not command[0]:
                    outcome[name] = 'CLI not found'
                    continue
                try:
                    p = subprocess.run(command, input=b'Reply with exactly: ok', capture_output=True, cwd=cwd,
                                       env=subscription_env(), timeout=timeout)
                except (OSError, subprocess.TimeoutExpired) as e:
                    outcome[name] = f'failed: {type(e).__name__}'
                    continue
                if name == 'claude':
                    infos = []
                    for line in p.stdout.decode('utf-8', errors='replace').splitlines():
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict) and event.get('type') == 'rate_limit_event':
                            infos.append(event.get('rate_limit_info'))
                    record_claude([i for i in infos if isinstance(i, dict)])
                    outcome[name] = 'refreshed' if infos else f'no rate data (exit {p.returncode})'
                else:
                    outcome[name] = 'refreshed' if p.returncode == 0 else f'failed (exit {p.returncode})'
    finally:
        _refreshing.release()
    return outcome


def main():
    now = datetime.now(timezone.utc)
    for label, (obs, windows) in [('claude', claude_observation()), ('codex', codex_observation())]:
        if obs is None:
            print(f'{label:7} unknown (no local quota data yet)')
            continue
        state = obs.status if now < obs.expires_at else 'unknown (stale)'
        age = int((now - obs.observed_at).total_seconds() // 60)
        parts = [f"{w['name']} {'?' if w['used'] is None else f'{w['used']:.0%}'}"
                 + (f" reset {w['resets_at'].astimezone():%d %b %H:%M}" if w['resets_at'] else '') for w in windows]
        print(f'{label:7} {state:16} {" | ".join(parts)}  (seen {age}m ago)')


if __name__ == '__main__':
    main()
