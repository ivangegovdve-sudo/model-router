"""Read-only seat advice for openDashboard2's dashboard_resolve_seat tool.

Only real, measured text API candidates can be advised. Tier and consumer policy
restrictions are applied before the normal router decision rules. Role is metadata;
the task contributes a token estimate and is never stored or used to infer competence.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .decision import Candidate, Verdict, assess, decide, money
from .requirements import finite_price


# The seat chain (Ivan, 2026-10-24): subscriptions first, then ONE paid tier in which price
# and capability decide among Sail, AkashML, OpenRouter and the Anthropic API -- no fixed
# provider order inside it. A tier number, not a pool position, is what ranks a seat.
SEAT_POOL = ("antigravity", "codex", "local", "sail", "akashml", "openrouter", "anthropic")
SEAT_TIER = {"antigravity": 0, "codex": 0, "local": 1,
             "sail": 2, "akashml": 2, "openrouter": 2, "anthropic": 2}
SEAT_TIER_NAMES = {0: "subscription", 1: "local", 2: "paid"}
# Excluded for every consumer unless the operator scopes it down per consumer. The Glass
# solver's Claude ban was lifted (2026-10-24): price + capability keep Claude to the cases
# where it is the cheapest capable seat. Cerebras stays reserved for Chloe everywhere.
SEAT_EXCLUDED = ("claude", "anthropic", "cerebras")
DEFAULT_CONSUMER_EXCLUDED = {"glass-solver": ("cerebras",)}
SEAT_ROLES = ("review", "fix", "rebase", "council", "general")
SEAT_POLICIES = ("cheapest", "free-only")
IMPLEMENTED_PROVIDERS = ("sail", "akashml", "openrouter")
DEFAULT_CONSUMER_POLICIES = {"glass-solver": "cheapest", "private-council": "cheapest",
                             "public-council": "free-only"}


class SeatUnavailable(Exception):
    """A validated advisory request has no qualifying seat; map detail to HTTP 422."""
    def __init__(self, detail: dict):
        self.detail = detail
        super().__init__(detail["because"])


def _validate(raw: dict, policies: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("seat request must be an object")
    unknown = set(raw) - {"consumer", "role", "policy", "exclude_families", "exclude_seats",
                          "need", "max_usd_per_m", "task"}
    if unknown:
        raise ValueError("unknown seat request field: " + ", ".join(sorted(unknown)))
    consumer = raw.get("consumer")
    if not isinstance(consumer, str) or not 1 <= len(consumer) <= 64:
        raise ValueError("consumer must be a string of 1 to 64 characters")
    configured = policies.get(consumer)
    if configured not in SEAT_POLICIES:
        raise ValueError("no valid seat policy configured for consumer %r" % consumer)
    requested = raw.get("policy", configured)
    if requested not in SEAT_POLICIES:
        raise ValueError("policy must be cheapest or free-only")
    # A client can add a restriction; it can never remove the operator's restriction.
    policy = "free-only" if "free-only" in (configured, requested) else "cheapest"
    role = raw.get("role", "general")
    if role not in SEAT_ROLES:
        raise ValueError("role must be one of " + ", ".join(SEAT_ROLES))
    exclusions = {}
    for name, maximum in (("exclude_families", 16), ("exclude_seats", 32)):
        value = raw.get(name, [])
        if not isinstance(value, list) or len(value) > maximum or any(
                not isinstance(entry, str) or not entry for entry in value):
            raise ValueError("%s must contain at most %d nonempty strings" % (name, maximum))
        exclusions[name] = list(dict.fromkeys(value))
    need = raw.get("need", {})
    if not isinstance(need, dict) or set(need) - {"tools", "min_context"}:
        raise ValueError("need must be an object containing tools and/or min_context")
    tools = need.get("tools", False)
    if not isinstance(tools, bool):
        raise ValueError("need.tools must be true or false")
    context = need.get("min_context")
    if "min_context" in need and (isinstance(context, bool) or not isinstance(context, int)
                                 or context < 1):
        raise ValueError("need.min_context must be an integer >= 1")
    ceiling = None
    if "max_usd_per_m" in raw:
        ceiling = finite_price(raw["max_usd_per_m"], "max_usd_per_m")
        if ceiling is None or ceiling <= 0:
            raise ValueError("max_usd_per_m must be a finite positive number")
    task = raw.get("task", "")
    if not isinstance(task, str) or len(task) > 4000:
        raise ValueError("task must be a string of at most 4000 characters")
    return {"consumer": consumer, "role": role, "policy": policy, "task": task,
            "tools": tools, "min_context": context, "ceiling": ceiling, **exclusions}


def _tier(candidate: Candidate) -> int:
    return SEAT_TIER.get(candidate.provider, -1)


def _excluded_for(consumer: str, cfg=None) -> tuple[str, ...]:
    table = getattr(cfg, "seat_consumer_excluded", None) or DEFAULT_CONSUMER_EXCLUDED
    return tuple(table.get(consumer, SEAT_EXCLUDED))


def _known_free(candidate: Candidate) -> bool:
    # Solved billed rates beat advertised rates. A blended rate or ':free' name does
    # not establish that both input and output are free.
    if candidate.billed_prompt is not None or candidate.billed_completion is not None:
        prompt, completion = candidate.billed_prompt, candidate.billed_completion
    else:
        if "billed" in candidate.measured_basis and candidate.measured_usd_per_mtok is not None \
                and money(candidate.measured_usd_per_mtok) > 0:
            return False
        prompt, completion = candidate.list_prompt, candidate.list_completion
    return prompt is not None and completion is not None and money(prompt) == 0 and money(completion) == 0


def _restriction(candidate: Candidate, request: dict, allow_free: bool) -> str | None:
    family = candidate.family.casefold() if candidate.family else None
    excluded = request.get("excluded", SEAT_EXCLUDED)
    if candidate.provider in excluded or family in excluded:
        return "provider or explicitly known family is excluded from the seat pool"
    if candidate.provider not in IMPLEMENTED_PROVIDERS:
        return "no implemented text API seat executor for provider %s" % candidate.provider
    if candidate.seat in request["exclude_seats"]:
        return "seat explicitly excluded by the caller"
    if request["exclude_families"]:
        if family is None:
            return "family unknown: cannot establish cross-family independence"
        if family in {entry.casefold() for entry in request["exclude_families"]}:
            return "explicitly known family is excluded by the caller"
    if request["policy"] == "free-only":
        if not allow_free:
            return "free-only blocked by operator policy: allow_free is false"
        if not _known_free(candidate):
            return "free-only requires known zero input and output token rates"
    return None


def resolve_seat(router, raw: dict) -> dict:
    """Return the dashboard seat contract, or raise ValueError / SeatUnavailable.

    The caller authenticates the operator before invoking this function. No model
    request, probe or quota charge is made; only an advisory decision is persisted.
    """
    policies = getattr(router.cfg, "seat_consumer_policies", DEFAULT_CONSUMER_POLICIES)
    request = _validate(raw, policies)
    request["excluded"] = _excluded_for(request["consumer"], router.cfg)
    requirements = {}
    if request["tools"]:
        requirements["tool_calls"] = True
    if request["min_context"] is not None:
        requirements["min_context_tokens"] = request["min_context"]
    # A zero effective ceiling ensures even an advertised free seat with nonzero
    # measured billing cannot be advised under free-only.
    ceiling = Decimal(0) if request["policy"] == "free-only" else request["ceiling"]
    proxy_request = {"model": "auto:background", "router_requirements": requirements,
                     "messages": [{"role": "user", "content": request["task"]}]}
    ask = replace(router._ask(proxy_request, ceiling, "background"),
                  needs_tools=request["tools"], allow_clamp=False)
    roster = router.gather.roster("background")
    restricted = []
    for candidate in roster:
        reason = _restriction(candidate, request, router.cfg.allow_free)
        restricted.append(replace(candidate, blocked=reason) if reason else candidate)
    assessments = [assess(ask, candidate) for candidate in restricted]
    qualifying_tiers = [_tier(candidate) for candidate, assessment in zip(restricted, assessments)
                        if assessment.verdict is Verdict.QUALIFIES]
    selected_tier = min(qualifying_tiers) if qualifying_tiers else None
    if selected_tier is not None:
        restricted = [replace(candidate, blocked="lower priority tier than first eligible tier %d" % selected_tier)
                      if not candidate.blocked and _tier(candidate) > selected_tier else candidate
                      for candidate in restricted]
    choice = decide(ask, restricted)
    assessments = [assess(ask, candidate) for candidate in restricted]
    facts = {**choice.facts, "seat_consumer": request["consumer"], "seat_role": request["role"],
             "seat_policy": request["policy"], "seat_pool": list(SEAT_POOL),
             "seat_tiers": dict(SEAT_TIER), "seat_excluded": list(request["excluded"]),
             "implemented_seat_providers": list(IMPLEMENTED_PROVIDERS),
             "exclude_families": request["exclude_families"], "exclude_seats": request["exclude_seats"]}
    because = choice.because
    if not choice.abstained:
        because = "first eligible tier %d (%s); %s" % (selected_tier, SEAT_TIER_NAMES.get(selected_tier, "?"), because)
    # Seat advice keeps every assessment, including policy-blocked and unknown seats.
    choice = replace(choice, considered=tuple(assessments), facts=facts, because=because)
    public = choice.public()
    considered = [{"seat": assessment.seat, "verdict": assessment.verdict.value,
                   "because": assessment.because, "tier": _tier(candidate), "family": candidate.family,
                   "usd_per_mtok": None if assessment.usd_per_mtok is None else str(assessment.usd_per_mtok)}
                  for candidate, assessment in zip(restricted, assessments)]
    record = {"requested": "seat:%s:%s" % (request["consumer"], request["role"]),
              "status": "ADVISED", "choice": public, "earlier_decisions": [], "attempts": [],
              "result": {"calls": 0, "cost_usd": 0, "cost_usd_exact": "0",
                         "cost_basis": "advisory; no provider call"}}
    decision_id = router.store.record(record)
    if choice.abstained:
        raise SeatUnavailable({"type": "seat_unavailable", "policy": request["policy"],
                               "because": because, "decision_id": decision_id, "considered": considered})
    selected = next(candidate for candidate in roster if candidate.seat == choice.seat)
    assessment = next(assessment for assessment in assessments if assessment.seat == choice.seat)
    invoke_headers = {"X-Router-Lane": "background"}
    if ask.ceiling_usd_per_mtok is not None:
        invoke_headers["X-Router-Max-Usd-Per-M"] = str(ask.ceiling_usd_per_mtok)
    return {"policy": request["policy"], "seat": selected.seat, "provider": selected.provider,
            "family": selected.family, "tier": _tier(selected), "cost_basis": assessment.price_basis,
            "usd_per_mtok": None if assessment.usd_per_mtok is None else str(assessment.usd_per_mtok),
            "invoke": {"api": "chat.completions", "model": selected.seat, "base_url": "/v1",
                       "headers": invoke_headers, "router_requirements": ask.requirements.public()},
            "because": because, "jev": {"confidence": public["confidence"],
                                         "scope": "measured-text-api-seats"},
            "decision_id": decision_id, "considered": considered}
