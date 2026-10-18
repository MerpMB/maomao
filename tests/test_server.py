"""Dashboard API over a real local socket; ledger and quota are fixtures."""
import http.client
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest
from merp_relay import server
from merp_relay.contracts import TaskRequest
from merp_relay.ledger import Ledger


@pytest.fixture
def api(tmp_path, monkeypatch):
    db = tmp_path / 'relay.db'
    ledger = Ledger(db)
    task = TaskRequest(task_id='t', objective='<script>alert(1)</script>', kind='IMPLEMENT',
                       execution_mode='repository_access', completion_criteria=('c',), read_scope=('.',))
    run_id = ledger.submit(task, tmp_path)
    ledger.transition(run_id, ['queued'], 'needs_approval', diff='--- a/x\n+++ b/x\n-old\n+new\n+more\n')
    monkeypatch.setattr(server.Handler, 'db_path', db)
    monkeypatch.setattr(server, 'claude_observation', lambda: (None, []))
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    from merp_relay.quota import _observation
    stale = _observation('codex-local', 'fixture', old, [{'name': '300m', 'used': 0.4, 'resets_at': None}], False)
    monkeypatch.setattr(server, 'codex_observation', lambda: (stale, [{'name': '300m', 'used': 0.4, 'resets_at': None}]))
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], run_id
    httpd.shutdown()


def get(port, path, host='127.0.0.1'):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('GET', path, headers={'Host': f'{host}:{port}'})
    r = conn.getresponse()
    return r.status, r.read(), r.getheader('Content-Security-Policy')


def test_runs_and_detail(api):
    port, run_id = api
    status, body, _ = get(port, '/api/runs')
    runs = json.loads(body)
    assert status == 200 and runs[0]['diff_stats'] == {'added': 2, 'removed': 1} and 'diff' not in runs[0]
    status, body, _ = get(port, f'/api/runs/{run_id}')
    detail = json.loads(body)
    assert detail['status'] == 'needs_approval' and '+new' in detail['diff']
    assert [e['type'] for e in detail['events']] == ['submitted', 'needs_approval']
    assert get(port, '/api/runs/r-missing')[0] == 404


def test_stale_pool_is_unknown_and_missing_pool_explained(api):
    port, _ = api
    pools = {p['pool']: p for p in json.loads(get(port, '/api/pools')[1])}
    assert pools['codex']['status'] == 'unknown' and pools['codex']['stale'] and pools['codex']['headroom'] is None
    assert pools['claude']['status'] == 'unknown' and pools['claude']['windows'] == []


def test_foreign_host_rejected(api):
    port, _ = api
    assert get(port, '/api/runs', host='evil.example')[0] == 403
    assert get(port, '/', host='localhost')[0] == 200


def test_page_served_with_csp_and_no_inline_script(api):
    port, _ = api
    status, body, csp = get(port, '/')
    assert status == 200 and "default-src 'self'" in csp
    assert b'<script src="/app.js"></script>' in body and b'innerHTML' not in get(port, '/app.js')[1]


def post(port, path, body, token=None, origin=None, host='127.0.0.1'):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    headers = {'Host': f'{host}:{port}', 'Content-Type': 'application/json'}
    if token:
        headers['X-Relay-Token'] = token
    if origin:
        headers['Origin'] = origin
    conn.request('POST', path, body=json.dumps(body), headers=headers)
    r = conn.getresponse()
    return r.status, json.loads(r.read() or b'{}')


@pytest.fixture
def repo(tmp_path):
    import subprocess
    path = tmp_path / 'target'
    path.mkdir()
    for args in (['init', '-q'], ['config', 'user.email', 't@example.invalid'], ['config', 'user.name', 't']):
        subprocess.run(['git', '-C', str(path), *args], check=True)
    (path / 'a.txt').write_bytes(b'one\n')
    subprocess.run(['git', '-C', str(path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(path), 'commit', '-qm', 'init'], check=True)
    return path


def test_writes_need_token_and_local_origin(api):
    port, run_id = api
    token = server.Handler.token
    assert post(port, '/api/routing', {'paused': True})[0] == 403
    assert post(port, '/api/routing', {'paused': True}, token='wrong')[0] == 403
    assert post(port, '/api/routing', {'paused': True}, token, origin='https://evil.example')[0] == 403
    assert post(port, '/api/routing', {'paused': True}, token, host='evil.example')[0] == 403
    assert post(port, '/api/routing', {'paused': True}, token, origin=f'http://127.0.0.1:{port}') == (200, {'paused': True})
    assert json.loads(get(port, '/api/status')[1])['paused'] is True


def test_page_carries_token_only_for_same_origin(api):
    port, _ = api
    assert server.Handler.token.encode() in get(port, '/')[1]
    assert get(port, '/', host='evil.example')[0] == 403


def test_submit_validates_and_queues(api, repo, tmp_path):
    port, _ = api
    token = server.Handler.token
    assert post(port, '/api/runs', {'objective': '', 'repo': str(repo)}, token)[0] == 400
    assert post(port, '/api/runs', {'objective': 'x', 'repo': str(tmp_path / 'nope')}, token)[0] == 400
    assert post(port, '/api/runs', {'objective': 'x', 'repo': str(tmp_path)}, token)[0] == 400  # not a git repo
    assert post(port, '/api/runs', {'objective': 'x', 'repo': str(repo), 'timeout': 5}, token)[0] == 400
    status, body = post(port, '/api/runs', {'objective': 'add b.txt', 'repo': str(repo)}, token)
    assert status == 201
    assert json.loads(get(port, f"/api/runs/{body['id']}")[1])['status'] == 'queued'
    assert post(port, f"/api/runs/{body['id']}/cancel", {}, token) == (200, {'status': 'cancelled'})


def test_approve_and_reject_over_api(api, repo):
    port, run_id = api
    token = server.Handler.token
    ledger = Ledger(server.Handler.db_path)
    status, body = post(port, '/api/runs', {'objective': 'edit', 'repo': str(repo)}, token)
    good = body['id']
    diff = 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n'
    ledger.transition(good, ['queued'], 'needs_approval', diff=diff)
    assert post(port, f'/api/runs/{good}/approve', {}, token) == (200, {'status': 'applied'})
    assert (repo / 'a.txt').read_bytes().replace(b'\r', b'') == b'two\n'
    status, body = post(port, f'/api/runs/{good}/reject', {}, token)
    assert status == 409 and body['status'] == 'applied'
    assert post(port, f'/api/runs/{run_id}/approve', {}, token)[0] == 409  # fixture diff does not apply
    assert post(port, '/api/runs/r-missing/approve', {}, token)[0] == 404


def test_revise_over_api(api, repo):
    port, _ = api
    token = server.Handler.token
    ledger = Ledger(server.Handler.db_path)
    first = post(port, '/api/runs', {'objective': 'edit', 'repo': str(repo)}, token)[1]['id']
    assert post(port, f'/api/runs/{first}/revise', {'feedback': 'more'}, token)[0] == 409  # still queued
    ledger.transition(first, ['queued'], 'needs_approval', diff='x')
    assert post(port, f'/api/runs/{first}/revise', {'feedback': ''}, token)[0] == 400
    status, body = post(port, f'/api/runs/{first}/revise', {'feedback': 'also add tests'}, token)
    assert status == 201
    detail = json.loads(get(port, f"/api/runs/{body['id']}")[1])
    assert detail['parent'] == first and detail['status'] == 'queued'
