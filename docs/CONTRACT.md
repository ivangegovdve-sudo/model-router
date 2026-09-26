# modelrouter — client contract (v1)

One engine, three faces: the OpenAI-compatible proxy (which actually routes), the
Windows/Android app (which shows why), and the MCP server (which advises). The app and
the MCP server are thin clients of the endpoints below; neither reimplements a decision,
and neither ever holds a provider key.

Base URL: wherever the operator runs `modelrouter serve` (default `http://127.0.0.1:7480`).
Auth: `Authorization: Bearer <router token>` on every route except `/health`. The token's
value lives in the operator's secret store; with `no_auth = true` (loopback only) no header
is needed.

## Routing

| Method | Path | Returns |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI chat completion. `model: "auto"` routes; `model: "provider:model"` is judged alone and refused if it cannot serve the request. `stream: true` supported. |
| GET | `/v1/models` | `{object:"list", data:[{id:"auto"}, {id:"provider:model"}, ...]}` |

Optional request header `X-Router-Max-Usd-Per-M: <float>` lowers the price ceiling for one call.

**Lanes.** `model: "auto"` is the **interactive** lane: someone is waiting, so any seat whose
measured latency exceeds `policy.interactive_max_latency_s` is excluded. `auto:background` and
`auto:batch` (or header `X-Router-Lane`) ignore latency and buy cheaper scheduling where a provider
sells it — Sail's `balanced` / `flex` completion windows, sent as `metadata.completion_window` and
priced from that window's rate card. A named `provider:model` skips the latency gate (the caller
chose). An unknown lane is HTTP 400.

**max_tokens is a correctness parameter.** A reasoning model given less than its measured floor
bills tokens and returns nothing, so the router never passes such a budget through. It prefers a
model that answers within the caller's budget; if none does, it raises the budget for the cheapest
reasoning model to `caller's budget + 2 × the most reasoning observed for it` (capped by
`max_clamp_extra`), records `facts.max_tokens_raised_from`, and says so in `because`. If that still
comes back empty (a real task can reason far longer than the probe), the observation raises the
floor and the router decides once more, which may raise it again for the same model.

A routed response is the upstream body unchanged plus one field:

```json
"router": {"decision_id": "20260926T031754-29e8ba", "seat": "nous:mistralai/mistral-nemo",
           "because": "lowest expected cost of 9 qualifying: $1.08e-05 expected ($0.02608/M measured); next venice:e2ee-qwen-2-5-7b-p at $2.14e-05 expected ($0.05165/M measured)",
           "cost_usd": 2.69e-07, "cost_basis": "billed", "attempts": 1}
```
and headers `X-Router-Decision`, `X-Router-Seat` (streams carry only the headers).

**Refusals are errors, never quiet successes** (OpenAI error shape, so clients show the message):

| HTTP | `error.type` | meaning |
|---|---|---|
| 422 | `router_abstained` | no model qualifies on known facts. `message` names the reasons; `decision_id`, `unknown[]` attached |
| 502 | `routed_call_failed` | every attempt failed or came back **empty** (HTTP 200 with no content is a failure) |
| 401 | — | missing/wrong router token |

## Legibility

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | `{status:"ok"|"not_ready", service, version, providers_with_keys[], problems[]}` — no auth |
| GET | `/router/setup` | `Setup` |
| GET | `/router/roster?provider=&measured_only=` | `{providers:[ProviderState], dashboard, seats:[Candidate]}` |
| POST | `/router/explain` | `{choice: Choice, providers:[ProviderState], dashboard}` — body is a chat request; **no model call, no cost** |
| POST | `/router/probe` | body `{seats?:[string], cheapest?:int, budget_usd?:float}` → `{spent_usd, budget_usd, probed:[{seat, rungs:[Attempt], profile}]}` — **spends money**, capped by config |
| GET | `/router/decisions?limit=N` | `[DecisionRow]` newest first |
| GET | `/router/decisions/{id}` | `Decision` |
| GET | `/router/observations/{provider:model}` | raw observations behind a seat's measured profile |

### DecisionRow
`{id, t (unix s), requested, outcome:"ROUTE|ABSTAIN", seat|null, because, cost_usd|null, cost_basis:"billed|computed|unknown|…"|null, status:"ANSWERED|ABSTAINED|FAILED|STREAMING"}`

### Decision
```
{id, t, requested, client,
 ask: {prompt_tokens, max_tokens|null, needs_tools, ceiling_usd_per_mtok|null, stream},
 choice: Choice,                 # the decision that stands
 earlier_decisions: [Choice],    # a failed attempt makes the router decide AGAIN; those are here
 attempts: [Attempt],
 providers: [ProviderState], dashboard: {provider: "ok (N rows)|not carried|unreachable (…)"},
 status, elapsed_s,
 result: {cost_usd|null, cost_basis|null, calls, calls_unknown_cost}}
```

### Choice
```
{outcome: "ROUTE|ABSTAIN", because, seat|null, max_tokens|null, expected_usd|null,
 unknown: [fact], facts: {prompt_tokens, max_tokens, needs_tools, ceiling_usd_per_mtok,
 only_seat, roster_size, unknown_not_listed, free_tier_not_listed},
 considered: [Assessment]}       # QUALIFIES (ranked, winner first), then EXCLUDED, then UNKNOWN
```
`because` is display text; clients must not parse it.

`considered` lists every qualifier and every exclusion. Of the UNKNOWN seats (usually most of
the roster: never measured) it lists only those whose list price undercuts the winner — they
might have been cheaper — and counts the rest in `facts.unknown_not_listed`.

### Assessment
`{seat, verdict:"QUALIFIES|EXCLUDED|UNKNOWN", because, unknown:[fact], usd_per_mtok|null, price_basis:"measured|list|", expected_usd|null}`

### Attempt
`{seat, window|null, max_tokens|null, http, ok, detail, finish_reason, content_chars, reasoning_chars, tool_calls, prompt_tokens, completion_tokens, cached_tokens, cost_usd|null, cost_basis, latency_s, scope:"model|provider|quota|edge|transport"|null}`

`reasoning_chars > 0` with `content_chars == 0` is the measured failure this product exists for:
the model spent the budget reasoning and returned nothing.

### Candidate (roster seat)
`{seat, provider, model, provider_state:"OK|NO_KEY|BLOCKED|QUOTA_EXHAUSTED|UNAVAILABLE|CATALOGUE_FAILED", provider_detail, available, context_length|null, supports_tools|null, list_prompt|null, list_completion|null, price_source:"dashboard|provider-catalogue", measured_usd_per_mtok|null, emits:"content|reasoning_then_content|reasoning_only"|null, min_max_tokens|null, floor_evidence, reasoning_overhead_tokens|null, probe_age_s|null, latency_s|null}`

Prices are USD per million tokens.

`min_max_tokens` is a **per-model hard gate from measurement**: one token above the most
reasoning this model was observed to spend before its answer on the probe task, and above every
budget that came back empty. `floor_evidence` states the measurement. A request whose
`max_tokens` is below it is EXCLUDED (that call would bill tokens and return nothing). It is a
lower bound — a long task can reason for far longer — so an empty answer above the floor is
recorded, raises the floor, and triggers a re-decision.

Provider states: `BLOCKED` — the account refused (auth, money); `QUOTA_EXHAUSTED` — a rate window;
`UNAVAILABLE` — unreachable, or a CDN refused *how* we called (e.g. Cloudflare `403 error code: 1010`).
UNAVAILABLE never implicates the key; do not rotate credentials over it.

### ProviderState
`{provider, key, state, detail, until|null, models, price_source, ratelimit:{header:value}, free_quota_until|null}`

### Setup
`{config, config_problems[], secrets_source:"gcp|env", gcp_project, gcloud_installed, keys:[{provider, source, name, present, detail}], auth, dashboard, dashboard_status, ready}`

`keys[].name` is the secret's NAME. No endpoint ever returns a key value or any part of one.

## Rendering rules (hard)

- `null` price is **UNKNOWN** — never shown as `$0`, never sorted as cheapest.
- `cost_basis` is always shown next to a cost: `billed` (the provider said so), `computed`
  (usage × the model's own rate card), `unknown`.
- `ABSTAIN` and `FAILED` are shown as refusals with their `because`, never as a gap.
- An attempt with `content_chars == 0` is shown as **empty**, even when `http` is 200.

Real fixtures: `docs/fixtures/*.json` (captured from a live router on 2026-09-26).
