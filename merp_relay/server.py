"""Local ops dashboard: JSON API + SSE over the ledger and an in-process runner, bound to 127.0.0.1."""
import argparse
import hmac
import json
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from . import ops, trace
from .ledger import Ledger
from .quota import STATE, claude_observation, codex_observation, refresh

STATIC = Path(__file__).parent / 'static'
# Rejecting foreign Host headers blocks DNS-rebinding pages from reading the API.
ALLOWED_HOSTS = {'127.0.0.1', 'localhost'}


def _iso(value):
    return value.isoformat() if value else None


def pools(now=None):
    now = now or datetime.now(timezone.utc)
    out = []
    for name, (obs, windows) in [('claude', claude_observation()), ('codex', codex_observation())]:
        if obs is None:
            out.append({'pool': name, 'status': 'unknown', 'reason': 'no local quota data yet', 'windows': []})
            continue
        fresh = now < obs.expires_at
        out.append({'pool': name, 'pool_id': obs.pool_id, 'status': obs.status if fresh else 'unknown',
            'stale': not fresh, 'headroom': obs.headroom if fresh else None, 'source': obs.source,
            'observed_at': _iso(obs.observed_at), 'expires_at': _iso(obs.expires_at),
            'reset_at': _iso(obs.reset_at),
            'windows': [{'name': w['name'], 'used': w['used'], 'resets_at': _iso(w['resets_at']),
                         'exhausted': w.get('exhausted', False)} for w in windows]})
    return out


def run_summary(run):
    lines = (run.get('diff') or '').splitlines()
    added = sum(1 for l in lines if l.startswith('+') and not l.startswith('+++'))
    removed = sum(1 for l in lines if l.startswith('-') and not l.startswith('---'))
    return {'id': run['id'], 'status': run['status'], 'created_at': run['created_at'],
            'updated_at': run['updated_at'], 'resource_id': run['resource_id'], 'kind': run['task']['kind'],
            'objective': run['task']['objective'], 'repo': run['repo'], 'summary': run['summary'],
            'diff_stats': {'added': added, 'removed': removed} if lines else None}


class Handler(BaseHTTPRequestHandler):
    db_path = STATE / 'relay.db'
    server_version = 'MerpRelay'
    # Per-start secret, handed to the page in a meta tag. Writes must echo it in a custom header,
    # which a cross-origin page cannot send without a CORS preflight we never grant.
    token = secrets.token_urlsafe(24)

    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]')
        return host in ALLOWED_HOSTS

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {'error': 'forbidden host'})
        path = urlsplit(self.path).path.rstrip('/') or '/'
        if path == '/':
            page = (STATIC / 'index.html').read_text(encoding='utf-8').replace('__RELAY_TOKEN__', self.token)
            return self._send(200, page.encode(), 'text/html; charset=utf-8')
        if path == '/api/status':
            ledger = Ledger(self.db_path)
            return self._send(200, {'paused': ops.paused(ledger), 'runner': ops.runner_active(),
                                    'queued': len(ledger.with_status('queued')),
                                    'recent_repos': list(dict.fromkeys(r['repo'] for r in ledger.runs(50)))[:8]})
        if path == '/app.js':
            return self._send(200, (STATIC / 'app.js').read_bytes(), 'text/javascript; charset=utf-8')
        if path == '/events':
            return self._events()
        ledger = Ledger(self.db_path)  # one connection per request thread
        if path == '/api/pools':
            return self._send(200, pools())
        if path == '/api/runs':
            return self._send(200, [run_summary(r) for r in ledger.runs(100)])
        if path == '/api/inbox':
            return self._send(200, ledger.questions(limit=30))
        hq = ledger.setting('hq_path')
        if path == '/api/projects':
            return self._send(200, {'hq': hq, 'projects': trace.projects(hq) if hq else []})
        if path.startswith('/api/projects/'):
            found = trace.trace(ledger, hq, path.rsplit('/', 1)[1]) if hq else None
            return self._send(200, found) if found else self._send(404, {'error': 'unknown project'})
        if path.startswith('/api/runs/'):
            run = ledger.run(path.rsplit('/', 1)[1])
            if not run:
                return self._send(404, {'error': 'unknown run'})
            return self._send(200, {**run_summary(run), 'diff': run['diff'], 'routing': run['routing'],
                'artifact': run['artifact'], 'base_commit': run['base_commit'],
                'timeout': run['timeout'], 'parent': run.get('parent'), 'options': run.get('options'),
                'depends_on': run.get('depends_on'), 'requested_worker': run.get('worker'),
                'events': ledger.events(run['id'])})
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        origin = self.headers.get('Origin')
        if not self._host_ok() or (origin and urlsplit(origin).hostname not in ALLOWED_HOSTS):
            return self._send(403, {'error': 'forbidden origin'})
        if not hmac.compare_digest(self.headers.get('X-Relay-Token', ''), self.token):
            return self._send(403, {'error': 'missing or wrong token'})
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length > 65536:
                raise ValueError('body too large')
            body = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(body, dict):
                raise ValueError('body must be an object')
        except ValueError as e:
            return self._send(400, {'error': str(e)})
        ledger = Ledger(self.db_path)
        parts = (urlsplit(self.path).path.rstrip('/') or '/').split('/')
        if parts == ['', 'api', 'quota', 'refresh']:
            outcome = refresh()  # one tiny call per CLI, ~10-30 s
            if outcome is None:
                return self._send(409, {'error': 'a refresh is already running'})
            ledger.event(None, 'quota_refresh', **outcome)
            return self._send(200, {'outcome': outcome, 'pools': pools()})
        if parts == ['', 'api', 'routing']:
            if not isinstance(body.get('paused'), bool):
                return self._send(400, {'error': 'paused must be true or false'})
            ledger.set_setting('paused', '1' if body['paused'] else '0')
            return self._send(200, {'paused': body['paused']})
        if parts == ['', 'api', 'runs']:
            return self._submit(ledger, body)
        if len(parts) == 5 and parts[:3] == ['', 'api', 'inbox'] and parts[4] == 'answer':
            answer = body.get('answer')
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 4000:
                return self._send(400, {'error': 'answer must be 1-4000 characters'})
            if not ledger.answer(parts[3], answer.strip()):
                return self._send(409, {'error': 'question not open'})
            return self._send(200, {'status': 'answered'})
        if len(parts) == 5 and parts[:3] == ['', 'api', 'runs'] and parts[4] == 'revise':
            feedback = body.get('feedback')
            if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 2000:
                return self._send(400, {'error': 'feedback must be 1-2000 characters'})
            if not ledger.run(parts[3]):
                return self._send(404, {'error': 'unknown run'})
            new_id = ops.revise(ledger, parts[3], feedback)
            if not new_id:
                return self._send(409, {'error': f'not allowed in status {ledger.status(parts[3])}'})
            return self._send(201, {'id': new_id})
        if len(parts) == 5 and parts[:3] == ['', 'api', 'runs'] and parts[4] in ('approve', 'reject', 'cancel'):
            if not ledger.run(parts[3]):
                return self._send(404, {'error': 'unknown run'})
            if parts[4] in ('approve', 'reject'):
                ledger.event(parts[3], 'decision', by='owner', action=parts[4])
            ok = getattr(ops, parts[4])(ledger, parts[3])
            if ok:
                return self._send(200, {'status': ledger.status(parts[3])})
            failure = [e for e in ledger.events(parts[3]) if e['type'] == 'apply_failed'][-1:]
            reason = failure[0]['data'].get('reason') if failure and parts[4] == 'approve' else None
            return self._send(409, {'error': reason or f'not allowed in status {ledger.status(parts[3])}',
                                    'status': ledger.status(parts[3])})
        self._send(404, {'error': 'not found'})

    def _submit(self, ledger, body):
        objective, repo = body.get('objective'), body.get('repo')
        kind, timeout = body.get('kind', 'IMPLEMENT'), body.get('timeout', 900)
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
            return self._send(400, {'error': 'objective must be 1-4000 characters'})
        if not isinstance(repo, str) or not Path(repo).is_dir():
            return self._send(400, {'error': 'repo must be an existing directory'})
        if kind not in ('IMPLEMENT', 'INVESTIGATE', 'REVIEW'):
            return self._send(400, {'error': 'unknown kind'})
        if type(timeout) is not int or not 30 <= timeout <= 3600:
            return self._send(400, {'error': 'timeout must be 30-3600 seconds'})
        try:
            run_id = ops.submit(ledger, repo, objective.strip(), kind, timeout=timeout)
        except RuntimeError:
            return self._send(400, {'error': 'repo must be a git repository with at least one commit'})
        return self._send(201, {'id': run_id})

    def _events(self):
        """Tail the ledger event table; the browser refetches what changed."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        ledger = Ledger(self.db_path)
        last = int(self.headers.get('Last-Event-ID') or 0)
        if not last:  # new client: start from now; the page loads current state by fetch
            tail = ledger.db.execute('SELECT MAX(id) FROM events').fetchone()[0]
            last = tail or 0
        idle = 0
        try:
            while True:
                rows = ledger.events(after=last)
                for e in rows:
                    last = e['id']
                    self.wfile.write(f'id: {last}\ndata: {json.dumps(e, default=str)}\n\n'.encode())
                idle = 0 if rows else idle + 1
                if idle >= 15:
                    self.wfile.write(b': keepalive\n\n')
                    idle = 0
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return


def main():
    parser = argparse.ArgumentParser(prog='python -m merp_relay.server')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-runner', action='store_true', help='serve only; run `ops work` separately')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    if not args.no_runner:
        server.runner = threading.Thread(target=ops.run_forever, args=(lambda: Ledger(Handler.db_path),), daemon=True)
        server.runner.start()
    print(f'Merp Relay ops dashboard: http://127.0.0.1:{args.port}/', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
