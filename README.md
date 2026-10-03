# Merp Relay — M0

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

Explicit subscription smoke, using existing Codex authentication without login:

```powershell
.\.venv\Scripts\python -m merp_relay.spike --run
```

Run from the repository root. The smoke supplies a small fixture snapshot to Codex,
disables shell and web search, uses a disposable working directory, and permits at most
45 seconds. It writes sanitized evidence to `docs/evidence/codex-smoke.json`.
Do not adapt this command to private repositories until the boundary gate is met.
Claude is explicitly blocked because supported local auth status reported no login.

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

See [feasibility](docs/adapter-feasibility.md) and [decision](docs/decisions/0001-v01-foundation.md).
