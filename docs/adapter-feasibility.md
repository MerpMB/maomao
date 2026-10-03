# M0 adapter feasibility

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
