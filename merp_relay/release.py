"""Release lane: owner-approved, guarded changes to a production target, one release per target at a time.

Born from the 2026-10-18 DCP incident: production writes happened outside Relay, two agents touched the same
target, and nothing recorded them. A release here is built only from committed git changes, needs the owner's
approval (never the lead's), holds an exclusive lock on its target (in the ledger and on the server), and runs
`remote/guarded-release.sh`, which backs up, verifies the live page and rolls back on any failure.

Targets live in RelayHQ as `targets/<name>.json`, so the lead and the dashboard read the same definitions.
"""
import hashlib
import io
import json
import re
import shlex
import subprocess
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from . import ops

SCRIPT = Path(__file__).parent / 'remote' / 'guarded-release.sh'
NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')
REQUIRED = ('ssh', 'release_dir', 'page_url', 'origin', 'backup_root')
DONE = ('deployed', 'rolled_back', 'refused', 'failed', 'rejected', 'blocked')
OUTPUT_LIMIT = 8000


def targets(hq):
    """name -> config for every valid targets/*.json in RelayHQ; invalid files are reported, not used."""
    found = {}
    for path in sorted(Path(hq, 'targets').glob('*.json')) if hq else []:
        name = path.stem
        try:
            cfg = json.loads(path.read_text(encoding='utf-8'))
            missing = [k for k in REQUIRED if not isinstance(cfg.get(k), str) or not cfg[k]]
            if not NAME.match(name) or missing:
                raise ValueError('missing ' + ', '.join(missing) if missing else 'bad name')
            found[name] = {'name': name, 'paths': ['public/'], 'markers': [], 'min_bytes': 1, **cfg}
        except (ValueError, OSError) as e:
            found[name] = {'name': name, 'invalid': str(e)}
    return found


def target(hq, name):
    cfg = targets(hq).get(name)
    if not cfg:
        raise ValueError(f'unknown target {name!r} (add RelayHQ/targets/{name}.json)')
    if cfg.get('invalid'):
        raise ValueError(f'target {name} is invalid: {cfg["invalid"]}')
    return cfg


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _show(repo, ref, path):
    out = subprocess.run(['git', '-C', str(repo), 'show', f'{ref}:{path}'], capture_output=True, timeout=60)
    return out.stdout if out.returncode == 0 else None


def bundle(repo, commits, allowed_paths):
    """Files the commits change: {path: (before_sha or None, after_bytes)}. Refuses deletions and foreign paths."""
    if not commits:
        raise ValueError('commits must list at least one commit')
    shas = [ops.git(repo, 'rev-parse', '--verify', c + '^{commit}').strip() for c in commits]
    order = ops.git(repo, 'rev-list', '--topo-order', '--reverse', f'{shas[0]}^..{shas[-1]}').split()
    if set(shas) - set(order):
        raise ValueError('commits must be one linear range, oldest first')
    files = {}
    for sha in shas:
        for line in ops.git(repo, 'diff-tree', '--no-commit-id', '--name-status', '-r', sha).splitlines():
            status, path = line.split('\t', 1)
            if status[0] in 'DRC':
                raise ValueError(f'{path}: deletions, renames and copies are not released automatically')
            if '\t' in path or '..' in path.split('/') or not any(path.startswith(p) for p in allowed_paths):
                raise ValueError(f'{path}: outside this target\'s release paths {allowed_paths}')
            files[path] = None
    out = {}
    for path in files:
        before = _show(repo, f'{shas[0]}^', path)
        out[path] = (_sha(before) if before is not None else None, _show(repo, shas[-1], path))
    return shas, out


def _check_bumps(bumps):
    for b in bumps:
        if (not isinstance(b, list) or len(b) != 3 or not all(isinstance(x, str) and x for x in b)
                or any(c in x for x in b for c in '\t\n\r')):
            raise ValueError('each bump must be [file, old, new] without tabs or newlines')
    return bumps


def plan(ledger, hq, name, repo, commits, bumps=(), summary='', requested_by='lead', project=None):
    cfg = target(hq, name)
    shas, files = bundle(repo, commits, cfg['paths'])
    bumps = _check_bumps([list(b) for b in bumps])
    listing = [{'path': p, 'before': before, 'after': _sha(after), 'new': before is None}
               for p, (before, after) in sorted(files.items())]
    return ledger.create_release(name, repo, shas, listing, bumps, summary.strip()[:2000], requested_by, project)


def _env(cfg, holder):
    values = {'REL': cfg['release_dir'], 'DOCROOT_LINK': cfg.get('docroot_link', ''), 'URL': cfg['page_url'],
              'ORIGIN': cfg['origin'].rstrip('/'), 'MARKERS': '|'.join(cfg.get('markers', [])),
              'MIN_BYTES': str(int(cfg.get('min_bytes', 1))), 'BACKUP_ROOT': cfg['backup_root'], 'HOLDER': holder,
              'CONFIG_GUARD_FILE': cfg.get('config_guard_file', ''), 'CONFIG_GUARD_TEXT': cfg.get('config_guard_text', ''),
              'ARTISAN': '1' if cfg.get('artisan') else ''}
    return ''.join(f'{k}={shlex.quote(v)}\n' for k, v in values.items())


def _stage(rel, cfg, files, workdir):
    """Write the upload set for guarded-release.sh into workdir."""
    work = Path(workdir)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for path, (_, data) in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    (work / 'bundle.tar').write_bytes(buf.getvalue())
    (work / 'after.sha256').write_bytes(''.join(f'{_sha(d)}  {p}\n' for p, (_, d) in sorted(files.items())).encode())
    (work / 'before.sha256').write_bytes(''.join(f'{b}  {p}\n' for p, (b, _) in sorted(files.items()) if b).encode())
    (work / 'bumps.tsv').write_bytes(''.join('\t'.join(b) + '\n' for b in rel['bumps'] or []).encode())
    (work / 'target.env').write_bytes(_env(cfg, rel['id']).encode())
    (work / 'guarded-release.sh').write_bytes(SCRIPT.read_bytes().replace(b'\r\n', b'\n'))


def _outcome(output, returncode):
    last = next((l for l in reversed(output.strip().splitlines()) if l.startswith('RELEASE_')), '')
    if last.startswith('RELEASE_OK') and returncode == 0:
        return 'deployed', last
    if last.startswith('RELEASE_ROLLED_BACK'):
        return 'rolled_back', last
    if last.startswith('RELEASE_ABORT'):
        return 'refused', last
    return 'failed', last or f'exit {returncode} without a result line'


def execute(ledger, hq, release_id, run=subprocess.run):
    """Run an owner-approved release. Returns the final status. `run` is injectable for tests."""
    rel = ledger.release(release_id)
    if not rel or not ledger.release_transition(release_id, ('approved',), 'running'):
        return rel and rel['status']
    cfg = target(hq, rel['target'])
    if not ledger.lock(rel['target'], release_id, reason=rel['summary'][:200]):
        holder = next((l['holder'] for l in ledger.locks() if l['target'] == rel['target']), '?')
        ledger.release_transition(release_id, ('running',), 'blocked', result={'reason': f'target locked by {holder}'})
        return 'blocked'
    try:
        shas, files = bundle(rel['repo'], rel['commits'], cfg['paths'])
        if shas != rel['commits'] or {f['path']: f['after'] for f in rel['files']} != {p: _sha(d) for p, (_, d) in files.items()}:
            raise ValueError('the commits no longer produce the approved files')
        remote = f'/tmp/relay-release/{release_id}'
        with tempfile.TemporaryDirectory() as work:
            _stage(rel, cfg, files, work)
            names = ['bundle.tar', 'after.sha256', 'before.sha256', 'bumps.tsv', 'target.env', 'guarded-release.sh']
            steps = [['ssh', '-o', 'BatchMode=yes', cfg['ssh'], f'mkdir -p {remote}'],
                     ['scp', '-q', '-o', 'BatchMode=yes', *[str(Path(work, n)) for n in names], f'{cfg["ssh"]}:{remote}/']]
            for step in steps:
                done = run(step, capture_output=True, timeout=300)
                if done.returncode:
                    raise RuntimeError(f'{step[0]} failed: {(done.stderr or b"").decode(errors="replace")[:300]}')
            done = run(['ssh', '-o', 'BatchMode=yes', cfg['ssh'], f'bash {remote}/guarded-release.sh {remote}; rm -rf {remote}'],
                       capture_output=True, timeout=900)
        output = ((done.stdout or b'') + (done.stderr or b'')).decode(errors='replace')
        status, line = _outcome(output, done.returncode)
        backup = re.search(r'backup=(\S+)', output)
        ledger.release_transition(release_id, ('running',), status,
                                  result={'line': line, 'backup': backup.group(1) if backup else None,
                                          'output': output[-OUTPUT_LIMIT:]})
        return status
    except Exception as e:  # nothing was changed remotely unless the script ran; the script reports its own state
        ledger.release_transition(release_id, ('running',), 'failed', result={'line': f'{type(e).__name__}: {e}'})
        return 'failed'
    finally:
        ledger.unlock(rel['target'], holder=release_id)


def approve(ledger, hq, release_id, start=True, ledger_factory=None):
    """Owner approval: needs_owner -> approved, then execute in the background."""
    if not ledger.release_transition(release_id, ('needs_owner',), 'approved'):
        return False
    ledger.event(release_id, 'decision', by='owner', action='approve')
    if start:
        factory = ledger_factory or (lambda: ledger)
        threading.Thread(target=lambda: execute(factory(), hq, release_id), daemon=True).start()
    return True


def reject(ledger, release_id):
    if not ledger.release_transition(release_id, ('needs_owner',), 'rejected'):
        return False
    ledger.event(release_id, 'decision', by='owner', action='reject')
    return True


def health(cfg, fetch=None, timeout=20):
    """Read-only check of a target's live page: size, markers and every linked same-origin asset."""
    def get(url):
        if fetch:
            return fetch(url)
        req = urllib.request.Request(url, headers={'User-Agent': 'merp-relay-health'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b''
        except OSError as e:
            return 0, str(e).encode()
    status, page = get(cfg['page_url'])
    text = page.decode('utf-8', errors='replace')
    problems = [] if status == 200 else [f'page HTTP {status}']
    if len(page) <= int(cfg.get('min_bytes', 1)):
        problems.append(f'page too small ({len(page)} bytes)')
    problems += [f'marker missing: {m}' for m in cfg.get('markers', []) if m not in text]
    assets = sorted(set(re.findall(r'(?:src|href)="(/[^"/][^"]*\.(?:css|js|png|jpg|webp|svg)[^"]*)"', text)))
    for a in assets:
        code, _ = get(cfg['origin'].rstrip('/') + a)
        if code != 200:
            problems.append(f'asset HTTP {code}: {a}')
    return {'target': cfg['name'], 'ok': not problems, 'bytes': len(page), 'assets': len(assets), 'problems': problems}
