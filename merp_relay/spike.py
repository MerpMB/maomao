import argparse
import hashlib
import json
from pathlib import Path
from .adapters import CodexAdapter
from .contracts import Artifact, StructuralCheckpoint, TaskRequest


def fixture_hash(root):
    h = hashlib.sha256()
    for p in sorted(root.rglob('*')):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode() + b'\0' + p.read_bytes() + b'\0')
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description='Explicit opt-in disposable Codex smoke')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    adapter = CodexAdapter()
    if not args.run:
        print(json.dumps(adapter.probe()))
        return
    fixture = Path('fixtures/investigation')
    before = fixture_hash(fixture)
    snapshot = '\n'.join(f'{p.name}:\n{p.read_text()}' for p in sorted(fixture.iterdir()) if p.is_file())
    task = TaskRequest(task_id='smoke-1', objective='Inspect reversed clamp bounds', kind='INVESTIGATE',
        completion_criteria=('Finding with source references and unresolved questions',),
        read_scope=('supplied fixture snapshot',))
    result, events = adapter.execute(task, snapshot, disposable=True)
    artifact = Artifact.from_result(result)
    checkpoint = StructuralCheckpoint(execution_id=result.execution_id, fixture_hash=before,
                                     artifact_refs=(artifact.artifact_id,))
    for e in events:
        if e.get('type') == 'turn.completed' and isinstance(e.get('usage'), dict):
            checkpoint = checkpoint.model_copy(update={'observed_usage': {
                k: v for k, v in e['usage'].items() if isinstance(v, int) and not isinstance(v, bool)}})
    out = Path('docs/evidence')
    out.mkdir(parents=True, exist_ok=True)
    evidence = {'date': '2026-10-03', 'label': 'locally_observed', 'runtime_version': adapter.probe(),
        'fixture_before': before, 'fixture_after': fixture_hash(fixture),
        'writes_prevented': 'unknown', 'event_types': [e.get('type') for e in events],
        'checkpoint': checkpoint.model_dump(mode='json'), 'artifact': artifact.model_dump(mode='json')}
    (out / 'codex-smoke.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
