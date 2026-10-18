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
Claude repo-mode verified for real on 2026-10-18 (CLI 2.1.240, Codex made unavailable for the run): diff → approve →
staged change; `worker_finished` recorded `2.1.240 (Claude Code)`. Two fixes came out of this. First, 2.1.240 writes a
`system` line after the `result` envelope, so the parser now takes the last `result` line instead of the last line
(before this, every Claude run failed as malformed). Second, `cwd` is resolved because Claude's file tools deny 8.3
short paths (`GAMING~1`).

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

**M4 — Dashboard controls. ✅ DONE 2026-10-18.** The server also runs the serial runner in-process (`--no-runner` to opt out).
From the browser you can queue a task (repo path picker from recent runs), stop or cancel, approve & apply (confirm dialog),
reject, and pause/resume routing (a ledger setting, also honored by `ops work`). Writes need a per-start token, sent
in the page `<meta>` and echoed as `X-Relay-Token`, and a local Origin/Host. The input is validated (objective ≤ 4000 chars,
git repo with a commit, timeout 30–3600 s). Verified for real: browser submit → Codex run → diff → Approve → README staged in the scratch repo.
**Cut on purpose (ultra):** reserve toggles and per-pool `limit_action`. No reserves are configured, and `route()`
already skips exhausted pools, which in effect is `rotate`. Add these when a second account or a reserve policy actually exists.

**M5 — Cross-vendor review + revise. ✅ DONE 2026-10-18.** Every diff gets a tool-free snapshot review by the
*other* vendor before it reaches `needs_approval` (objective + diff + the author's artifact). The verdict is a summary that starts
with `APPROVE:` or `CHANGES:` and is shown as a badge next to Approve. A review is skipped with a reason, and never blocks approval, when
there is no other vendor or the diff exceeds the 16 KiB packet (`# shortcut:`). **Revise** sends a diff back with feedback:
the run becomes `revised` (Superseded), and a child run (`parent`) starts from the previous diff, so the operator approves one
cumulative diff. CLI: `ops revise ID "feedback"`. Verified for real: Codex implemented, Claude reviewed with `CHANGES:`
and caught a mismatch between the objective and the repo, a revision added tests, the re-review said `APPROVE:`, and approve
applied it with the worker's tests passing. Lessons: the review prompt must say the diff is cumulative against the base. One review
returned placeholder output ("test"); it shows as "No verdict" rather than a false approve, and a rerun was fine.
Fixed from live data: a Claude window whose reset time has passed now reads unknown instead of its stale pre-reset %.

**Later (only when a need shows up; see V2 below):** parallel runs, auto-rotation across several accounts, notifications,
fix-round session resume, more runtimes (Gemini/OpenCode).

## V2 — Lead agent + RelayHQ (decided 2026-10-18)

Owner need: one **lead** model takes instructions, briefs with the owner, splits the work, picks workers (intelligence,
effort, parallelism), reads their reports, and decides whether to ask the owner or keep going. It also keeps context and history
outside any git repo, and can later add more runtimes (Cursor, local models, Grok).

**Decision: Relay stays the hands, a CLI agent is the head.** Don't build an orchestrator loop. Relay ships an **MCP server**,
and any MCP-capable agent CLI becomes the lead. Both subscription CLIs are MCP clients, so the lead can be swapped:

| Lead | How | Verdict |
|---|---|---|
| **GPT-6.1 Sol via Codex CLI** | `codex -m gpt-6.1-sol` with `[mcp_servers.relay]` in config.toml (stdio) | ✅ Verified 2026-10-18 on the ChatGPT Plus login. Uses the ChatGPT quota |
| **Opus 5.5 via Claude Code** | `.mcp.json` in RelayHQ (stdio) | ✅ Same server. Uses the Claude Pro quota. Use it when ChatGPT quota is low |
| ChatGPT app (Developer Mode) | Remote MCP only, so the local control plane would need a public HTTPS tunnel. Plan support for write tools is unclear | ❌ Not now: it would expose a tool that runs agents on your repos to the internet |
| API lead (Responses API, Sol $2/$10 per M tokens, 1.05M context) | Our own loop, pay per token, "unlimited" | ⏸ Later, if subscription quota for the lead becomes the limit. Estimated ~$1–5/day for a lead with cached context |

Switching lead = opening the other CLI in RelayHQ. Both read the same files, so state survives a switch.

**RelayHQ** (`D:\Work\RelayHQ`, its own git repo, so every context change can be traced):
```
AGENTS.md / CLAUDE.md        lead role, policy, how to use the relay tools (same content for both CLIs)
policy.md                    when to ask the owner, approval rules, budget rules
projects/<name>/brief.md     goal, scope, constraints, repo paths
projects/<name>/decisions.md decision log (why)
projects/<name>/state.md     current summary the lead keeps updated (not transcripts)
journal/YYYY-MM-DD.md        the lead's daily log: what it dispatched, got back, decided
```
Machine history stays in `relay.db` (runs, events, reviews). Journal entries cite run IDs, so you can trace from a decision
to a run, its diff and its review.

**Relay MCP tools:** `workers()` (runtimes, tiers, live quota) · `dispatch(objective, repo, kind, worker?, tier?,
depends_on?)` · `runs(status?)` / `report(run_id)` (artifact + review, never transcripts) · `wait(run_ids, timeout)` ·
`approve(run_id, reason)` / `reject` / `revise(run_id, feedback)` · `ask_owner(question)` → dashboard inbox.
Context lives as plain files the lead edits itself.

**Approval policy (owner, 2026-10-18):** the lead approves on its own and asks the owner only when it judges it necessary.
Guardrails in code, not just in the prompt: `approve` needs a reason, is logged as `approved_by: lead`, applies staged and
never commits or pushes. `policy.md` lists when the lead must ask (e.g. reviewer said `CHANGES:`, no review, deletions,
config/secrets/CI files, more than N files). The owner commits.

**Phases**
- **P1 — Lead MVP:** RelayHQ skeleton, `merp_relay.mcp` (stdio, stdlib JSON-RPC), lead instructions, owner inbox
  (ledger table, dashboard panel). Done when Sol (Codex) and Opus (Claude Code) can each brief → dispatch → wait →
  approve or ask, on a scratch project, with a journal entry and traceable run IDs.
- **P1 ✅ DONE 2026-10-18.** `python -m merp_relay.mcp` (stdio JSON-RPC, stdlib) with 12 tools: `workers dispatch runs
  report wait approve reject revise cancel ask_owner inbox checkpoint`. `approve` enforces the policy in code (review must be
  `APPROVE`; no deletions, sensitive paths or more than 20 files) and logs `decision by=lead` with its reason. RelayHQ at `D:\Work\RelayHQ`
  holds AGENTS.md/CLAUDE.md, policy, templates, journal and launchers. The dashboard has an inbox. A cross-process `RunnerLock`
  means the dashboard and MCP servers never run two runners. Verified for real on a demo repo:
  - **Sol (Codex, gpt-6.1-sol):** dispatch → wait → report. The review said APPROVE, but the lead saw an ambiguity (Unicode
    in slugify) and **asked the owner** (`q-ae82b90a`) instead of approving, then journaled and checkpointed.
  - **Opus (Claude Code):** read the pending state, wrote task 2 so it could not conflict with unapplied task 1,
    verified the diff, **approved with a reason** (staged), then journaled and checkpointed. The demo tests pass.
  - Lessons: Codex refuses MCP calls unattended unless `mcp_servers.relay.default_tools_approval_mode="approve"` is set
    (Relay's tools enforce their own policy). Codex's sandbox protects `.git`, hence the `checkpoint` tool. The lead's
    context cost is real: one loop took about 425k input tokens on Sol (95% cached) and about 450k cached on Opus (≈$0.94 equivalent).
    The `opus` alias is Opus 5 on Claude Code 2.1.240; Opus 5.5 needs `claude update` (≥ 2.1.280).
  - Known gap → P2: tasks that touch the same files and are approved in a different order conflict (task 1 and 2 both
    create `test_textutils.py`). Needs `depends_on` plus re-basing a pending run on the new HEAD.
- **P2 — Parallel + tiers:** N slots per pool (worktrees already isolate), worker tiers by model and effort (Codex `-m`,
  `model_reasoning_effort`; Claude `--model`), `depends_on` task tree.
- **P3 — Trace view:** dashboard view from journal/brief → subtasks → runs → reviews → decisions, plus inbox answers.
- **P4 — More runtimes:** Cursor CLI, local models through a headless harness, Grok (API key, explicit opt-in).

Main risk: lead token use. The lead stays thin: it plans, decides and reads summaries, and workers do the heavy work. Two
subscription leads double the available headroom.

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
