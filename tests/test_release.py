"""Release lane: locks, bundles from commits, owner-only approval, guarded execution (fake SSH) and the real
remote script end to end against a local HTTP server."""
import functools
import http.client
import json
import shutil
import subprocess
import tarfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from merp_relay import mcp, release, server
from merp_relay.ledger import Ledger


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True).stdout.decode().strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    (repo / 'public').mkdir(parents=True)
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.email', 't@example.invalid')
    git(repo, 'config', 'user.name', 't')
    git(repo, 'config', 'core.autocrlf', 'false')
    (repo / 'public' / 'a.css').write_bytes(b'a{color:red}\n')
    (repo / 'notes.md').write_bytes(b'notes\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    (repo / 'public' / 'a.css').write_bytes(b'a{color:blue}\n')
    git(repo, 'commit', '-qam', 'blue')
    first = git(repo, 'rev-parse', 'HEAD')
    (repo / 'public' / 'b.svg').write_bytes(b'<svg/>\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'add b')
    second = git(repo, 'rev-parse', 'HEAD')
    hq = tmp_path / 'hq'
    (hq / 'targets').mkdir(parents=True)
    (hq / 'targets' / 'demo.json').write_text(json.dumps({
        'ssh': 'demo-host', 'release_dir': '/srv/rel', 'page_url': 'https://demo.example/s/x',
        'origin': 'https://demo.example', 'backup_root': '/srv/backups', 'markers': ['shell'], 'min_bytes': 10}))
    (hq / 'targets' / 'broken.json').write_text('{"ssh": "x"}')
    monkeypatch.setenv('MERP_HQ', str(hq))
    return Ledger(tmp_path / 'relay.db'), repo, hq, [first, second]


def test_locks_are_exclusive_and_holder_scoped(env):
    ledger = env[0]
    assert ledger.lock('demo', 'lead', 'other agent at work')
    assert not ledger.lock('demo', 'x-1')
    assert not ledger.unlock('demo', holder='x-1')
    assert ledger.unlock('demo', holder='lead') and ledger.locks() == []
    assert ledger.lock('demo', 'x-1') and ledger.unlock('demo')  # owner force-release


def test_bundle_takes_final_contents_and_expected_before_hashes(env):
    _, repo, _, commits = env
    shas, files = release.bundle(repo, commits, ['public/'])
    assert shas == commits and sorted(files) == ['public/a.css', 'public/b.svg']
    assert files['public/a.css'][1] == b'a{color:blue}\n'
    assert files['public/a.css'][0] == release._sha(b'a{color:red}\n') and files['public/b.svg'][0] is None


def test_bundle_refuses_foreign_paths_deletions_and_reordered_commits(env):
    _, repo, _, commits = env
    with pytest.raises(ValueError, match='release paths'):
        release.bundle(repo, commits, ['config/'])
    with pytest.raises(ValueError, match='linear range'):
        release.bundle(repo, list(reversed(commits)), ['public/'])
    (repo / 'public' / 'b.svg').unlink()
    git(repo, 'commit', '-qam', 'drop b')
    with pytest.raises(ValueError, match='deletions'):
        release.bundle(repo, [git(repo, 'rev-parse', 'HEAD')], ['public/'])


def test_plan_needs_a_valid_target_and_clean_bumps(env):
    ledger, repo, hq, commits = env
    with pytest.raises(ValueError, match='unknown target'):
        release.plan(ledger, hq, 'nope', repo, commits)
    with pytest.raises(ValueError, match='invalid'):
        release.plan(ledger, hq, 'broken', repo, commits)
    with pytest.raises(ValueError, match='bump'):
        release.plan(ledger, hq, 'demo', repo, commits, bumps=[['f', 'a\tb', 'c']])
    rid = release.plan(ledger, hq, 'demo', repo, commits, bumps=[['config/x.php', '?v=1', '?v=2']], summary='blue')
    rel = ledger.release(rid)
    assert rel['status'] == 'needs_owner' and [f['path'] for f in rel['files']] == ['public/a.css', 'public/b.svg']
    assert rel['files'][1]['new'] is True and rel['bumps'] == [['config/x.php', '?v=1', '?v=2']]


class FakeRemote:
    """Records ssh/scp calls; inspects the upload set; answers with a canned script result."""
    def __init__(self, result=b'backup=/srv/backups/s-x\nRELEASE_OK backup=/srv/backups/s-x\n', code=0):
        self.result, self.code, self.calls, self.uploaded = result, code, [], {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[0] == 'scp':
            for path in cmd[4:-1]:
                self.uploaded[Path(path).name] = Path(path).read_bytes()
        out = self.result if cmd[0] == 'ssh' and 'guarded-release.sh' in cmd[-1] else b''
        return subprocess.CompletedProcess(cmd, self.code if out else 0, out, b'')


def test_execute_only_after_owner_approval_and_releases_the_lock(env):
    ledger, repo, hq, commits = env
    rid = release.plan(ledger, hq, 'demo', repo, commits, summary='blue')
    fake = FakeRemote()
    assert release.execute(ledger, hq, rid, run=fake) == 'needs_owner' and fake.calls == []
    assert release.approve(ledger, hq, rid, start=False)
    assert release.execute(ledger, hq, rid, run=fake) == 'deployed'
    rel = ledger.release(rid)
    assert rel['result']['backup'] == '/srv/backups/s-x' and ledger.locks() == []
    assert 'HOLDER=' + rid in fake.uploaded['target.env'].decode() and b'\r\n' not in fake.uploaded['guarded-release.sh']
    with tarfile.open(fileobj=__import__('io').BytesIO(fake.uploaded['bundle.tar'])) as tar:
        assert sorted(tar.getnames()) == ['public/a.css', 'public/b.svg']
    assert fake.uploaded['before.sha256'].decode().endswith('  public/a.css\n')  # new files have no expected hash
    assert release.execute(ledger, hq, rid, run=fake) == 'deployed'  # not re-run once done
    assert len([c for c in fake.calls if 'guarded-release.sh' in c[-1]]) == 1


def test_execute_is_blocked_by_a_held_lock_and_reports_rollbacks(env):
    ledger, repo, hq, commits = env
    first = release.plan(ledger, hq, 'demo', repo, commits, summary='one')
    ledger.lock('demo', 'lead', 'codex deploying')
    release.approve(ledger, hq, first, start=False)
    assert release.execute(ledger, hq, first, run=FakeRemote()) == 'blocked'
    assert 'locked by lead' in ledger.release(first)['result']['reason']
    ledger.unlock('demo', holder='lead')
    second = release.plan(ledger, hq, 'demo', repo, commits, summary='two')
    release.approve(ledger, hq, second, start=False)
    fake = FakeRemote(b'backup=/b\nRELEASE_ROLLED_BACK marker missing: shell\n', code=3)
    assert release.execute(ledger, hq, second, run=fake) == 'rolled_back'
    assert ledger.release(second)['result']['line'] == 'RELEASE_ROLLED_BACK marker missing: shell'
    third = release.plan(ledger, hq, 'demo', repo, commits, summary='three')
    release.approve(ledger, hq, third, start=False)
    assert release.execute(ledger, hq, third, run=FakeRemote(b'RELEASE_ABORT target locked by x-1\n', 1)) == 'refused'
    assert ledger.locks() == []


def call(ledger, name, **arguments):
    response = mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': name, 'arguments': arguments}}, ledger)
    return json.loads(response['result']['content'][0]['text']), response['result']['isError']


def test_lead_tools_plan_and_lock_but_cannot_approve(env):
    ledger, repo, _, commits = env
    targets, _ = call(ledger, 'targets')
    assert {t['name'] for t in targets['targets']} == {'demo', 'broken'}
    planned, err = call(ledger, 'release_plan', target='demo', repo=str(repo), commits=commits, summary='blue')
    assert not err and planned['status'] == 'needs_owner' and planned['files'] == ['public/a.css', 'public/b.svg (new)']
    assert 'approve' not in {t for t in mcp.TOOLS if t.startswith('release')}
    refused, _ = call(ledger, 'approve', run_id=planned['release_id'], reason='lead tries')
    assert ledger.release(planned['release_id'])['status'] == 'needs_owner' and 'error' in refused
    assert call(ledger, 'lock', target='demo', reason='codex at work')[0]['locked']
    assert not call(ledger, 'lock', target='demo', reason='again')[0]['locked']
    assert call(ledger, 'unlock', target='demo')[0]['unlocked']
    assert call(ledger, 'release_plan', target='demo', repo=str(repo), commits=[], summary='x')[1]


def test_health_checks_page_markers_and_assets():
    pages = {'https://demo.example/s/x': (200, b'<div class="shell"><script src="/a.js"></script>'
                                                   b'<link rel="stylesheet" href="/b.css?v=2"></div>'),
             'https://demo.example/a.js': (200, b''), 'https://demo.example/b.css?v=2': (404, b'')}
    cfg = {'name': 'demo', 'page_url': 'https://demo.example/s/x', 'origin': 'https://demo.example',
           'markers': ['shell', 'runtime.js'], 'min_bytes': 10}
    result = release.health(cfg, fetch=lambda url: pages.get(url, (0, b'')))
    assert not result['ok'] and result['assets'] == 2
    assert result['problems'] == ['marker missing: runtime.js', 'asset HTTP 404: /b.css?v=2']


@pytest.fixture
def api(env, monkeypatch):
    ledger = env[0]
    monkeypatch.setattr(server.Handler, 'db_path', ledger.path)
    ledger.set_setting('hq_path', str(env[2]))
    started = []
    monkeypatch.setattr(release, 'execute', lambda led, hq, rid, **k: started.append(rid))
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], env, started
    httpd.shutdown()


def request(port, method, path, body=None, token=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    headers = {'Host': f'127.0.0.1:{port}', 'Content-Type': 'application/json'}
    if token:
        headers['X-Relay-Token'] = token
    conn.request(method, path, json.dumps(body or {}) if method == 'POST' else None, headers)
    r = conn.getresponse()
    return r.status, json.loads(r.read() or b'{}')


def test_dashboard_lists_and_owner_approves_releases(api):
    port, (ledger, repo, hq, commits), started = api
    rid = release.plan(ledger, hq, 'demo', repo, commits, summary='blue')
    status, data = request(port, 'GET', '/api/releases')
    assert status == 200 and data['releases'][0]['id'] == rid and 'demo' in data['targets']
    assert request(port, 'POST', f'/api/releases/{rid}/approve')[0] == 403  # token required
    status, data = request(port, 'POST', f'/api/releases/{rid}/approve', token=server.Handler.token)
    assert status == 200 and data['status'] == 'approved'
    for _ in range(50):
        if started:
            break
        threading.Event().wait(0.05)
    assert started == [rid]
    assert request(port, 'POST', f'/api/releases/{rid}/approve', token=server.Handler.token)[0] == 409
    ledger.lock('demo', 'lead', 'x')
    assert request(port, 'POST', '/api/locks/demo/release', token=server.Handler.token)[0] == 200
    assert ledger.locks() == []


def _bash():
    """Git Bash on Windows (System32's bash is WSL, which cannot see these paths); plain bash elsewhere."""
    git_exe = shutil.which('git')
    if git_exe and Path(git_exe).suffix.lower() == '.exe':
        for parent in Path(git_exe).resolve().parents:
            for candidate in (parent / 'bin' / 'bash.exe', parent / 'usr' / 'bin' / 'bash.exe'):
                if candidate.exists():
                    return str(candidate)
        return None
    return shutil.which('bash')


def _posix(path):
    """Paths as the script's bash sees them (Git Bash on Windows maps C:/x to /c/x)."""
    cyg = shutil.which('cygpath')
    return subprocess.run([cyg, '-u', str(path)], capture_output=True, text=True).stdout.strip() if cyg else str(path)


@pytest.mark.skipif(not _bash(), reason='needs bash with sha256sum, tar and curl')
@pytest.mark.parametrize('page, outcome', [(b'<html>shell page with enough bytes</html>', 'RELEASE_OK'),
                                           (b'<html>no marker here, enough bytes</html>', 'RELEASE_ROLLED_BACK')])
def test_guarded_script_deploys_or_rolls_back(tmp_path, page, outcome):
    rel_dir = tmp_path / 'rel'
    (rel_dir / 'public').mkdir(parents=True)
    (rel_dir / 'public' / 'index.html').write_bytes(page)
    (rel_dir / 'public' / 'a.css').write_bytes(b'old\n')
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler,
                                                                     directory=str(rel_dir / 'public')))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        origin = f'http://127.0.0.1:{httpd.server_address[1]}'
        cfg = {'release_dir': _posix(rel_dir), 'page_url': origin + '/index.html', 'origin': origin,
               'backup_root': _posix(tmp_path / 'backups'), 'markers': ['shell'], 'min_bytes': 10}
        files = {'public/a.css': (release._sha(b'old\n'), b'new\n'), 'public/b.css': (None, b'b\n')}
        work = tmp_path / 'upload'
        work.mkdir()
        release._stage({'id': 'x-test', 'bumps': []}, cfg, files, work)
        done = subprocess.run([_bash(), str(work / 'guarded-release.sh'), _posix(work)], capture_output=True, timeout=120)
        out = (done.stdout + done.stderr).decode(errors='replace')
        assert out.strip().splitlines()[-1].startswith(outcome), out
        if outcome == 'RELEASE_OK':
            assert (rel_dir / 'public' / 'a.css').read_bytes() == b'new\n' and (rel_dir / 'public' / 'b.css').exists()
        else:
            assert (rel_dir / 'public' / 'a.css').read_bytes() == b'old\n' and not (rel_dir / 'public' / 'b.css').exists()
        assert not (tmp_path / 'backups' / '.relay-lock').exists()
    finally:
        httpd.shutdown()
