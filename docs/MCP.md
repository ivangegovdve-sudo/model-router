# Model Router MCP

The MCP server advises which seat would serve a request and exposes the router's
decision evidence. It makes no model calls and spends nothing. To execute a
request, send it to the proxy's `/v1/chat/completions` or `/v1/responses` endpoint
with `model: "auto"`. An MCP tool cannot change the model already selected by its
client.

## Python SDK compatibility

Both major versions of the official Python `mcp` package are supported. SDK 1
uses `FastMCP`; SDK 2 uses `MCPServer`. The router selects the available API at
import time and supports the same tools and transports with either SDK.

Install from the repository root:

```sh
pip install -e './engine[mcp]'
```

To constrain the SDK major for an existing client environment:

```sh
pip install -e './engine[mcp1]'  # mcp >= 1.26, < 2
pip install -e './engine[mcp2]'  # mcp >= 2.2, < 3
```

These numbers refer to Python package releases. MCP itself negotiates a
date-version protocol during `initialize`; it is not a protocol named “MCP 1”
or “MCP 2.” Protocol version support comes from the installed SDK. Legacy SSE
and Streamable HTTP are distinct transports, and both are available with either
supported SDK major.

## Connection setup

Start the router proxy separately. Configure its address and operator token for
the MCP process:

```sh
export MODELROUTER_URL=http://127.0.0.1:7480
export MODELROUTER_TOKEN=your-router-token
```

`MODELROUTER_URL` defaults to `http://127.0.0.1:7480`. The token is omitted when
unset; configure it when the proxy requires authentication. Provider keys stay
in the proxy.

### Standard input/output

`modelrouter-mcp` defaults to stdio. A typical MCP client configuration is:

```json
{
  "mcpServers": {
    "modelrouter": {
      "command": "modelrouter-mcp",
      "env": {
        "MODELROUTER_URL": "http://127.0.0.1:7480",
        "MODELROUTER_TOKEN": "your-router-token"
      }
    }
  }
}
```

Use the executable's absolute path when the client does not inherit its virtual
environment. MCP messages use stdout; server logging uses stderr.

### Legacy HTTP + SSE

```sh
modelrouter-mcp --transport sse --host 127.0.0.1 --port 7481
```

Point an SSE client at `http://127.0.0.1:7481/sse`. The SDK advertises the
`/messages/` endpoint for client messages through the SSE connection.

### Streamable HTTP

```sh
modelrouter-mcp --transport streamable-http --host 127.0.0.1 --port 7481
```

Point a Streamable HTTP client at `http://127.0.0.1:7481/mcp`. The SDK manages
session initialization, HTTP requests and event streaming.

The HTTP listener defaults to loopback and rejects non-loopback binds. It has no
public client authentication; `MODELROUTER_TOKEN` authenticates its requests to
the proxy. Host and Origin checks remain enabled to prevent DNS rebinding. Use
stdio or a local HTTP connection for this server.

## Tools

| Tool | Purpose |
| --- | --- |
| `explain_route` | Evaluate a prompt and requirements without calling a model. |
| `router_roster` | Read live seats, capabilities, prices and measurement evidence; optionally filter by provider or measured seats. |
| `recent_decisions` | Read recent call outcomes and costs. |
| `get_decision` | Read one call's decision evidence and attempts. |
| `router_setup` | Read provider readiness and configuration problems. |

`explain_route` accepts `prompt`, `max_tokens`, `needs_tools`, `model`,
`ceiling_usd_per_mtok`, `answer_tokens`, `lane` and an optional `requirements`
object. The latter is forwarded unchanged as `router_requirements` to the
proxy, which validates and applies it. For example:

```json
{
  "prompt": "Plan a code review with function tools",
  "max_tokens": 1024,
  "requirements": {
    "required_capabilities": ["tools"],
    "max_usd_per_mtok": 2,
    "thinking_level": "high",
    "providers": ["openrouter"]
  }
}
```

The result retains the router's facts, confidence metadata and exact estimated
price when provided. Candidate summaries include up to eight qualifying and
eight excluded seats plus unknown counts and facts. Required capabilities
without evidence can cause `ABSTAIN`; read `because` and the unknown facts.
`router_roster` returns the proxy's full capability evidence without inventing
support for unknown features. See [CONTRACT.md](CONTRACT.md) for request and
decision details.

## Compatibility verification

The engine CI runs the suite on Linux and Windows with both SDK majors. MCP
tests perform real `initialize`, `tools/list` and `tools/call` exchanges over
stdio, SSE and Streamable HTTP against a local HTTP router fixture. No provider
is contacted. To run locally:

```sh
pip install -e './engine[test,mcp1]' # or test,mcp2
python -m pytest -q engine/tests/test_mcp.py
```

With separate SDK environments, `MODELROUTER_MCP_TEST_SERVER_PYTHON` selects a
different server interpreter so the same tests can check cross-major clients
and servers.
