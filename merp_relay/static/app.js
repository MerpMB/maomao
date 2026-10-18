'use strict';
// Every value from the ledger is untrusted worker/operator text: build DOM with textContent only.

const STATUS = {
  queued: ['Queued', '--muted'], running: ['Running', '--accent'], cancel_requested: ['Cancelling', '--warning'],
  needs_approval: ['Needs approval', '--warning'], applying: ['Applying', '--accent'], applied: ['Applied', '--good'],
  completed: ['Completed', '--good'], rejected: ['Rejected', '--muted'], failed: ['Failed', '--critical'],
  cancelled: ['Cancelled', '--muted'], blocked: ['Blocked', '--serious'], revised: ['Superseded', '--muted'],
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
const TOKEN = document.querySelector('meta[name="relay-token"]').content;
async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Relay-Token': TOKEN },
    body: JSON.stringify(body || {}) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}
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
    actions(r),
    el('div', { class: 'notice error', id: 'action-notice', role: 'alert', hidden: '' }),
    el('dl', { class: 'kv' },
      el('dt', {}, 'Status'), el('dd', {}, badge(r.status)),
      el('dt', {}, 'Objective'), el('dd', {}, r.objective),
      el('dt', {}, 'Repository'), el('dd', { class: 'mono' }, r.repo),
      el('dt', {}, 'Worker'), el('dd', {}, r.resource_id || '—'),
      lineage(r),
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
    reviewSection(r.events),
    el('h4', {}, r.diff_stats ? `Diff +${r.diff_stats.added} −${r.diff_stats.removed}` : 'Diff'),
    r.diff ? diffView(r.diff) : el('div', { class: 'meta' }, 'No changes proposed.'),
    el('h4', {}, 'Timeline'),
    el('ol', { class: 'events' }, r.events.map((e) => el('li', {},
      el('span', { class: 'mono', title: e.at }, new Date(e.at).toLocaleTimeString()), el('span', {}, e.type),
      el('span', { class: 'meta' }, eventText(e)))))].flat(Infinity).filter((n) => n != null));
}

function runLink(id) {
  const a = el('a', { href: '#', class: 'mono' }, id);
  a.addEventListener('click', (ev) => { ev.preventDefault(); selected = id; loadRuns(); loadDetail(); });
  return a;
}

function lineage(r) {
  const next = r.events.filter((e) => e.type === 'revision_queued').map((e) => e.data.next)[0];
  return [r.parent ? [el('dt', {}, 'Revision of'), el('dd', {}, runLink(r.parent))] : null,
          next ? [el('dt', {}, 'Superseded by'), el('dd', {}, runLink(next))] : null];
}

function reviewSection(events) {
  const last = events.filter((e) => ['review_started', 'reviewed', 'review_skipped'].includes(e.type)).pop();
  if (!last) return null;
  const head = el('h4', {}, 'Cross-vendor review');
  if (last.type === 'review_skipped') return [head, el('div', { class: 'meta' }, `Skipped: ${last.data.reason}`)];
  if (last.type === 'review_started') return [head, el('div', { class: 'meta' }, `Reviewing with ${last.data.reviewer}…`)];
  const res = last.data.result;
  const summary = res.summary || '';
  const verdict = res.status !== 'completed' ? [`Review ${res.status}`, '--muted']
    : summary.startsWith('APPROVE:') ? ['Reviewer approves', '--good']
    : summary.startsWith('CHANGES:') ? ['Reviewer requests changes', '--warning'] : ['No verdict', '--muted'];
  return [head,
    el('div', { class: 'actions' }, el('span', { class: 'badge', style: `--sc: var(${verdict[1]})` }, verdict[0]),
      el('span', { class: 'meta' }, `by ${last.data.reviewer} · worker reported`)),
    el('div', {}, summary.replace(/^(APPROVE|CHANGES):\s*/, '')),
    res.findings && res.findings.length ? el('ul', { class: 'findings' }, res.findings.map((f) => el('li', {}, f.text))) : null,
    res.unresolved && res.unresolved.length ? [el('div', { class: 'meta' }, 'Unresolved:'),
      el('ul', { class: 'findings' }, res.unresolved.map((u) => el('li', {}, u)))] : null];
}

function reviseForm(r) {
  const area = el('textarea', { maxlength: '2000', 'aria-label': 'Feedback for the next attempt',
    placeholder: 'What should change? The next run starts from this diff.' });
  const send = el('button', { type: 'button', class: 'primary' }, 'Send back');
  const form = el('div', { class: 'submit', hidden: '' }, area, el('div', { class: 'actions' }, send));
  send.addEventListener('click', async () => {
    const notice = document.getElementById('action-notice');
    notice.hidden = true;
    send.disabled = true;
    try {
      const res = await post(`/api/runs/${encodeURIComponent(r.id)}/revise`, { feedback: area.value });
      selected = res.id;
    } catch (err) { notice.textContent = `Revise failed: ${err.message}`; notice.hidden = false; }
    finally { send.disabled = false; loadRuns(); loadDetail(); }
  });
  return form;
}

function actions(r) {
  const buttons = [];
  const act = (label, verb, cls, confirmText) => {
    const b = el('button', { type: 'button', class: cls }, label);
    b.addEventListener('click', async () => {
      if (confirmText && !confirm(confirmText)) return;
      const notice = document.getElementById('action-notice');
      notice.hidden = true;
      b.disabled = true;
      try { await post(`/api/runs/${encodeURIComponent(r.id)}/${verb}`); }
      catch (err) { notice.textContent = `${label} failed: ${err.message}`; notice.hidden = false; }
      finally { b.disabled = false; loadRuns(); loadDetail(); }
    });
    buttons.push(b);
  };
  if (r.status === 'needs_approval') {
    act('Approve & apply', 'approve', 'primary',
      `Apply this diff to ${r.repo}?\nIt is staged, not committed. Files changed since the run are refused.`);
    act('Reject', 'reject', 'danger', 'Reject this diff? It stays in the ledger for audit.');
    const form = reviseForm(r);
    const toggle = el('button', { type: 'button', 'aria-expanded': 'false' }, 'Revise…');
    toggle.addEventListener('click', () => {
      form.hidden = !form.hidden;
      toggle.setAttribute('aria-expanded', String(!form.hidden));
      if (!form.hidden) form.querySelector('textarea').focus();
    });
    buttons.splice(1, 0, toggle);
    return [el('div', { class: 'actions' }, buttons), form];
  }
  if (['queued', 'running'].includes(r.status)) act(r.status === 'queued' ? 'Cancel' : 'Stop run', 'cancel', 'danger');
  return buttons.length ? el('div', { class: 'actions' }, buttons) : null;
}

function eventText(e) {
  const d = e.data || {};
  if (e.type === 'routed') return `selected ${d.selected || 'none'} — ${d.rationale}`;
  if (e.type === 'worker_finished') return `result ${d.result}` + (d.metrics && d.metrics.runtime_version ? ` · ${d.metrics.runtime_version}` : '');
  if (e.type === 'apply_failed') return d.reason || '';
  if (e.type === 'submitted') return `${d.kind} in ${d.repo}`;
  if (e.type === 'setting') return `${d.key} = ${d.value}`;
  if (e.type === 'quota_refresh') return Object.entries(d).map(([k, v]) => `${k}: ${v}`).join(' · ');
  if (e.type === 'decision') return `${d.by} ${d.action}` + (d.reason ? ` — ${d.reason}` : '');
  if (e.type === 'question') return `${d.asked_by} asks: ${d.question}`;
  if (e.type === 'answer') return `owner answered ${d.id}`;
  if (e.type === 'review_started') return `reviewer ${d.reviewer}`;
  if (e.type === 'reviewed') return `${d.reviewer}: ${(d.result && d.result.summary || '').slice(0, 80)}`;
  if (e.type === 'review_skipped') return d.reason;
  if (e.type === 'revision_queued') return `next attempt ${d.next}`;
  return '';
}

function feed(e) {
  const list = document.getElementById('events');
  list.prepend(el('li', {}, el('span', { class: 'mono' }, new Date(e.at).toLocaleTimeString()),
    el('span', { class: 'mono' }, e.run_id || 'relay'), el('span', {}, `${e.type} ${eventText(e)}`)));
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
    if (e.type === 'setting' || e.type === 'submitted') loadStatus();
    if (e.type === 'question' || e.type === 'answer') loadInbox();
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

async function loadInbox() {
  let questions;
  try { questions = await getJSON('/api/inbox'); } catch (_) { return; }
  const open = questions.filter((q) => q.status === 'open');
  document.getElementById('inbox').hidden = open.length === 0;
  document.getElementById('questions').replaceChildren(...open.map((q) => {
    const area = el('textarea', { maxlength: '4000', 'aria-label': `Answer to ${q.id}` });
    const send = el('button', { type: 'button', class: 'primary' }, 'Answer');
    const notice = el('div', { class: 'notice error', role: 'alert', hidden: '' });
    send.addEventListener('click', async () => {
      send.disabled = true;
      try { await post(`/api/inbox/${encodeURIComponent(q.id)}/answer`, { answer: area.value }); loadInbox(); }
      catch (err) { notice.textContent = err.message; notice.hidden = false; }
      finally { send.disabled = false; }
    });
    return el('div', { class: 'question' },
      el('div', { class: 'meta' }, `${q.asked_by} · ${ago(q.created_at)} ago`, q.project ? ` · ${q.project}` : '',
        q.run_id ? [' · ', runLink(q.run_id)] : null),
      el('div', {}, q.question), area, el('div', { class: 'actions' }, send), notice);
  }));
}

let status = null;
async function loadStatus() {
  try { status = await getJSON('/api/status'); } catch (_) { return; }
  const runner = document.getElementById('runner');
  const state = !status.runner ? ['Runner off', '--muted'] : status.paused ? ['Routing paused', '--warning'] : ['Runner active', '--good'];
  runner.replaceChildren(el('span', { class: 'badge', style: `--sc: var(${state[1]})` }, state[0]),
    status.queued ? ` ${status.queued} queued` : '');
  const pause = document.getElementById('pause');
  pause.disabled = false;
  pause.textContent = status.paused ? 'Resume routing' : 'Pause routing';
  document.getElementById('repos').replaceChildren(...status.recent_repos.map((r) => el('option', { value: r })));
  const repo = document.querySelector('#submit-form [name=repo]');
  if (!repo.value && status.recent_repos.length) repo.value = status.recent_repos[0];
}

document.getElementById('refresh').addEventListener('click', async (ev) => {
  const button = ev.target;
  button.disabled = true;
  button.textContent = 'Refreshing…';
  try {
    const res = await post('/api/quota/refresh');
    const failed = Object.entries(res.outcome).filter(([, v]) => v !== 'refreshed');
    button.title = failed.length ? failed.map(([k, v]) => `${k}: ${v}`).join('; ') : 'Refreshed just now';
  } catch (err) { button.title = `Refresh failed: ${err.message}`; }
  finally { button.disabled = false; button.textContent = 'Refresh quota'; loadPools(); }
});

document.getElementById('pause').addEventListener('click', async (ev) => {
  ev.target.disabled = true;
  try { await post('/api/routing', { paused: !(status && status.paused) }); } finally { loadStatus(); }
});

document.getElementById('submit-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const form = ev.target, notice = document.getElementById('submit-notice'), button = form.querySelector('button');
  const data = new FormData(form);
  button.disabled = true;
  notice.classList.remove('error');
  try {
    const res = await post('/api/runs', { objective: data.get('objective'), repo: data.get('repo').trim(),
      kind: data.get('kind'), timeout: Number(data.get('timeout')) });
    notice.textContent = `Queued ${res.id}.` + (status && status.paused ? ' Routing is paused: it waits until you resume.' : '');
    form.querySelector('[name=objective]').value = '';
    selected = res.id;
    loadRuns(); loadDetail(); loadStatus();
  } catch (err) {
    notice.textContent = err.message;
    notice.classList.add('error');
  } finally {
    notice.hidden = false;
    button.disabled = false;
  }
});

theme();
loadStatus(); setInterval(loadStatus, 10000);
loadInbox(); setInterval(loadInbox, 15000);
const tick = () => { document.getElementById('clock').textContent = new Date().toLocaleTimeString([], { timeZoneName: 'short' }); };
tick(); setInterval(tick, 1000);
loadPools(); setInterval(loadPools, 30000);
loadRuns().catch(() => {}); setInterval(() => loadRuns().catch(() => {}), 15000);
connect();
