# 0002 — Explicit snapshot execution

Date: 2026-10-03. Status: foundation implemented; ordinary runtime admission blocked.

Snapshot analysis is deliberate packet-based operation. Relay selects source inputs,
hashes them, labels them as untrusted data, and supplies them with authorized objectives
and completion criteria. A review receives both a hashed upstream artifact and original
source. No transcript is forwarded. This is not a filesystem sandbox.

Required controls: no model tools, shell/code, filesystem tools, browsing/external requests,
or inherited integrations. An unknown, unsupported or observed-only required control blocks
normal admission. Runtime transport networking is necessary, and OS read/write confinement
is separately unknown; snapshot mode does not require repository-scoped reads.

Codex 0.160.0: two weaker disposable experiments completed. Shell/web toggles are documented
and accepted, but complete tool availability and inherited integration exclusion remain
unknown. Ordinary snapshot admission is blocked. A worker declining requests cannot qualify it.

Claude 2.1.240: supported subscription authentication is unavailable. CLI help and documentation
support empty built-in tools and safe mode with auth retained. Managed-policy hooks/commands
can remain, so inherited integrations are unknown. Ordinary admission and inference are blocked.
The adapter's invocation and result envelope are implemented and tested synthetically;
combined configuration and actual runtime output are not claimed verified by inference.

Controls are declared separately in the evidence report: model tools, shell/code, filesystem
tools, external access, inherited integrations, OS filesystem scope, transport network,
local process-tree cancellation and remote cancellation. Documented runtime enforcement
declarations describe configuration intent; they are not locally observed denial events.

Real two-worker handoff did not succeed: an actual Codex artifact and a review packet were
created, but no Claude reviewer ran. Author claims stay worker-reported, reviewer claims
are absent, and Relay facts are packet sizes/hashes, counters and observed event categories.

Serial persistent snapshot execution is a reasonable next design slice but is not yet
approved for ordinary use of either installed profile. Smallest acceptance gate: supported
Claude subscription auth established by the owner outside Relay; authoritative inventory or
denial evidence for complete tool/integration restrictions for both versions; then one serial
two-worker artifact review with original source, honest native telemetry and unchanged fixture.
After that, add minimal durable serial execution records and artifact continuation.

Repository access additionally requires verified scoped reads, write prevention, command
policy, worker external-access controls and inherited integration policy in an OS-contained
disposable test. Neither snapshot success nor parent-process termination proves those gates.
Remote reconciliation, scheduling, retries, quota mapping and a recovery engine remain deferred.
