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
