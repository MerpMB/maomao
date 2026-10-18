# Merp Relay

> **Current plan: [docs/ROADMAP.md](docs/ROADMAP.md).** M1–M2 are done: live 5h/7d quota (`python -m merp_relay.quota`),
> a real Codex→Claude handoff, and worktree runs with operator approval (`python -m merp_relay.ops`).
> Ops dashboard with runner and controls: `python -m merp_relay.server` → http://127.0.0.1:8765/.
> Every diff is reviewed by the other vendor before approval; Revise sends it back with feedback (M5).
> The M0/M0.1 notes below are historical.

## M0 / M0.1 (historical)

Personal resource-aware orchestration foundation. M0 implements validated contracts,
pure deterministic routing, structured artifact handoff, and a disposable Codex spike.
It does not implement the complete V0.1 scheduler or establish a real-repository boundary.

Python 3.12+ (locally tested on 3.13.7). PowerShell setup:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m merp_relay.spike
```

M0.1 probes and fail-closed snapshot admission report (no inference):

```powershell
.\.venv\Scripts\python -m merp_relay.conformance
```

Explicit disposable experiment, at most two serial calls of 45 seconds each:

```powershell
.\.venv\Scripts\python -m merp_relay.conformance --run
```

Run from the repository root. The smoke supplies a small fixture snapshot to Codex,
disables shell and web search, uses a disposable working directory, and permits at most
45 seconds per call. It writes sanitized evidence to `docs/evidence/m01-snapshot.json`.
Do not adapt this command to private repositories until the boundary gate is met.
Claude is explicitly blocked because supported local auth status reported no login.
The implemented Claude stdin/JSON path remains unexercised. Both profiles fail ordinary
snapshot admission because required controls remain unknown. The `--run` harness explicitly
uses weaker disposable Codex experiments and records that exception; it does not qualify a profile.
Runtime upgrades require interface revalidation. Subscription auth is checked with supported commands.
Routing and dispatch share `task_profile_eligibility()`: mode, capabilities, tools and required
controls must agree. The disposable weaker-boundary exception can waive control qualification
only; it cannot waive incompatible mode, capabilities or tools. Pool state, reserves and ranking
remain routing concerns, not claims made by dispatch admission.

`route()` accepts a task, profiles, observations, reserves, preference order, an explicit
timezone-aware clock, and configuration reference. It filters requirements and enforced
controls, rejects current exhausted/unavailable pools, applies reserves, compares headroom
only when every eligible pool has fresh observations with the same accounting basis,
then breaks ties by configured preference and resource ID. Quota unknowns are eligible
but carry no headroom. Latest expired observations become unknown; they never release a
reserve or prove availability. A later unknown observation supersedes an older exhausted
one. Future observations are ignored. Callers must supply trusted observations and mappings.

Artifacts validate their content hash and contain results, references and provenance;
they contain no transcript. Structural checkpoints and worker continuations are separate
contracts. M0 persists smoke artifacts, not a general recovery engine.

`snapshot_analysis` supplies selected, hashed source data and upstream artifacts in a packet
limited to 16 KiB; source instructions are treated as data. Review packets retain the original
source so reviewers can challenge upstream findings. Snapshot admission requires declared
enforcement of disabled model tools, shell/code, filesystem tools, browsing/external requests,
and inherited integrations. Scoped repository filesystem reads are not required in this mode.
`repository_access` remains unsupported by these runtime profiles. Snapshot tool restrictions
do not establish OS confinement or prevent runtime transport networking.

Timeout tests use harmless local Python processes. Windows cleanup requests termination of
the owned PID and descendants; the recorded runtime outcome proves only the local parent exit.
Remote cancellation remains unknown, and no usage-limit observations are automatically created.
The adapters' public `cancel()` API is explicitly **unsupported**: synchronous dispatch exposes
no active process handle, so it sends no stop request. Only runner timeout cleanup requests a
stop. A timeout reports `cancelled` only when local parent exit is observed; otherwise it reports
`failed`. Tree and remote state remain unknown. Stdin/stdout use temporary files so cleanup does
not wait on reader threads or pipes held by descendants; stderr is discarded. Each cleanup wait
is bounded at five seconds (at most three such waits on Windows). A failed kill may leave a live
process; the result records that uncertainty.

Codex normalization requires exactly one `item.completed` agent message before exactly one
`turn.completed`. Started/updated message text is ignored rather than parsed as final JSON;
ambiguous multiple finals, malformed final data and failure events fail closed. Runtime profiles
separate documented interface support from verified control qualification. All current required
controls are unknown, including Claude flags whose combined enforcement path remains unvalidated.

See [feasibility](docs/adapter-feasibility.md) and [decision](docs/decisions/0001-v01-foundation.md).
The [snapshot decision](docs/decisions/0002-snapshot-execution.md) records M0.1 gates.
