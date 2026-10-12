"""Explicit request filters, honest unknowns, and the facts recorded for routing."""
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient

from modelrouter import providers as P
from modelrouter.config import Config
from modelrouter.decision import Ask, Candidate, Emits, Outcome, Verdict, assess, decide
from modelrouter.requirements import RoutingRequirements, parse_requirements
from modelrouter.server import build


def candidate(model="plain", provider="akashml", price="0.10", **overrides):
    facts = dict(seat=f"{provider}:{model}", provider=provider, model=model,
                 context_length=32768, supports_tools=False,
                 supports_reasoning=False, supports_editing=False,
                 supported_parameters=(), input_modalities=("text",),
                 output_modalities=("text",), list_prompt=Decimal(price),
                 list_completion=Decimal(price), emits=Emits.CONTENT,
                 min_max_tokens=1, price_source="provider-catalogue",
                 has_reasoning_field=False)
    return Candidate(**{**facts, **overrides})


def request_ask(filters=None, **overrides):
    return Ask(**{**dict(prompt_tokens=10, max_tokens=32,
                        requirements=parse_requirements({"router_requirements": filters or {}})),
                  **overrides})


CHAT_TOOL = {"type": "function", "function": {"name": "lookup",
             "parameters": {"type": "object", "properties": {}}}}
RESPONSE_TOOL = {"type": "function", "name": "lookup",
                 "parameters": {"type": "object", "properties": {}}}


class Upstream:
    def __init__(self):
        self.calls = []
        self.streams = []
        self.fail = set()

    @staticmethod
    def payload(model):
        return {"id": "chatcmpl-requirements", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Ready"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}}

    def __call__(self, provider, model, key, req, *, max_tokens, timeout=180, window=None):
        self.calls.append((provider, model, deepcopy(req), max_tokens))
        if f"{provider}:{model}" in self.fail:
            return P.Result(False, 500, provider, model, detail="test model failure", scope="model")
        result = P.extract(provider, model, self.payload(model))
        result.latency_s = 0.01
        return result

    def stream(self, provider, model, key, req, *, max_tokens, on_done, timeout=180, window=None):
        self.streams.append((provider, model, deepcopy(req), max_tokens))

        def events():
            yield b'data: {"choices":[{"index":0,"delta":{"content":"Ready"},"finish_reason":null}]}\n\n'
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"
            result = P.extract(provider, model, self.payload(model))
            result.latency_s = 0.01
            on_done(result)

        return events()


@dataclass
class Harness:
    client: TestClient
    upstream: Upstream
    roster: list[Candidate]

    @property
    def router(self):
        return self.client.app.state.router


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIREMENTS_AKASH_KEY", "test-akash-key")
    monkeypatch.setenv("REQUIREMENTS_OPENROUTER_KEY", "test-openrouter-key")
    upstream = Upstream()
    monkeypatch.setattr(P, "call", upstream)
    monkeypatch.setattr(P, "stream", upstream.stream)
    # No catalogue reads or live probes: every test supplies its complete decision facts.
    monkeypatch.setattr(P, "fetch_catalogue", lambda *args, **kwargs: [])
    cfg = Config(path=tmp_path / "requirements.toml", source="env", no_auth=True,
                 secrets={"akashml": "REQUIREMENTS_AKASH_KEY",
                          "openrouter": "REQUIREMENTS_OPENROUTER_KEY"},
                 state_dir=tmp_path, dashboard_url="", interactive_max_latency_s=None)
    client = TestClient(build(cfg))
    roster = [candidate()]
    monkeypatch.setattr(client.app.state.router.gather, "roster", lambda lane="interactive": list(roster))

    def listing(seat, window=None):
        c = next((c for c in roster if c.seat == seat), None)
        return None if c is None else P.Listing(c.provider, c.model, c.list_prompt,
                                               c.list_completion, context_length=c.context_length)

    monkeypatch.setattr(client.app.state.router.gather, "listing", listing)
    return Harness(client, upstream, roster)


def post(app, path="/v1/chat/completions", filters=None, **extra):
    body = {"model": "auto", "max_tokens": 32,
            "messages": [{"role": "user", "content": "private test prompt"}]}
    if path == "/v1/responses":
        body = {"model": "auto", "max_output_tokens": 32, "input": "private test prompt"}
    if filters is not None:
        body["router_requirements"] = filters
    return app.client.post(path, json={**body, **extra})


def test_parsing_returns_typed_filters_and_preserves_all_supported_options():
    req = {"router_requirements": {
        "providers": ["akashml", "openrouter"], "model_types": ["text", "reasoning", "vision"],
        "required_capabilities": ["tools", "reasoning", "structured_output", "editing"],
        "thinking_level": "high", "editable": True, "tool_calls": True,
        "min_context_tokens": 8192, "max_usd_per_mtok": 0,
    }}
    requirements = parse_requirements(req)
    assert isinstance(requirements, RoutingRequirements)
    assert set(requirements.providers) == {"akashml", "openrouter"}
    assert set(requirements.model_types) == {"text", "reasoning", "vision"}
    assert set(requirements.required_capabilities) == {"tools", "reasoning", "structured_output", "editing"}
    assert requirements.thinking_level == "high"
    assert requirements.editable is True and requirements.tool_calls is True
    assert requirements.min_context_tokens == 8192 and requirements.max_usd_per_mtok == 0
    assert req["router_requirements"]["providers"] == ["akashml", "openrouter"]


def test_parsing_preserves_explicit_false_and_zero():
    requirements = parse_requirements({"router_requirements": {
        "editable": False, "tool_calls": False, "max_usd_per_mtok": 0}})
    assert requirements.editable is False and requirements.tool_calls is False
    assert requirements.max_usd_per_mtok == 0


INVALID_FILTERS = [
    [], "text", 1, True,
    {"unknown_filter": True},
    {"providers": "akashml"}, {"providers": [123]}, {"providers": [""]},
    {"model_types": "text"}, {"model_types": ["embedding"]},
    {"required_capabilities": "tools"}, {"required_capabilities": ["telepathy"]},
    {"thinking_level": "extreme"}, {"thinking_level": True},
    {"editable": "false"}, {"editable": 0}, {"tool_calls": "true"}, {"tool_calls": 1},
    {"min_context_tokens": 0}, {"min_context_tokens": -1}, {"min_context_tokens": 12.5},
    {"min_context_tokens": True}, {"min_context_tokens": "1024"},
    {"max_usd_per_mtok": -0.1}, {"max_usd_per_mtok": True}, {"max_usd_per_mtok": "0.2"},
    {"tool_calls": False, "required_capabilities": ["tools"]},
    {"editable": False, "required_capabilities": ["editing"]},
]


@pytest.mark.parametrize("filters", INVALID_FILTERS)
@pytest.mark.parametrize("path", ["/router/explain", "/v1/chat/completions", "/v1/responses"])
def test_malformed_requirements_are_http_400_before_upstream_calls(app, filters, path):
    response = post(app, path, filters)
    assert response.status_code == 400, response.text
    assert "router_requirements" in response.text
    assert app.upstream.calls == [] and app.upstream.streams == []


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_price_filter_rejects_nonfinite_numbers(value):
    with pytest.raises(ValueError):
        parse_requirements({"router_requirements": {"max_usd_per_mtok": value}})


@pytest.mark.parametrize("path", ["/router/explain", "/v1/chat/completions", "/v1/responses"])
def test_null_requirements_are_an_invalid_filter_object(app, path):
    response = post(app, path, router_requirements=None)
    assert response.status_code == 400, response.text
    assert app.upstream.calls == [] and app.upstream.streams == []


@pytest.mark.parametrize("effort,filters", [("extreme", {}), ("low", {"thinking_level": "high"})])
@pytest.mark.parametrize("path", ["/router/explain", "/v1/chat/completions", "/v1/responses"])
def test_native_effort_is_validated_and_cannot_conflict_with_filter(app, effort, filters, path):
    response = post(app, path, filters, reasoning_effort=effort)
    assert response.status_code == 400, response.text
    assert app.upstream.calls == [] and app.upstream.streams == []


@pytest.mark.parametrize("path", ["/router/explain", "/v1/chat/completions"])
@pytest.mark.parametrize("field,value", [
    ("messages", "not a message list"), ("messages", ["not a message object"]),
    ("messages", [{"role": "not-a-chat-role", "content": "hi"}]),
    ("max_tokens", True), ("max_tokens", 0), ("max_completion_tokens", "32"),
])
def test_invalid_message_shapes_and_token_budgets_are_http_400(app, path, field, value):
    response = post(app, path, **{field: value})
    assert response.status_code == 400, response.text
    assert app.upstream.calls == [] and app.upstream.streams == []


@pytest.mark.parametrize("path", ["/router/explain", "/v1/chat/completions", "/v1/responses"])
@pytest.mark.parametrize("content", ['{"model":', "[]"])
def test_malformed_json_and_nonobject_bodies_are_http_400(app, path, content):
    response = app.client.post(path, content=content, headers={"Content-Type": "application/json"})
    assert response.status_code == 400, response.text
    assert app.upstream.calls == [] and app.upstream.streams == []


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
def test_invalid_filters_do_not_reserve_caller_funds_or_consume_request_limit(app, monkeypatch, path):
    keys = app.client.app.state.client_keys
    value, key = keys.create("requirements-caller", Decimal("1"), rpm=1)
    app.client.headers["Authorization"] = "Bearer " + value
    reserved = []
    reserve = keys.reserve

    def track_reserve(*args, **kwargs):
        reserved.append(True)
        return reserve(*args, **kwargs)

    monkeypatch.setattr(keys, "reserve", track_reserve)
    invalid = post(app, path, {"required_capabilities": ["telepathy"]})
    assert invalid.status_code == 400, invalid.text
    assert reserved == [] and app.upstream.calls == []
    assert keys.get(key.id).spent_usd == 0 and keys.get(key.id).calls == 0
    valid = post(app, path)
    assert valid.status_code == 200, valid.text
    assert len(reserved) == 1 and len(app.upstream.calls) == 1


def test_provider_filter_excludes_cheaper_provider():
    cheap = candidate("cheap", price="0.01")
    permitted = candidate("permitted", provider="openrouter", price="0.20")
    choice = decide(request_ask({"providers": ["openrouter"]}), [cheap, permitted])
    assert choice.seat == permitted.seat
    assert next(a for a in choice.considered if a.seat == cheap.seat).verdict is Verdict.EXCLUDED


@pytest.mark.parametrize("model_type,matching,excluded,unknown", [
    ("text", {}, {"input_modalities": ("image",), "output_modalities": ("image",)},
     {"input_modalities": None}),
    ("reasoning", {"supports_reasoning": True}, {"supports_reasoning": False},
     {"supports_reasoning": None, "supported_parameters": None}),
    ("vision", {"input_modalities": ("text", "image")}, {"input_modalities": ("text",)},
     {"input_modalities": None}),
])
def test_model_types_use_explicit_facts(model_type, matching, excluded, unknown):
    ask = request_ask({"model_types": [model_type]})
    assert assess(ask, candidate(**matching)).verdict is Verdict.QUALIFIES
    assert assess(ask, candidate(**excluded)).verdict is Verdict.EXCLUDED
    verdict = assess(ask, candidate(**unknown))
    assert verdict.verdict is Verdict.UNKNOWN and verdict.unknown


def test_text_type_requires_known_text_output():
    ask = request_ask({"model_types": ["text"]})
    assert assess(ask, candidate(output_modalities=None)).verdict is Verdict.UNKNOWN
    assert assess(ask, candidate(output_modalities=("image",))).verdict is Verdict.EXCLUDED


def test_text_and_reasoning_types_can_overlap_and_alternatives_are_allowed():
    c = candidate(supports_reasoning=True)
    for types in (["text"], ["reasoning"], ["text", "reasoning"]):
        assert assess(request_ask({"model_types": types}), c).verdict is Verdict.QUALIFIES


@pytest.mark.parametrize("capability,field,match,mismatch", [
    ("tools", "supports_tools", True, False),
    ("reasoning", "supports_reasoning", True, False),
    ("editing", "supports_editing", True, False),
    ("structured_output", "supported_parameters", ("response_format",), ()),
])
def test_required_capabilities_match_exclude_or_stay_unknown(capability, field, match, mismatch):
    ask = request_ask({"required_capabilities": [capability]})
    assert assess(ask, candidate(**{field: match})).verdict is Verdict.QUALIFIES
    assert assess(ask, candidate(**{field: mismatch})).verdict is Verdict.EXCLUDED
    missing = {field: None}
    if field in ("supports_tools", "supports_reasoning"):
        missing["supported_parameters"] = None
    unknown = assess(ask, candidate(**missing))
    assert unknown.verdict is Verdict.UNKNOWN and unknown.unknown


def test_all_required_capabilities_must_match_before_price_can_win():
    partial = candidate("partial", price="0.01", supports_tools=True)
    complete = candidate("complete", price="0.20", supports_tools=True,
                         supports_reasoning=True, supports_editing=True,
                         supported_parameters=("response_format",))
    choice = decide(request_ask({"required_capabilities": [
        "tools", "reasoning", "editing", "structured_output"]}), [partial, complete])
    assert choice.seat == complete.seat


@pytest.mark.parametrize("filter_name,fact", [("editable", "supports_editing"),
                                             ("tool_calls", "supports_tools")])
@pytest.mark.parametrize("required", [True, False])
def test_boolean_filters_are_exact_and_do_not_assume_unknown_is_false(filter_name, fact, required):
    ask = request_ask({filter_name: required})
    assert assess(ask, candidate(**{fact: required})).verdict is Verdict.QUALIFIES
    assert assess(ask, candidate(**{fact: not required})).verdict is Verdict.EXCLUDED
    missing = {fact: None}
    if fact == "supports_tools":
        missing["supported_parameters"] = None
    assert assess(ask, candidate(**missing)).verdict is Verdict.UNKNOWN


@pytest.mark.parametrize("level", ["none", "low", "medium", "high"])
def test_thinking_level_needs_explicit_parameter_support(level):
    ask = request_ask({"thinking_level": level})
    c = candidate(supports_reasoning=True, supported_parameters=("reasoning_effort",))
    assert assess(ask, c).verdict is Verdict.QUALIFIES
    assert assess(ask, replace(c, supported_parameters=())).verdict is Verdict.EXCLUDED
    assert assess(ask, replace(c, supported_parameters=None)).verdict is Verdict.UNKNOWN


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_positive_thinking_levels_require_explicit_reasoning_capability(level):
    ask = request_ask({"thinking_level": level})
    c = candidate(supported_parameters=("reasoning_effort",))
    assert assess(ask, c).verdict is Verdict.EXCLUDED
    assert assess(ask, replace(c, supports_reasoning=None, supported_parameters=None)).verdict is Verdict.UNKNOWN


def test_none_effort_can_disable_reasoning_only_when_the_parameter_is_supported():
    ask = request_ask({"thinking_level": "none"})
    assert assess(ask, candidate(supported_parameters=("reasoning_effort",))).verdict is Verdict.QUALIFIES
    assert assess(ask, candidate()).verdict is Verdict.EXCLUDED


def test_explicit_effort_values_limit_which_level_can_be_requested():
    c = candidate(supports_reasoning=True, supported_parameters=("reasoning_effort",),
                  reasoning_efforts=("low", "medium"))
    assert assess(request_ask({"thinking_level": "low"}), c).verdict is Verdict.QUALIFIES
    assert assess(request_ask({"thinking_level": "high"}), c).verdict is Verdict.EXCLUDED


def test_context_filter_is_a_minimum_independent_of_this_short_prompt():
    ask = request_ask({"min_context_tokens": 65536})
    assert assess(ask, candidate(context_length=65536)).verdict is Verdict.QUALIFIES
    assert assess(ask, candidate(context_length=32768)).verdict is Verdict.EXCLUDED
    assert assess(ask, candidate(context_length=None)).verdict is Verdict.UNKNOWN


def test_price_filter_gates_known_prices_and_keeps_null_prices_unknown():
    ask = request_ask({"max_usd_per_mtok": 0.10})
    assert assess(ask, candidate(price="0.10")).verdict is Verdict.QUALIFIES
    assert assess(ask, candidate(price="0.11")).verdict is Verdict.EXCLUDED
    unknown = candidate(list_prompt=None, list_completion=None)
    assert assess(ask, unknown).verdict is Verdict.UNKNOWN


@pytest.mark.parametrize("filters,overrides", [
    ({"model_types": ["reasoning"]}, {"supports_reasoning": None, "supported_parameters": None}),
    ({"model_types": ["vision"]}, {"input_modalities": None}),
    ({"editable": True}, {"supports_editing": None}),
    ({"required_capabilities": ["structured_output"]}, {"supported_parameters": None}),
])
def test_model_names_do_not_supply_missing_capability_facts(filters, overrides):
    c = candidate("reasoning-vision-editor-json-pro", **overrides)
    choice = decide(request_ask(filters), [c])
    assert choice.outcome is Outcome.ABSTAIN
    assert choice.considered[0].verdict is Verdict.UNKNOWN


@pytest.mark.parametrize("path", ["/router/explain", "/v1/chat/completions", "/v1/responses"])
def test_disallowing_tool_calls_conflicts_with_supplied_tools_before_routing(app, path):
    tool = RESPONSE_TOOL if path == "/v1/responses" else CHAT_TOOL
    response = post(app, path, {"tool_calls": False}, tools=[tool])
    assert response.status_code == 400, response.text
    assert "tool" in response.text.lower()
    assert app.upstream.calls == [] and app.upstream.streams == []


@pytest.mark.parametrize("request_limit,header_limit,expected", [(9, "10", 0.15),
                                                               (0.05, "10", 0.05),
                                                               (9, "0.08", 0.08)])
def test_request_and_header_price_limits_only_lower_operator_policy(app, request_limit,
                                                                   header_limit, expected):
    app.router.cfg.ceiling_usd_per_mtok = 0.15
    response = app.client.post("/router/explain", json={
        "model": "auto", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 32, "router_requirements": {"max_usd_per_mtok": request_limit},
    }, headers={"X-Router-Max-Usd-Per-M": header_limit})
    assert response.status_code == 200, response.text
    choice = response.json()["choice"]
    assert choice["facts"]["ceiling_usd_per_mtok"] == expected
    assert choice["outcome"] == ("ROUTE" if expected >= 0.10 else "ABSTAIN")


@pytest.mark.parametrize("stream", [False, True])
def test_only_supported_effort_and_no_router_requirements_reach_upstream(app, stream):
    app.roster[:] = [candidate("think", supports_reasoning=True,
                              supported_parameters=("reasoning_effort",))]
    filters = {"providers": ["akashml"], "thinking_level": "high", "min_context_tokens": 8192}
    response = post(app, filters=filters, stream=stream)
    assert response.status_code == 200, response.text
    calls = app.upstream.streams if stream else app.upstream.calls
    assert len(calls) == 1
    assert calls[0][2]["reasoning_effort"] == "high"
    assert "router_requirements" not in calls[0][2]
    did = response.headers["X-Router-Decision"] if stream else response.json()["router"]["decision_id"]
    record = app.client.get("/router/decisions/" + did).json()
    assert record["choice"]["facts"]["router_requirements"]["thinking_level"] == "high"


def test_unfiltered_request_does_not_invent_an_effort_setting(app):
    response = post(app)
    assert response.status_code == 200, response.text
    assert "reasoning_effort" not in app.upstream.calls[0][2]
    assert "router_requirements" not in app.upstream.calls[0][2]


def test_prompt_wording_does_not_infer_provider_or_capability_filters(app):
    expensive = candidate("reasoner", provider="openrouter", price="0.50", supports_reasoning=True)
    app.roster.append(expensive)
    response = post(app, messages=[{"role": "user", "content":
        "Use only the expensive OpenRouter reasoning model and think deeply."}])
    assert response.status_code == 200, response.text
    assert response.json()["router"]["seat"] == "akashml:plain"


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
def test_native_effort_requires_supported_model_even_without_a_filter_object(app, path):
    response = post(app, path, reasoning_effort="high")
    assert response.status_code == 422, response.text
    assert app.upstream.calls == [] and app.upstream.streams == []


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
def test_supported_native_effort_is_forwarded_without_a_filter_object(app, path):
    app.roster[:] = [candidate(supports_reasoning=True, supported_parameters=("reasoning_effort",))]
    response = post(app, path, reasoning_effort="low")
    assert response.status_code == 200, response.text
    assert app.upstream.calls[0][2]["reasoning_effort"] == "low"
    assert "router_requirements" not in app.upstream.calls[0][2]


def test_chat_reasoning_object_uses_supported_effort_and_is_normalized_upstream(app):
    app.roster[:] = [candidate(provider="openrouter", supports_reasoning=True,
                              supported_parameters=("reasoning",))]
    response = post(app, reasoning={"effort": "low"})
    assert response.status_code == 200, response.text
    request = app.upstream.calls[0][2]
    assert request["reasoning_effort"] == "low"
    assert "reasoning" not in request
    record = app.client.get("/router/decisions/" + response.json()["router"]["decision_id"]).json()
    assert record["choice"]["facts"]["request_features"]["requested_effort"] == "low"


@pytest.mark.parametrize("path", ["/router/explain", "/v1/chat/completions", "/v1/responses"])
def test_native_reasoning_object_cannot_bypass_unknown_effort_support(app, path):
    app.roster[:] = [candidate(provider="openrouter", supports_reasoning=None,
                              supported_parameters=None)]
    response = post(app, path, reasoning={"effort": "low"})
    assert response.status_code == (200 if path == "/router/explain" else 422), response.text
    if path == "/router/explain":
        choice = response.json()["choice"]
        assert choice["outcome"] == "ABSTAIN"
        assert choice["considered"][0]["verdict"] == "UNKNOWN"
    assert app.upstream.calls == [] and app.upstream.streams == []


@pytest.mark.parametrize("path", ["/router/explain", "/v1/chat/completions", "/v1/responses"])
@pytest.mark.parametrize("extra", [
    {"router_requirements": {"thinking_level": "high"}}, {"reasoning_effort": "high"},
])
def test_native_reasoning_object_cannot_conflict_with_another_effort_setting(app, path, extra):
    response = post(app, path, reasoning={"effort": "low"}, **extra)
    assert response.status_code == 400, response.text
    assert app.upstream.calls == [] and app.upstream.streams == []


@pytest.mark.parametrize("stream", [False, True])
def test_capacity_race_removes_selected_model_confidence_from_abstentions(app, monkeypatch, stream):
    app.roster[:] = [candidate(observation_count=7, price_age_s=10, probe_age_s=10)]
    monkeypatch.setattr(app.router.gather, "try_acquire", lambda provider: False)
    response = post(app, stream=stream)
    assert response.status_code == 429, response.text
    record = app.client.get("/router/decisions/" + response.json()["error"]["decision_id"]).json()
    assert record["status"] == "ABSTAINED"
    for choice in [*record["earlier_decisions"], record["choice"]]:
        assert choice["outcome"] == "ABSTAIN" and choice["seat"] is None
        assert choice["confidence"]["level"] == "unknown"
        assert choice["confidence"]["sample_count"] is None
        assert choice["confidence"]["probability"] is None
    assert app.upstream.calls == [] and app.upstream.streams == []


def test_retry_retains_provider_and_capability_filters(app):
    first = candidate("first", price="0.01", supports_editing=True)
    allowed_retry = candidate("retry", price="0.20", supports_editing=True)
    forbidden_provider = candidate("forbidden", provider="openrouter", price="0.02", supports_editing=True)
    forbidden_capability = candidate("not-editable", price="0.03", supports_editing=False)
    app.roster[:] = [first, allowed_retry, forbidden_provider, forbidden_capability]
    app.upstream.fail.add(first.seat)
    response = post(app, filters={"providers": ["akashml"], "editable": True})
    assert response.status_code == 200, response.text
    assert [(provider, model) for provider, model, _, _ in app.upstream.calls] == [
        ("akashml", "first"), ("akashml", "retry")]
    record = app.client.get("/router/decisions/" + response.json()["router"]["decision_id"]).json()
    assert len(record["earlier_decisions"]) == 1
    for choice in [*record["earlier_decisions"], record["choice"]]:
        assert choice["facts"]["router_requirements"]["providers"] == ["akashml"]
        assert choice["facts"]["router_requirements"]["editable"] is True


def test_responses_conversion_preserves_filters_effort_and_structured_output(app):
    app.roster[:] = [candidate("think-json", supports_reasoning=True,
                              supported_parameters=("reasoning_effort", "response_format"))]
    response = post(app, "/v1/responses", {
        "providers": ["akashml"], "thinking_level": "medium",
        "required_capabilities": ["structured_output"],
    }, reasoning={"effort": "medium"}, text={"format": {"type": "json_object"}})
    assert response.status_code == 200, response.text
    request = app.upstream.calls[0][2]
    assert request["reasoning_effort"] == "medium"
    assert request["response_format"] == {"type": "json_object"}
    assert "router_requirements" not in request and "reasoning" not in request
    record = app.client.get("/router/decisions/" + response.json()["router"]["decision_id"]).json()
    assert record["choice"]["facts"]["router_requirements"]["providers"] == ["akashml"]


def test_recorded_features_explain_shape_without_retaining_prompt_or_tool_schema(app):
    app.roster[:] = [candidate(supports_tools=True)]
    prompt = "UNIQUE_PRIVATE_PROMPT_MUST_NOT_BE_RECORDED"
    tool_description = "UNIQUE_PRIVATE_TOOL_DESCRIPTION"
    response = post(app, filters={"tool_calls": True}, messages=[
        {"role": "system", "content": "UNIQUE_PRIVATE_SYSTEM_INSTRUCTION"},
        {"role": "user", "content": prompt},
    ], tools=[{"type": "function", "function": {"name": "lookup",
              "description": tool_description, "parameters": {"type": "object"}}}])
    assert response.status_code == 200, response.text
    record = app.client.get("/router/decisions/" + response.json()["router"]["decision_id"]).json()
    serialized = json.dumps(record)
    assert prompt not in serialized and tool_description not in serialized
    assert "UNIQUE_PRIVATE_SYSTEM_INSTRUCTION" not in serialized
    features = record["choice"]["facts"]["request_features"]
    assert features["message_count"] == 2
    assert features["role_counts"] == {"system": 1, "user": 1}
    assert features["tool_count"] == 1
    assert features["prompt_tokens_estimate"] > 0
    assert features["prompt_tokens_basis"] == "serialized_messages_and_tools_chars_div_4"
    assert features["context_needed_tokens"] >= features["prompt_tokens_estimate"] + 32


def test_confidence_is_evidence_metadata_without_an_invented_probability():
    choice = decide(request_ask(), [candidate()]).public()
    confidence = choice["confidence"]
    assert confidence["level"] and confidence["basis"] and confidence["evidence"]
    assert confidence["unknown_count"] == 0
    assert confidence["sample_count"] is None and confidence["probability"] is None


def test_confidence_counts_all_unknown_candidates_even_when_records_are_limited():
    known = candidate("known", price="0.01")
    unknown = [candidate(f"unknown-{i}", price="0.20", supports_editing=None) for i in range(30)]
    choice = decide(request_ask({"editable": False}), [known, *unknown]).public()
    assert choice["outcome"] == "ROUTE"
    assert choice["confidence"]["unknown_count"] == 30
    assert choice["confidence"]["probability"] is None
    assert choice["facts"]["unknown_not_listed"] > 0


def test_confidence_is_present_for_abstention_too():
    choice = decide(request_ask({"editable": True}), [candidate(supports_editing=None)]).public()
    assert choice["outcome"] == "ABSTAIN"
    confidence = choice["confidence"]
    assert confidence["level"] and confidence["basis"] and confidence["evidence"]
    assert confidence["unknown_count"] == 1 and confidence["probability"] is None


def test_confidence_reports_the_selected_models_observation_count():
    choice = decide(request_ask(), [candidate(observation_count=7)]).public()
    assert choice["confidence"]["sample_count"] == 7
    assert choice["confidence"]["probability"] is None


def test_absent_effort_parameter_does_not_invent_a_negative_reasoning_fact():
    c = candidate(supports_reasoning=None, supported_parameters=())
    assert assess(request_ask({"model_types": ["reasoning"]}), c).verdict is Verdict.UNKNOWN


def test_unnamed_reasoning_support_does_not_invent_controllable_effort_levels():
    c = candidate(supports_reasoning=None, reasoning_efforts=(), supported_parameters=None)
    assert assess(request_ask({"model_types": ["reasoning"]}), c).verdict is Verdict.QUALIFIES
    assert assess(request_ask({"thinking_level": "high"}), c).verdict is Verdict.UNKNOWN


@pytest.mark.parametrize("path", ["/router/explain", "/v1/chat/completions", "/v1/responses"])
@pytest.mark.parametrize("reasoning,filters", [
    ({"enabled": True}, {}),
    ({"enabled": False}, {"thinking_level": "high"}),
    ({"effort": "high", "enabled": False}, {"thinking_level": "high"}),
    ({"budget": 1024}, {}),
    ({"max_tokens": 1024}, {}),
    ({"exclude": True}, {}),
    ({"summary": "auto"}, {}),
])
def test_non_effort_reasoning_controls_are_rejected_before_routing(app, path, reasoning, filters):
    app.roster[:] = [candidate("think", supports_reasoning=True,
                              supported_parameters=("reasoning_effort",))]
    response = post(app, path, filters, reasoning=reasoning, stream=True)
    assert response.status_code == 400, response.text
    assert "unsupported" in response.text.lower() and "reasoning" in response.text.lower()
    assert app.upstream.calls == [] and app.upstream.streams == []


@pytest.mark.parametrize("path", ["/router/explain", "/v1/chat/completions", "/v1/responses"])
def test_enabling_native_reasoning_cannot_bypass_a_negative_capability_fact(app, path):
    response = post(app, path, reasoning={"enabled": True})
    assert response.status_code == 400, response.text
    assert app.upstream.calls == [] and app.upstream.streams == []
