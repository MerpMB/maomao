# Merp Relay — Roadmap (Ponytail mode)

Date: 2026-10-18. Owner: MerpMB. Supersedes the "smallest next slice" sections of 0001/0002.

## What Relay is

A personal, local control plane that sends coding/analysis tasks to the AI subscriptions
you already pay for (Claude Code Pro, Codex/ChatGPT). It routes by capability and **real**
quota headroom, hands work between workers as hashed artifacts (no transcripts), and lets
one operator see and control everything from one screen.

## Where we are (verified 2026-10-18)

- 1.3k lines Python: contracts, deterministic `route()`, shared eligibility, bounded runner,
  Codex/Claude adapters, artifact hashing. 88 tests pass. 5 error only because the sandbox
  blocks temp dirs (they need a normal shell).
- **Claude auth blocker is gone:** `claude auth status` → `loggedIn=true, authMethod=claude.ai,
  subscriptionType=pro`.
- `codex` is not on PATH in this shell, but `~/.codex/sessions` has 106 rollout logs. Re-check the install path.
- The cost of M0–M0.1 went into proving controls that no CLI exposes for proof. Every profile is
  still `unknown`, so nothing can run in normal use. **This is the main thing to change.**

## The one design change

Stop trying to prove that the runtime enforces its controls. Contain the work instead, and put a human gate where it matters:

| Mode | Containment | Gate |
|---|---|---|
| `snapshot_analysis` | Packet only, tools off (current flags) | none: output is just an artifact |
| `repository_access` | Disposable **git worktree** per run, runtime's own sandbox (`codex --sandbox workspace-write`, `claude --permission-mode` + `--allowedTools`) | **Operator approves the diff** before `apply` |

Controls keep their honest state (`unknown`, `observed_only`). A new admission rule accepts
`unknown` when the run is worktree-contained and needs approval. Nothing reaches the real
branch without a click. This is the pattern proven by
[claude-orchestrator](https://github.com/egorpol/claude-orchestrator) (worktree + cross-vendor review).

## Prior art used

- **[Claudexor](https://github.com/razzant/claudexor)** (MIT, TS, macOS/Linux): quota-aware rotation,
  5h/7d bars, rotate **only on typed limit signals**, `limit_action = fail|ask|rotate`. Copy the
  policy names and the typed `subscription_window_exhausted` refusal. Don't copy the stack (no Windows).
- **claude-orchestrator**: worktree per job, brief + log + check command, fix rounds resume the
  session, the other vendor reviews read-only, logs kept outside the repo.
- **Quota sources (real, local, no API key):**
  - Claude: statusline JSON `rate_limits.five_hour/seven_day` (`used_percentage`, `resets_at`) for Pro/Max,
    present after the first response. Some versions dropped it ([#45133](https://github.com/anthropics/claude-code/issues/45133)), so treat a missing field as `unknown`.
  - Codex: `token_count` events in `~/.codex/sessions/**/rollout-*.jsonl` carry `rate_limits.primary/secondary`
    (300 / 10080 min windows). `codex exec --json` may emit `null` ([#14728](https://github.com/openai/codex/issues/14728)), so read the rollouts.
  These become `ResourceObservation(evidence='locally_observed', accounting_basis='used_percent')`.
  `route()` already compares headroom when the basis matches.

## Milestones (each ≈ 1–2 sessions, ends with a runnable check)

**M1 — Live on both workers. ✅ DONE 2026-10-18.** Real Codex 0.163 INVESTIGATE → Claude 2.1.240 REVIEW
handoff succeeded, with the fixture unchanged ([evidence](evidence/m1-handoff.json)). `python -m merp_relay.quota` prints both pools
with reset times. Changes: Codex is found through the desktop-app package when it is not on PATH. Version pins were dropped
(the version is recorded; a changed interface fails closed in normalization). Claude uses `stream-json` and every run records
its `rate_limit_event` to the quota cache. A typed `rejected` status means `exhausted`. The snapshot review runs under the contained-snapshot policy.
Carried into M2: `route()` still excludes both profiles for snapshot mode (controls `unknown`), so the queue
must apply the same contained policy that `execute_snapshot(weaker_experiment=True)` uses.

**M2 — Ledger + worktree runs. ✅ DONE 2026-10-18.** `ledger.py`: stdlib sqlite (`runs`, `events`) at
`%LOCALAPPDATA%\merp-relay\relay.db`. Status changes are compare-and-set, so a race cannot apply twice or revive a run.
`ops.py`: serial queue → `route(contained=True)` on live quota → `git worktree add --detach` → worker
(Codex `workspace-write`, or Claude with file tools only plus `acceptEdits`) → stored `git diff` → worktree removed.
`approve` = `git apply --index` (staged, never committed; refuses files the operator has changed). `reject` keeps the diff for audit.
`cancel` works across processes: the runner polls the ledger once a second and stops the process tree.
Verified for real on a scratch repo: Codex diff → approve → staged change. A mid-run cancel stopped Codex about 0.6 s after the request.
Reject, conflicts, crashes and orphan recovery are covered by tests. Lessons: Windows Codex needs `windows.sandbox="elevated"`
when user config is ignored, and patches must go through git as bytes (CRLF).

```
python -m merp_relay.ops submit "objective" --repo PATH [--timeout 900]
python -m merp_relay.ops work [--watch]      # serial runner
python -m merp_relay.ops list | show ID | approve ID | reject ID | cancel ID
```

**M3 — Ops Dashboard v1 (read-only). ✅ DONE 2026-10-18.** `python -m merp_relay.server` → http://127.0.0.1:8765/.
Stdlib `ThreadingHTTPServer`: `GET /api/pools|runs|runs/{id}` and SSE `/events` (tails the ledger event table).
Static `index.html` + `app.js` with no build step. Shows the pools (5h/7d meters, reset countdown, stale shown as unknown),
the runs, run detail (route rationale, worker-reported findings, diff, timeline) and a live event feed. Safety:
bound to 127.0.0.1, a foreign `Host` gets 403 (DNS rebinding), CSP `default-src 'self'`, and all ledger text goes through
`textContent`. Checked in the browser at desktop and 375px width, light and dark.
Note: Codex repo runs may execute shell commands inside its workspace-write sandbox (it ran its own checks).
Claude repo runs have file tools only.

**M4 — Dashboard controls.** Submit task, cancel, approve/reject diff, pause routing, toggle reserves,
`limit_action` per pool. ✅ Done when a full task lifecycle runs from the browser with no CLI.

**Later (only when a need shows up):** parallel runs, auto-rotation across several accounts, notifications,
fix-round session resume, more runtimes (Gemini/OpenCode).

## Control Dashboard (ops center)

**Stack (stdlib first):** `http.server.ThreadingHTTPServer` + JSON API + SSE (`/events`) +
one `static/index.html` with vanilla JS/CSS. No build step and no new dependencies. Bound to `127.0.0.1` only,
with a random token printed at start (header check on every write, M4). Command: `python -m merp_relay.server`.

```
┌ MERP RELAY ─ OPS ──────────────────────── ● routing ON  [Pause] ─ 14:02 ICT ┐
│ POOLS                                                                        │
│  claude-pro   5h ███████░░░ 68%  reset 15:40   7d ███░░░░░ 31%  fresh 2m   │
│  codex-plus   5h ██░░░░░░░░ 18%  reset 16:05   7d ██████░░ 74%  STALE 3h ⚠ │
├──────────────────────────────┬───────────────────────────────────────────────┤
│ QUEUE / RUNS                 │ RUN r-0142  REVIEW → claude-pro   running 1:12 │
│ ● r-0142 REVIEW  running     │ route: codex excluded: reserved for INVESTIGATE│
│ ◐ r-0141 FIX     needs approval│ artifact sha256:9f3c…   findings (3)        │
│ ✓ r-0140 INVEST  completed   │ diff +12 −3  [View] [Approve] [Reject]        │
│ ✕ r-0139 INVEST  exhausted   │ [Cancel run]                                  │
├──────────────────────────────┴───────────────────────────────────────────────┤
│ EVENTS  14:01 claude-pro observation 68% · 13:58 r-0141 awaiting approval …  │
└──────────────────────────────────────────────────────────────────────────────┘
```

Panels map 1:1 to existing contracts:
Pools ← `ResourceObservation` (+ freshness/expiry). Runs ← ledger. Route line ← `RoutingDecision.excluded/rationale`.
Detail ← `Artifact`/`Claim` (shows `worker_reported` vs `relay_observed`). Events ← SSE ledger tail.

Status colors are always paired with a text label. Stale or unknown is shown as **unknown**, never as 0%.

API: `GET /api/pools`, `GET /api/runs`, `GET /api/runs/{id}`, `POST /api/runs` (submit),
`POST /api/runs/{id}/cancel|approve|reject`, `POST /api/routing/pause|resume`, `GET /events`.

## Working rules (Ponytail)

- Smallest complete change. Stdlib → installed deps (pydantic) → new code. No frameworks.
- Each milestone = one branch, one PR, one acceptance check. No evidence essays: one ADR paragraph per real decision.
- Known shortcuts are marked inline: `# shortcut: <limit>, <when to upgrade>`. Collect them with `grep shortcut:`.
- Never cut: approval before writing to a real branch, token on write APIs, localhost bind, honest `unknown`.

## Deferred on purpose

Multi-user/auth, remote access, a database server, a React/build pipeline, API-key backends,
automatic retries, and parsing undocumented session files beyond the rollout `token_count` lines.
