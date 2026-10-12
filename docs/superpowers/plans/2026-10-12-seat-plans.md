# Multi-seat integration implementation plan

**Goal:** Deliver the approved Windows UI and shared, enforceable Council/Glass seat plans.
**Architecture:** The router resolves and pins complete model-weight constraint graphs;
consumer adapters execute immutable assignments; Kotlin renders the shared contract.
**Spec:** [Approved design](../specs/2026-10-12-seat-plans-design.md).
**Stack:** Python/FastAPI/httpx/SQLite/MCP; Kotlin Compose/Ktor; GitHub Actions Windows MSI.

## Global constraints

Up to 32 slots; `seat-plan/1`; preserve `/v1/seats/resolve`; unknown remains unknown;
no paid/live provider calls or external PR actions during development; no credential
values in output; no prompt bodies in router telemetry; no weakened consumer policy.

## Review focus

Alias/checkpoint collisions, unknown/stale PR author provenance, overlapping groups
and greedy dead ends, execution substitution/constraint weakening, and partial/stale
review completion must have behavior-level regression coverage.

## Task 1 — Router identity and complete assignment

- [x] Add failing tests for weight aliases, arbitrary groups/references, fixed seats,
  unknown identity, policy floors, exact counts, greedy dead ends and search limits.
- [x] Extend candidate/catalogue/config identity fields and implement `SeatPlanManager`
  in focused new modules with durable plans, templates and pinned preparation.
- [x] Wire authenticated HTTP/MCP plan endpoints and execution lifecycle; add tests
  for head/version changes, attempts to weaken requirements and re-selection pins.
- [x] Run the full engine suite with both MCP SDK majors and published bridge.

## Task 2 — Council adapter

- [x] Add failing tests for per-run plans, real pinned calls, slot-ID results, concurrent
  isolation, counts above ten and legacy behavior/MCP schemas.
- [x] Add opt-in adapter and immutable execution roster through every council stage.
- [x] Run offline regression suite; document setup and identity requirements.

## Task 3 — Glass adapter and reviewer cohort

- [x] Add failing tests for two required reviewers, per-slot retries, exact head,
  unknown provenance, gateway pinning and write/merge gates.
- [x] Implement opt-in plan adapter and head-bound origin records; preserve legacy
  selector, action safety and trusted upstream credentials.
- [x] Run the full Glass regression suite and document the consumer contract.

## Task 4 — Kotlin and Windows

- [x] Add typed plan/template models and tests for request editing, preservation and
  response evidence; implement both approved visual screens and goal templates.
- [x] Verify native controls and actual backend-selected evidence with local fixtures.
- [x] Build/test desktop and Android, fix CI setup, bump MSI version and verify Windows
  package generation and installation/launch where tooling permits.

## Task 5 — Integration and handoff

- [x] Run cross-repo HTTP integration and independent review; fix material findings.
- [x] Commit/push reviewable changes in all repos, create/update draft PRs, check CI.
- [x] Deliver Windows installer link and verification evidence with explicit limits.

Final evidence is recorded in [verification](../../VERIFICATION.md). Windows installer
execution and Glass Windows engine CI passed. All 29 Glass Linux failures reproduced
on the unchanged baseline; no isolation policy was weakened. All three draft PRs
are pushed, and the verified Windows MSI is included in the handoff.
