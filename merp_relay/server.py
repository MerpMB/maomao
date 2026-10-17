"""Local ops dashboard: read-only JSON API + SSE over the ledger, bound to 127.0.0.1."""
import argparse
import json
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .ledger import Ledger
from .quota import STATE, claude_observation, codex_observation

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

    def do_GET(self):
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]')
        if host not in ALLOWED_HOSTS:
            return self._send(403, {'error': 'forbidden host'})
        path = urlsplit(self.path).path.rstrip('/') or '/'
        if path == '/':
            return self._send(200, (STATIC / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        if path == '/app.js':
            return self._send(200, (STATIC / 'app.js').read_bytes(), 'text/javascript; charset=utf-8')
        if path == '/events':
            return self._events()
        ledger = Ledger(self.db_path)  # one connection per request thread
        if path == '/api/pools':
            return self._send(200, pools())
        if path == '/api/runs':
            return self._send(200, [run_summary(r) for r in ledger.runs(100)])
        if path.startswith('/api/runs/'):
            run = ledger.run(path.rsplit('/', 1)[1])
            if not run:
                return self._send(404, {'error': 'unknown run'})
            return self._send(200, {**run_summary(run), 'diff': run['diff'], 'routing': run['routing'],
                'artifact': run['artifact'], 'base_commit': run['base_commit'],
                'timeout': run['timeout'], 'events': ledger.events(run['id'])})
        self._send(404, {'error': 'not found'})

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
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    print(f'Merp Relay ops dashboard: http://127.0.0.1:{args.port}/', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
