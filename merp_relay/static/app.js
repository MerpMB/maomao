'use strict';
// Every value from the ledger is untrusted worker/operator text: build DOM with textContent only.

const STATUS = {
  queued: ['Queued', '--muted'], running: ['Running', '--accent'], cancel_requested: ['Cancelling', '--warning'],
  needs_approval: ['Needs approval', '--warning'], applying: ['Applying', '--accent'], applied: ['Applied', '--good'],
  completed: ['Completed', '--good'], rejected: ['Rejected', '--muted'], failed: ['Failed', '--critical'],
  cancelled: ['Cancelled', '--muted'], blocked: ['Blocked', '--serious'],
  available: ['Available', '--good'], exhausted: ['Exhausted', '--critical'], unknown: ['Unknown', '--muted'],
};
const WINDOW_NAMES = { five_hour: '5-hour', '300m': '5-hour', seven_day: '7-day', '10080m': '7-day' };
let selected = null;

function el(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') node.className = v; else if (k === 'style') node.style.cssText = v; else node.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null) node.append(kid instanceof Node ? kid : String(kid));
  return node;
}
const badge = (status) => {
  const [label, color] = STATUS[status] || [status, '--muted'];
  return el('span', { class: 'badge', style: `--sc: var(${color})` }, label);
};
const getJSON = (url) => fetch(url, { cache: 'no-store' }).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });

function ago(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  return s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)}m` : s < 86400 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`;
}
function until(iso) {
  const m = Math.round((new Date(iso) - Date.now()) / 60000);
  if (m <= 0) return 'now';
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
  return d ? `${d}d ${h}h` : h ? `${h}h ${mm}m` : `${mm}m`;
}
const clock = (iso) => new Date(iso).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });

function level(used) {
  if (used >= 1) return ['Exhausted', '--critical'];
  if (used >= 0.9) return ['Near limit', '--serious'];
  if (used >= 0.75) return ['High', '--warning'];
  return ['OK', '--good'];
}

function meter(w) {
  const name = WINDOW_NAMES[w.name] || w.name;
  const known = typeof w.used === 'number';
  const [lvl, color] = known ? level(w.used) : ['Unknown', '--muted'];
  const pct = known ? Math.round(w.used * 100) : null;
  return el('div', { class: 'meter' },
    el('div', { class: 'row' },
      el('span', {}, `${name} window · `, el('b', {}, known ? `${pct}% used` : 'usage unknown'), ` · ${lvl}`),
      el('span', {}, w.resets_at ? `resets in ${until(w.resets_at)} (${clock(w.resets_at)})` : 'reset unknown')),
    el('div', { class: 'track', role: 'meter', 'aria-label': `${name} window used`, 'aria-valuemin': '0',
      'aria-valuemax': '100', 'aria-valuenow': known ? String(pct) : '0', 'aria-valuetext': known ? `${pct}% used, ${lvl}` : 'unknown' },
      el('i', { style: `width:${known ? Math.min(100, pct) : 0}%; background: var(${color})` })));
}

async function loadPools() {
  const box = document.getElementById('pools');
  try {
    const pools = await getJSON('/api/pools');
    box.replaceChildren(...pools.map((p) => el('section', { class: 'pool' },
      el('h3', {}, p.pool === 'claude' ? 'Claude' : p.pool === 'codex' ? 'Codex' : p.pool, badge(p.status)),
      el('div', { class: 'meta' }, p.observed_at
        ? `${p.stale ? 'STALE — last' : 'Last'} seen ${ago(p.observed_at)} ago · ${p.source}` +
          (p.headroom != null ? ` · headroom ${Math.round(p.headroom * 100)}%` : '')
        : p.reason),
      p.stale ? el('div', { class: 'meta' }, 'Data expired: shown for context only; routing treats this pool as unknown.') : null,
      p.windows.length ? p.windows.map(meter) : el('div', { class: 'empty' }, 'No window data. A run on this worker records it.'))));
  } catch (e) {
    box.replaceChildren(el('section', {}, el('div', { class: 'empty' }, `Pools unavailable (${e.message}).`)));
  }
}

async function loadRuns() {
  const runs = await getJSON('/api/runs');
  document.getElementById('runs-empty').hidden = runs.length > 0;
  document.getElementById('runs').replaceChildren(...runs.map((r) => {
    const row = el('tr', { tabindex: '0', 'aria-selected': String(r.id === selected) },
      el('td', {}, badge(r.status)), el('td', { class: 'mono' }, r.id), el('td', {}, r.kind),
      el('td', {}, r.resource_id || '—'), el('td', { class: 'obj', title: r.objective }, r.objective),
      el('td', { title: r.created_at }, ago(r.created_at)));
    const open = () => { selected = r.id; loadRuns(); loadDetail(); };
    row.addEventListener('click', open);
    row.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); } });
    return row;
  }));
}

function diffView(diff) {
  return el('pre', { class: 'diff', 'aria-label': 'Proposed diff' }, diff.split('\n').map((line) => el('span', {
    class: line.startsWith('@@') ? 'hunk' : line.startsWith('+') && !line.startsWith('+++') ? 'add'
      : line.startsWith('-') && !line.startsWith('---') ? 'del' : '' }, line || ' ')));
}

async function loadDetail() {
  const box = document.getElementById('detail');
  if (!selected) return;
  const r = await getJSON(`/api/runs/${encodeURIComponent(selected)}`);
  const result = r.artifact && r.artifact.result;
  const excluded = Object.entries((r.routing && r.routing.excluded) || {});
  box.replaceChildren(...[
    el('h2', { id: 'detail-h' }, `Run ${r.id}`),
    el('dl', { class: 'kv' },
      el('dt', {}, 'Status'), el('dd', {}, badge(r.status)),
      el('dt', {}, 'Objective'), el('dd', {}, r.objective),
      el('dt', {}, 'Repository'), el('dd', { class: 'mono' }, r.repo),
      el('dt', {}, 'Worker'), el('dd', {}, r.resource_id || '—'),
      el('dt', {}, 'Route'), el('dd', {}, r.routing ? r.routing.rationale : 'not routed yet'),
      excluded.length ? [el('dt', {}, 'Excluded'), el('dd', {}, excluded.map(([k, v]) => `${k}: ${v.join('; ')}`).join(' · '))] : null,
      el('dt', {}, 'Base'), el('dd', { class: 'mono' }, r.base_commit ? r.base_commit.slice(0, 12) : '—'),
      el('dt', {}, 'Artifact'), el('dd', { class: 'mono' }, r.artifact ? r.artifact.artifact_id.slice(0, 23) + '…' : '—')),
    result ? [
      el('h4', {}, 'Worker summary'), el('div', {}, result.summary),
      result.findings.length ? [el('h4', {}, `Findings (${result.findings.length})`), el('ul', { class: 'findings' },
        result.findings.map((f) => el('li', {}, f.text, el('span', { class: 'prov' }, f.provenance.replace('_', ' ')))))] : null,
      result.unresolved.length ? [el('h4', {}, 'Unresolved'), el('ul', { class: 'findings' }, result.unresolved.map((u) => el('li', {}, u)))] : null,
    ] : null,
    el('h4', {}, r.diff_stats ? `Diff +${r.diff_stats.added} −${r.diff_stats.removed}` : 'Diff'),
    r.diff ? diffView(r.diff) : el('div', { class: 'meta' }, 'No changes proposed.'),
    r.status === 'needs_approval' ? el('p', { class: 'meta' }, 'Review, then run ',
      el('code', {}, `python -m merp_relay.ops approve ${r.id}`), ' or ', el('code', {}, `reject ${r.id}`), '.') : null,
    el('h4', {}, 'Timeline'),
    el('ol', { class: 'events' }, r.events.map((e) => el('li', {},
      el('span', { class: 'mono', title: e.at }, new Date(e.at).toLocaleTimeString()), el('span', {}, e.type),
      el('span', { class: 'meta' }, eventText(e)))))].flat(Infinity).filter((n) => n != null));
}

function eventText(e) {
  const d = e.data || {};
  if (e.type === 'routed') return `selected ${d.selected || 'none'} — ${d.rationale}`;
  if (e.type === 'worker_finished') return `result ${d.result}` + (d.metrics && d.metrics.runtime_version ? ` · ${d.metrics.runtime_version}` : '');
  if (e.type === 'apply_failed') return d.reason || '';
  if (e.type === 'submitted') return `${d.kind} in ${d.repo}`;
  return '';
}

function feed(e) {
  const list = document.getElementById('events');
  list.prepend(el('li', {}, el('span', { class: 'mono' }, new Date(e.at).toLocaleTimeString()),
    el('span', { class: 'mono' }, e.run_id), el('span', {}, `${e.type} ${eventText(e)}`)));
  while (list.children.length > 100) list.lastChild.remove();
}

function connect() {
  const conn = document.getElementById('conn');
  const source = new EventSource('/events');
  source.onopen = () => conn.replaceChildren(el('span', { class: 'dot ok' }), 'live');
  source.onerror = () => conn.replaceChildren(el('span', { class: 'dot bad' }), 'reconnecting…');
  source.onmessage = (msg) => {
    const e = JSON.parse(msg.data);
    feed(e);
    loadRuns();
    if (e.run_id === selected) loadDetail();
    if (e.type === 'worker_finished') loadPools();  // a finished run may carry fresh quota
  };
}

function theme() {
  const root = document.documentElement;
  try { const saved = localStorage.getItem('relay-theme'); if (saved) root.dataset.theme = saved; } catch (_) {}
  document.getElementById('theme').addEventListener('click', () => {
    const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('relay-theme', root.dataset.theme); } catch (_) {}
  });
}

theme();
const tick = () => { document.getElementById('clock').textContent = new Date().toLocaleTimeString([], { timeZoneName: 'short' }); };
tick(); setInterval(tick, 1000);
loadPools(); setInterval(loadPools, 30000);
loadRuns().catch(() => {}); setInterval(() => loadRuns().catch(() => {}), 15000);
connect();
