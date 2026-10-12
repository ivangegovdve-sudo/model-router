"""Complete, evidence-backed seat assignments and durable pinned participation.

Advice never invokes a provider. A graph is solved against a frozen set of priced
lane views, with operator-configured identities rather than model-name guesses.
Only metadata and numeric estimates are stored; invocation bodies stay ephemeral.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
import hashlib
import itertools
import json
import sqlite3
import threading
import time
import uuid
from urllib.parse import quote

from .decision import Ask, Candidate, Emits, Verdict, assess, money
from .requirements import check_requirements, lower_ceiling, parse_requirements
from .router import LANES
from .seats import DEFAULT_CONSUMER_POLICIES, SEAT_POOL, _known_free, _restriction, _tier

SCHEMA = "seat-plan/1"
MAX_SLOTS = 32
MAX_CONSTRAINTS = 128
MAX_REFERENCES = 64


class SeatPlanError(ValueError):
    def __init__(self, status_code: int, detail):
        self.status_code = status_code
        self.detail = detail if isinstance(detail, dict) else {"type": "seat_plan_error", "because": str(detail)}
        super().__init__(self.detail.get("because", "seat plan error"))


def _fail(message, status=400):
    raise SeatPlanError(status, message)


def _object(value, name, allowed):
    if not isinstance(value, dict):
        _fail(name + " must be an object")
    unknown = set(value) - set(allowed)
    if unknown:
        _fail("unknown " + name + " field: " + ", ".join(sorted(unknown)))


def _id(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        _fail(name + " must be a nonempty string of at most 128 characters")
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def seat_plan_templates():
    members = [{"id": "member_%d" % i, "roles": ["proposer", "critic"] +
                (["chair"] if i == 1 else []) + (["triage"] if i == 4 else []),
                "binding": {"kind": "auto"}, "lane": "background", "requirements": {}}
               for i in range(1, 5)]
    glass_slots = [{"id": sid, "role": role, "binding": {"kind": "auto"},
                    "lane": "background", "requirements": {"tool_calls": True}}
                   for sid, role in (("review_a", "review"), ("review_b", "review"),
                                     ("fixer", "fix"), ("rebase", "rebase"))]
    return {"schema": SCHEMA, "templates": [
        {"id": "council-roundtable", "version": "1", "name": "Council roundtable",
         "consumer": "private-council", "context": {}, "slots": members, "references": [],
         "constraints": [{"id": "council-diversity", "kind": "all_distinct", "dimension": "weights",
                          "slots": [s["id"] for s in members]}]},
        {"id": "glass-pr-solver", "version": "1", "name": "Glass PR solver",
         "consumer": "glass-solver", "context": {"kind": "pull-request"}, "slots": glass_slots,
         "references": [{"id": "pr-author", "models": [], "revision": ""}],
         "constraints": [{"id": "review-pair", "kind": "all_distinct", "dimension": "weights",
                          "slots": ["review_a", "review_b"]}] + [
             {"id": sid + "-vs-author", "kind": "different_from", "dimension": "weights",
              "slot": sid, "others": [{"reference": "pr-author"}]}
             for sid in ("review_a", "review_b")]}
    ]}


@dataclass(frozen=True)
class _Option:
    assignment: dict
    unknown: tuple[str, ...] = ()

    @property
    def weights(self):
        return self.assignment["weights_id"]

    @property
    def seat(self):
        return self.assignment["seat"] or self.assignment["binding"].get("model_ref")

    @property
    def cost(self):
        return money(self.assignment["estimated_usd"]) or Decimal(0)


class SeatPlanManager:
    def __init__(self, router, search_limit=100_000):
        self.router = router
        if isinstance(search_limit, bool) or not isinstance(search_limit, int) or search_limit < 1:
            raise ValueError("search_limit must be a positive integer")
        self.search_limit = search_limit
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(router.store.path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("CREATE TABLE IF NOT EXISTS seat_plans (id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                         "version INTEGER NOT NULL, t REAL NOT NULL, doc TEXT NOT NULL)")
        self._db.commit()

    @staticmethod
    def templates():
        return seat_plan_templates()

    def _validate(self, raw):
        _object(raw, "seat plan", {"schema", "consumer", "policy", "template", "context", "slots", "references", "constraints"})
        if raw.get("schema") != SCHEMA:
            _fail("schema must be " + SCHEMA)
        consumer = _id(raw.get("consumer"), "consumer")
        configured = getattr(self.router.cfg, "seat_consumer_policies", DEFAULT_CONSUMER_POLICIES).get(consumer)
        if configured not in ("cheapest", "free-only"):
            _fail("no valid seat policy configured for consumer %r" % consumer)
        requested = raw.get("policy", configured)
        if requested not in ("cheapest", "free-only"):
            _fail("policy must be cheapest or free-only")
        result = deepcopy(raw)
        result["policy"] = "free-only" if "free-only" in (configured, requested) else "cheapest"
        context = result.setdefault("context", {})
        _object(context, "context", {"kind", "repo", "number", "head_sha", "revision", "run_id"})
        for key, value in context.items():
            if key == "number":
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    _fail("context.number must be a positive integer")
            elif not isinstance(value, str) or len(value) > 256:
                _fail("context.%s must be a string of at most 256 characters" % key)
        if "template" in result:
            _object(result["template"], "template", {"id", "version"})
            if not any(t["id"] == result["template"].get("id") and t["version"] == result["template"].get("version")
                       for t in self.templates()["templates"]):
                _fail("unknown template or version")
        slots = result.get("slots")
        if not isinstance(slots, list) or not 1 <= len(slots) <= MAX_SLOTS:
            _fail("slots must contain between 1 and %d explicit slots" % MAX_SLOTS)
        ids = set()
        for slot in slots:
            _object(slot, "slot", {"id", "role", "roles", "binding", "requirements", "estimate", "lane"})
            sid = _id(slot.get("id"), "slot.id")
            if sid in ids:
                _fail("duplicate slot id: " + sid)
            ids.add(sid)
            roles = slot.get("roles", [slot.get("role", "general")])
            if not isinstance(roles, list) or not 1 <= len(roles) <= 16:
                _fail("slot.roles must contain 1 to 16 role strings")
            roles = list(dict.fromkeys(_id(role, "role") for role in roles))
            if "role" in slot and slot["role"] not in roles:
                _fail("slot.role must be included in slot.roles")
            slot["roles"], slot["role"] = roles, slot.get("role", roles[0])
            binding = slot.setdefault("binding", {"kind": "auto"})
            kind = binding.get("kind") if isinstance(binding, dict) else None
            allowed = {"auto": {"kind"}, "router-fixed": {"kind", "seat"},
                       "app-fixed": {"kind", "executor_id", "model_ref"}}
            if not isinstance(kind, str) or kind not in allowed:
                _fail("binding.kind must be auto, router-fixed or app-fixed")
            _object(binding, "binding", allowed[kind])
            for key in allowed[kind] - {"kind"}:
                _id(binding.get(key), "binding." + key)
            lane = slot.setdefault("lane", "background")
            if lane not in LANES:
                _fail("unknown slot lane")
            try:
                req = parse_requirements({"router_requirements": slot.get("requirements", {})})
            except ValueError as exc:
                _fail(str(exc))
            slot["requirements"] = req.public()
            estimate = slot.setdefault("estimate", {})
            _object(estimate, "estimate", {"prompt_tokens", "answer_tokens", "max_tokens"})
            for key, value in estimate.items():
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000_000:
                    _fail("estimate.%s must be a positive integer <= 10000000" % key)
        references = result.setdefault("references", [])
        if not isinstance(references, list) or len(references) > MAX_REFERENCES:
            _fail("too many references")
        reference_ids = set()
        for ref in references:
            _object(ref, "reference", {"id", "models", "revision", "evidence_id"})
            rid = _id(ref.get("id"), "reference.id")
            if rid in reference_ids:
                _fail("duplicate reference id")
            reference_ids.add(rid)
            models = ref.get("models", [])
            if not isinstance(models, list) or len(models) > 64:
                _fail("reference.models must contain at most 64 model identifiers")
            for model in models:
                _id(model, "reference model")
            for key in ("revision", "evidence_id"):
                if key in ref and (not isinstance(ref[key], str) or len(ref[key]) > 256):
                    _fail("reference.%s must be a string" % key)
        constraints = result.setdefault("constraints", [])
        if not isinstance(constraints, list) or len(constraints) > MAX_CONSTRAINTS:
            _fail("too many constraints")
        rule_ids = set()
        for rule in constraints:
            kind = rule.get("kind") if isinstance(rule, dict) else None
            allowed = {"all_distinct": {"id", "kind", "dimension", "slots"},
                       "different_from": {"id", "kind", "dimension", "slot", "others"}}
            if not isinstance(kind, str) or kind not in allowed:
                _fail("unknown constraint kind")
            _object(rule, "constraint", allowed[kind])
            rid = _id(rule.get("id"), "constraint.id")
            if rid in rule_ids:
                _fail("duplicate constraint id")
            rule_ids.add(rid)
            if rule.get("dimension") != "weights":
                _fail("constraint.dimension must be weights")
            if kind == "all_distinct":
                members = rule.get("slots")
                if not isinstance(members, list) or len(members) < 2 or len(members) > MAX_SLOTS \
                        or any(not isinstance(s, str) or s not in ids for s in members) or len(set(members)) != len(members):
                    _fail("all_distinct requires at least two unique existing slot IDs")
            else:
                if not isinstance(rule.get("slot"), str) or rule["slot"] not in ids:
                    _fail("different_from subject is not a slot")
                others = rule.get("others")
                if not isinstance(others, list) or not others or len(others) > MAX_SLOTS + MAX_REFERENCES:
                    _fail("different_from.others must contain existing slots or references")
                for other in others:
                    if not isinstance(other, dict) or len(other) != 1 or not (
                        ("slot" in other and isinstance(other["slot"], str) and other["slot"] in ids and other["slot"] != rule["slot"]) or
                        ("reference" in other and isinstance(other["reference"], str) and other["reference"] in reference_ids)):
                        _fail("invalid different_from edge")
        return result

    def _identity(self, seat, candidate=None, metadata=None):
        configured = getattr(self.router.cfg, "seat_model_identities", {}).get(seat) or {}
        extra = metadata or {}
        explicit = getattr(candidate, "weights_id", None)
        weights = configured.get("weights_id") or explicit or extra.get("weights_id")
        if configured.get("weights_id") and explicit and configured["weights_id"] != explicit:
            return None, None, configured.get("family"), "identity_conflict"
        evidence = configured.get("identity_evidence_id") or getattr(candidate, "identity_evidence_id", None) \
            or extra.get("identity_evidence_id")
        if configured.get("weights_id") and not evidence:
            evidence = "operator-model:" + hashlib.sha256(_json(configured).encode()).hexdigest()[:20]
        family = configured.get("family") or getattr(candidate, "family", None) or extra.get("family")
        return weights, evidence, family, None

    def _references(self, request, roster):
        lookup = {c.seat: c for view in roster.values() for c in view}
        refs, unknown = {}, []
        head = request["context"].get("head_sha")
        is_pr = request["context"].get("kind") == "pull-request" or bool(head)
        configured = getattr(self.router.cfg, "seat_reference_models", {})
        for ref in request["references"]:
            registration = configured.get(ref.get("evidence_id")) or {}
            delegated = bool(registration.get("allow_consumer_models") and
                             registration.get("consumer") == request["consumer"])
            models = ref.get("models", []) if delegated or not registration else registration.get("models", [])
            provenance = "consumer-configured" if delegated else "operator-configured" if registration else "consumer-supplied"
            facts = []
            if is_pr and (not head or ref.get("revision") != head):
                facts.append("reference_revision")
            if registration.get("revision") and registration["revision"] != ref.get("revision"):
                facts.append("reference_revision")
            if registration and not delegated and ref.get("models") and set(ref["models"]) != set(models):
                facts.append("reference_models_conflict")
            if is_pr and (not registration or registration.get("verified") is False):
                facts.append("author_provenance")
            if not models:
                facts.append("reference_models")
            weights = set()
            identities = []
            for model in models:
                weight, evidence, family, conflict = self._identity(model, lookup.get(model))
                if not weight:
                    facts.append(conflict or "weights_id")
                else:
                    weights.add(weight)
                identities.append({"model": model, "weights_id": weight, "identity_evidence_id": evidence})
            refs[ref["id"]] = {"weights_ids": sorted(weights), "unknown": sorted(set(facts)),
                "models": identities, "provenance": provenance, "revision": ref.get("revision"),
                "evidence_id": ref.get("evidence_id")}
            if facts:
                unknown.append({"reference_id": ref["id"], "slot_id": None, "seat": None,
                                "facts": sorted(set(facts)), "because": "reference evidence is incomplete or stale"})
        return refs, unknown

    def _ask(self, slot, policy):
        requirements = parse_requirements({"router_requirements": slot["requirements"]})
        ceiling = lower_ceiling(self.router.cfg.ceiling_usd_per_mtok, requirements.max_usd_per_mtok,
                                Decimal(0) if policy == "free-only" else None)
        return Ask(prompt_tokens=slot["estimate"].get("prompt_tokens", 1),
            max_tokens=slot["estimate"].get("max_tokens"), answer_tokens=slot["estimate"].get("answer_tokens"),
            needs_tools=requirements.tool_calls is True, allow_free=self.router.cfg.allow_free,
            lane=slot["lane"], interactive_max_latency_s=self.router.cfg.interactive_max_latency_s,
            max_price_age_s=self.router.cfg.max_price_age_s, requirements=requirements,
            ceiling_usd_per_mtok=ceiling, allow_clamp=False)

    def _assignment(self, slot, request, candidate, *, app=False, cost=None):
        binding = slot["binding"]
        metadata = {}
        if app:
            metadata = getattr(self.router.cfg, "seat_app_executors", {}).get(binding["executor_id"], {}).get(
                "model_metadata", {}).get(binding["model_ref"], {})
        weights, evidence, family, conflict = self._identity(candidate.seat, candidate if not app else None, metadata)
        ask = self._ask(slot, request["policy"])
        requirements = ask.requirements.public()
        if ask.ceiling_usd_per_mtok is not None:
            requirements["max_usd_per_mtok"] = float(ask.ceiling_usd_per_mtok)
        return {"slot_id": slot["id"], "role": slot["role"], "roles": slot["roles"],
            "binding": deepcopy(binding), "seat": None if app else candidate.seat,
            "provider": candidate.provider, "model": candidate.model, "weights_id": weights,
            "identity_evidence_id": evidence, "family": family, "requirements": requirements,
            "lane": slot["lane"], "estimated_usd": None if cost is None else str(cost),
            "tier": None if app else _tier(candidate), "policy": request["policy"],
            "effective_ceiling": None if ask.ceiling_usd_per_mtok is None else float(ask.ceiling_usd_per_mtok),
            "state": "PENDING", "invoke": {"api": "app", "executor_id": binding["executor_id"],
                "model": binding["model_ref"]} if app else {"api": "chat.completions", "model": candidate.seat,
                "base_url": "/v1", "headers": {"X-Router-Lane": slot["lane"], **(
                    {"X-Router-Max-Usd-Per-M": str(ask.ceiling_usd_per_mtok)} if ask.ceiling_usd_per_mtok is not None else {})},
                "router_requirements": requirements}, "identity_conflict": conflict}

    def _app_option(self, slot, request):
        binding = slot["binding"]
        registry = getattr(self.router.cfg, "seat_app_executors", {}).get(binding["executor_id"])
        model = binding["model_ref"]
        if not registry or registry.get("consumer") != request["consumer"] or model not in registry.get("models", []):
            return None, {"slot_id": slot["id"], "seat": model, "executor_id": binding["executor_id"],
                          "verdict": "EXCLUDED", "because": "app executor/model is not registered for this consumer",
                          "unknown": [], "weights_id": None, "tier": None, "estimated_usd": None}
        metadata = registry.get("model_metadata", {}).get(model, {})
        provider, _, name = model.partition(":")
        fields = {k: v for k, v in metadata.items() if k in Candidate.__dataclass_fields__ and k not in ("seat", "provider", "model")}
        for key in ("list_prompt", "list_completion", "billed_prompt", "billed_completion", "list_cached"):
            if key in fields:
                fields[key] = money(fields[key])
        c = Candidate(seat=model, provider=provider, model=name, **fields)
        ask = self._ask(slot, request["policy"])
        checks = check_requirements(ask.requirements, c)
        failures = [r.because for r in checks if r.supported is False]
        missing = [r.unknown_fact for r in checks if r.supported is None]
        if request["policy"] == "free-only":
            if not self.router.cfg.allow_free:
                failures.append("free-only blocked by operator policy")
            elif not _known_free(c):
                if c.list_prompt is None or c.list_completion is None:
                    missing.append("price")
                else:
                    failures.append("free-only requires known zero input and output rates")
        # App prices have no measured-emission requirement: the registered app owns execution.
        cost = None
        if _known_free(c):
            cost = Decimal(0)
        else:
            billed = c.billed_prompt is not None or c.billed_completion is not None
            prompt_rate, completion_rate = (c.billed_prompt, c.billed_completion) if billed else (c.list_prompt, c.list_completion)
            if prompt_rate is None or completion_rate is None:
                if slot["requirements"].get("max_usd_per_mtok") is not None:
                    missing.append("price")
            else:
                tokens = slot["estimate"]
                cost = (money(prompt_rate) * tokens.get("prompt_tokens", 1) +
                        money(completion_rate) * tokens.get("answer_tokens", tokens.get("max_tokens", 512))) / Decimal(1_000_000)
                if ask.ceiling_usd_per_mtok is not None:
                    blended = cost * Decimal(1_000_000) / (tokens.get("prompt_tokens", 1) +
                        tokens.get("answer_tokens", tokens.get("max_tokens", 512)))
                    if blended > ask.ceiling_usd_per_mtok:
                        failures.append("app model price exceeds effective ceiling")
        a = self._assignment(slot, request, c, app=True, cost=cost)
        if not a["weights_id"]:
            missing.append(a.pop("identity_conflict") or "weights_id")
        else:
            a.pop("identity_conflict")
        verdict = "EXCLUDED" if failures else "UNKNOWN" if missing else "QUALIFIES"
        row = {"slot_id": slot["id"], "seat": model, "executor_id": binding["executor_id"],
            "verdict": verdict, "because": "; ".join(failures or missing) or "registered app executor with explicit model identity",
            "unknown": sorted(set(missing)), "weights_id": a["weights_id"], "tier": None,
            "estimated_usd": a["estimated_usd"]}
        return (None if failures else _Option(a, tuple(sorted(set(missing))))), row

    @staticmethod
    def _edges(request):
        edges, references = set(), {}
        for rule in request["constraints"]:
            if rule["kind"] == "all_distinct":
                edges.update(tuple(sorted(pair)) for pair in itertools.combinations(rule["slots"], 2))
            else:
                for other in rule["others"]:
                    if "slot" in other:
                        edges.add(tuple(sorted((rule["slot"], other["slot"]))))
                    else:
                        references.setdefault(rule["slot"], set()).add(other["reference"])
        return edges, references

    def _search(self, domains, edges, exclusions, histories, optimistic=False, budget=None):
        ids = list(domains)
        neighbors = {sid: set() for sid in ids}
        for left, right in edges:
            neighbors[left].add(right)
            neighbors[right].add(left)
        visited, best, best_score = 0, None, None
        limit_hit = False
        budget = self.search_limit if budget is None else budget

        def identities(sid, option):
            return set(histories.get(sid, ())) | {option.weights or "unknown-route:" + option.seat}

        def allowed(sid, option, assignment):
            own = identities(sid, option)
            if own & exclusions.get(sid, set()):
                return False
            for neighbor in neighbors[sid]:
                if own & set(histories.get(neighbor, ())):
                    return False
                if neighbor in assignment and own & identities(neighbor, assignment[neighbor]):
                    return False
            return True

        def score(options):
            return tuple(sum(o.assignment["tier"] == tier for o in options) for tier in reversed(range(len(SEAT_POOL)))) + \
                (sum((o.cost for o in options), Decimal(0)),)

        def visit(assignment):
            nonlocal visited, best, best_score, limit_hit
            if visited >= budget:
                limit_hit = True
                return
            visited += 1
            if len(assignment) == len(ids):
                rank = score(assignment.values())
                tie = tuple(assignment[sid].seat for sid in ids)
                full_score = rank + (tie,)
                if best_score is None or full_score < best_score:
                    best, best_score = dict(assignment), full_score
                return
            remaining = {}
            for sid in ids:
                if sid not in assignment:
                    remaining[sid] = [o for o in domains[sid] if allowed(sid, o, assignment)]
                    if not remaining[sid]:
                        return
            # A safe lower bound cannot assume that independent minima are feasible.
            if best_score is not None and not optimistic:
                lower = score(list(assignment.values()) + [min(opts, key=lambda o: (
                    tuple(int(o.assignment["tier"] == tier) for tier in reversed(range(len(SEAT_POOL)))), o.cost, o.seat))
                    for opts in remaining.values()])
                optimistic_tie = tuple(assignment[s].seat if s in assignment else min(o.seat for o in remaining[s])
                                       for s in ids)
                if lower + (optimistic_tie,) >= best_score:
                    return
            sid = min(remaining, key=lambda key: (len(remaining[key]), -len(neighbors[key]), ids.index(key)))
            for option in sorted(remaining[sid], key=lambda o: (o.assignment["tier"] if o.assignment["tier"] is not None else -1,
                                                               o.cost, o.seat)):
                assignment[sid] = option
                visit(assignment)
                assignment.pop(sid)
                if limit_hit or (optimistic and best is not None):
                    break
        visit({})
        return best, limit_hit, visited

    def _solve(self, request, *, existing=None, failed_slots=()):
        lanes = sorted({slot["lane"] for slot in request["slots"]})
        roster = {lane: tuple(self.router.gather.roster(lane)) for lane in lanes}
        refs, unknown = self._references(request, roster)
        snapshot_id = "snapshot-" + hashlib.sha256(_json({"roster": {lane: [c.__dict__ for c in view]
            for lane, view in roster.items()}, "references": refs,
            "identities": getattr(self.router.cfg, "seat_model_identities", {}),
            "executors": getattr(self.router.cfg, "seat_app_executors", {})}).encode()).hexdigest()[:24]
        edges, reference_edges = self._edges(request)
        histories = {}
        if existing:
            for participant in existing.get("participants", []):
                histories.setdefault(participant["slot_id"], set()).add(participant["weights_id"])
        exclusions = {sid: {w for rid in rids for w in refs[rid]["weights_ids"]}
                      for sid, rids in reference_edges.items()}
        domains, optimistic_domains, considered = {}, {}, []
        previous = {a["slot_id"]: a for a in existing["response"]["assignments"]} if existing else {}
        for slot in request["slots"]:
            sid = slot["id"]
            domains[sid], optimistic_domains[sid] = [], []
            old = previous.get(sid)
            pinned = old and old["state"] in ("RUNNING", "ANSWERED", "COMPLETED")
            if pinned:
                option = _Option(deepcopy(old))
                domains[sid].append(option)
                optimistic_domains[sid].append(option)
                considered.append({"slot_id": sid, "seat": option.seat, "executor_id": None,
                    "verdict": "QUALIFIES", "because": "retained running/completed participant",
                    "unknown": [], "weights_id": option.weights, "tier": old["tier"], "estimated_usd": old["estimated_usd"]})
                continue
            if slot["binding"]["kind"] == "app-fixed":
                option, row = self._app_option(slot, request)
                options = [] if option is None else [option]
                considered.append(row)
            else:
                options = []
                ask = self._ask(slot, request["policy"])
                for c in roster[slot["lane"]]:
                    if slot["binding"]["kind"] == "router-fixed" and c.seat != slot["binding"]["seat"]:
                        continue
                    reason = _restriction(c, {"exclude_seats": [], "exclude_families": [], "policy": request["policy"]},
                                          self.router.cfg.allow_free)
                    checked = replace(c, blocked=reason) if reason else c
                    assessment = assess(ask, checked)
                    a = self._assignment(slot, request, c, cost=assessment.expected_usd)
                    missing = list(assessment.unknown)
                    if not a["weights_id"]:
                        missing.append(a.pop("identity_conflict") or "weights_id")
                    else:
                        a.pop("identity_conflict")
                    verdict = assessment.verdict.value
                    if verdict == "QUALIFIES" and missing:
                        verdict = "UNKNOWN"
                    because = assessment.because
                    if missing:
                        because += "; unknown " + ", ".join(sorted(set(missing)))
                    considered.append({"slot_id": sid, "seat": c.seat, "executor_id": None, "verdict": verdict,
                        "because": because, "unknown": sorted(set(missing)), "weights_id": a["weights_id"],
                        "tier": a["tier"], "estimated_usd": a["estimated_usd"]})
                    if assessment.verdict is not Verdict.EXCLUDED:
                        options.append(_Option(a, tuple(sorted(set(missing)))))
            for option in options:
                if old and sid in failed_slots and (option.seat == (old["seat"] or old["binding"].get("model_ref"))
                                                     or option.weights == old["weights_id"]):
                    continue
                ref_unknown = ["reference:" + rid + ":" + fact for rid in reference_edges.get(sid, ())
                               for fact in refs[rid]["unknown"]]
                if option.unknown:
                    unknown.append({"slot_id": sid, "reference_id": None, "seat": option.seat,
                        "facts": list(option.unknown), "because": "required candidate evidence is unknown"})
                optimistic_domains[sid].append(option)
                if not option.unknown and not ref_unknown:
                    domains[sid].append(option)
        found, limit_hit, states = self._search(domains, edges, exclusions, histories)
        outcome = "SEARCH_LIMIT" if limit_hit else "RESOLVED" if found else "UNSATISFIABLE"
        if outcome == "UNSATISFIABLE" and unknown:
            optimistic_exclusions = {sid: {w for rid in rids if not refs[rid]["unknown"] for w in refs[rid]["weights_ids"]}
                                     for sid, rids in reference_edges.items()}
            potential, uncertain_limit, extra_states = self._search(optimistic_domains, edges, optimistic_exclusions,
                histories, optimistic=True, budget=self.search_limit - states)
            states += extra_states
            outcome = "SEARCH_LIMIT" if uncertain_limit else "UNKNOWN" if potential else "UNSATISFIABLE"
        because = {"RESOLVED": "complete assignment; global provider-tier ordering then lowest established estimated cost",
            "UNKNOWN": "missing identity, provenance, capability or price evidence prevents a certified complete plan",
            "UNSATISFIABLE": "complete constraint search established no eligible assignment",
            "SEARCH_LIMIT": "bounded search ended before complete feasibility and ranking were established"}[outcome]
        assignments = [deepcopy(found[slot["id"]].assignment) for slot in request["slots"]] if outcome == "RESOLVED" else []
        evidence = []
        for rule in request["constraints"]:
            pairs = []
            raw_pairs = [(a, {"slot": b}) for a, b in itertools.combinations(rule["slots"], 2)] \
                if rule["kind"] == "all_distinct" else [(rule["slot"], other) for other in rule["others"]]
            for left, right in raw_pairs:
                lw = found[left].weights if assignments else None
                rw = ([found[right["slot"]].weights] if assignments else []) if "slot" in right else refs[right["reference"]]["weights_ids"]
                pairs.append({"left": {"slot": left}, "right": right, "left_weights_id": lw,
                              "right_weights_ids": rw, "satisfied": (lw not in rw) if assignments else None})
            evidence.append({"id": rule["id"], "kind": rule["kind"], "dimension": "weights",
                             "satisfied": True if assignments else None, "pairs": pairs})
        known = sum((money(a["estimated_usd"]) for a in assignments if a["estimated_usd"] is not None), Decimal(0))
        total = str(known) if assignments and all(a["estimated_usd"] is not None for a in assignments) else None
        response = {"schema": SCHEMA, "outcome": outcome, "because": because, "plan_id": None, "version": None,
            "snapshot_id": snapshot_id, "consumer": request["consumer"], "context": request["context"],
            "policy": request["policy"], "assignments": assignments, "constraint_evidence": evidence,
            "unknown": unknown, "considered": considered, "references": refs,
            "estimates": {"scope": "one invocation per slot; excludes retries and additional rounds",
                          "known_subtotal_usd": str(known), "total_usd": total},
            "search": {"states": states, "limit": self.search_limit, "ranking_proven": outcome == "RESOLVED"}}
        return response

    @staticmethod
    def _decorate(response, plan_id, version):
        response["plan_id"], response["version"] = plan_id, version
        for a in response["assignments"]:
            if a["invoke"]["api"] == "chat.completions":
                a["invoke"]["path"] = "/v1/seat-plans/%s/slots/%s/chat/completions" % (quote(plan_id, safe=""), quote(a["slot_id"], safe=""))
            a["invoke"]["version"] = version

    async def resolve(self, body, owner):
        request = self._validate(body)
        _id(owner, "owner")
        response = await asyncio.to_thread(self._solve, request)
        if response["outcome"] != "RESOLVED":
            return response
        pid = "plan-" + uuid.uuid4().hex
        self._decorate(response, pid, 1)
        doc = {"request": request, "response": response, "participants": [], "receipts": []}
        with self._lock:
            self._db.execute("INSERT INTO seat_plans VALUES (?,?,?,?,?)", (pid, owner, 1, time.time(), _json(doc)))
            self._db.commit()
        return deepcopy(response)

    def _load(self, plan_id, owner):
        row = self._db.execute("SELECT doc FROM seat_plans WHERE id=? AND owner=?", (plan_id, owner)).fetchone()
        if not row:
            _fail("seat plan not found", 404)
        return json.loads(row["doc"])

    def get(self, plan_id, owner):
        with self._lock:
            doc = self._load(plan_id, owner)
        result = deepcopy(doc["response"])
        result["participants"], result["receipts"] = doc["participants"], doc["receipts"]
        return result

    @staticmethod
    def _check_version_context(doc, body):
        version = body.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version != doc["response"]["version"]:
            _fail("seat plan version changed", 409)
        supplied = body.get("context", {})
        if supplied != doc["request"]["context"]:
            _fail("seat plan context or PR head changed", 409)

    async def reselect(self, plan_id, body, owner):
        _object(body, "reselect", {"version", "context", "failed_slots"})
        with self._lock:
            doc = self._load(plan_id, owner)
            self._check_version_context(doc, body)
        try:
            request = self._validate(doc["request"])
        except SeatPlanError as exc:
            _fail("stored plan no longer satisfies current operator configuration: " + str(exc), 409)
        failed = body.get("failed_slots", [])
        ids = {s["id"] for s in doc["request"]["slots"]}
        if not isinstance(failed, list) or len(failed) > MAX_SLOTS or any(not isinstance(s, str) or s not in ids for s in failed):
            _fail("failed_slots must contain existing slot IDs")
        previous = {a["slot_id"]: a for a in doc["response"]["assignments"]}
        if any(previous[s]["state"] in ("RUNNING", "ANSWERED", "COMPLETED") for s in failed):
            _fail("cannot reselect running or completed slots", 409)
        response = await asyncio.to_thread(self._solve, request, existing=doc, failed_slots=failed)
        if response["outcome"] != "RESOLVED":
            response["previous_plan_id"] = plan_id
            return response
        version = body["version"] + 1
        self._decorate(response, plan_id, version)
        updated = {**doc, "request": request, "response": response}
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                live = self._load(plan_id, owner)
                # Participation can change without a version bump while search runs.
                if live != doc:
                    _fail("seat plan participation changed during re-selection; retry", 409)
                changed = self._db.execute("UPDATE seat_plans SET version=?,t=?,doc=? WHERE id=? AND owner=? AND version=?",
                    (version, time.time(), _json(updated), plan_id, owner, body["version"])).rowcount
                if changed != 1:
                    _fail("seat plan version changed", 409)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return deepcopy(response)

    @staticmethod
    def _merge_requirements(base, request):
        supplied = request.get("router_requirements", {})
        try:
            incoming = parse_requirements(request).public()
        except ValueError as exc:
            _fail(str(exc))
        merged = deepcopy(base)
        for key in ("providers", "model_types"):
            if supplied.get(key):
                if base.get(key) and not set(incoming[key]).issubset(base[key]):
                    _fail("invocation weakens " + key, 409)
                merged[key] = incoming[key]
        if supplied.get("required_capabilities"):
            if not set(base.get("required_capabilities", [])).issubset(incoming["required_capabilities"]):
                _fail("invocation removes required capabilities", 409)
            merged["required_capabilities"] = incoming["required_capabilities"]
        for key in ("tool_calls", "editable"):
            if supplied.get(key) is not None:
                if base.get(key) is not None and incoming[key] != base[key]:
                    _fail("invocation weakens " + key, 409)
                merged[key] = incoming[key]
        minimum = incoming["min_context_tokens"]
        if minimum is not None:
            if base.get("min_context_tokens") is not None and minimum < base["min_context_tokens"]:
                _fail("invocation weakens minimum context", 409)
            merged["min_context_tokens"] = minimum
        price = incoming["max_usd_per_mtok"]
        if price is not None:
            if base.get("max_usd_per_mtok") is not None and price > base["max_usd_per_mtok"]:
                _fail("invocation weakens price ceiling", 409)
            merged["max_usd_per_mtok"] = price
        effort = incoming["thinking_level"]
        if effort is not None:
            levels = ("none", "low", "medium", "high")
            if base.get("thinking_level") is not None and levels.index(effort) < levels.index(base["thinking_level"]):
                _fail("invocation weakens thinking level", 409)
            merged["thinking_level"] = effort
        return merged

    async def prepare_invoke(self, plan_id, slot_id, body, owner):
        return await asyncio.to_thread(self._prepare_invoke, plan_id, slot_id, body, owner)

    def _prepare_invoke(self, plan_id, slot_id, body, owner):
        _object(body, "invoke", {"version", "context", "request"})
        request = body.get("request")
        if not isinstance(request, dict):
            _fail("invoke.request must be an object")
        forbidden = {"models", "route", "plugins", "provider"} & set(request)
        forbidden.update(key for key in request if key.startswith("router_") and key != "router_requirements")
        if forbidden:
            _fail("pinned invocation cannot use model fallback or untrusted router controls: " + ", ".join(sorted(forbidden)), 409)
        with self._lock:
            doc = self._load(plan_id, owner)
            self._check_version_context(doc, body)
            assignments = {a["slot_id"]: a for a in doc["response"]["assignments"]}
            a = assignments.get(slot_id)
            if not a:
                _fail("seat plan slot not found", 404)
            if a["binding"]["kind"] == "app-fixed":
                _fail("app-fixed slot must use its registered app executor", 409)
            if request.get("model") not in (None, a["seat"], a["model"]):
                _fail("invocation model differs from pinned assignment", 409)
            if a["state"] == "RUNNING":
                _fail("seat plan slot is already running", 409)
            current_policy = getattr(self.router.cfg, "seat_consumer_policies", {}).get(doc["request"]["consumer"])
            if current_policy not in ("cheapest", "free-only") or (current_policy == "free-only" and doc["request"]["policy"] != "free-only"):
                _fail("operator consumer policy changed; resolve a new plan", 409)
            pinned = deepcopy(request)
            pinned["model"] = a["seat"]
            pinned["router_requirements"] = self._merge_requirements(a["requirements"], request)
            pinned["router_expected_weights_id"] = a["weights_id"]
            if doc["request"]["consumer"] == "glass-solver" and a["provider"] == "openrouter":
                pinned["provider"] = {"ignore": ["Cerebras", "Groq"]}
            # Actual supplied tools/history/effort must also satisfy the merged contract.
            try:
                ask = self.router._ask(pinned, a["effective_ceiling"], a["lane"])
            except ValueError as exc:
                _fail(str(exc), 409)
            candidates = tuple(self.router.gather.roster(a["lane"]))
            references, _ = self._references(doc["request"], {a["lane"]: candidates})
            _, reference_edges = self._edges(doc["request"])
            relevant = {rid for rids in reference_edges.values() for rid in rids}
            if any(references[rid] != doc["response"]["references"][rid] for rid in relevant):
                _fail("pinned reference identity or provenance changed; resolve a new plan", 409)
            live = next((c for c in candidates if c.seat == a["seat"]), None)
            if live is None:
                _fail("pinned model is absent from current roster", 409)
            weights, _, _, conflict = self._identity(a["seat"], live)
            if conflict or weights != a["weights_id"]:
                _fail("pinned model weight identity changed or became unknown", 409)
            policy = doc["request"]["policy"]
            reason = _restriction(live, {"exclude_seats": [], "exclude_families": [], "policy": policy}, self.router.cfg.allow_free)
            checked = assess(replace(ask, allow_clamp=False), replace(live, blocked=reason) if reason else live)
            if checked.verdict is not Verdict.QUALIFIES:
                _fail("pinned model no longer qualifies: " + checked.because, 409)
            self._db.execute("BEGIN IMMEDIATE")
            try:
                current = self._load(plan_id, owner)
                if current != doc:
                    _fail("seat plan changed before invocation", 409)
                execution_id = "execution-" + uuid.uuid4().hex
                a["state"] = "RUNNING"
                doc["participants"].append({"slot_id": slot_id, "seat": a["seat"], "weights_id": a["weights_id"],
                    "identity_evidence_id": a["identity_evidence_id"], "version": body["version"],
                    "status": "RUNNING", "execution_id": execution_id})
                self._db.execute("UPDATE seat_plans SET t=?,doc=? WHERE id=? AND owner=?", (time.time(), _json(doc), plan_id, owner))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            metadata = deepcopy(a)
            metadata.update({"plan_id": plan_id, "version": body["version"], "only_seat": a["seat"],
                             "execution_id": execution_id, "snapshot_id": doc["response"]["snapshot_id"]})
            return pinned, metadata

    def record_execution(self, plan_id, slot_id, owner, *, version, status, decision_id=None, receipt=None):
        if status not in ("ANSWERED", "FAILED", "COMPLETED"):
            _fail("execution status must be ANSWERED, FAILED or COMPLETED")
        safe_keys = {"execution_id", "cost_usd", "cost_usd_exact", "cost_basis", "prompt_tokens", "completion_tokens",
                     "cached_tokens", "latency_s", "http", "finish_reason", "calls", "status"}
        safe = {k: v for k, v in (receipt or {}).items() if k in safe_keys and isinstance(v, (str, int, float, bool, type(None)))}
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                doc = self._load(plan_id, owner)
                a = next((a for a in doc["response"]["assignments"] if a["slot_id"] == slot_id), None)
                if a is None:
                    _fail("seat plan slot not found", 404)
                active = next((p for p in reversed(doc["participants"]) if p["slot_id"] == slot_id and p["status"] == "RUNNING"), None)
                if active is None:
                    _fail("no running execution for this slot", 409)
                if active["version"] != version or a["state"] != "RUNNING" or active["weights_id"] != a["weights_id"] \
                        or active["seat"] != a["seat"]:
                    _fail("execution no longer matches the retained running assignment", 409)
                if safe.get("execution_id") and safe["execution_id"] != active["execution_id"]:
                    _fail("execution ID does not match running slot", 409)
                active["status"], a["state"] = status, status
                record = {"slot_id": slot_id, "weights_id": active["weights_id"], "identity_evidence_id": active["identity_evidence_id"],
                    "execution_id": active["execution_id"], "version": version, "status": status, "decision_id": decision_id,
                    "receipt": safe}
                doc["receipts"].append(record)
                self._db.execute("UPDATE seat_plans SET t=?,doc=? WHERE id=? AND owner=?", (time.time(), _json(doc), plan_id, owner))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return deepcopy(record)
