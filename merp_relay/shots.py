"""Before/after screenshots of a run's web pages, for visual review.

Opt-in per project: RelayHQ/projects/<project>/preview.json, e.g.
    {"serve": "public", "pages": ["/s/cust-001-dcp/"], "widths": [1280, 390], "height": 2600, "wait_ms": 6000}
`serve` is a folder inside the repo served as the site root. Pages are shot with a local headless Chromium (Edge or
Chrome; MERP_BROWSER overrides) once from the run's changed worktree ("after") and once from its base ("before").
Capturing never blocks a run: any problem becomes a `screenshots_skipped` event.
"""
import functools
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from .quota import STATE

SHOTS = STATE / 'shots'
FILE = re.compile(r'^(before|after)-\d{1,2}-\d{3,4}\.png$')
BROWSERS = [Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), r'Microsoft\Edge\Application\msedge.exe'),
            Path(os.environ.get('ProgramFiles', r'C:\Program Files'), r'Microsoft\Edge\Application\msedge.exe'),
            Path(os.environ.get('ProgramFiles', r'C:\Program Files'), r'Google\Chrome\Application\chrome.exe')]


def browser():
    found = os.environ.get('MERP_BROWSER')
    if found:
        return found
    for path in BROWSERS:
        if path.exists():
            return str(path)
    return next(filter(None, map(shutil.which, ('msedge', 'google-chrome', 'chromium', 'chromium-browser'))), None)


def config(hq, project):
    """The project's preview config, or None when the project has not opted in."""
    if not hq or not project or not re.match(r'^[A-Za-z0-9._-]{1,64}$', project):
        return None
    path = Path(hq, 'projects', project, 'preview.json')
    if not path.exists():
        return None
    cfg = json.loads(path.read_text(encoding='utf-8'))
    pages = cfg.get('pages')
    if not isinstance(cfg.get('serve'), str) or not isinstance(pages, list) or not pages or len(pages) > 5 \
            or not all(isinstance(p, str) and p.startswith('/') for p in pages):
        raise ValueError(f'{path}: needs "serve" and 1-5 "pages" starting with /')
    widths = cfg.get('widths') or [1280, 390]
    if not all(isinstance(w, int) and 320 <= w <= 2560 for w in widths) or len(widths) > 3:
        raise ValueError(f'{path}: widths must be 1-3 integers between 320 and 2560')
    return {'serve': cfg['serve'], 'pages': pages, 'widths': widths,
            'height': min(max(int(cfg.get('height', 2400)), 600), 8000),
            'wait_ms': min(max(int(cfg.get('wait_ms', 5000)), 0), 20000)}


FRAME = '/__relay_frame__'


class _Site(SimpleHTTPRequestHandler):
    """Static site plus a wrapper page: headless Chromium will not lay a window out narrower than ~500 px, so the page
    is shown in an iframe of exactly the requested width (its media queries see that width) and the screenshot,
    which is cropped to the window size, shows only the iframe."""
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != FRAME:
            return super().do_GET()
        q = parse_qs(url.query)
        src, width, height = q.get('src', ['/'])[0], int(q.get('w', ['1280'])[0]), int(q.get('h', ['2400'])[0])
        if not src.startswith('/') or src.startswith('//'):
            self.send_error(400)
            return
        body = (f'<!doctype html><meta charset="utf-8"><style>html,body{{margin:0;overflow:hidden}}</style>'
                f'<iframe src="{html.escape(src, quote=True)}" style="display:block;border:0;width:{width}px;'
                f'height:{height}px"></iframe>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def capture(root, cfg, phase, out_dir, exe=None, run=subprocess.run):
    """Shoot every page at every width from `root/serve`. Returns the file names written."""
    exe = exe or browser()
    if not exe:
        raise RuntimeError('no Edge/Chrome found (set MERP_BROWSER)')
    site = Path(root, cfg['serve'])
    if not site.is_dir():
        raise RuntimeError(f'{cfg["serve"]} is not a folder in the worktree')
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(_Site, directory=str(site)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    written = []
    try:
        with tempfile.TemporaryDirectory() as profile:
            for i, page in enumerate(cfg['pages']):
                for width in cfg['widths']:
                    name = f'{phase}-{i}-{width}.png'
                    target = Path(out_dir, name)
                    run([exe, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
                         '--disable-extensions', f'--user-data-dir={profile}', f'--window-size={width},{cfg["height"]}',
                         f'--virtual-time-budget={cfg["wait_ms"]}', f'--screenshot={target}',
                         f'http://127.0.0.1:{httpd.server_address[1]}{FRAME}?'
                         + urlencode({'src': page, 'w': width, 'h': cfg['height']})],
                        capture_output=True, timeout=120)
                    if target.exists() and target.stat().st_size:
                        written.append(name)
    finally:
        httpd.shutdown()
    return written


def capture_run(ledger, run_id, project, worktree, base, git, **kwargs):
    """After a run produced a diff: shoot the worktree, reset it to `base`, shoot again. Never raises."""
    try:
        cfg = config(ledger.setting('hq_path'), project)
        if not cfg:
            return
        out = SHOTS / run_id
        out.mkdir(parents=True, exist_ok=True)
        files = capture(worktree, cfg, 'after', out, **kwargs)
        git(worktree, 'reset', '-q', '--hard', base)
        git(worktree, 'clean', '-fdq')
        files += capture(worktree, cfg, 'before', out, **kwargs)
        if not files:
            raise RuntimeError('the browser wrote no screenshots')
        ledger.event(run_id, 'screenshots', files=files, pages=cfg['pages'], widths=cfg['widths'])
    except Exception as e:  # visual review is an aid; it must not cost the run
        ledger.event(run_id, 'screenshots_skipped', reason=f'{type(e).__name__}: {e}'[:300])


def files(ledger, run_id):
    """Absolute paths of a run's screenshots, newest capture only."""
    shots = [e for e in ledger.events(run_id) if e['type'] == 'screenshots'][-1:]
    return [str(SHOTS / run_id / f) for f in shots[0]['data']['files']] if shots else []


def path(run_id, name):
    """A screenshot file the dashboard may serve, or None."""
    if not re.match(r'^r-[0-9a-f]{8}$', run_id) or not FILE.match(name):
        return None
    found = SHOTS / run_id / name
    return found if found.is_file() else None
