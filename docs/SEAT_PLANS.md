# Complete seat plans

Seat plans resolve a whole Council or Glass cohort before execution. Existing
`POST /v1/seats/resolve` keeps its original single-seat contract.

The operator-authenticated endpoints are:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/seat-plans/templates` | Versioned Council and Glass starter requests |
| POST | `/v1/seat-plans/resolve` | Advisory complete assignment; no model call |
| GET | `/v1/seat-plans/{plan_id}` | Durable assignment, participants and receipts |
| POST | `/v1/seat-plans/{plan_id}/reselect` | Atomic pending-slot replacement |
| POST | `/v1/seat-plans/{plan_id}/slots/{slot_id}/chat/completions` | Pinned router execution |

Plans are stored in the router SQLite database and bound to the authenticated
owner. Unknown plans and another owner's plans both return 404. IDs in invocation
paths are percent encoded. Invalid requests return 400; stale versions, changed
heads, changed identities, weakened requirements and invalid invocations return 409.

## Requests and templates

`schema` must be `seat-plan/1`. `consumer` must have an operator-configured policy.
Policy is `cheapest` or `free-only`; callers can tighten the configured floor but
cannot remove it. The request supplies 1–32 explicit `slots`, up to 64 `references`
and up to 128 `constraints`. Unknown fields are rejected before gathering.

```json
{
  "schema": "seat-plan/1",
  "consumer": "private-council",
  "template": {"id": "council-roundtable", "version": "1"},
  "context": {"kind": "council", "run_id": "round-42"},
  "slots": [
    {
      "id": "member_a",
      "roles": ["proposer", "critic", "chair"],
      "binding": {"kind": "auto"},
      "requirements": {"min_context_tokens": 32768},
      "estimate": {"prompt_tokens": 12000, "answer_tokens": 1500},
      "lane": "background"
    },
    {
      "id": "member_b",
      "roles": ["proposer", "critic", "triage"],
      "binding": {"kind": "router-fixed", "seat": "sail:vendor/checkpoint"}
    }
  ],
  "references": [],
  "constraints": [
    {"id": "pair", "kind": "all_distinct", "dimension": "weights", "slots": ["member_a", "member_b"]}
  ]
}
```

Slot IDs are unique nonempty strings, at most 128 characters, without control
characters. `role` or `roles` accepts metadata labels; labels do not establish model
competence. Multiple roles may share one explicitly selected slot. Requirements use
the existing typed router requirements: provider/model-type lists, capabilities,
thinking level, tools/editing support, minimum context and a rate ceiling.
Positive integer prompt/answer/max token estimates never contain task text.
The default lane is `background`.

Bindings are automatic, an exact `router-fixed` seat, or an `app-fixed` binding:

```json
{"kind": "app-fixed", "executor_id": "registered-codex", "model_ref": "codex:exact-checkpoint"}
```

An app binding requires an executor registered for this consumer and this exact
model. It participates in weight constraints but has no invented router executor,
provider tier, emission measurement, capability or price. Its invoke API is `app`,
and the registered consumer executes it. The router chat endpoint refuses app slots.
The current Council and Glass adapters fail closed for app-fixed execution because
they do not yet have an application executor that can prove the pinned model call.
Use `router-fixed` for executable seats, and references for models used by an
external PR author. App-fixed bindings remain useful for advisory plan constraints.

`GET /templates` returns `{schema, templates}`. Each template contains `id`, string
`version`, `name`, `consumer`, `context`, `slots`, `references`, and `constraints`.
`council-roundtable/1` supplies four proposer/critic members, chair on the first and
triage on the last, with a distinct-member group. `glass-pr-solver/1` supplies two
reviewers, one fixer and one rebase slot; reviewers differ from each other and the
dynamic `pr-author` reference. Its author placeholder must be filled with current
head evidence. Templates do not expand or overwrite an explicit request at resolve
time; UI and consumer adapters expand counts into concrete slots and preserve groups.

Context retains only scalar metadata: `kind`, `repo`, positive `number`, `head_sha`,
`revision`, and `run_id`. Prompts, questions and task bodies are not accepted there.

## Weight identity and references

`provider:model` identifies an execution route. `weights_id` identifies the declared
trained weights. Provider aliases and quantizations of the same weights must share
one ID. `family` is broader lineage metadata. No identity is inferred from names,
namespaces, branch hints, trailers, accounts or prices.

Operator configuration supplies `seat_model_identities`, keyed by exact execution
route, with `weights_id`, optional `identity_evidence_id` and optional `family`.
Explicit catalogue weight/evidence pairs may supplement that mapping. Conflicting
current identities cannot satisfy a guarantee.

A reference is `{id, models:["provider:model"], revision, evidence_id?}`. Models may
be absent when `evidence_id` resolves an operator registry entry. Static entries in
`seat_reference_models` contain `models`, optional `revision` and `verified`.
Delegation supports dynamic Glass evidence without editing router configuration
for every new head:

```toml
[seat_reference_models."glass-author-ledger"]
consumer = "glass-solver"
source = "operator-configured"
allow_consumer_models = true
```

The matching consumer then supplies exact model IDs, the current head revision and
that evidence ID. This is labeled `consumer-configured` provenance, not a GitHub
attestation. Static provenance is `operator-configured`; generic supplied references
are labeled `consumer-supplied`. PR references require registered/static or delegated
provenance and an exact match to `context.head_sha`. Unverified, unknown or stale
provenance yields unknown facts and cannot certify independence.

`all_distinct` contains slot IDs only and expands into every pair. Groups may
overlap and may contain fixed/app-filled slots. `different_from` connects one slot
to requested slot/reference edges:

```json
{
  "id": "review-vs-origin",
  "kind": "different_from",
  "dimension": "weights",
  "slot": "review_a",
  "others": [{"reference": "pr-author"}, {"reference": "fixer"}]
}
```

The two references may contain the same weights. They constrain the reviewer
independently and are not required to differ from each other. A reference can hold
multiple contributing model identities; the slot must differ from every one.

## Resolution and evidence

The manager freezes priced roster views once for each requested lane and assigns a
single snapshot ID covering those views, identities, executors and references.
The solver never regathers during search or greedily removes later provider tiers.
It uses smallest-domain-first bounded backtracking, forward checking and a safe
ranking lower bound. The default shared search budget is 100,000 visited states.

Among complete eligible assignments, ranking first minimizes counts at the
lowest-priority provider tier, then each preceding tier, then total estimated cost.
Deterministic route-ID ordering breaks ties. App-fixed slots do not acquire a tier.
Only established eligible candidates participate in cost ranking.

Outcomes are `RESOLVED`, `UNKNOWN`, `UNSATISFIABLE` and `SEARCH_LIMIT`. A resolved
response includes every slot with ranking proved. Proven shortage requires exhaustive
infeasibility, including an optimistic pass when relevant facts are unknown. Unknown
reference identities are relaxed only in that diagnostic pass; no such assignment
is exposed as executable. Search exhaustion yields no partial executable plan.

The response carries nullable `plan_id`/numeric `version`, `snapshot_id`, consumer,
policy/context, assignments, considerations, unknown facts and constraint evidence.
Each considered row identifies the slot/seat, verdict, reason, unknown facts, weight
ID, nullable tier and exact nullable estimate. Constraint evidence contains:

```json
{
  "id": "pair",
  "kind": "all_distinct",
  "dimension": "weights",
  "satisfied": true,
  "pairs": [{
    "left": {"slot": "member_a"},
    "right": {"slot": "member_b"},
    "left_weights_id": "weights-A",
    "right_weights_ids": ["weights-B"],
    "satisfied": true
  }]
}
```

Assignment amounts are decimal strings. `estimates` contains `scope`, exact
`known_subtotal_usd` and nullable `total_usd`; scope is one invocation per slot and
excludes retries and additional deliberation/fix rounds. Unknown app cost makes the
total unknown. A caller's explicit app price ceiling requires price evidence.
Free-only requires known zero input/output rates for every executed binding; solved
billed rates override advertised rates. A cheapest app-fixed advisory may retain
unknown cost outside router execution, clearly shown as unknown rather than free.

## Invocation, participation and re-selection

Router assignments return a pinned invocation path, exact model, lane/price headers,
expanded requirements and numeric plan version. Execute with:

```json
{
  "version": 1,
  "context": {"kind": "council", "run_id": "round-42"},
  "request": {
    "model": "sail:vendor/checkpoint",
    "messages": [{"role": "user", "content": "Actual task sent only for execution"}]
  }
}
```

The request may omit `model` or supply the exact route/raw model ID. Another model,
`auto`, native `models`/`route`/`plugins` fallback controls, caller-supplied `provider`
controls and untrusted `router_*` fields are rejected. Glass/OpenRouter invocations
receive the trusted upstream exclusion `provider.ignore = ["Cerebras", "Groq"]`.
Requirements may tighten the plan and are never weakened.
Owner, version, context/head, operator policy, current weight identity, references,
eligibility, lane and effective price ceiling are checked before execution.

Preparation durably marks the slot running and records its identity/evidence and
execution ID. Completion stores sanitized status/cost/token metadata and router
decision ID, without prompts or answers. A role-sharing consumer may invoke the same
pinned slot sequentially. Simultaneous invocation of a running slot is refused.

Re-select with `{version, context, failed_slots:["slot_id"]}`. Running and completed
participants remain pinned. Pending fixed slots retain their exact bindings and
are re-assessed against current eligibility and price facts; automatic pending slots
are solved together under every original group/reference edge and current operator
policy floor. Failed requested routes/weights are excluded from
their replacement slot. Every started identity remains in its original slot's history
and participates in that slot's diversity edges. A failed model cannot migrate to a
different member of a connected group. Updates commit atomically or leave the previous
plan intact. A retained running call can finish with its original execution version
after another slot causes the plan version to advance.

A changed PR head requires a fresh plan and fresh current-head review completion.
The router certifies requested identity constraints; consumer adapters must actually
execute the assigned roles and enforce their completion/merge rules. Distinct model
reviewers do not create additional GitHub approving principals.
