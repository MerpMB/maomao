"""Project trace: RelayHQ files (brief, state, journal) joined with ledger runs, reviews, decisions and questions."""
import re
from datetime import datetime
from pathlib import Path

RUN_ID = re.compile(r'\br-[0-9a-f]{8}\b')
HEADING = re.compile(r'^##\s+(\d{1,2}:\d{2})\s+(\S+)\s+[—-]+\s*(.*)$')
STATUS_EVENTS = ('applied', 'rejected', 'revised', 'failed', 'cancelled', 'blocked', 'needs_approval')


def _read(path):
    try:
        return path.read_text(encoding='utf-8-sig')
    except OSError:
        return None


def projects(hq):
    root = Path(hq) / 'projects'
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(('_', '.')))


def journal(hq, project):
    """Journal sections whose heading names the project, oldest first, with local timestamps."""
    entries, local = [], datetime.now().astimezone().tzinfo
    for path in sorted((Path(hq) / 'journal').glob('*.md')):
        try:
            day = datetime.strptime(path.stem, '%Y-%m-%d')
        except ValueError:
            continue
        current = None
        for line in (_read(path) or '').splitlines():
            match = HEADING.match(line.strip())
            if match:
                hour, minute = map(int, match.group(1).split(':'))
                current = {'at': day.replace(hour=hour, minute=minute, tzinfo=local).isoformat(),
                           'title': match.group(3), 'body': []} if match.group(2) == project else None
                if current:
                    entries.append(current)
            elif current is not None and line.strip():
                current['body'].append(line.strip())
    for entry in entries:
        entry['runs'] = sorted(set(RUN_ID.findall(entry['title'] + ' ' + ' '.join(entry['body']))))
    return entries


def trace(ledger, hq, project):
    """Everything about one project in time order. Runs belong to it by `project` or by journal mention."""
    if project not in projects(hq):
        return None
    base = Path(hq) / 'projects' / project
    entries = journal(hq, project)
    mentioned = {r for e in entries for r in e['runs']}
    runs = [r for r in ledger.runs(500) if r.get('project') == project or r['id'] in mentioned]
    ids = {r['id'] for r in runs}
    timeline = [{'at': e['at'], 'kind': 'journal', 'title': e['title'], 'text': '\n'.join(e['body']),
                 'runs': e['runs']} for e in entries]
    for run in runs:
        options = run.get('options') or {}
        timeline.append({'at': run['created_at'], 'kind': 'dispatch', 'run': run['id'],
                         'title': run['task']['objective'].splitlines()[0][:160],
                         'text': ' · '.join(filter(None, [run.get('worker') and f"asked for {run['worker']}",
                                                          *(f'{k} {v}' for k, v in options.items()),
                                                          run.get('depends_on') and 'after ' + ', '.join(run['depends_on']),
                                                          run.get('parent') and f"revision of {run['parent']}"]))})
        for e in ledger.events(run['id']):
            d = e['data']
            if e['type'] == 'routed' and d.get('selected'):
                timeline.append({'at': e['at'], 'kind': 'routed', 'run': run['id'], 'title': f"→ {d['selected']}",
                                 'text': d.get('rationale', '')})
            elif e['type'] == 'reviewed':
                summary = (d.get('result') or {}).get('summary', '')
                timeline.append({'at': e['at'], 'kind': 'review', 'run': run['id'], 'title': f"{d.get('reviewer')} review",
                                 'verdict': 'approve' if summary.startswith('APPROVE:') else
                                            'changes' if summary.startswith('CHANGES:') else 'none',
                                 'text': summary[:400]})
            elif e['type'] == 'review_skipped':
                timeline.append({'at': e['at'], 'kind': 'review', 'run': run['id'], 'title': 'review skipped',
                                 'verdict': 'skipped', 'text': d.get('reason', '')})
            elif e['type'] == 'decision':
                timeline.append({'at': e['at'], 'kind': 'decision', 'run': run['id'],
                                 'title': f"{d.get('by')} {d.get('action')}", 'text': d.get('reason') or ''})
            elif e['type'] in STATUS_EVENTS:
                timeline.append({'at': e['at'], 'kind': 'status', 'run': run['id'], 'title': e['type'], 'text': ''})
    for q in ledger.questions(limit=200):
        if q.get('project') == project or q.get('run_id') in ids:
            timeline.append({'at': q['created_at'], 'kind': 'question', 'run': q.get('run_id'),
                             'title': f"{q['asked_by']} asked {q['id']}", 'text': q['question']})
            if q.get('answered_at'):
                timeline.append({'at': q['answered_at'], 'kind': 'answer', 'run': q.get('run_id'),
                                 'title': f"owner answered {q['id']}", 'text': q.get('answer') or ''})
    timeline.sort(key=lambda item: datetime.fromisoformat(item['at']))
    counts = {}
    for run in runs:
        counts[run['status']] = counts.get(run['status'], 0) + 1
    return {'project': project, 'brief': _read(base / 'brief.md'), 'state': _read(base / 'state.md'),
            'decisions': _read(base / 'decisions.md'), 'run_counts': counts, 'timeline': timeline}
