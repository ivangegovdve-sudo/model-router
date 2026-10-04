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

Optional request headers: `X-Router-Max-Usd-Per-M` (price ceiling for one call),
`X-Router-Answer-Tokens` (expected answer length — cost per answer is rate × tokens burned),
`X-Router-Session` (conversation id for prompt-cache warmth; the OpenAI `user` field also works),
`X-Router-Ensemble` (reserved: 501 until the experiment that decides it has run).

**Lanes.** `model: "auto"` is the **interactive** lane: someone is waiting, so a seat is excluded
when its **predicted** latency for this request — measured short-call latency + (completion +
reasoning tokens) ÷ decode speed measured on a real generation — exceeds
`policy.interactive_max_latency_s`. A seat with no decode measurement is UNKNOWN there. (A one-word
probe measures connection + prefill, not decode: AkashML answered one word in 0.6 s and decoded at
24 tok/s.) `auto:background` and
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
| 429 | `router_at_capacity` | every eligible model's provider is at its measured concurrency cap; `Retry-After: 1` |
| 502 | `routed_call_failed` | every attempt failed or came back **empty** (HTTP 200 with no content is a failure) |
| 401 | — | missing/wrong router token |

## Seat selection (consumers: Glass solver, councils)

`POST /v1/seats/resolve` (operator token) answers "which provider/seat fills ROLE under POLICY
and a cross-family constraint". It is advisory: it makes no inference call on the seat it
returns. The caller then invokes the seat (`invoke`) itself, or via `/v1/chat/completions` with
`model: "<seat>"` for router-proxied seats.

```json
{ "consumer": "glass-solver|private-council|public-council",
  "role": "review|fix|rebase|council|general",
  "policy": "free-only",                 // optional; may tighten the consumer's policy, never loosen
  "exclude_families": ["openai"],        // the author's base-weight family
  "exclude_seats": [], "need": {"tools": false, "min_context": 32000},
  "max_usd_per_m": 5, "task": "bounded summary for Jev (data, not instructions)" }
```

Policy is **server config per consumer** (`[consumers.<name>] policy`). Defaults: `glass-solver`
and `private-council` = `cheapest`; `public-council` = `free-only`. An unknown or mistyped consumer is
HTTP 400 even when the request names a policy (fail-closed); `exclude_families` / `exclude_seats` must
be lists of strings. The configured `policy.ceiling_usd_per_mtok` applies; a request's `max_usd_per_m`
can only lower it. `invoke` in the response is an allowlist (`kind`, `base_url`, `model`, `command`)
and never carries credentials.

* **cheapest** -- pool order, then cheapest live price within the tier:
  `sail` (0), `codex` (1), `antigravity` (2), `local` Ollama GPU (3), `openrouter` (4, last).
  Providers outside the pool (akashml, venice, nous, ionet, groq) are never selected.
* **free-only** -- only seats that are actually free: `:free` / $0 list models, and the local GPU.
  A subscription seat (Codex, Antigravity) is a paid plan, so it is not free
  (`seat_policy.free_only_allows_subscription = true` overrides). An unknown price is not free.
  With no free seat the answer is **UNAVAILABLE** -- there is no paid fallback.
* **Never selected, under any policy:** Claude (provider, family, or a Claude model id reached
  through any provider) and Cerebras. Reserved for Dispatch chat and Chloe.
* **Cross-family:** `exclude_families` removes every seat sharing base weights with the author
  (a fine-tune shares its base's family; Gemma and Gemini are both `google`). A seat whose family
  cannot be established is a conflict, not a pass.
* **Jev** (TypeSafe System One, key = GCP SM `typesafe-api-key`) is the switching layer: given
  `task`, it picks the cheapest *sufficient* seat among seats that already passed every hard
  rule. It cannot add a seat or override a rule, applies only at probability >= 0.6, and any
  failure keeps the deterministic order (`jev.used = false`, with `why`).

Response: `{outcome:"SEAT", policy, seat, provider, family, tier, cost_basis:
"list/measured|free|local-gpu|subscription", usd_per_mtok, invoke, because, jev, considered[],
decision_id}`. `considered` lists every seat with `QUALIFIES|EXCLUDED|UNKNOWN` and the reason.
No seat: HTTP 422 `{type:"seat_unavailable", outcome:"UNAVAILABLE", because, considered[]}`.

## `POST /v1/systemone` -- Jev behind the hard daily spend cap

TypeSafe has no server-side budget, so the cap is enforced here, before each call. Same body
and same answer as `https://api.typesafe.ai/v1/systemone` (`{model, state, questions}` ->
`{model, answers, usage}`), plus `guard`. A caller that points here holds no TypeSafe key.

* **Cap:** `[jev] daily_cap_usd` (default **$1.00**), reset at local midnight (`timezone`).
  Env overrides: `JEV_DAILY_CAP_USD`, `JEV_USD_PER_MTOK_IN`, `JEV_USD_PER_MTOK_OUT`,
  `JEV_GUARD_TZ`, `JEV_FALLBACK`, `JEV_FALLBACK_URL`, `JEV_ALERT_URL`, `JEV_GUARD_LEDGER`.
* **Cost:** TypeSafe's published price -- $0.042 per million input tokens, output free
  (jev-1.13.0) -- applied to the `usage` each answer returns. Integer nano-USD, never a guess.
  A call with unknown billing (timeout, any HTTP error except 401/403/429, no usable `usage`)
  is charged its worst case, never $0. The price cannot be configured below the published one
  and the cap cannot be set above $100: no config or env value turns the cap off.
* **How it holds:** each call reserves its worst case (one input token per request byte, plus
  the API's 64k output limit at the output price) in one `BEGIN IMMEDIATE` transaction on
  `<state>/jev_spend.sqlite3`, then settles to the actual cost. Concurrent callers and separate
  processes share the ledger; a crash keeps the hold; a call still in flight at midnight keeps
  counting against the new day until it settles. A request above 256 KB cannot be bounded and
  is refused (HTTP 413).
* **At the cap** the paid call is not made. `fallback = "laya"` (in-process, `pip install
  modelrouter[laya]`) or `"url"` (a Jev-compatible endpoint on loopback only; redirects are not followed) answers for free: HTTP 200,
  `guard.served_by = "fallback:..."`, `guard.blocked = true`. With `"defer"`, or if the
  fallback fails: HTTP 429 `{error.code: "jev_daily_cap_reached"}` with `Retry-After` =
  seconds to local midnight.
* **Alert:** the first block of a day logs `JEV DAILY CAP TRIPPED`, appends one line to
  `<state>/jev_guard_alerts.jsonl`, and POSTs it to `alert_url` if set.

`guard`: `{served_by, cost_usd, blocked, day, cap_usd, spent_usd, remaining_usd}`. Headers:
`X-Jev-Served-By`, `X-Jev-Spent-Usd`, `X-Jev-Cap-Usd`. Optional request header `X-Jev-Caller`
names the caller in the ledger. `GET /v1/systemone/spend` (or `modelrouter jev-spend`) reports
today's spend, remaining, paid / blocked calls and when the cap tripped.

The seat advisor above goes through the same guard: at the cap it makes no paid call and the
deterministic order stands (`jev.why` says so).

## Legibility

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | `{status:"ok"|"not_ready", service, version, providers_with_keys[], problems[]}` — no auth |
| GET | `/router/setup` | `Setup` |
| GET | `/router/roster?provider=&measured_only=` | `{providers:[ProviderState], dashboard, seats:[Candidate]}` |
| POST | `/router/explain` | `{choice: Choice, providers:[ProviderState], dashboard}` — body is a chat request; **no model call, no cost** |
| POST | `/router/probe` | body `{seats?:[string], cheapest?:int, generation?:bool, measured?:bool, budget_usd?:float}` (`generation`: a ~400-token run that measures decode speed) → `{spent_usd, budget_usd, probed:[{seat, rungs:[Attempt], profile}]}` — **spends money**, capped by config |
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

Monetary values in records are floats for display; `result.cost_usd_exact` and
`choice.expected_usd_exact` carry the exact Decimal strings sums are made from. `decide_ms` is the
time spent deciding (roster lookup + comparison), per decision.

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
`{seat, provider, model, provider_state:"OK|NO_KEY|BLOCKED|QUOTA_EXHAUSTED|UNAVAILABLE|AT_CAPACITY|CATALOGUE_FAILED", blocked, has_reasoning_field|null, decode_tps|null, window|null, correlated_with[], billed_prompt|null, billed_completion|null, billed_evidence, billing_ratio|null, provider_detail, available, context_length|null, supports_tools|null, list_prompt|null, list_completion|null, price_source:"dashboard|provider-catalogue", measured_usd_per_mtok|null, emits:"content|reasoning_then_content|reasoning_only"|null, min_max_tokens|null, floor_evidence, reasoning_overhead_tokens|null, probe_age_s|null, latency_s|null}`

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
`{provider, key, state:"…|MIRROR", detail, mirror_of, same_price_share|null, until|null, models, price_source, ratelimit:{header:value}, free_quota_until|null, in_flight, max_concurrency|null, concurrency_source}`

`correlated_with` lists providers that share this one's upstream (both directions).

`advertised_unreliable` (on seats) is set once a provider has billed above its advertised price on
any model (>10% on either leg, from solved billed rates). Its models without their own solved bills
are then priced UNKNOWN — an advertised price from that provider is a claim, not a price.

`max_concurrency` is the largest concurrency the provider completed with zero failures (imported
or configured); at the cap the provider is `AT_CAPACITY` and routes go elsewhere. Slots are reserved
atomically at call time.

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
