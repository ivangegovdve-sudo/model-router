# Router, seat-plan and desktop verification

## Seat plans and approved Windows UI

Verified on 2026-10-12 using local synthetic providers and PR fixtures.

| Check | Result |
|---|---|
| Full engine, MCP SDK 1 and 2, published dashboard bridge | 466 passed on each; no skips |
| New plan tools across SDK majors | Nine-tool exchanges passed both directions over stdio, SSE and Streamable HTTP |
| Council offline suite | 70 passed on each SDK; 3 explicitly live tests skipped |
| Glass final plan/gateway tests | 43 passed |
| Glass full Linux suite | 641 passed, 29 failed, 1 skipped; all 29 failures reproduced on unchanged `20830b4` |
| Glass Windows CI | Engine test check passed on `f203fe8` |
| Actual consumer HTTP integration | Four-member Council completed all 10 stage calls; two Glass gateways executed distinct reviewer weights and excluded the author |
| Kotlin desktop tests | 33 passed |
| Native desktop and Android | Desktop executable JAR and debug APK built |
| Native seat editor | Real HTTP response resolved B/C/A/B; overlapping pair/triple groups preserved; All seats sent the additional full group |
| Independent review | Identity, costs, complete-search proofs, receipts, stale heads, retries, upstream controls and draft preservation reviewed; material findings fixed |
| Windows installer | Version 1.1.0 MSI passed current-user installation, visible responsive GUI with bundled JVM and no system Java, and clean exit |

Router CI passed all four Ubuntu/Windows × SDK 1/2 jobs. The Android release/debug
packaging and [Windows installer smoke workflow](https://github.com/ivangegovdve-sudo/model-router/actions/runs/38259474822)
passed on `705498bd1d42547254d7ed6991fd1c1aee06815e`. Windows Installer registered
version 1.1.0 in the current user's context. With system Java absent, the visible
GUI loaded `runtime/bin/server/jvm.dll`, remained responsive for ten seconds,
and both its GUI and launcher exited with code 0 after normal window close.

The verified MSI is available in that run's `modelrouter-msi` artifact. Its SHA-256 is
`243cc534e77cf5c4aa5c049b5205f4b72d3c2d9d9343a4509fe2488e5fec3780`.

Run the optional cross-repository integration with local consumer checkouts:

```sh
python tools/verify_consumer_seat_plans.py \
  --council /path/to/council --glass /path/to/glass-pr-solver
```

It starts a real local HTTP router, uses both real consumer adapters, verifies
execution receipts and does not contact a provider, Secret Manager or GitHub.
The default resolver consumes one frozen priced view for each requested lane.
Plans require explicit exact-weight identities. Glass additionally requires an
operator-authorized origin record for the current PR head. App-fixed bindings can
participate in advisory constraints; the current Council and Glass adapters refuse
their execution until an actual consumer executor is available. No deployment or
live PR actions were performed.

## Earlier openDashboard 2.0 compatibility checks

Verified on 2026-10-12 in Linux with Python 3.12 and JDK 17.

| Check | Result |
|---|---|
| Full engine suite, MCP Python SDK 2.3.0 | 380 passed; no failures or skips |
| Full engine suite, MCP Python SDK 1.30.0 | 380 passed; no failures or skips |
| Minimum SDK 1.26 / 2.2 transport tests | Passed |
| SDK 1 client to SDK 2 server, and reverse | Stdio, SSE and Streamable HTTP passed |
| Published `open-dashboard-mcp@2.0.0` | Actual MCP initialize/list/call round trip; seat success and strict free-only refusal |
| Kotlin desktop tests | 19 passed; no failures or skips |
| Kotlin desktop executable JAR | Built successfully |
| Android debug APK | Built successfully |
| Native desktop UI | Inspected at 1200 px and 480 px; clicking a tools filter changed the engine's choice |
| Independent code review | Pricing, requirements, telemetry, MCP and seat adapter reviewed; identified regressions fixed and verified |
| Git whitespace checks | Passed |

Engine runs included `OPENDASHBOARD_MCP_ENTRY` pointing to the installed published
2.0.0 package, so the published-wrapper integration was exercised rather than skipped.
The output includes existing FastAPI lifecycle deprecation warnings. Tests use local
measured fixtures and fake upstream responses, with no paid provider inference.

Native checks used:

```sh
./gradlew --offline --no-daemon :shared:desktopTest \
  :desktopApp:packageUberJarForCurrentOS :androidApp:assembleDebug
```

Screenshots contain clearly named synthetic fixture seats. Temporary fixture servers
and displays were stopped after inspection. Android runtime and Windows installer
execution were not tested locally; the existing app workflow builds Android and MSI
artifacts. Engine CI now runs both SDK majors on Ubuntu and Windows, including the
published openDashboard integration test.

Confidence remains an evidence-completeness heuristic with null probability. The
editing filter cannot qualify a model without explicit editing evidence. The seat
adapter implements measured Sail/OpenRouter text API seats and does not implement
Codex, Antigravity or local executor integrations.
