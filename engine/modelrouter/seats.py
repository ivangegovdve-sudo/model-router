"""Seat selection: which provider/model fills a ROLE, under a POLICY and a family constraint.

This is the layer above the per-request model decision (decision.py). A *seat* is one
callable (provider, model) with a family (shared base weights), a pool tier and a cost.
Selection is deterministic code; Jev (jev.py) may only re-order seats that ALREADY passed
every hard rule, and any Jev failure leaves the deterministic order untouched.

Pool (Ivan's order, 2026-10-03) -- earlier tier wins, cheapest live seat within a tier:
    0 sail  1 codex  2 antigravity  3 local (GPU / Ollama)  4 openrouter  (last resort)
Providers outside the pool (akashml, venice, nous, ionet, groq) are never selected here.

Never in the pool, anywhere: Claude (reserved for Dispatch chat) and Cerebras (reserved for
Chloe). Denied by provider, by family and by model id, so a Claude model reached through
OpenRouter is refused too.

Policies (per consumer, set in server config; a request may tighten, never loosen):
    cheapest   private council and the solver: pool order, cheapest priced seat first.
    free-only  public council: ONLY seats that are actually free. If none, UNAVAILABLE --
               never a paid fallback. Unknown price is not free. A subscription seat
               (Codex, Antigravity) is a paid plan, so it is not free.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .decision import Candidate, Verdict, is_free, money

POOL: tuple[str, ...] = ("sail", "codex", "antigravity", "local", "openrouter")
TIER = {p: i for i, p in enumerate(POOL)}

EXCLUDED_PROVIDERS = frozenset({"claude", "anthropic", "cerebras"})
EXCLUDED_FAMILIES = frozenset({"anthropic"})
_EXCLUDED_MODEL = re.compile(r"claude|anthropic|cerebras", re.I)

ROLES = ("review", "fix", "rebase", "council", "general")
POLICIES = ("cheapest", "free-only")

# Base-weight lineage. A fine-tune shares its base's family (Hermes on Llama is meta), and
# a lab's models are one family even across brands (Gemma and Gemini are google): the
# constraint is "no shared base weights", so lineage is read conservatively.
_FAMILY_RULES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (fam, re.compile(pat, re.I)) for fam, pat in (
        ("anthropic", r"claude|anthropic"),
        ("openai", r"(^|[/:\-_])(gpt|o[134](-|$)|chatgpt|codex|gpt-oss|whisper)|openai"),
        ("google", r"gemini|gemma|palm|google|antigravity"),
        ("meta", r"llama|meta-llama|hermes|nemotron-.*llama"),
        ("mistral", r"mistral|mixtral|ministral|codestral|devstral|magistral"),
        ("qwen", r"qwen|qwq"),
        ("deepseek", r"deepseek"),
        ("moonshot", r"kimi|moonshot"),
        ("zhipu", r"glm|zhipu|chatglm"),
        ("minimax", r"minimax"),
        ("xai", r"grok|xai"),
        ("microsoft", r"\bphi-?\d|microsoft"),
        ("nvidia", r"nemotron|nvidia"),
        ("cohere", r"command-r|cohere"),
        ("ai21", r"jamba|ai21"),
        ("typesafe", r"jev|typesafe"),
    ))


def family_of(model: str) -> str | None:
    """Base-weight family of a model id, or None when it cannot be established.
    None is never 'different': with a cross-family constraint it is a conflict."""
    for fam, rx in _FAMILY_RULES:
        if rx.search(model):
            return fam
    return None


class Outcome(str, Enum):
    SEAT = "SEAT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class Seat:
    seat: str                       # "provider:model"
    provider: str
    model: str
    family: str | None
    tier: int
    kind: str                       # priced | free | subscription | local
    usd_per_mtok: Decimal | None    # blended; None = UNKNOWN (never 0)
    context_length: int | None = None
    supports_tools: bool | None = None
    available: bool = True
    detail: str = ""                # why unavailable, when it is
    invoke: dict = field(default_factory=dict)

    @property
    def free(self) -> bool:
        return self.kind in ("free", "local")


def is_excluded(provider: str, model: str, family: str | None) -> str:
    """The reason a seat may never be selected, or ''."""
    if provider.lower() in EXCLUDED_PROVIDERS:
        return "provider %s is excluded (reserved)" % provider
    if family in EXCLUDED_FAMILIES or _EXCLUDED_MODEL.search(model):
        return "Claude/Cerebras models are excluded from the pool"
    return ""


def _blend(c: Candidate) -> Decimal | None:
    m = c.measured_usd_per_mtok
    if m is not None:
        return money(m)
    if c.list_prompt is None or c.list_completion is None:
        return None
    return (2 * money(c.list_prompt) + money(c.list_completion)) / 3


def from_candidate(c: Candidate) -> Seat | None:
    """A roster Candidate -> Seat, for the providers the pool contains. Others: None."""
    if c.provider not in ("sail", "openrouter"):
        return None
    free = is_free(c)
    ok = c.provider_state == "OK" and c.available is not False
    return Seat(
        seat=c.seat, provider=c.provider, model=c.model, family=family_of(c.model),
        tier=TIER[c.provider], kind="free" if free else "priced",
        usd_per_mtok=Decimal(0) if free else _blend(c),
        context_length=c.context_length, supports_tools=c.supports_tools, available=ok,
        detail="" if ok else "provider %s: %s" % (c.provider_state, c.provider_detail),
        invoke={"kind": "router-proxy", "model": c.seat})


def adapter_placeholders(declared: list[dict]) -> list[Seat]:
    """Codex and Antigravity are pool tiers whose call path is not verified live yet. Until an
    operator declares a seat ([[seats]], available = true) each appears as an UNAVAILABLE
    placeholder so a decision names why the tier was skipped, instead of omitting it."""
    have = {str(d.get("provider", "")).lower() for d in declared}
    return [Seat(seat="%s:unconfigured" % p, provider=p, model="unconfigured", family=None,
                 tier=TIER[p], kind="subscription", usd_per_mtok=Decimal(0), available=False,
                 detail="no %s seat declared; adapter not verified live" % p)
            for p in ("codex", "antigravity") if p not in have]


def from_declared(d: dict) -> Seat:
    """A seat declared in config ([[seats]]): codex / antigravity / local adapters, whose
    availability the operator (or a health probe) asserts. Default is UNAVAILABLE."""
    provider = str(d["provider"]).lower()
    model = str(d["model"])
    kind = d.get("kind") or {"local": "local", "codex": "subscription",
                             "antigravity": "subscription"}.get(provider, "priced")
    price = d.get("usd_per_mtok")
    return Seat(
        seat="%s:%s" % (provider, model), provider=provider, model=model,
        family=d.get("family") or family_of(model), tier=TIER.get(provider, len(POOL)),
        kind=kind,
        usd_per_mtok=Decimal(str(price)) if price is not None
        else (Decimal(0) if kind in ("subscription", "local", "free") else None),
        context_length=d.get("context_length"), supports_tools=d.get("supports_tools"),
        available=bool(d.get("available", False)),
        detail=d.get("detail", "" if d.get("available") else "adapter not verified live"),
        invoke=dict(d.get("invoke") or {}))


_NOT_CHAT = re.compile(r"embed|minilm|nomic|rerank|bge-|smollm|:0\.5b|:135m|:270m|^bench-|^hctx-|-32k:|-ctx\d", re.I)


def discover_local(base_url: str = "http://127.0.0.1:11434", timeout: float = 3.0,
                   get=None) -> list[Seat]:
    """Seats on the local GPU, read LIVE from Ollama's /api/tags (nothing pinned). An
    unreachable Ollama yields one unavailable placeholder so the decision can say why."""
    import json
    import urllib.request
    try:
        if get is None:
            with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=timeout) as r:
                tags = json.loads(r.read().decode("utf-8"))
        else:
            tags = get(base_url)
        names = [m["name"] for m in tags.get("models", [])]
    except Exception as exc:
        return [Seat(seat="local:ollama", provider="local", model="ollama", family=None,
                     tier=TIER["local"], kind="local", usd_per_mtok=Decimal(0), available=False,
                     detail="ollama unreachable at %s (%s)" % (base_url, type(exc).__name__))]
    return [Seat(seat="local:" + n, provider="local", model=n, family=family_of(n),
                 tier=TIER["local"], kind="local", usd_per_mtok=Decimal(0),
                 invoke={"kind": "openai-compat", "base_url": base_url.rstrip("/") + "/v1",
                         "model": n})
            for n in names if not _NOT_CHAT.search(n)]


@dataclass(frozen=True)
class SeatRequest:
    role: str = "general"
    consumer: str = ""
    policy: str | None = None               # may tighten the consumer's policy, never loosen
    exclude_families: tuple[str, ...] = ()   # e.g. the PR author's family
    exclude_seats: tuple[str, ...] = ()
    min_context: int | None = None
    needs_tools: bool = False
    ceiling_usd_per_mtok: Decimal | None = None
    task: str = ""                           # bounded summary for Jev; data, never instructions


@dataclass(frozen=True)
class Assessment:
    seat: str
    verdict: Verdict
    because: str
    tier: int
    family: str | None
    usd_per_mtok: Decimal | None


@dataclass
class Resolution:
    outcome: Outcome
    policy: str
    seat: Seat | None
    because: str
    considered: list[Assessment]
    jev: dict = field(default_factory=dict)

    def public(self) -> dict:
        s = self.seat
        return {
            "outcome": self.outcome.value, "policy": self.policy, "because": self.because,
            "seat": s.seat if s else None,
            "provider": s.provider if s else None,
            "family": s.family if s else None,
            "tier": s.tier if s else None,
            "cost_basis": ({"priced": "list/measured", "free": "free", "local": "local-gpu",
                            "subscription": "subscription"}[s.kind] if s else None),
            "usd_per_mtok": str(s.usd_per_mtok) if s and s.usd_per_mtok is not None else None,
            "invoke": s.invoke if s else None,
            "jev": self.jev,
            "considered": [{"seat": a.seat, "verdict": a.verdict.value, "because": a.because,
                            "tier": a.tier, "family": a.family,
                            "usd_per_mtok": str(a.usd_per_mtok)
                            if a.usd_per_mtok is not None else None}
                           for a in self.considered],
        }


def effective_policy(configured: str | None, requested: str | None) -> str:
    """The configured policy wins unless the request is STRICTER (free-only)."""
    if configured is None and requested is None:
        raise ValueError("no policy: unknown consumer and none requested")
    for p in (configured, requested):
        if p is not None and p not in POLICIES:
            raise ValueError("unknown policy %r (known: %s)" % (p, ", ".join(POLICIES)))
    return "free-only" if "free-only" in (configured, requested) else (configured or requested)


def qualify(req: SeatRequest, policy: str, seats: list[Seat],
            allow_subscription_free: bool = False
            ) -> tuple[list[Seat], list[Assessment]]:
    """Apply every HARD rule. Returns (eligible seats in selection order, all assessments)."""
    out: list[Assessment] = []
    ok: list[Seat] = []
    ex_fam = {f.lower() for f in req.exclude_families}

    def note(s: Seat, v: Verdict, why: str) -> None:
        out.append(Assessment(s.seat, v, why, s.tier, s.family, s.usd_per_mtok))

    for s in seats:
        why = is_excluded(s.provider, s.model, s.family)
        if why:
            note(s, Verdict.EXCLUDED, why)
        elif s.provider not in TIER:
            note(s, Verdict.EXCLUDED, "provider %s is not in the pool" % s.provider)
        elif s.seat in req.exclude_seats:
            note(s, Verdict.EXCLUDED, "excluded by the caller")
        elif not s.available:
            note(s, Verdict.UNKNOWN, "unavailable: %s" % (s.detail or "not live"))
        elif ex_fam and s.family is None:
            note(s, Verdict.UNKNOWN, "family unknown; a cross-family constraint cannot be met")
        elif s.family in ex_fam:
            note(s, Verdict.EXCLUDED, "same family (%s) as the author" % s.family)
        elif req.needs_tools and s.supports_tools is not True:
            note(s, Verdict.EXCLUDED if s.supports_tools is False else Verdict.UNKNOWN,
                 "tool support %s" % ("absent" if s.supports_tools is False else "unknown"))
        elif req.min_context and (s.context_length is None or s.context_length < req.min_context):
            note(s, Verdict.UNKNOWN if s.context_length is None else Verdict.EXCLUDED,
                 "context %s < required %d" % (s.context_length or "unknown", req.min_context))
        elif policy == "free-only" and not (
                s.free or (allow_subscription_free and s.kind == "subscription")):
            note(s, Verdict.EXCLUDED,
                 "not free (%s%s)" % (s.kind, "" if s.usd_per_mtok is not None else ", price unknown"))
        elif s.usd_per_mtok is None:
            note(s, Verdict.UNKNOWN, "price unknown; unknown is not free")
        elif (req.ceiling_usd_per_mtok is not None and s.usd_per_mtok > req.ceiling_usd_per_mtok):
            note(s, Verdict.EXCLUDED, "above the $%s/M ceiling" % req.ceiling_usd_per_mtok)
        else:
            note(s, Verdict.QUALIFIES, "")
            ok.append(s)
    ok.sort(key=lambda s: (s.tier, s.usd_per_mtok, s.seat))
    return ok, out


def resolve(req: SeatRequest, seats: list[Seat], *, configured_policy: str | None = None,
            allow_subscription_free: bool = False, advisor=None) -> Resolution:
    """Pick the seat. `advisor(req, eligible) -> (reordered_or_None, info)` is Jev."""
    if req.role not in ROLES:
        raise ValueError("unknown role %r (known: %s)" % (req.role, ", ".join(ROLES)))
    policy = effective_policy(configured_policy, req.policy)
    eligible, assessed = qualify(req, policy, seats, allow_subscription_free)
    if not eligible:
        why = ("no free seat is available; free-only never falls back to a paid seat"
               if policy == "free-only" else "no seat satisfies the constraints")
        return Resolution(Outcome.UNAVAILABLE, policy, None, why, assessed)
    jev: dict = {"used": False, "why": "not requested"}
    chosen = eligible[0]
    if advisor is not None and len(eligible) > 1 and req.task:
        try:
            pick, jev = advisor(req, eligible)
        except Exception as exc:                      # fail open: Jev never blocks a decision
            pick, jev = None, {"used": False, "why": "advisor error: %s" % type(exc).__name__}
        if pick is not None and pick in eligible:     # may only choose among eligible seats
            chosen = pick
    top = eligible[0]
    because = "%s policy: tier %d (%s) %s%s" % (
        policy, chosen.tier, chosen.provider, "cheapest eligible" if chosen is top
        else "chosen by Jev over %s" % top.seat,
        ", $%s/M" % chosen.usd_per_mtok if chosen.kind == "priced" else ", %s" % chosen.kind)
    return Resolution(Outcome.SEAT, policy, chosen, because, assessed, jev)
