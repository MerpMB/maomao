import argparse
import hashlib
import json
from pathlib import Path
from .adapters import CodexAdapter


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
    from .conformance import main as conformance_main
    conformance_main()



if __name__ == '__main__':
    main()
