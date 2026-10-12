# openDashboard2 compatibility

## Repository and published package

The publisher is [`ivangegovdve-sudo/openrouter-dashboard-mcp`](https://github.com/ivangegovdve-sudo/openrouter-dashboard-mcp).
Its package is [`open-dashboard-mcp`](https://www.npmjs.com/package/open-dashboard-mcp),
and npm published version `2.0.0` on **2026-10-12 at 04:30:25 UTC**. The registry
repository field matches this GitHub repository; its `v2.0.0` tag and checkout
point to `9a63a0ff2af4f8b679ad608466abca2e93922534`. The registry tarball's SHA-1
is `f95856943f9da545eebf295d94cd704671d516ba`, verified against the downloaded artifact.
Registry metadata contains neither `gitHead` nor a provenance attestation, so
this identifies the publisher without claiming cryptographic commit provenance.

The 2.0.0 release aligns branding with openDashboard 2.0; shipped tool and response
implementations are unchanged from 1.6.3 after newline normalization. Publication
is defined in that repository's `.github/workflows/publish.yml`.

## Public catalogue evidence

The router reads `https://openrouter-github-dashboard.vercel.app/api/public/v2/`
when configured by `[policy] dashboard_url`. The new openDashboard website host
does not expose these REST endpoints. No Node runtime is required by the Python
router; the npm integration test checks the published tool during development.

`live-models` collection cursors remain opaque and filters remain attached to every
page. An incomplete or malformed collection is discarded. Both canonical
`pricePoints` and deployed legacy `pricing.promptUsdPerToken` / `completionUsdPerToken`
are supported. Token amounts convert to per-million rates with Decimal arithmetic.
Ambiguous, conditional, non-token or unsourced prices remain unknown unless a
scheduling condition matches the provider window actually sent upstream. Legacy
prices require a valid confirmed quote time and preserve `pricingWindow` conditions.

Provider catalogue membership remains authoritative for availability, and known
provider price legs remain intact when the dashboard fills a missing leg. Source
and age are recorded. Dashboard `reasoningEfforts: []` establishes reasoning
support without named effort levels; null means unknown. The OpenRouter-only
`models` collection fills missing parameters and modalities by exact model ID.
Metadata is never joined to another provider by a similar name. The deployed
capability ledger is unavailable, so unknown facts remain unknown. No retrieved
public dashboard surface establishes code/content editing.

The published package root launches MCP stdio. Library consumers use the explicit
`open-dashboard-mcp/public-catalogue` export.

To test the actual published MCP bridge locally:

```sh
npm install --ignore-scripts --prefix /tmp/router-dashboard-test open-dashboard-mcp@2.0.0
OPENDASHBOARD_MCP_ENTRY=/tmp/router-dashboard-test/node_modules/open-dashboard-mcp/build/index.js \
  python -m pytest engine/tests/test_opendashboard_mcp.py -q
```

The fixture uses a local measured candidate, no provider keys and no inference.

## Seat resolution contract

openDashboard2's published `dashboard_resolve_seat` tool delegates selection to
the router's operator-authenticated `POST /v1/seats/resolve` endpoint. The tool
and router use the same pool order:

```text
Sail → Codex → Antigravity → local → OpenRouter
```

The router currently implements text API seats for Sail (tier 0) and OpenRouter
(tier 4). It does not fabricate Codex, Antigravity or local executors. Claude,
Anthropic and Cerebras providers, and explicitly known families with those
names, are excluded. The router chooses the cheapest qualifying seat in the
first eligible tier. A cheaper later tier does not replace a qualifying earlier
tier.

Selection reuses the router's measured content, capability, context, quota and
price rules. The background lane applies because the request supplies no
interactive latency requirement. A role records intent; it does not establish
the model's review, fixing or rebasing competence. The task contributes a token
estimate without being retained in the decision record.

The request uses the tool's snake-case HTTP body:

```json
{
  "consumer": "glass-solver",
  "role": "review",
  "policy": "cheapest",
  "exclude_families": ["openai"],
  "exclude_seats": [],
  "need": {"tools": true, "min_context": 8192},
  "max_usd_per_m": 2,
  "task": "Review lock ordering"
}
```

Only `consumer` is required. `role` defaults to `general` and supports `review`,
`fix`, `rebase`, `council` and `general`. Exclusion arrays default to empty and
accept at most 16 families and 32 seats. `need.tools` defaults to false;
`need.min_context` must be a positive integer. `max_usd_per_m` must be a finite
positive number. `task` accepts up to 4,000 characters. Unknown fields and
invalid values return HTTP 400 before roster lookup or decision recording.

Family exclusions use an explicit catalogue `family` fact. Names, model IDs and
provider namespaces do not establish base-weight families. When any family
exclusion is requested, an unknown family is a conflict and the seat is
excluded.

Consumer policy belongs to server configuration. The defaults are:

| Consumer | Policy |
| --- | --- |
| `glass-solver` | `cheapest` |
| `private-council` | `cheapest` |
| `public-council` | `free-only` |

Configure additional consumers or operator overrides in TOML:

```toml
[seat_consumers]
my-reviewer = "cheapest"
my-public-council = "free-only"
```

Unknown consumers are rejected even when they supply a policy. A client may
tighten `cheapest` to `free-only`; a client requesting `cheapest` for a
`free-only` consumer still receives the configured `free-only` policy.

`free-only` also requires the operator's `[policy] allow_free = true`. Both
input and output rates must be known and zero; solved billed rates take
precedence over advertised rates. A `:free` suffix, an unknown rate or a blended
zero price is insufficient. The normal engine must establish zero effective
cost as well. Positive measured billing prevents a free-only selection unless
complete solved zero billed rates supersede that history. There is no fallback
to a paid seat.

HTTP 200 returns the contract consumed by the published tool:

```json
{
  "policy": "cheapest",
  "seat": "sail:vendor/model",
  "provider": "sail",
  "family": "explicit-family",
  "tier": 0,
  "cost_basis": "rate card",
  "usd_per_mtok": "0.12",
  "invoke": {
    "api": "chat.completions",
    "model": "sail:vendor/model",
    "base_url": "/v1",
    "headers": {
      "X-Router-Lane": "background",
      "X-Router-Max-Usd-Per-M": "2"
    },
    "router_requirements": {
      "providers": [],
      "model_types": [],
      "required_capabilities": [],
      "thinking_level": null,
      "editable": null,
      "tool_calls": true,
      "min_context_tokens": 8192,
      "max_usd_per_mtok": null
    }
  },
  "because": "first eligible tier 0 (sail); lowest expected cost ...",
  "jev": {
    "scope": "measured-text-api-seats",
    "confidence": {
      "level": "medium",
      "basis": "heuristic evidence completeness; not a success probability",
      "probability": null
    }
  },
  "decision_id": "recorded-advisory-id",
  "considered": []
}
```

Every considered row contains `seat`, `verdict`, `because`, integer `tier`,
nullable `family` and nullable decimal-string `usd_per_mtok`. Unsupported
providers outside the pool use tier `-1`. Policy and tier exclusions remain
visible. Confidence describes evidence completeness rather than a calibrated
probability or predicted task quality.

When no seat qualifies, HTTP 422 contains:

```json
{
  "detail": {
    "type": "seat_unavailable",
    "policy": "free-only",
    "because": "no candidate qualifies ...",
    "decision_id": "recorded-advisory-id",
    "considered": []
  }
}
```

The tool converts HTTP 200 to `status: "seat"` and this HTTP 422 to
`status: "unavailable"`. The router need not send a `status` field. If the
router cannot be reached, the tool reports `router_unreachable` and does not
substitute another resolver.

Both successful and unavailable advisory decisions are recorded with
`status: "ADVISED"`, `requested: "seat:<consumer>:<role>"`, the engine's
facts/confidence, no attempts, zero calls and zero spend. Task text and prompts
are not persisted. Resolution never probes or invokes the advised model. The
returned `invoke` object describes a separate, authorized proxy call; provider
credentials remain in the router. Copy its `headers` to that call and its
`router_requirements` into the request body. They preserve the background pricing
window, required capabilities, minimum context and effective price ceiling used
by the advice. The price header is explicitly zero for `free-only`.
