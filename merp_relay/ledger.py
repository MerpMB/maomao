"""Durable run records in one stdlib sqlite file; status changes are atomic compare-and-set."""
import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4
from .quota import STATE

JSON_FIELDS = ('task', 'artifact', 'routing')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, status TEXT,
    task TEXT, repo TEXT, timeout INTEGER, resource_id TEXT, base_commit TEXT, diff TEXT,
    artifact TEXT, routing TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, at TEXT,
    type TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
'''


def _now():
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, path=STATE / 'relay.db'):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)

    def submit(self, task, repo, timeout=900):
        run_id = 'r-' + uuid4().hex[:8]
        now = _now()
        self.db.execute('INSERT INTO runs (id, created_at, updated_at, status, task, repo, timeout) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (run_id, now, now, 'queued', task.model_dump_json(), str(repo), timeout))
        self.event(run_id, 'submitted', kind=task.kind, repo=str(repo))
        return run_id

    def update(self, run_id, **fields):
        self._set(run_id, None, fields)

    def transition(self, run_id, allowed, status, **fields):
        """True only if the run was in one of `allowed`; losers of a race change nothing."""
        changed = self._set(run_id, allowed, {**fields, 'status': status})
        if changed:
            self.event(run_id, status)
        return changed

    def _set(self, run_id, allowed, fields):
        fields = {k: json.dumps(v) if k in JSON_FIELDS else v for k, v in fields.items()}
        fields['updated_at'] = _now()
        sql = 'UPDATE runs SET ' + ', '.join(f'{k} = ?' for k in fields) + ' WHERE id = ?'
        args = [*fields.values(), run_id]
        if allowed:
            sql += f' AND status IN ({",".join("?" * len(allowed))})'
            args += list(allowed)
        return self.db.execute(sql, args).rowcount == 1

    def event(self, run_id, type, **data):
        self.db.execute('INSERT INTO events (run_id, at, type, data) VALUES (?, ?, ?, ?)',
                        (run_id, _now(), type, json.dumps(data, default=str)))

    def _decode(self, row):
        run = dict(row)
        for k in JSON_FIELDS:
            run[k] = json.loads(run[k]) if run[k] else None
        return run

    def run(self, run_id):
        row = self.db.execute('SELECT * FROM runs WHERE id = ?', (run_id,)).fetchone()
        return self._decode(row) if row else None

    def status(self, run_id):
        row = self.db.execute('SELECT status FROM runs WHERE id = ?', (run_id,)).fetchone()
        return row[0] if row else None

    def runs(self, limit=50):
        rows = self.db.execute('SELECT * FROM runs ORDER BY created_at DESC LIMIT ?', (limit,))
        return [self._decode(r) for r in rows]

    def with_status(self, *statuses):
        rows = self.db.execute(f'SELECT id FROM runs WHERE status IN ({",".join("?" * len(statuses))}) '
                               'ORDER BY created_at', statuses)
        return [r[0] for r in rows]

    def setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        self.db.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                        'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
        self.event(None, 'setting', key=key, value=value)

    def events(self, run_id=None, after=0, limit=200):
        sql, args = 'SELECT * FROM events WHERE id > ?', [after]
        if run_id:
            sql, args = sql + ' AND run_id = ?', args + [run_id]
        rows = self.db.execute(sql + ' ORDER BY id LIMIT ?', args + [limit])
        return [{**dict(r), 'data': json.loads(r['data'])} for r in rows]
