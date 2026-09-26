"""The typed decision layer: which model serves this request, or ABSTAIN.

Same shape as the PR solver's `tools/pr_decision.py`, on purpose. Routing to a model
IS a typed decision over a closed option set: the closed set is the live roster
gathered for THIS request, and the answer is one member of it or an explicit
ABSTAIN. It always carries the facts that produced it -- per candidate, not just for
the winner -- so anyone can see why a call went where it went and cost what it cost.

WHY A PRICE TABLE IS NOT ENOUGH
-------------------------------
Measured on AkashML on 2026-09-25, three models, one provider:

    gpt-oss-20b              106 tokens  $0.00000  EMPTY
    Llama-3.3-70B-Instruct    44 tokens  $0.00001  'Ready'
    Qwen3.8-27B               33 tokens  $0.00004  EMPTY

Two are reasoning models. Their text lands in `reasoning_content` and a small
`max_tokens` is spent on reasoning before any answer is emitted: HTTP 200, tokens
billed, empty string. A cheapest-capable router reading only prices picks the model
that returned nothing and reports success. So a candidate qualifies only on
MEASURED behaviour -- where it emits, how many tokens it needs before content
appears -- and it is ranked on its own measured price, not its provider's headline.

ABSTAIN IS THE FEATURE. When nothing qualifies, the router refuses. A request that
cannot be routed honestly must fail loudly, not quietly cost money.

Stdlib only. No I/O. Deterministic: the same facts always give the same Choice.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    """The closed set of outcomes. The ROUTE target is one member of the roster."""

    ROUTE = "ROUTE"        # one candidate qualifies on known facts and is cheapest
    ABSTAIN = "ABSTAIN"    # none qualifies, or a fact every qualifier needs is unknown


class Verdict(str, Enum):
    """What the layer concluded about ONE candidate."""

    QUALIFIES = "QUALIFIES"
    EXCLUDED = "EXCLUDED"  # a known fact rules it out
    UNKNOWN = "UNKNOWN"    # a fact it needs has not been established


class Emits(str, Enum):
    """Where a model puts its answer, as measured by a probe."""

    CONTENT = "content"                              # answer in message.content
    REASONING_THEN_CONTENT = "reasoning_then_content"  # reasons first; content after enough tokens
    REASONING_ONLY = "reasoning_only"                # never produced content at any probed budget


#: A raised budget gives this multiple of the most reasoning ever observed for the model:
#: the floor comes from a one-word probe, and a real task reasons longer (GLM-5.3-Flash
#: probed at 35 tokens, then came back empty on a classification prompt at 43).
CLAMP_HEADROOM = 2

#: When a client sets no max_tokens, cost is estimated at this many completion tokens.
DEFAULT_COMPLETION_ESTIMATE = 512


@dataclass(frozen=True)
class Candidate:
    """Everything known about one (provider, model) at decision time.

    `None` always means UNKNOWN -- never zero, never false. A null price is not free.
    """

    seat: str                                   # "provider:model" -- the routable id
    provider: str
    model: str
    provider_state: str = "OK"                  # OK | NO_KEY | BLOCKED | QUOTA_EXHAUSTED
    provider_detail: str = ""
    available: bool | None = True               # False: gone from the live catalogue
    context_length: int | None = None
    supports_tools: bool | None = None
    # Prices in USD per million tokens.
    list_prompt: float | None = None            # headline, from the catalogue
    list_completion: float | None = None
    price_source: str = ""                      # dashboard | provider-catalogue
    measured_usd_per_mtok: float | None = None  # cost / tokens, from this model's own calls
    measured_basis: str = ""                    # billed | computed | billed+computed
    window: str | None = None                   # provider scheduling window priced for this lane
    # Behaviour, from probes.
    emits: Emits | None = None
    min_max_tokens: int | None = None           # smallest budget that produced content
    reasoning_overhead_tokens: int | None = None  # completion tokens spent before content
    floor_evidence: str = ""                    # the measurement behind min_max_tokens
    max_reasoning_tokens: int | None = None     # most reasoning seen before an answer, any task
    probe_age_s: float | None = None
    latency_s: float | None = None


@dataclass(frozen=True)
class Ask:
    """The facts about the request that bear on routing. Never the prompt itself."""

    prompt_tokens: int                          # estimated
    max_tokens: int | None = None               # as the client set it; None = unset
    needs_tools: bool = False
    ceiling_usd_per_mtok: float | None = None   # refuse anything dearer than this
    only_seat: str | None = None                # client named a model: judge only that one
    failed: tuple[tuple[str, str], ...] = ()    # (seat, why) already tried for THIS request
    allow_free: bool = False                    # free tiers may log prompts; separate quotas
    # LANE: interactive (someone is waiting) | background | batch (nobody is waiting).
    # Only the interactive lane gates on measured latency; the others buy cheaper, slower
    # capacity (e.g. Sail's balanced / flex completion windows).
    lane: str = "interactive"
    interactive_max_latency_s: float | None = None
    # A budget below a reasoning model's measured floor bills tokens and returns nothing.
    # With allow_clamp, when NO model qualifies at the caller's budget, the cheapest
    # reasoning model is used with the budget raised to the caller's budget plus
    # CLAMP_HEADROOM x the most reasoning observed for it -- visibly, never silently.
    allow_clamp: bool = False
    max_clamp_extra: int = 1024


@dataclass(frozen=True)
class Assessment:
    seat: str
    verdict: Verdict
    because: str
    unknown: tuple[str, ...] = ()
    usd_per_mtok: float | None = None           # the price used to rank it
    price_basis: str = ""                       # measured | list | ""
    expected_usd: float | None = None           # for THIS request
    clamp_to: int | None = None                 # would qualify with max_tokens raised to this


@dataclass(frozen=True)
class Choice:
    """One decision. Typed, closed-set, self-explaining."""

    outcome: Outcome
    because: str
    seat: str | None = None
    max_tokens: int | None = None               # what will be sent upstream
    expected_usd: float | None = None
    considered: tuple[Assessment, ...] = ()
    unknown: tuple[str, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def abstained(self) -> bool:
        return self.outcome is Outcome.ABSTAIN

    def public(self) -> dict:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        d["unknown"] = list(self.unknown)
        d["considered"] = [
            {**asdict(a), "verdict": a.verdict.value, "unknown": list(a.unknown)}
            for a in self.considered
        ]
        return d

    def to_json(self) -> str:
        return json.dumps(self.public(), sort_keys=True)


UNKNOWN_KEPT = 25


def _list_blend(c: Candidate) -> float:
    if c.list_prompt is None or c.list_completion is None:
        return float("inf")
    return (2 * c.list_prompt + c.list_completion) / 3


def is_free(c: Candidate) -> bool:
    return c.model.endswith(":free") or (c.list_prompt == 0 and c.list_completion == 0)


def _cost(ask: Ask, c: Candidate, completion: int) -> tuple[float | None, float | None, str]:
    """(expected USD for this request, effective $/M, basis).

    - A provider that BILLS per call: its measured price (it catches what a rate card
      does not, e.g. Venice's injected system prompt).
    - Otherwise this model's own rate card for the lane's window, split into prompt and
      completion, with the measured reasoning tokens charged at the completion rate --
      so a no-reasoning model's short answer is priced as the short answer it is.
    - Half a price is not a price: both legs must be known.
    """
    oh = c.reasoning_overhead_tokens or 0
    tokens = ask.prompt_tokens + completion + oh
    if c.measured_usd_per_mtok is not None and "billed" in c.measured_basis:
        return c.measured_usd_per_mtok * tokens / 1e6, c.measured_usd_per_mtok, "measured, billed"
    if c.list_prompt is not None and c.list_completion is not None:
        exp = (ask.prompt_tokens * c.list_prompt + (completion + oh) * c.list_completion) / 1e6
        basis = "rate card" + (" " + c.window if c.window else "")
        return exp, exp / tokens * 1e6, basis
    if c.measured_usd_per_mtok is not None:
        return c.measured_usd_per_mtok * tokens / 1e6, c.measured_usd_per_mtok, "measured"
    return None, None, ""


def assess(ask: Ask, c: Candidate) -> Assessment:
    """Judge one candidate against one request. Order matters: account-level facts
    first, because nothing about a model helps if its provider will refuse the call."""
    def out(verdict: Verdict, because: str, unknown: tuple[str, ...] = (),
            usd: float | None = None, basis: str = "", exp: float | None = None) -> Assessment:
        return Assessment(c.seat, verdict, because, unknown, usd, basis, exp)

    for seat, why in ask.failed:
        if seat == c.seat:
            return out(Verdict.EXCLUDED, "already failed for this request: " + why)
    if c.provider_state != "OK":
        return out(Verdict.EXCLUDED, "provider %s%s" % (
            c.provider_state, (": " + c.provider_detail) if c.provider_detail else ""))
    if c.available is False:
        return out(Verdict.EXCLUDED, "no longer in the provider's live catalogue")
    if not ask.allow_free and is_free(c):
        return out(Verdict.EXCLUDED, "free tier: excluded by policy (free endpoints may "
                   "log prompts and run on a separate daily quota)")

    if ask.needs_tools:
        if c.supports_tools is False:
            return out(Verdict.EXCLUDED, "request uses tools; model does not support them")
        if c.supports_tools is None:
            return out(Verdict.UNKNOWN, "request uses tools; tool support unknown",
                       ("supports_tools",))

    # Behaviour before price: a cheap model that returns nothing is the failure
    # this layer exists to prevent.
    if c.emits is None:
        return out(Verdict.UNKNOWN, "never measured: a price alone does not show it "
                   "returns content", ("emits",))
    if c.emits is Emits.REASONING_ONLY:
        return out(Verdict.EXCLUDED, "answered only into reasoning_content at every "
                   "probed budget: content comes back empty")
    need = c.min_max_tokens or 0
    if c.emits is Emits.REASONING_THEN_CONTENT and c.min_max_tokens is None:
        return out(Verdict.UNKNOWN, "reasons before answering; the budget it needs "
                   "is unknown", ("min_max_tokens",))
    # The latency gate chooses FOR the caller; a caller who named the model has chosen.
    if ask.lane == "interactive" and ask.interactive_max_latency_s is not None             and ask.only_seat is None:
        if c.latency_s is None:
            return out(Verdict.UNKNOWN, "latency never measured; the interactive lane "
                       "needs it", ("latency_s",))
        if c.latency_s > ask.interactive_max_latency_s:
            return out(Verdict.EXCLUDED, "measured latency %.2fs is above the interactive "
                       "lane's %.2fs -- a background or batch lane can use it" % (
                           c.latency_s, ask.interactive_max_latency_s))
    if ask.max_tokens is not None and ask.max_tokens < need:
        why = "needs max_tokens >= %d before any content appears%s; request allows %d" % (
            need, " (measured: %s)" % c.floor_evidence if c.floor_evidence else "",
            ask.max_tokens)
        reasoning = max(need - 1, c.max_reasoning_tokens or 0)
        if ask.allow_clamp and CLAMP_HEADROOM * reasoning <= ask.max_clamp_extra:
            raised = ask.max_tokens + CLAMP_HEADROOM * reasoning
            again = assess(replace(ask, max_tokens=raised, allow_clamp=False), c)
            if again.verdict is Verdict.QUALIFIES:
                return Assessment(c.seat, Verdict.EXCLUDED, why + "; would serve with "
                                  "max_tokens raised to %d" % raised, (), again.usd_per_mtok,
                                  again.price_basis, again.expected_usd, raised)
        return out(Verdict.EXCLUDED, why)

    completion = ask.max_tokens if ask.max_tokens is not None else max(
        DEFAULT_COMPLETION_ESTIMATE, need)
    if c.context_length is None:
        return out(Verdict.UNKNOWN, "context length unknown", ("context_length",))
    if ask.prompt_tokens + completion > c.context_length:
        return out(Verdict.EXCLUDED, "needs %d tokens of context; model has %d" % (
            ask.prompt_tokens + completion, c.context_length))

    exp, usd, basis = _cost(ask, c, completion)
    if usd is None:
        return out(Verdict.UNKNOWN, "price unknown (null is not zero)", ("price",))
    if ask.ceiling_usd_per_mtok is not None and usd > ask.ceiling_usd_per_mtok:
        return out(Verdict.EXCLUDED, "$%.4g/M (%s) is above the $%.4g/M ceiling" % (
            usd, basis, ask.ceiling_usd_per_mtok), usd=usd, basis=basis)
    return out(Verdict.QUALIFIES, "$%.4g/M (%s), emits into %s%s" % (
        usd, basis, c.emits.value,
        ", %.2fs measured" % c.latency_s if c.latency_s is not None else ""),
        usd=usd, basis=basis, exp=exp)


def decide(ask: Ask, roster: list[Candidate]) -> Choice:
    """Choose one candidate, or abstain.

    Rank: lowest expected cost for THIS request (price x tokens including measured
    reasoning overhead), then lower latency, then seat id for determinism.
    """
    pool = [c for c in roster if ask.only_seat in (None, c.seat)]
    facts = {"prompt_tokens": ask.prompt_tokens, "max_tokens": ask.max_tokens,
             "needs_tools": ask.needs_tools, "ceiling_usd_per_mtok": ask.ceiling_usd_per_mtok,
             "only_seat": ask.only_seat, "roster_size": len(roster), "lane": ask.lane,
             "interactive_max_latency_s": ask.interactive_max_latency_s}
    if ask.only_seat and not pool:
        return Choice(Outcome.ABSTAIN, "requested model %s is not in the live roster"
                      % ask.only_seat, facts=facts)
    if not pool:
        return Choice(Outcome.ABSTAIN, "the live roster is empty: no provider with a key "
                      "returned a catalogue", facts=facts, unknown=("roster",))

    by_seat = {c.seat: c for c in pool}
    judged = [assess(ask, c) for c in pool]
    ok = [a for a in judged if a.verdict is Verdict.QUALIFIES]
    ok.sort(key=lambda a: (a.expected_usd, by_seat[a.seat].latency_s or float("inf"), a.seat))
    excluded = sorted((a for a in judged if a.verdict is Verdict.EXCLUDED
                       and not a.because.startswith("free tier")), key=lambda a: a.seat)
    facts["free_tier_not_listed"] = sum(1 for a in judged if a.because.startswith("free tier"))
    # Unmeasured seats are most of any roster. Keep the ones that matter -- those whose
    # LIST price undercuts the winner (they might have been cheaper; nobody knows) --
    # and count the rest, so the record stays legible instead of a thousand rows.
    bar = ok[0].usd_per_mtok if ok else float("inf")
    unk = [a for a in judged if a.verdict is Verdict.UNKNOWN]
    unk.sort(key=lambda a: (_list_blend(by_seat[a.seat]), a.seat))
    kept = [a for a in unk if _list_blend(by_seat[a.seat]) < bar][:UNKNOWN_KEPT]
    facts["unknown_not_listed"] = len(unk) - len(kept)
    considered = tuple(ok + excluded + kept)

    clampable = sorted((a for a in judged if a.clamp_to is not None and a.expected_usd is not None),
                       key=lambda a: (a.expected_usd, by_seat[a.seat].latency_s or float("inf"),
                                      a.seat))
    if not ok and clampable:
        # Nothing answers at the caller's budget. Passing it through to a reasoning model
        # would bill tokens and return nothing; raise it -- on the record -- instead.
        win = clampable[0]
        chosen = replace(win, verdict=Verdict.QUALIFIES, because="qualifies with max_tokens "
                         "raised %d -> %d; %s" % (ask.max_tokens, win.clamp_to, win.because))
        considered = tuple([chosen] + [a for a in considered if a.seat != win.seat])
        facts["max_tokens_raised_from"] = ask.max_tokens
        because = ("no model answers within max_tokens=%d; raised to %d for %s, the cheapest "
                   "that can: below its measured floor it bills tokens and returns nothing "
                   "(%s). $%.3g expected (%s)" % (
                       ask.max_tokens, win.clamp_to, win.seat,
                       by_seat[win.seat].floor_evidence or "measured",
                       win.expected_usd, win.price_basis))
        return Choice(Outcome.ROUTE, because, seat=win.seat, max_tokens=win.clamp_to,
                      expected_usd=win.expected_usd, considered=considered, facts=facts)

    if not ok:
        counts: dict[str, int] = {}
        for a in judged:
            key = a.because.split(":")[0] if a.verdict is Verdict.UNKNOWN else a.verdict.value
            counts[key] = counts.get(key, 0) + 1
        unknown = tuple(sorted({u for a in judged for u in a.unknown}))
        summary = ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items(), key=lambda x: -x[1]))
        # Name the reasons, not just the counts: the nearest misses first.
        near = [a for a in considered if a.verdict is Verdict.EXCLUDED
                and not a.because.startswith(("provider ", "free tier"))][:3]
        if near:
            summary += "; " + "; ".join("%s: %s" % (a.seat, a.because) for a in near)
        return Choice(Outcome.ABSTAIN, "no candidate qualifies (%s)" % summary,
                      considered=considered, unknown=unknown, facts=facts)

    win = ok[0]
    runner = ok[1] if len(ok) > 1 else None
    # Say what actually decided: expected cost for THIS request, which folds in the
    # reasoning tokens a model spends before answering -- a lower $/M can still lose.
    def cost(a: Assessment) -> str:
        oh = by_seat[a.seat].reasoning_overhead_tokens
        return "$%.3g expected ($%.4g/M %s%s)" % (
            a.expected_usd, a.usd_per_mtok, a.price_basis,
            ", +%d reasoning tokens" % oh if oh else "")
    because = "lowest expected cost of %d qualifying: %s" % (len(ok), cost(win))
    if runner:
        because += "; next %s at %s" % (runner.seat, cost(runner))
    # The client's budget is forwarded untouched here: some model answers within it,
    # so no reasoning model is handed a budget it would spend on thinking alone.
    return Choice(Outcome.ROUTE, because, seat=win.seat, max_tokens=ask.max_tokens,
                  expected_usd=win.expected_usd, considered=considered, facts=facts)
