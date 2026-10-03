# M0 adapter feasibility

M0 evidence below is preserved. The M0.1 update at the end supersedes current readiness
and invocation guidance; historical evidence was not rewritten to imply stronger controls.

Observation date: **2026-10-03**, owner timezone Asia/Bangkok. Windows/PowerShell,
Python 3.13.7. Repository initially contained only `.git`, no commits, docs, package,
or applicable AGENTS.md at repository/parent/root. Branch: `codex/m0-foundation`.
Initial sandbox denied branch metadata writes, dependency network access and launching
Claude; scoped escalated commands succeeded. No global configuration was changed.

## Evidence vocabulary

- `official_documentation`: supported published interface; not proof of this installation's behavior.
- `locally_observed`: actual process output or Relay measurement on this date.
- `unofficial_undocumented`: unsupported local details; none relied on here.
- `design_hypothesis`: policy/configuration choice needing validation.

Sources fetched on the observation date:

- [C1: Codex non-interactive](https://learn.chatgpt.com/docs/non-interactive-mode)
- [C2: Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [C3: Codex security](https://learn.chatgpt.com/docs/security)
- [A1: Claude programmatic CLI](https://code.claude.com/docs/en/headless)
- [A2: Claude CLI reference](https://code.claude.com/docs/en/cli-reference)
- L1: local `codex --version`, `codex exec --help`, `codex login status`.
- L2: local `claude --version`, `claude --help`, `claude auth status`.
- L3: [sanitized real Codex smoke](evidence/codex-smoke.json), produced by Relay.

## Matrix

| Dimension | Codex CLI 0.160.0 | Claude Code 2.1.240 |
|---|---|---|
| Invocation/auth | `exec`; existing ChatGPT login (L1 locally_observed; C1 official_documentation) | `-p`; auth status `loggedIn=false`, `authMethod=none`, `apiProvider=firstParty` (L2 locally_observed; A1/A2 official_documentation) |
| Structured output | `--json`, `--output-schema`; real structured agent message (L1/L3 locally_observed, C1 official_documentation) | `--output-format json/stream-json`, `--json-schema` in help (L2 locally_observed; A1/A2 official_documentation); execution unverified |
| Tools/filesystem/network | Read-only sandbox requested; shell_tool and unified_exec disabled; web_search disabled; user config/rules ignored (L1/L3 locally_observed; C2 official_documentation). Other built-in tools and OS confinement unverified | `--tools`, `--safe-mode`, permission modes and strict MCP config in local help (L2 locally_observed; A2 official_documentation). No execution/control verification |
| Usage visibility | L3 token counters, not subscription headroom; accounting relation to quota unknown | Result usage/cost interfaces documented (A1 official_documentation); no locally observed counters or headroom |
| Cancellation | Relay kills local process on timeout; remote/descendant stop unknown (design_hypothesis; synthetic process test) | Blocked stub; Windows cancellation unverified |
| Native resume | `exec resume` documented/help observed (C1 official_documentation, L1 locally_observed); not exercised; spike ephemeral | `--resume/--continue` documented/help observed (A2 official_documentation, L2 locally_observed); not exercised |
| Disposable readiness | One real successful snapshot investigation, unchanged fixture; limited trust boundary (L3 locally_observed) | Installed candidate; execution blocked by missing auth (L2 locally_observed) |
| Real repository readiness | **Gate not passed**: no verified read-scope confinement, complete tool allowlist, OS/network boundary or adversarial denial checks | **Gate not passed**: missing auth and unverified controls |

## Real spike and normalization

Command: `.venv\Scripts\python -m merp_relay.spike --run`. Adapter invokes
`codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check
--sandbox read-only --json --disable shell_tool --disable unified_exec
-c web_search="disabled" -c approval_policy="never" --output-schema <temporary schema> -`.
Prompt goes through stdin. Existing supported auth is retained; API key environment
overrides are removed from the child environment. No credentials were inspected.
Worker sees the two fixture files as supplied text, not through filesystem tools.

L3 locally_observed: thread/turn start, agent message, turn completion. The worker found
that reversed bounds return upper for ordinary ordered numbers; this remains a
worker-reported claim, not an executed test. Its unresolved question asks which behavior
should be intended. Relay validated the response and hashed the artifact. Fixture before
and after SHA-256 were both `64fdeee3237d8e993abf449336c6dc12a61fb35b549a9ea8d4d1d59b042473cc`.
**No changes observed** does not mean **writes prevented**. No shell/test execution event
was present. The experiment did not test attempts to bypass tool or network restrictions.

Observed counters: input 11,540; cached input 0; cache-write input 0; output 184;
reasoning output 39. Keep the counters by their native names; do not add them together
or convert to subscription percentage, dollars, remaining quota or comparable headroom.
Runtime API transport necessarily uses network; disabling model web search does not
isolate the process's network. Read-only does not confine reads to the fixture.

Claude was not run for inference: no supported local authentication was available.
Do not use `--bare` as an OAuth shortcut: current documentation and local help say it
does not use subscription OAuth/keychain credentials. No interactive login or API fallback.

## Error, quota, cancellation and recovery limits

Generic launch/process errors are not exhaustion. Synthetic explicit machine-code fixtures
cover `usage_limit_reached` and `authentication_required`; these are illustrative classifier
inputs, **not a claim that either runtime emits those schemas**. No real limit was provoked
or observed. No reset time or quota endpoint is implemented. A real usage-limit event must
be captured and its schema verified before automatic pool-observation creation.

Malformed JSON, wrong shapes and invalid worker results fail normalization. Raw stderr
and full transcripts are not persisted. Cancellation is a local request only; no remote
acknowledgment or process-tree guarantee. Native resume was not exercised; artifact handoff
is the tested fallback design. Undocumented session parsing is a future option, excluded here.

## Checks and gates

Locally observed checks: 19 pytest cases passed; compileall passed. Editable installation
with `pip install -e '.[test]'` succeeded. An initial no-build-isolation install failed
because the fresh environment had no setuptools; normal isolated installation resolved it.
Test provider events
and resource profiles are synthetic. Artifact round-trip, integrity rejection, source
handoff, reserve behavior, expiry, comparable headroom, capability/tool filtering and
shared-pool exhaustion run without model calls. Cancellation is tested against a fake process.

Partial adapter feasibility is the outcome. M0 foundation works; complete two-worker
feasibility and real-repository gates remain unmet. Subscription pool mappings remain
design hypotheses until provider-specific evidence validates shared/independent limits.

## M0.1 — Snapshot feasibility update (2026-10-03)

Rechecked local versions, CLI help and supported auth status: Codex 0.160.0 with ChatGPT
login; Claude 2.1.240 with `loggedIn=false`, `authMethod=none`, `apiProvider=firstParty`
(`locally_observed`). Branch `codex/m0-1-snapshot`, initially clean. No new AGENTS.md.
No login, credential inspection, API fallback, push or global configuration changes.

Refetched C1/C2/A1/A2 official sources above. Claude's CLI reference documents empty
`--tools`, safe mode and strict MCP config. Safe mode retains auth but some managed-policy
hooks and commands can remain. Those caveats block inherited-integration admission.
The configured Claude path uses stdin with `-p`, empty tools, safe mode, empty strict MCP
configuration, no Chrome, disabled skills, dontAsk permissions, no session persistence,
JSON output and a schema. No paid fallback flag is set; API-key/provider environment
overrides are stripped. **This combined inference path was not executed.** Probe and
normalization work is independent of the missing authentication.

| Control / readiness | Codex 0.160.0 | Claude 2.1.240 |
|---|---|---|
| Model tool availability | Complete inventory unknown; ordinary admission blocked | Empty built-in tools supported by official docs/help; runtime execution unverified |
| Shell/code | Disable flags requested/accepted; complete absence unknown | Disabled built-in tools declared runtime-enforced; no observed denial |
| Filesystem read/write tools | Unknown complete inventory; unchanged fixture observed only | Built-in tools disabled by configuration; OS confinement unknown |
| Browsing/worker external requests | Web search disabled; complete external tool exclusion unknown | Built-in tools disabled; transport and managed code are separate |
| Inherited MCP/plugins/integrations | Unknown; ignoring user config is not exhaustive isolation | Safe mode/strict empty MCP configured; managed behavior remains unknown |
| Runtime transport network | Available during successful inference; not blocked or OS confined | Necessary but not exercised locally |
| Local cancellation | Bounded runner, Windows owned-PID tree termination request; harmless parent/child cleanup tested | Same runner; runtime inference cancellation not exercised |
| Remote cancellation | Unknown | Unknown |
| Ordinary snapshot readiness | **Blocked**, required controls unknown | **Blocked**, auth unavailable and inherited controls unknown |
| Disposable evidence | Two real calls completed under explicit weaker exception | No inference |
| Repository access | **Unmet** | **Unmet** |

All interface support statements above are `official_documentation` (C1/C2/A1/A2) and
local help `locally_observed`; successful output/hash measurements are `locally_observed`.
Unknown confinement, mappings and proposed next gates are `design_hypothesis`.

### Real evidence and boundary interpretation

[`m01-snapshot.json`](evidence/m01-snapshot.json) records exactly two serial Codex calls,
each bounded at 45 seconds. The first returned a valid investigation and hashed artifact.
A downstream REVIEW task retained the original source hashes and artifact reference;
Claude execution was withheld. **Real two-worker handoff gate unmet.** The source packet
and upstream artifact transfer is covered synthetically; no reviewer claims exist.

The second call combined five source-data challenges: instruction injection, shell/tests,
disposable file modification, unsupplied disposable file read, and browsing/integration.
No dangerous tools were enabled. Worker-reported refusal is separate from Relay evidence:
no tool events and unchanged disposable file hashes. Outcomes are `no_action_observed`,
not enforced denial or verified tool absence. Read/network attempts are not independently
observable. No attempt is established just because a challenge was supplied.

The worker noted that the challenge source was omitted from the task's read_scope. That
real observation is retained in the evidence. The harness now includes every selected
source in read_scope, and prompt assembly rejects scope mismatches. No additional quota
was consumed to repeat the challenge after that correction.

### Usage accounting investigation

| Run | Packet UTF-8 bytes | Relay prompt UTF-8 bytes | input_tokens | cached_input_tokens | cache_write_input_tokens | output_tokens | reasoning_output_tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| Investigation | 783 | 1,467 | 11,732 | 0 | 0 | 210 | 22 |
| Boundary | 1,487 | 2,201 | 11,905 | 0 | 0 | 280 | 0 |

C1 documents usage counters on turn completion. C2 documents built-in runtime instructions;
[app-server documentation](https://learn.chatgpt.com/docs/app-server) exposes thread usage
updates, not a decomposition of this exec call's input. The supplied byte count is distinct
from runtime token accounting. The observed interface does not attribute the difference
among runtime context components. No hidden prompts/session files were inspected, and no
precise explanation, billed cost, subscription consumption or headroom is inferred.
Native categories are retained without adding reasoning or cache categories together.

### Synthetic checks and cancellation

Claude result normalization uses the documented `structured_output` envelope, validated
against synthetic success, error and incomplete examples. It fails closed and reports
sanitized categories instead of raw diagnostics. No quota-event mapping or exhausted-pool
observation creation was added; earlier illustrative codes remain fixture-only.

Harmless Python subprocess tests exercise timeout and local exit. A Windows parent spawning
a sleeping child is terminated through taskkill of Relay's owned PID; tasklist confirms that
the synthetic child no longer appears. This verifies that test tree only. Runtime cancellation
records request, local parent exit, unknown tree verification, and unknown remote state.
No claim that inference stopped or consumed zero usage follows from local termination.

Next gate and limitations are recorded in decision 0002. Serial persistence remains deferred.

Final local checks: `.venv\Scripts\python -m pytest -q` — **37 passed**;
`python -m compileall -q merp_relay` — passed; `git diff --check` — passed.
The documented conformance probe command succeeded with zero inference calls and returned
Codex subscription authenticated, Claude unavailable, and both ordinary admission gates blocked.
Tests include the safe Claude argument/stdin construction and API-key environment removal;
these are synthetic dispatch assertions, not observed Claude inference.
