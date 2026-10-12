# openDashboard 2.0 implementation plan

**Goal:** Upgrade the existing router, MCP wrapper and Kotlin app while preserving
one source of truth for routing decisions.

**Architecture:** A Python dashboard adapter normalizes evidence into the current
candidate contract. The engine validates optional requirements and reports evidence
confidence; MCP and Kotlin remain thin clients.

**Tech stack:** Python/FastAPI/httpx, MCP Python SDK 1 and 2, Kotlin Compose/Ktor.

**Spec:** [Design](../specs/2026-10-12-opendashboard-2-design.md).

## Task 1: Dashboard and provider evidence

- [x] Add failing adapter tests for canonical token price sets, deployed legacy
  prices, conditions, malformed prices, missing evidence and opaque pagination.
- [x] Add `engine/modelrouter/dashboard.py` and extend `gather.py`/`providers.py`.
- [x] Preserve provider catalogue price precedence and propagate capabilities.
- [x] Run adapter tests and engine regression suite.

## Task 2: Requirements and telemetry

- [x] Add tests in `engine/tests/test_requirements.py` covering malformed filters,
  unknown capabilities, HTTP/Responses forwarding and request metadata privacy.
- [x] Add `router_requirements` validation and candidate assessment in the decision
  and HTTP request paths. Preserve requirements across retries and streaming.
- [x] Add `Choice.confidence` and request features with honest estimate labels.
- [x] Run new tests and full engine suite.

## Task 3: MCP compatibility

- [x] Test stdio, SSE and Streamable HTTP initialize/list/call cycles with SDK 2.
- [x] Extend `mcp_server.py` with SDK-major compatibility and transport CLI options.
- [x] Forward optional requirements and preserve confidence/facts in results.
- [x] Run the same integration tests in isolated SDK 1 and SDK 2 environments.
- [x] Document client configuration and add CI coverage for both SDK majors.

## Task 4: Kotlin UI

- [x] Extend typed models and fixture tests for new capabilities and telemetry.
- [x] Add actual roster model selection and optional requirements controls to the
  existing explanation screen. Render uncertainty and context in decision details.
- [x] Build/test desktop; compile Android if SDK setup is available.
- [x] Capture and inspect the running UI where a display is available.

## Task 5: Integration and review

- [x] Document publication evidence, API additions, filter meanings and startup.
- [x] Review the combined changes for restrictive-policy bypass, unsupported
  capability assertions, ambiguous prices, prompt retention and client mismatches.
- [x] Run final engine, MCP and Kotlin checks and record remaining limitations.
