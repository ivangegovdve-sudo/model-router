# Approved multi-seat routing and Windows UI

The user approved `windows-seat-plan-preview.png` and the scope in
`/workspace/artifacts/seat-plan-compatibility.md` on 2026-10-12. Preserve the companion
single-request visual `windows-revised-composer.png`. Implement real compatibility
with `ivangegovdve-sudo/council` and `ivangegovdve-sudo/glass-pr-solver`.

One router owns model identity, eligibility and complete plan assignment. Council
and Glass execute pinned assignments through trusted invocations; the Kotlin client
previews and edits the same contract. Keep the existing single-seat endpoint intact.

## Wire contract

`POST /v1/seat-plans/resolve` accepts `schema: "seat-plan/1"`, `consumer`, optional
`policy`, optional `template: {id, version}`, `context`, `slots`, `references`,
`constraints`. At most 32 slots, unique nonempty IDs. A slot has `id`, `role` (or
`roles`), `binding`, optional `requirements`, optional `estimate` and optional `lane`.
Requirements mirror the existing router requirements. Estimates contain positive
prompt/answer/max token counts, never prompt text.

Bindings: `{kind:"auto"}`, `{kind:"router-fixed",seat:"provider:model"}`, or
`{kind:"app-fixed",executor_id:"configured-executor",model_ref:"provider:model"}`.
App executors and model identities are operator-configured. Known catalogue/model
identity metadata may supplement explicit operator mappings; names are not heuristics.

Reference: `{id, models:["provider:model"], revision, evidence_id?}` or an
operator-configured `evidence_id`. PR references must match `context.head_sha`.
Consumer-supplied provenance is explicitly labeled; branch and coauthor hints cannot
be silently promoted to model evidence. The consumer must provide verified/configured
author-model provenance before requesting an independence guarantee.

Constraints: `{id,kind:"all_distinct",dimension:"weights",slots:[...]}` or
`{id,kind:"different_from",dimension:"weights",slot:"id",others:[{slot:"id"}|{reference:"id"}]}`.
All-distinct groups expand to all pairwise edges among their slots. References only
constrain the requested slot edges; duplicated reference identities do not conflict
with each other. Different providers, aliases or quantizations of identical weights
share one `weights_id`. Broad family remains a separate fact.

Response contains `schema`, `outcome` (`RESOLVED`, `UNKNOWN`, `UNSATISFIABLE`,
`SEARCH_LIMIT`), `because`, nullable `plan_id`, `version`, `snapshot_id`, `consumer`,
`context`, `assignments`, `constraint_evidence`, `unknown`, `considered`, `estimates`.
An assignment contains `slot_id`, `role`, `roles`, `binding`, nullable `seat`,
`provider`, `model`, `weights_id`, `identity_evidence_id`, `family`, `requirements`,
`lane`, exact nullable `estimated_usd`, and `invoke`. Estimate scope is one invocation
per slot, with `known_subtotal_usd` and nullable total when app costs are unknown.

`GET /v1/seat-plans/templates` exposes Council and Glass templates. Council defaults
to four proposer/critic members with chair/triage roles sharing explicit members.
Glass defaults to two reviews, one fix and one rebase, reviewers differing from one
another and the dynamic `pr-author` reference. UI/consumers expand custom counts into
explicit slots and preserve all supplied custom groups.

## Identity and execution

New Config fields: `seat_model_identities`, `seat_reference_models`,
`seat_app_executors` (dicts). Model identity entries keyed by `provider:model` contain
`weights_id`, optional `identity_evidence_id`, optional `family`; aliases can share an ID.
App executor metadata supplies exact eligible models/capabilities/prices where known.
Never invent an executor or use unknown prices under free-only policy.

Resolve all slots from one roster snapshot with deterministic, bounded complete
constraint search. A greedy resolver or silent partial plan is insufficient. Preserve
operator policy floors and global tier ordering while checking complete feasibility.
Record metadata without task/prompt bodies. Missing identity and exhausted search
are different from proven impossibility.

Persist plans with owner, version and assignments. `GET /v1/seat-plans/{id}` reads a
plan; `POST /v1/seat-plans/{id}/reselect` atomically re-solves pending slots while
retaining fixed/running/completed participants and reference exclusions.

Pinned invocation path: `POST /v1/seat-plans/{id}/slots/{slot}/chat/completions`, body
`{version,context,request}`. Enforce owner/version, current PR head, weight identity,
lane, requirements and price ceiling before normal router execution. Never invoke
`auto` after choosing a plan. Native request fields may tighten but not weaken the
plan. App-fixed slots use the registered consumer executor and are clearly separate
from router calls. Preserve selection evidence in execution receipts.

## Consumer changes

Council: add opt-in plan request fields, preserve auth/defaults, use immutable per-run
rosters, route actual proposer/critic/reducer calls through pinned slots, use seat IDs
for results, remove the ten-label limit, and fix active MCP schema mismatches.

Glass: preserve legacy operation, add opt-in router configuration and pinned gateway
adapter, head-bound author provenance, per-head/per-slot reviewer completion and
required cohort merge gating. Both reviewers must finish on the current head; changed
heads invalidate old results. Preserve successful slots during retries. Model plurality
does not manufacture GitHub approving principals. Existing action, isolation, secret,
upstream allowlist, lease and branch-protection controls remain authoritative.

## UI and delivery

Implement the approved light Windows layout, editable goal templates, per-seat/default
filters, custom counts/roles/bindings, overlapping groups and dynamic references.
Use labeled blue/green/amber/neutral evidence statuses. No fabricated probabilities
or implied competency from emission probes. Preserve edited user text and filters.
Build Windows x64 MSI with bundled runtime, bump native package version to 1.1.0,
fix Android CI's retired `tools` package, and inspect native UI behavior.
