# openDashboard 2.0 upgrade verification

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
