# 0002 — Explicit snapshot execution

Date: 2026-10-03. Status: foundation implemented; ordinary runtime admission blocked.
Corrected after independent review of ba36c2764034ee9f5b3291a54f858c82804fe453.

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
local process-tree cancellation and remote cancellation. Documented interface support describes
configuration intent, not qualified enforcement. The earlier Claude `supported_enforced`
declarations were premature and are now `unknown`. Controls carry runtime version, source and
validation basis; no combined-path validation exists. Missing auth evidence or an unvalidated
version cannot qualify any controls. Voluntary refusal and absence of tool events remain insufficient.

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

Correction choices: a shared task/profile eligibility function covers mode, capabilities,
tools and mode-specific/explicit controls; routing alone adds quota state and reserves.
Weaker disposable dispatch can waive unverified controls but never task incompatibilities.
Codex final normalization requires one completed agent message followed by one turn completion;
multiple completed messages are rejected as ambiguous without relying on undocumented final flags.
Intermediate started/updated message contents are not parsed as results.

The disconnected public cancellation API remains callable for compatibility but explicitly
returns unsupported and sends no request. Timeout cleanup belongs to the synchronous process
runner. Its parent exit, unknown tree state and unknown remote state remain separate. Failed
termination is a failed execution, not a successful cancellation. Temporary file streams replace
pipes to avoid cleanup waits on descendant-held reader pipes. No background execution framework.

Claude probes validate both process exits, the pinned version and the required typed JSON fields.
Only a successful first-party claude.ai subscription result is available. Malformed, unsupported
or failed probes return sanitized unavailable results. Corrections were tested without inference;
historical smoke evidence remains historical and neither handoff nor boundary gates advanced.
