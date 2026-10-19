"""Before/after screenshots: opt-in config, the local site server, the worktree reset between phases, serving."""
import http.client
import json
import re
import subprocess
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from merp_relay import ops, server, shots
from merp_relay.ledger import Ledger

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 32


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True).stdout.decode().strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    hq = tmp_path / 'hq'
    (hq / 'projects' / 'site').mkdir(parents=True)
    (hq / 'projects' / 'site' / 'preview.json').write_text(json.dumps(
        {'serve': 'public', 'pages': ['/index.html'], 'widths': [1280, 390], 'height': 900, 'wait_ms': 0}))
    ledger = Ledger(tmp_path / 'relay.db')
    ledger.set_setting('hq_path', str(hq))
    monkeypatch.setattr(shots, 'SHOTS', tmp_path / 'shots')
    return ledger, hq, tmp_path


class FakeBrowser:
    """Fetches the page like a browser would and writes a PNG; records what each phase served."""
    def __init__(self):
        self.seen = []

    def __call__(self, cmd, **kwargs):
        frame = urllib.request.urlopen(cmd[-1], timeout=5).read().decode()  # the wrapper page
        src = re.search(r'<iframe src="([^"]+)" style="[^"]*width:(\d+)px', frame)
        target = next(a.split('=', 1)[1] for a in cmd if a.startswith('--screenshot='))
        page = urllib.request.urlopen(cmd[-1].split('/__relay_frame__')[0] + src.group(1), timeout=5).read()
        self.seen.append((Path(target).name, src.group(2), page))
        Path(target).write_bytes(PNG)
        return subprocess.CompletedProcess(cmd, 0, b'', b'')


def test_config_is_opt_in_and_validated(env):
    ledger, hq, _ = env
    assert shots.config(str(hq), 'other') is None and shots.config(None, 'site') is None
    assert shots.config(str(hq), '../site') is None
    cfg = shots.config(str(hq), 'site')
    assert cfg['widths'] == [1280, 390] and cfg['height'] == 900
    (hq / 'projects' / 'site' / 'preview.json').write_text(json.dumps({'serve': 'public', 'pages': ['index.html']}))
    with pytest.raises(ValueError, match='pages'):
        shots.config(str(hq), 'site')


def test_capture_run_shoots_after_then_before(env):
    ledger, hq, tmp = env
    repo = tmp / 'repo'
    (repo / 'public').mkdir(parents=True)
    git(repo, 'init', '-q')
    (repo / 'public' / 'index.html').write_bytes(b'<p>old</p>')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@x.invalid', 'commit', '-qm', 'base')
    base = git(repo, 'rev-parse', 'HEAD')
    (repo / 'public' / 'index.html').write_bytes(b'<p>new</p>')
    (repo / 'public' / 'extra.css').write_bytes(b'p{}')
    git(repo, 'add', '-A')
    fake = FakeBrowser()
    shots.capture_run(ledger, 'r-0000000a', 'site', repo, base, ops.git, exe='browser', run=fake)
    assert [(n, w, body) for n, w, body in fake.seen] == [
        ('after-0-1280.png', '1280', b'<p>new</p>'), ('after-0-390.png', '390', b'<p>new</p>'),
        ('before-0-1280.png', '1280', b'<p>old</p>'), ('before-0-390.png', '390', b'<p>old</p>')]
    event = [e for e in ledger.events('r-0000000a') if e['type'] == 'screenshots'][0]['data']
    assert event['files'] == ['after-0-1280.png', 'after-0-390.png', 'before-0-1280.png', 'before-0-390.png']
    assert not (repo / 'public' / 'extra.css').exists()  # worktree reset to base for the before shots
    assert len(shots.files(ledger, 'r-0000000a')) == 4


def test_capture_problems_are_recorded_not_raised(env):
    ledger, _, tmp = env
    shots.capture_run(ledger, 'r-0000000b', 'site', tmp / 'missing', 'HEAD', ops.git, exe='browser', run=FakeBrowser())
    shots.capture_run(ledger, 'r-0000000c', 'unconfigured', tmp, 'HEAD', ops.git, exe='browser', run=FakeBrowser())
    skipped = [e for e in ledger.events('r-0000000b') if e['type'] == 'screenshots_skipped']
    assert skipped and 'not a folder' in skipped[0]['data']['reason']
    assert ledger.events('r-0000000c') == []  # not opted in: nothing recorded


def test_dashboard_serves_only_valid_shot_names(env, monkeypatch):
    ledger, _, tmp = env
    (tmp / 'shots' / 'r-0000000a').mkdir(parents=True)
    (tmp / 'shots' / 'r-0000000a' / 'after-0-1280.png').write_bytes(PNG)
    (tmp / 'secret.txt').write_text('no')
    monkeypatch.setattr(server.Handler, 'db_path', ledger.path)
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def get(path):
            conn = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=5)
            conn.request('GET', path, headers={'Host': '127.0.0.1'})
            r = conn.getresponse()
            return r.status, r.getheader('Content-Type'), r.read()
        assert get('/api/runs/r-0000000a/shots/after-0-1280.png') == (200, 'image/png', PNG)
        assert get('/api/runs/r-0000000a/shots/..%2F..%2Fsecret.txt')[0] == 404
        assert get('/api/runs/r-0000000a/shots/notes.png')[0] == 404
        assert get('/api/runs/x/shots/after-0-1280.png')[0] == 404
    finally:
        httpd.shutdown()


@pytest.mark.skipif(not shots.browser(), reason='no Edge/Chrome installed')
def test_real_browser_writes_a_png(env):
    _, _, tmp = env
    (tmp / 'site' / 'public').mkdir(parents=True)
    (tmp / 'site' / 'public' / 'index.html').write_text('<h1 style="color:red">hello</h1>')
    out = tmp / 'out'
    out.mkdir()
    cfg = {'serve': 'public', 'pages': ['/index.html'], 'widths': [800], 'height': 600, 'wait_ms': 500}
    files = shots.capture(tmp / 'site', cfg, 'after', out)
    assert files == ['after-0-800.png'] and (out / files[0]).read_bytes()[:8] == PNG[:8]
