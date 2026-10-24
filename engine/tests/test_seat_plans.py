"""Complete seat graphs, durable ownership and pinned execution."""
import asyncio
from dataclasses import replace
from decimal import Decimal
import json
import itertools
from types import SimpleNamespace

import pytest

from modelrouter.config import Config
from modelrouter.decision import Candidate, Emits
from modelrouter.router import Router
from modelrouter.secrets import Keyring
from modelrouter.store import Store


def call(coro):
    return asyncio.run(coro)


def candidate(model, price="0.1", provider="sail", **changes):
    return replace(Candidate(seat=f"{provider}:{model}", provider=provider, model=model,
        list_prompt=Decimal(price), list_completion=Decimal(price), context_length=65536,
        supports_tools=True, emits=Emits.CONTENT, min_max_tokens=1,
        has_reasoning_field=False, price_age_s=0, observation_count=3), **changes)


@pytest.fixture
def router(tmp_path, monkeypatch):
    cfg = Config(path=tmp_path / "router.toml", source="env", state_dir=tmp_path,
                 dashboard_url="", allow_free=True)
    cfg.seat_model_identities = {}
    cfg.seat_reference_models = {}
    cfg.seat_app_executors = {}
    r = Router(cfg, Keyring("env", {}), Store(tmp_path / "state.sqlite3"))
    r.test_roster = []
    r.roster_calls = []
    def roster(lane="background"):
        r.roster_calls.append(lane)
        return r.test_roster
    monkeypatch.setattr(r.gather, "roster", roster)
    def forbidden(*args, **kwargs):
        pytest.fail("resolution called a provider")
    monkeypatch.setattr("modelrouter.providers.call", forbidden)
    return r


def manager(router, **options):
    from modelrouter.seat_plans import SeatPlanManager
    return SeatPlanManager(router, **options)


def identify(router, **models):
    router.cfg.seat_model_identities.update({name: {"weights_id": weights,
        "identity_evidence_id": "operator:" + name} for name, weights in models.items()})


def request(count=2, consumer="private-council"):
    return {"schema": "seat-plan/1", "consumer": consumer, "slots": [
        {"id": f"s{i}", "roles": ["proposer", "critic"], "binding": {"kind": "auto"}}
        for i in range(count)], "constraints": [{"id": "group", "kind": "all_distinct",
        "dimension": "weights", "slots": [f"s{i}" for i in range(count)]}]}


def test_complete_assignment_escapes_greedy_and_tier_dead_end(router):
    router.test_roster = [candidate("a", "0.01"), candidate("b", "1", "openrouter")]
    identify(router, **{"sail:a": "A", "openrouter:b": "B"})
    body = request()
    body["slots"][1]["binding"] = {"kind": "router-fixed", "seat": "sail:a"}
    resolved = call(manager(router).resolve(body, "alice"))
    assert resolved["outcome"] == "RESOLVED"
    assert [a["weights_id"] for a in resolved["assignments"]] == ["B", "A"]
    assert router.roster_calls == ["background"]
    assert all(e["satisfied"] for e in resolved["constraint_evidence"])


def test_aliases_quantizations_and_provider_routes_do_not_create_weights(router):
    router.test_roster = [candidate("a"), candidate("a-int4", provider="openrouter")]
    identify(router, **{"sail:a": "A", "openrouter:a-int4": "A"})
    result = call(manager(router).resolve(request(), "alice"))
    assert result["outcome"] == "UNSATISFIABLE"
    assert result["assignments"] == [] and result["plan_id"] is None


def test_unknown_identity_and_search_limit_are_not_shortages(router):
    router.test_roster = [candidate("a"), candidate("b")]
    identify(router, **{"sail:a": "A"})
    unknown = call(manager(router).resolve(request(), "alice"))
    assert unknown["outcome"] == "UNKNOWN"
    assert any("weights_id" in u["facts"] for u in unknown["unknown"])
    identify(router, **{"sail:b": "B"})
    limited = call(manager(router, search_limit=1).resolve(request(), "alice"))
    assert limited["outcome"] == "SEARCH_LIMIT" and limited["assignments"] == []


def test_overlapping_groups_allow_nonadjacent_reuse_and_duplicate_refs(router):
    router.test_roster = [candidate("a"), candidate("b"), candidate("c")]
    identify(router, **{"sail:a": "A", "sail:b": "B", "sail:c": "C"})
    body = request(4)
    body["constraints"] = [
        {"id": "pair", "kind": "all_distinct", "dimension": "weights", "slots": ["s0", "s1"]},
        {"id": "triple", "kind": "all_distinct", "dimension": "weights", "slots": ["s1", "s2", "s3"]},
        {"id": "references", "kind": "different_from", "dimension": "weights", "slot": "s0",
         "others": [{"reference": "author"}, {"reference": "fixer"}]}]
    body["slots"][2]["binding"] = {"kind": "router-fixed", "seat": "sail:a"}
    body["references"] = [{"id": "author", "models": ["sail:a"]},
                          {"id": "fixer", "models": ["sail:a"]}]
    r = call(manager(router).resolve(body, "alice"))
    assert r["outcome"] == "RESOLVED"
    a = {a["slot_id"]: a["weights_id"] for a in r["assignments"]}
    assert a["s0"] == a["s3"] and len({a[k] for k in ("s1", "s2", "s3")}) == 3


def test_pr_provenance_requires_registered_exact_head_or_delegation(router):
    router.test_roster = [candidate("a"), candidate("b")]
    identify(router, **{"sail:a": "A", "sail:b": "B", "external:author": "AUTHOR"})
    body = request()
    body["context"] = {"kind": "pull-request", "head_sha": "head-one"}
    body["references"] = [{"id": "pr-author", "models": ["external:author"],
        "revision": "head-one", "evidence_id": "glass-author"}]
    body["constraints"].append({"id": "author", "kind": "different_from", "dimension": "weights",
        "slot": "s0", "others": [{"reference": "pr-author"}]})
    m = manager(router)
    assert call(m.resolve(body, "alice"))["outcome"] == "UNKNOWN"
    router.cfg.seat_reference_models = {"glass-author": {"consumer": "private-council",
        "allow_consumer_models": True, "source": "operator-configured"}}
    r = call(m.resolve(body, "alice"))
    assert r["outcome"] == "RESOLVED"
    body["references"][0]["revision"] = "older-head"
    assert call(m.resolve(body, "alice"))["outcome"] == "UNKNOWN"


def test_policy_floor_and_app_fixed_unknown_cost(router):
    router.test_roster = [candidate("a")]
    identify(router, **{"sail:a": "A", "codex:fixed": "F"})
    router.cfg.seat_app_executors = {"codex-ui": {"consumer": "private-council", "models": ["codex:fixed"]}}
    body = request()
    body["slots"][1]["binding"] = {"kind": "app-fixed", "executor_id": "codex-ui", "model_ref": "codex:fixed"}
    r = call(manager(router).resolve(body, "alice"))
    assert r["outcome"] == "RESOLVED" and r["estimates"]["total_usd"] is None
    assert r["assignments"][1]["invoke"]["api"] == "app"
    body["policy"] = "free-only"
    assert call(manager(router).resolve(body, "alice"))["outcome"] != "RESOLVED"
    free = request(1, "public-council")
    free["constraints"] = []
    free["policy"] = "cheapest"
    assert call(manager(router).resolve(free, "alice"))["outcome"] == "UNSATISFIABLE"


def test_durable_owner_pinning_and_reselect_retains_participants(router):
    from modelrouter.seat_plans import SeatPlanError
    router.test_roster = [candidate("a"), candidate("b", "0.2"), candidate("c", "0.3")]
    identify(router, **{"sail:a": "A", "sail:b": "B", "sail:c": "C"})
    m = manager(router)
    r = call(m.resolve(request(), "alice"))
    pid = r["plan_id"]
    assert manager(router).get(pid, "alice")["assignments"] == r["assignments"]
    with pytest.raises(SeatPlanError) as forbidden:
        m.get(pid, "bob")
    assert forbidden.value.status_code == 404
    first = r["assignments"][0]
    envelope = {"version": 1, "context": {}, "request": {"model": first["model"],
        "messages": [{"role": "user", "content": "PRIVATE PROMPT"}]}}
    req, meta = call(m.prepare_invoke(pid, first["slot_id"], envelope, "alice"))
    assert req["model"] == first["seat"] and meta["only_seat"] == first["seat"]
    assert req["router_requirements"] == first["requirements"]
    m.record_execution(pid, first["slot_id"], "alice", version=1, status="ANSWERED", receipt={"content": "PRIVATE ANSWER"})
    updated = call(m.reselect(pid, {"version": 1, "context": {}, "failed_slots": ["s1"]}, "alice"))
    assert updated["outcome"] == "RESOLVED" and updated["version"] == 2
    assert updated["assignments"][0]["weights_id"] == first["weights_id"]
    assert updated["assignments"][1]["weights_id"] != first["weights_id"]
    assert updated["assignments"][1]["seat"] != r["assignments"][1]["seat"]
    db = m._db.execute("SELECT doc FROM seat_plans").fetchone()[0]
    assert "PRIVATE PROMPT" not in db and "PRIVATE ANSWER" not in db
    with pytest.raises(SeatPlanError) as stale:
        call(m.prepare_invoke(pid, "s1", envelope, "alice"))
    assert stale.value.status_code == 409


def test_invoke_rejects_head_model_and_weakened_requirements(router):
    from modelrouter.seat_plans import SeatPlanError
    router.test_roster = [candidate("a")]
    identify(router, **{"sail:a": "A"})
    body = request(1)
    body["constraints"] = []
    body["context"] = {"kind": "pull-request", "head_sha": "head-one"}
    body["slots"][0]["requirements"] = {"tool_calls": True, "min_context_tokens": 8192, "max_usd_per_mtok": 1}
    m = manager(router)
    r = call(m.resolve(body, "alice"))
    for envelope in [
        {"version": 1, "context": {"head_sha": "head-two"}, "request": {}},
        {"version": 1, "context": body["context"], "request": {"model": "other"}},
        {"version": 1, "context": body["context"], "request": {"router_requirements": {"tool_calls": False}}},
        {"version": 1, "context": body["context"], "request": {"router_requirements": {"min_context_tokens": 1}}},
        {"version": 1, "context": body["context"], "request": {"router_requirements": {"max_usd_per_mtok": 2}}}
    ]:
        with pytest.raises(SeatPlanError):
            call(m.prepare_invoke(r["plan_id"], "s0", envelope, "alice"))


def test_running_execution_finishes_after_other_slot_is_reselected(router):
    router.test_roster = [candidate("a"), candidate("b", "0.2"), candidate("c", "0.3")]
    identify(router, **{"sail:a": "A", "sail:b": "B", "sail:c": "C"})
    m = manager(router)
    plan = call(m.resolve(request(), "alice"))
    _, active = call(m.prepare_invoke(plan["plan_id"], "s0", {"version": 1, "context": {}, "request": {}}, "alice"))
    updated = call(m.reselect(plan["plan_id"], {"version": 1, "context": {}, "failed_slots": ["s1"]}, "alice"))
    assert updated["version"] == 2 and updated["assignments"][0]["state"] == "RUNNING"
    receipt = m.record_execution(plan["plan_id"], "s0", "alice", version=1, status="ANSWERED",
                                 receipt={"execution_id": active["execution_id"]})
    assert receipt["version"] == 1 and m.get(plan["plan_id"], "alice")["assignments"][0]["state"] == "ANSWERED"


@pytest.mark.parametrize("extra", [{"models": ["other"]}, {"route": "fallback"},
    {"plugins": [{"id": "auto-router"}]}, {"router_expected_weights_id": "other"}, {"router_lane": "batch"}])
def test_pinned_invocation_rejects_fallback_and_untrusted_router_controls(router, extra):
    from modelrouter.seat_plans import SeatPlanError
    router.test_roster = [candidate("a")]
    identify(router, **{"sail:a": "A"})
    body = request(1)
    body["constraints"] = []
    m = manager(router)
    plan = call(m.resolve(body, "alice"))
    with pytest.raises(SeatPlanError) as error:
        call(m.prepare_invoke(plan["plan_id"], "s0", {"version": 1, "context": {}, "request": extra}, "alice"))
    assert error.value.status_code in (400, 409)
    assert m.get(plan["plan_id"], "alice")["participants"] == []


def test_stale_reference_is_unknown_even_if_supplied_old_model_excludes_all_candidates(router):
    router.test_roster = [candidate("a")]
    identify(router, **{"sail:a": "A"})
    router.cfg.seat_reference_models = {"author": {"models": ["sail:a"], "revision": "old"}}
    body = request(1)
    body["context"] = {"kind": "pull-request", "head_sha": "new"}
    body["references"] = [{"id": "author", "models": ["sail:a"], "revision": "old", "evidence_id": "author"}]
    body["constraints"] = [{"id": "vs", "kind": "different_from", "dimension": "weights",
                             "slot": "s0", "others": [{"reference": "author"}]}]
    assert call(manager(router).resolve(body, "alice"))["outcome"] == "UNKNOWN"


def test_unknown_candidate_does_not_hide_proven_structural_shortage(router):
    router.test_roster = [candidate("a"), candidate("unmeasured", emits=None)]
    identify(router, **{"sail:a": "A", "sail:unmeasured": "B"})
    r = call(manager(router).resolve(request(3), "alice"))
    assert r["outcome"] == "UNSATISFIABLE"


def test_reselection_retains_failed_participant_identity_on_original_group_edges(router):
    router.test_roster = [candidate("a"), candidate("b", "0.2"), candidate("c", "0.3")]
    identify(router, **{"sail:a": "A", "sail:b": "B", "sail:c": "C"})
    m = manager(router)
    first = call(m.resolve(request(), "alice"))
    _, invoked = call(m.prepare_invoke(first["plan_id"], "s0", {"version": 1, "context": {}, "request": {}}, "alice"))
    m.record_execution(first["plan_id"], "s0", "alice", version=1, status="FAILED",
                       receipt={"execution_id": invoked["execution_id"]})
    updated = call(m.reselect(first["plan_id"], {"version": 1, "context": {}, "failed_slots": ["s0"]}, "alice"))
    assert updated["outcome"] == "RESOLVED"
    assert all(a["weights_id"] != invoked["weights_id"] for a in updated["assignments"])
    assert m.get(first["plan_id"], "alice")["participants"][0]["weights_id"] == invoked["weights_id"]


def test_explicit_app_budget_needs_price_evidence(router):
    router.test_roster = [candidate("a")]
    identify(router, **{"sail:a": "A", "codex:fixed": "F"})
    router.cfg.seat_app_executors = {"external": {"consumer": "private-council", "models": ["codex:fixed"]}}
    body = request()
    body["slots"][1]["binding"] = {"kind": "app-fixed", "executor_id": "external", "model_ref": "codex:fixed"}
    body["slots"][1]["requirements"] = {"max_usd_per_mtok": 1}
    result = call(manager(router).resolve(body, "alice"))
    assert result["outcome"] == "UNKNOWN"
    assert any("price" in item["facts"] for item in result["unknown"])


def test_app_prices_use_billed_legs_before_advertised_rates(router):
    identify(router, **{"codex:fixed": "F"})
    router.cfg.seat_app_executors = {"external": {"consumer": "private-council", "models": ["codex:fixed"],
        "model_metadata": {"codex:fixed": {"list_prompt": "0.1", "list_completion": "0.1",
                                             "billed_prompt": "9", "billed_completion": "9"}}}}
    body = request(1)
    body["constraints"] = []
    body["slots"][0]["binding"] = {"kind": "app-fixed", "executor_id": "external", "model_ref": "codex:fixed"}
    result = call(manager(router).resolve(body, "alice"))
    assert result["outcome"] == "UNSATISFIABLE"


def test_pinned_reference_and_operator_policy_changes_require_new_advice(router):
    from modelrouter.seat_plans import SeatPlanError
    router.test_roster = [candidate("a")]
    identify(router, **{"sail:a": "A", "external:author": "AUTHOR"})
    body = request(1)
    body["context"] = {"kind": "pull-request", "head_sha": "head"}
    body["references"] = [{"id": "author", "models": ["external:author"], "revision": "head", "evidence_id": "author"}]
    router.cfg.seat_reference_models = {"author": {"models": ["external:author"], "revision": "head"}}
    body["constraints"] = [{"id": "vs", "kind": "different_from", "dimension": "weights",
        "slot": "s0", "others": [{"reference": "author"}]}]
    m = manager(router)
    r = call(m.resolve(body, "alice"))
    identify(router, **{"external:author": "A"})
    with pytest.raises(SeatPlanError):
        call(m.prepare_invoke(r["plan_id"], "s0", {"version": 1, "context": body["context"], "request": {}}, "alice"))
    identify(router, **{"external:author": "AUTHOR"})
    router.cfg.seat_consumer_policies["private-council"] = "free-only"
    with pytest.raises(SeatPlanError):
        call(m.prepare_invoke(r["plan_id"], "s0", {"version": 1, "context": body["context"], "request": {}}, "alice"))


def test_invocation_paths_encode_custom_slot_ids(router):
    router.test_roster = [candidate("a")]
    identify(router, **{"sail:a": "A"})
    body = request(1)
    body["constraints"] = []
    body["slots"][0]["id"] = "review/a #1"
    r = call(manager(router).resolve(body, "alice"))
    assert "/slots/review%2Fa%20%231/chat/completions" in r["assignments"][0]["invoke"]["path"]


def test_global_ranking_matches_exhaustive_small_graphs(router):
    router.test_roster = [candidate("a", "0.3"), candidate("b", "0.1"), candidate("c", "0.01", "openrouter")]
    identify(router, **{"sail:a": "A", "sail:b": "B", "openrouter:c": "C"})
    routes = {c.seat: c for c in router.test_roster}
    possible_edges = [("s0", "s1"), ("s0", "s2"), ("s1", "s2")]
    m = manager(router)
    for bits in itertools.product((False, True), repeat=3):
        edges = [edge for edge, present in zip(possible_edges, bits) if present]
        body = request(3)
        body["constraints"] = [{"id": "e%d" % i, "kind": "all_distinct", "dimension": "weights", "slots": list(edge)}
                               for i, edge in enumerate(edges)]
        valid = [combo for combo in itertools.product(routes, repeat=3)
                 if all(combo[int(a[1])] != combo[int(b[1])] for a, b in edges)]
        # Sail and OpenRouter share the paid tier, so price alone ranks them.
        expected = min(valid, key=lambda combo: (sum(routes[s].list_prompt for s in combo), combo))
        result = call(m.resolve(body, "alice"))
        assert result["outcome"] == "RESOLVED"
        assert tuple(a["seat"] for a in result["assignments"]) == expected


def test_equal_free_cohort_proves_ranking_without_enumerating_all_permutations(router):
    router.test_roster = [candidate("free-%02d" % i, "0") for i in range(20)]
    identify(router, **{c.seat: "W%d" % i for i, c in enumerate(router.test_roster)})
    result = call(manager(router, search_limit=5000).resolve(request(6, "public-council"), "alice"))
    assert result["outcome"] == "RESOLVED"
    assert result["search"]["ranking_proven"]
    assert [a["seat"] for a in result["assignments"]] == ["sail:free-%02d" % i for i in range(6)]


def test_reselect_reassesses_pending_fixed_slots_and_current_policy(router):
    router.test_roster = [candidate("a")]
    identify(router, **{"sail:a": "A"})
    body = request(1)
    body["constraints"] = []
    body["slots"][0]["binding"] = {"kind": "router-fixed", "seat": "sail:a"}
    m = manager(router)
    r = call(m.resolve(body, "alice"))
    router.test_roster = [replace(router.test_roster[0], available=False)]
    result = call(m.reselect(r["plan_id"], {"version": 1, "context": {}, "failed_slots": []}, "alice"))
    assert result["outcome"] == "UNSATISFIABLE"
    router.test_roster = [candidate("a")]
    router.cfg.seat_consumer_policies["private-council"] = "free-only"
    result = call(m.reselect(r["plan_id"], {"version": 1, "context": {}, "failed_slots": []}, "alice"))
    assert result["outcome"] == "UNSATISFIABLE"


def test_glass_openrouter_reserved_upstreams_are_pinned_by_operator(router):
    from modelrouter.seat_plans import SeatPlanError
    router.test_roster = [candidate("a", provider="openrouter")]
    identify(router, **{"openrouter:a": "A"})
    body = request(1, "glass-solver")
    body["constraints"] = []
    m = manager(router)
    r = call(m.resolve(body, "alice"))
    with pytest.raises(SeatPlanError):
        call(m.prepare_invoke(r["plan_id"], "s0", {"version": 1, "context": {},
            "request": {"provider": {"only": ["Groq"]}}}, "alice"))
    pinned, _ = call(m.prepare_invoke(r["plan_id"], "s0", {"version": 1, "context": {}, "request": {}}, "alice"))
    assert pinned["provider"] == {"ignore": ["Cerebras", "Groq"]}


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(task="private"),
    lambda b: b["slots"][0].update(estimate={"prompt_tokens": 0}),
    lambda b: b["slots"][1].update(id="s0"),
    lambda b: b["constraints"][0].update(slots=["missing", "s1"]),
])
def test_invalid_requests_never_gather(router, mutate):
    from modelrouter.seat_plans import SeatPlanError
    body = request()
    mutate(body)
    with pytest.raises((SeatPlanError, ValueError)):
        call(manager(router).resolve(body, "alice"))
    assert router.roster_calls == []


@pytest.mark.parametrize("mutate", [
    lambda b: b["slots"][0].update(binding={"kind": []}),
    lambda b: b["constraints"][0].update(kind=[]),
    lambda b: b["constraints"][0].update(slots=[{}, "s1"]),
    lambda b: b["constraints"].append({"id": "bad", "kind": "different_from", "dimension": "weights",
        "slot": "s0", "others": [{"slot": {}}]}),
])
def test_malformed_identifier_types_are_validation_errors(router, mutate):
    from modelrouter.seat_plans import SeatPlanError
    body = request()
    mutate(body)
    with pytest.raises(SeatPlanError) as error:
        call(manager(router).resolve(body, "alice"))
    assert error.value.status_code == 400
    assert router.roster_calls == []
