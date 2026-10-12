# openDashboard 2.0 router upgrade

The user wants the existing model router updated for the published openDashboard 2.0,
usable through MCP clients, with a small Kotlin app that exposes routing context,
confidence, model selection, and optional price and capability requirements.

## Sources and architecture

`ivangegovdve-sudo/model-router` contains the Python engine and Kotlin Compose app.
`ivangegovdve-sudo/openrouter-dashboard-mcp` publishes `open-dashboard-mcp`;
npm version 2.0.0 was published on 2026-10-12. Its tools are unchanged from 1.6.3.
The router consumes the dashboard public REST contract directly; it must handle
canonical price sets and the deployed legacy scalar price envelope without needing
Node or running the dashboard MCP as a child process.

One engine decides. HTTP routes execute calls; MCP tools advise and expose the
same records; Kotlin renders the same contract. No provider keys enter the app or
MCP wrapper. Preserve the existing desktop and Android targets.

## Evidence and filters

Keep provider catalogue membership authoritative for availability. Provider prices
remain preferred; dashboard prices fill missing prices. Only unambiguous token
input/output prices apply to text calls. Conditional prices require a matching
scheduling window; ambiguous ranges or media prices remain unknown. Preserve exact
decimal arithmetic and source timestamps.

Carry supported parameters, input/output modalities, advertised reasoning and named
reasoning efforts into candidates. Never infer capabilities from marketing names.
Unknown editability stays unknown unless a source explicitly provides evidence.
The user confirmed that editability means code/content editing, the primary client
is a desktop JVM app, and MCP compatibility means legacy SSE plus Streamable HTTP.

`router_requirements` supports provider selections, text/reasoning/vision model
types, required capabilities, thinking level, editing and tool-call tri-state
requirements, minimum context, and a per-request price ceiling. Selections are
optional; filters that are disabled do not constrain the engine. Price filters can
lower operator policy, never raise it. Preserve filters through retries and streams
and strip router metadata from upstream calls.

## Telemetry and presentation

Record routing-relevant request features, estimates and requirements without
persisting prompt text. Confidence is an evidence-strength label with its basis,
sample count and missing facts. It is not a calibrated probability of correctness.
The Kotlin app lets the user navigate connection, decisions, roster and explanation;
select an actual model or automatic routing; enable filters; and inspect the server's
candidate assessments and uncertainty.

## MCP and verification

Support Python MCP SDK majors 1 and 2, stdio, legacy SSE and Streamable HTTP where
the SDK supports them. MCP wire revisions use dated versions, not major labels.
Network MCP binds to loopback by default. Add real initialize/list/call tests in both
SDK environments. Verify dashboard canonical/legacy payloads, malformed prices,
pagination and evidence propagation; filtered HTTP/Responses calls and retries;
Kotlin serialization and desktop build, with Android packaging where tools permit.

The published dashboard tool additionally requires `/v1/seats/resolve`. Provide
advisory selection over actual measured Sail/OpenRouter seats with the advertised
tier order and operator-configured consumer policy. Free-only never selects a paid
seat. Missing family evidence conflicts with a requested family exclusion. Invocation
metadata preserves the lane, ceiling and requirements used by the advice. No inference
or task text persistence occurs while resolving a seat.
