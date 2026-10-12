"""Seat advice uses real decision rules and never invokes a model."""
from dataclasses import replace
from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient

from modelrouter.config import Config, load
from modelrouter.decision import Candidate, Emits
from modelrouter.router import Router
from modelrouter.secrets import Keyring
from modelrouter.seats import SeatUnavailable, resolve_seat
from modelrouter.server import build
from modelrouter.store import Store


def candidate(provider="sail", model="one", price="0.1", family="known", **changes):
    base = Candidate(seat=f"{provider}:{model}", provider=provider, model=model,
                     family=family, list_prompt=Decimal(price), list_completion=Decimal(price),
                     context_length=8192, supports_tools=True, emits=Emits.CONTENT,
                     min_max_tokens=1, has_reasoning_field=False, price_age_s=0,
                     observation_count=3)
    return replace(base, **changes)


@pytest.fixture
def router(tmp_path, monkeypatch):
    cfg = Config(path=tmp_path / "router.toml", source="env", state_dir=tmp_path,
                 dashboard_url="", allow_free=True)
    cfg.seat_consumer_policies = {"glass-solver": "cheapest", "private-council": "cheapest",
                                 "public-council": "free-only"}
    router = Router(cfg, Keyring("env", {}), Store(tmp_path / "state.sqlite3"))
    router.test_roster = []
    monkeypatch.setattr(router.gather, "roster", lambda lane="interactive": router.test_roster)

    def forbidden(*args, **kwargs):
        pytest.fail("advisory seat resolution made a model call")
    monkeypatch.setattr("modelrouter.providers.call", forbidden)
    monkeypatch.setattr("modelrouter.providers.stream", forbidden)
    return router


def test_seat_prefers_first_eligible_tier_and_cheapest_within_it(router):
    router.test_roster = [candidate("openrouter", "cheap", "0.01"),
                          candidate("sail", "dear", "1"), candidate("sail", "cheap", "0.2")]
    result = resolve_seat(router, {"consumer": "glass-solver", "role": "review"})
    assert result["seat"] == "sail:cheap"
    assert result["tier"] == 0 and result["provider"] == "sail"
    assert isinstance(result["usd_per_mtok"], str)
    assert Decimal(result["usd_per_mtok"]) == Decimal("0.2")
    assert {key: result["invoke"][key] for key in ("api", "model", "base_url")} == {
        "api": "chat.completions", "model": "sail:cheap", "base_url": "/v1"}
    assert result["invoke"]["headers"] == {"X-Router-Lane": "background", "X-Router-Max-Usd-Per-M": "5.0"}
    assert result["invoke"]["router_requirements"]["required_capabilities"] == []
    assert result["jev"]["scope"] == "measured-text-api-seats"
    assert result["jev"]["confidence"]["probability"] is None
    assert "tier" in result["because"]
    late = next(c for c in result["considered"] if c["seat"] == "openrouter:cheap")
    assert late["tier"] == 4 and late["verdict"] == "EXCLUDED"
    assert set(late) >= {"seat", "verdict", "because", "tier", "family", "usd_per_mtok"}


def test_seat_falls_back_only_when_earlier_tier_cannot_qualify(router):
    router.test_roster = [candidate("sail", emits=None), candidate("openrouter", "fallback")]
    result = resolve_seat(router, {"consumer": "glass-solver"})
    assert result["seat"] == "openrouter:fallback" and result["tier"] == 4
    assert next(c for c in result["considered"] if c["seat"] == "sail:one")["verdict"] == "UNKNOWN"


@pytest.mark.parametrize("consumer", ["public-council", "glass-solver"])
def test_free_only_never_falls_back_to_paid_and_records_abstention(router, consumer):
    router.test_roster = [candidate("sail", price="0.1"), candidate("openrouter", price="0.01")]
    with pytest.raises(SeatUnavailable) as unavailable:
        resolve_seat(router, {"consumer": consumer, "policy": "free-only"})
    detail = unavailable.value.detail
    assert detail["type"] == "seat_unavailable" and detail["policy"] == "free-only"
    assert detail["decision_id"] and len(detail["considered"]) == 2
    record = router.store.get(detail["decision_id"])
    assert record["status"] == "ADVISED" and record["choice"]["outcome"] == "ABSTAIN"
    assert record["result"]["calls"] == 0


def test_free_only_selects_known_zero_two_leg_price(router):
    router.test_roster = [candidate("sail", "paid", "0.1"),
                          candidate("openrouter", "free", "0")]
    result = resolve_seat(router, {"consumer": "public-council", "policy": "cheapest"})
    assert result["policy"] == "free-only"
    assert result["seat"] == "openrouter:free" and Decimal(result["usd_per_mtok"]) == 0


@pytest.mark.parametrize("changes", [
    {"list_prompt": None}, {"list_completion": None},
    {"list_prompt": None, "list_completion": None, "measured_usd_per_mtok": Decimal(0)},
    {"billed_prompt": Decimal("0.1"), "billed_completion": Decimal("0.1")},
    {"measured_usd_per_mtok": Decimal("0.1"), "measured_basis": "billed"},
    {"measured_usd_per_mtok": Decimal("0.1"), "measured_basis": "billed", "billing_ratio": Decimal(1)},
])
def test_free_only_rejects_unknown_or_nonzero_actual_prices(router, changes):
    router.test_roster = [candidate("openrouter", "model:free", "0", **changes)]
    with pytest.raises(SeatUnavailable):
        resolve_seat(router, {"consumer": "public-council"})


def test_free_only_cannot_override_operator_policy(router):
    router.cfg.allow_free = False
    router.test_roster = [candidate("openrouter", "free", "0")]
    with pytest.raises(SeatUnavailable) as unavailable:
        resolve_seat(router, {"consumer": "public-council"})
    assert "operator" in unavailable.value.detail["considered"][0]["because"]


def test_seat_constraints_reuse_capability_context_and_price_assessment(router):
    router.test_roster = [candidate("sail", "no-tools", supports_tools=False),
                          candidate("sail", "small", context_length=1024),
                          candidate("sail", "expensive", "3"),
                          candidate("openrouter", "sufficient", "0.2")]
    result = resolve_seat(router, {"consumer": "glass-solver", "need": {"tools": True,
                          "min_context": 4096}, "max_usd_per_m": 0.5, "task": "review code"})
    assert result["seat"] == "openrouter:sufficient"
    assert all(row["verdict"] == "EXCLUDED" for row in result["considered"] if row["tier"] == 0)


def test_seat_advice_uses_background_lane_without_unmeasured_latency_gate(router):
    router.cfg.interactive_max_latency_s = 0.001
    router.test_roster = [candidate(latency_s=None, decode_tps=None)]
    result = resolve_seat(router, {"consumer": "private-council", "role": "council"})
    assert result["seat"] == "sail:one"
    assert router.store.get(result["decision_id"])["choice"]["facts"]["lane"] == "background"


def test_seat_invoke_preserves_balanced_lane_and_request_constraints(router, monkeypatch):
    router.cfg.token_secret = "SEAT_INVOKE_TOKEN"
    monkeypatch.setenv("SEAT_INVOKE_TOKEN", "invoke-token")
    app = build(router.cfg)
    balanced = candidate("sail", "balanced-only", "0.1", window="balanced")

    def roster(lane="interactive"):
        return [balanced if lane == "background" else
                replace(balanced, blocked="ASAP unavailable: seat is balanced-only")]
    monkeypatch.setattr(app.state.router.gather, "roster", roster)
    client = TestClient(app)
    auth = {"Authorization": "Bearer invoke-token"}
    advised = client.post("/v1/seats/resolve", headers=auth, json={
        "consumer": "glass-solver", "need": {"tools": True, "min_context": 4096},
        "max_usd_per_m": 0.5, "task": "review code"})
    assert advised.status_code == 200, advised.text
    invocation = advised.json()["invoke"]
    body = {"model": invocation["model"], "messages": [{"role": "user", "content": "review code"}]}
    if "router_requirements" in invocation:
        body["router_requirements"] = invocation["router_requirements"]
    explained = client.post("/router/explain", json=body,
                            headers={**auth, **invocation.get("headers", {})})
    assert explained.status_code == 200, explained.text
    choice = explained.json()["choice"]
    assert choice["outcome"] == "ROUTE", choice["because"]
    assert choice["seat"] == "sail:balanced-only"
    assert choice["facts"]["lane"] == "background"
    assert choice["facts"]["ceiling_usd_per_mtok"] == 0.5
    assert choice["facts"]["router_requirements"]["tool_calls"] is True
    assert choice["facts"]["router_requirements"]["min_context_tokens"] == 4096


def test_free_only_invoke_retains_zero_price_ceiling(router):
    router.test_roster = [candidate("openrouter", "free", "0")]
    result = resolve_seat(router, {"consumer": "public-council"})
    assert Decimal(result["invoke"].get("headers", {}).get("X-Router-Max-Usd-Per-M", "-1")) == 0


def test_family_exclusions_use_explicit_evidence_and_treat_unknown_as_conflict(router):
    router.test_roster = [candidate("sail", "vendor/LooksDifferent", family="openai"),
                          candidate("sail", "vendor/SeemsKnown", family=None),
                          candidate("openrouter", "vendor/LooksOpenAI", family="deepseek")]
    result = resolve_seat(router, {"consumer": "glass-solver", "exclude_families": ["openai"]})
    assert result["family"] == "deepseek" and result["seat"] == "openrouter:vendor/LooksOpenAI"
    unknown = next(c for c in result["considered"] if c["family"] is None)
    assert unknown["verdict"] == "EXCLUDED" and "unknown" in unknown["because"]


def test_explicit_seat_exclusion_and_unsupported_executors_are_never_selected(router):
    router.test_roster = [candidate("sail", "excluded"), candidate("codex", "fake", "0"),
                          candidate("antigravity", "fake", "0"), candidate("local", "fake", "0"),
                          candidate("anthropic", "fake", "0"), candidate("cerebras", "fake", "0"),
                          candidate("openrouter", "fallback")]
    result = resolve_seat(router, {"consumer": "glass-solver", "exclude_seats": ["sail:excluded"]})
    assert result["seat"] == "openrouter:fallback"
    assert len(result["considered"]) == 7


@pytest.mark.parametrize("family", ["claude", "anthropic", "cerebras"])
def test_excluded_known_families_cannot_be_selected_through_openrouter(router, family):
    router.test_roster = [candidate("openrouter", "model", family=family)]
    with pytest.raises(SeatUnavailable):
        resolve_seat(router, {"consumer": "glass-solver"})


def test_role_is_metadata_and_task_text_is_never_persisted(router):
    task = "private task text that must not appear in the record"
    router.test_roster = [candidate()]
    result = resolve_seat(router, {"consumer": "glass-solver", "role": "fix", "task": task})
    record = router.store.get(result["decision_id"])
    assert record["requested"] == "seat:glass-solver:fix"
    assert record["status"] == "ADVISED" and record["attempts"] == []
    assert record["choice"]["facts"]["seat_role"] == "fix"
    assert record["result"]["calls"] == 0 and record["result"]["cost_usd"] == 0
    assert task not in json.dumps(record) and "messages" not in record
    assert record["choice"]["confidence"]["probability"] is None


def test_unknown_consumer_rejected_even_when_it_requests_policy(router):
    with pytest.raises(ValueError, match="consumer"):
        resolve_seat(router, {"consumer": "stranger", "policy": "cheapest"})
    assert router.store.recent() == []
    router.cfg.seat_consumer_policies["stranger"] = "free-only"
    router.test_roster = [candidate(price="0")]
    assert resolve_seat(router, {"consumer": "stranger", "policy": "cheapest"})["policy"] == "free-only"


def test_seat_consumer_config_preserves_defaults_and_validates_policies(tmp_path):
    path = tmp_path / "router.toml"
    path.write_text('[secrets]\nsource="env"\n[providers.openrouter]\nsecret="TEST_KEY"\n'
                    '[seat_consumers]\ncustom="free-only"\nbad="paid"\n', encoding="utf-8")
    cfg = load(path)
    assert cfg.seat_consumer_policies["public-council"] == "free-only"
    assert cfg.seat_consumer_policies["glass-solver"] == "cheapest"
    assert cfg.seat_consumer_policies["custom"] == "free-only"
    assert "bad" not in cfg.seat_consumer_policies
    assert any("seat_consumers.bad" in problem for problem in cfg.problems)


def test_published_seat_http_contract_requires_operator_and_maps_unavailable(router, monkeypatch):
    router.cfg.token_secret = "SEAT_OPERATOR_TOKEN"
    monkeypatch.setenv("SEAT_OPERATOR_TOKEN", "operator-token")
    app = build(router.cfg)
    monkeypatch.setattr(app.state.router.gather, "roster", lambda lane="interactive": router.test_roster)
    client = TestClient(app)
    body = {"consumer": "glass-solver", "role": "review", "need": {"tools": True}}
    assert client.post("/v1/seats/resolve", json=body).status_code == 401
    router.test_roster = [candidate()]
    headers = {"Authorization": "Bearer operator-token"}
    result = client.post("/v1/seats/resolve", json=body, headers=headers)
    assert result.status_code == 200, result.text
    payload = result.json()
    assert set(payload) == {"policy", "seat", "provider", "family", "tier", "cost_basis",
                            "usd_per_mtok", "invoke", "because", "jev", "decision_id", "considered"}
    assert app.state.router.store.get(payload["decision_id"])["status"] == "ADVISED"
    free = client.post("/v1/seats/resolve", json={"consumer": "public-council"}, headers=headers)
    assert free.status_code == 422, free.text
    assert free.json()["detail"]["type"] == "seat_unavailable"
    assert free.json()["detail"]["policy"] == "free-only"
    invalid = client.post("/v1/seats/resolve", json={"consumer": "unknown"}, headers=headers)
    assert invalid.status_code == 400


@pytest.mark.parametrize("raw", [
    {}, [], {"consumer": ""}, {"consumer": "x" * 65}, {"consumer": True},
    {"consumer": "glass-solver", "role": "deploy"},
    {"consumer": "glass-solver", "policy": "paid"},
    {"consumer": "glass-solver", "extra": True},
    {"consumer": "glass-solver", "task": "x" * 4001},
    {"consumer": "glass-solver", "task": 10},
    {"consumer": "glass-solver", "exclude_families": [""]},
    {"consumer": "glass-solver", "exclude_families": ["family"] * 17},
    {"consumer": "glass-solver", "exclude_seats": ["seat"] * 33},
    {"consumer": "glass-solver", "exclude_seats": "seat"},
    {"consumer": "glass-solver", "need": []},
    {"consumer": "glass-solver", "need": {"tools": 1}},
    {"consumer": "glass-solver", "need": {"min_context": 0}},
    {"consumer": "glass-solver", "need": {"min_context": True}},
    {"consumer": "glass-solver", "need": {"max_context": 2}},
    {"consumer": "glass-solver", "max_usd_per_m": 0},
    {"consumer": "glass-solver", "max_usd_per_m": -1},
    {"consumer": "glass-solver", "max_usd_per_m": True},
    {"consumer": "glass-solver", "max_usd_per_m": float("inf")},
    {"consumer": "glass-solver", "max_usd_per_m": float("nan")},
])
def test_invalid_seat_request_is_rejected_before_gather_or_record(router, monkeypatch, raw):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid request accessed roster")
    monkeypatch.setattr(router.gather, "roster", forbidden)
    with pytest.raises(ValueError):
        resolve_seat(router, raw)
    assert router.store.recent() == []
