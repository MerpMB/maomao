# 0001 — V0.1 foundation

Date: 2026-10-03. Status: accepted for M0 foundation, conditional for runtime use.

M0 proved that validated contracts, deterministic routing, role reserves, shared-pool
state filtering, explicit unknown quota, and transcript-free artifact handoff can run
locally. Codex's supported subscription CLI produced a real structured result on a
disposable snapshot. These claims have different evidence: tests prove Relay logic;
the smoke proves only the observed runtime path. See the feasibility report.

Use Python/Pydantic, subprocess and filesystem support. Route by ordered rules rather
than scores or provider-role coupling. Controls qualify only when declared enforced;
observed-only/unknown controls do not meet required boundaries. Profiles and observations
are trusted configuration inputs, not automatically promoted from model claims.

Latest observation wins, with observation ID as deterministic timestamp tie-breaker.
After expiry state becomes unknown and can permit low-risk scheduling, but cannot release
a reserve or prove availability. Future observations do not apply. Headroom compares only
fresh known values with identical declared accounting basis for every eligible candidate.
Unknown capacity receives no fabricated allowance. Reserve policy protects scheduling
inside Relay; it cannot preserve quota used outside Relay.

Structural checkpoint facts are distinct from semantic continuation. Content-addressed
artifacts validate integrity; source references and findings still need reviewer judgment.
Native resume is optional and unverified. Keep artifacts as the fallback.

Unknown: Claude subscription execution, provider quota grouping and accounting, limit/reset
schemas, complete tool inventories, OS/network/read-scope enforcement, process-tree and
remote cancellation, and native resume behavior. No real-repository adapter is approved.

Deferred: persistent scheduler/recovery engine, concurrency, proactive quota polling,
session-file parsing, API backends, agent frameworks, web UI, databases, deployment and
automatic retries. The Claude adapter is intentionally blocked rather than speculative.

Two-worker V0.1 may proceed with policy development and disposable experiments, but **not
with real-repository worker execution**. This is partial feasibility, not a complete M0
integration pass or a completed V0.1.

Smallest next slice: after owner establishes Claude subscription authentication outside
Relay, implement one verified Claude disposable snapshot execution and a boundary
conformance harness for both installed runtime versions. Acceptance: supported auth paths,
structured results, enforced denial of shell/test/write tools, scoped reads, unapproved
network and inherited integrations, bounded local process-tree cancellation with remote
uncertainty recorded, unchanged disposable fixture, and sanitized evidence. Only then add
serial durable execution records and artifact-based continuation on real repositories.
