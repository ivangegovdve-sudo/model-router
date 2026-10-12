"""Exercise real HTTP plan ownership, pinning and execution with fake providers."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from modelrouter import providers as P
from modelrouter.config import Config
from modelrouter.decision import Candidate, Emits
from modelrouter.server import build


@pytest.fixture
def plan_app(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAN_TOKEN", "operator")
    cfg = Config(path=tmp_path / "router.toml", source="env", state_dir=tmp_path,
                 token_secret="PLAN_TOKEN", interactive_max_latency_s=None,
                 seat_model_identities={
                     "openrouter:a": {"weights_id": "weights-A"},
                     "openrouter:b": {"weights_id": "weights-B"},
                     "openrouter:c": {"weights_id": "weights-C"},
                 })
    app = build(cfg)
    router = app.state.router
    roster = [Candidate(seat="openrouter:" + model, provider="openrouter", model=model,
                        weights_id="weights-" + model.upper(),
                        context_length=8192, supports_tools=True, emits=Emits.CONTENT,
                        has_reasoning_field=False, observation_count=3,
                        list_prompt=Decimal("0.1"), list_completion=Decimal("0.2"))
              for model in "abc"]
    monkeypatch.setattr(router.gather, "roster", lambda lane="interactive": list(roster))
    calls = []

    def call(provider, model, key, request, *, max_tokens, **kwargs):
        calls.append((provider, model, request))
        return P.extract(provider, model, {
            "choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1}})

    monkeypatch.setattr(P, "call", call)
    monkeypatch.setattr(router.gather, "try_acquire", lambda provider: True)
    monkeypatch.setattr(router.gather, "release", lambda provider: None)
    client = TestClient(app)
    return client, roster, calls


def resolve(client, **updates):
    body = {"schema": "seat-plan/1", "consumer": "private-council",
            "context": {"run_id": "round-1"},
            "slots": [{"id": "member-1", "roles": ["proposer", "chair"],
                       "binding": {"kind": "auto"}, "requirements": {"tool_calls": True}},
                      {"id": "member-2", "role": "critic", "binding": {"kind": "auto"}}],
            "constraints": [{"id": "pair", "kind": "all_distinct", "dimension": "weights",
                             "slots": ["member-1", "member-2"]}], **updates}
    return client.post("/v1/seat-plans/resolve", json=body)


def invoke(client, plan, **updates):
    slot = plan["assignments"][0]["slot_id"]
    return client.post(f'/v1/seat-plans/{plan["plan_id"]}/slots/{slot}/chat/completions',
                       json={"version": plan["version"], "context": plan["context"],
                             "request": {"messages": [{"role": "user", "content": "secret task"}],
                                         "max_tokens": 64}, **updates})


def test_plan_http_auth_atomic_resolution_execution_and_prompt_free_history(plan_app):
    client, _, calls = plan_app
    assert resolve(client).status_code == 401
    client.headers["Authorization"] = "Bearer operator"
    result = resolve(client)
    assert result.status_code == 200, result.text
    plan = result.json()
    assert plan["outcome"] == "RESOLVED"
    assert len({a["weights_id"] for a in plan["assignments"]}) == 2
    assert calls == []
    response = invoke(client, plan)
    assert response.status_code == 200, response.text
    assert calls[0][0] + ":" + calls[0][1] == plan["assignments"][0]["seat"]
    receipt = response.json()["router"]["seat_plan"]
    assert receipt["plan_id"] == plan["plan_id"]
    assert receipt["weights_id"] == plan["assignments"][0]["weights_id"]
    assert "router_requirements" not in calls[0][2]
    assert "secret task" not in client.get(f'/v1/seat-plans/{plan["plan_id"]}').text
    assert "secret task" not in client.get("/router/decisions").text


def test_pinned_invocation_rejects_stale_version_and_weakened_filters(plan_app):
    client, _, calls = plan_app
    client.headers["Authorization"] = "Bearer operator"
    plan = resolve(client).json()
    assert invoke(client, plan, version=plan["version"] + 1).status_code == 409
    assert invoke(client, plan, request={
        "messages": [{"role": "user", "content": "hi"}],
        "router_requirements": {"tool_calls": False}}).status_code == 409
    assert calls == []


def test_pinned_invocation_rechecks_identity_before_upstream(plan_app):
    from dataclasses import replace
    client, roster, calls = plan_app
    client.headers["Authorization"] = "Bearer operator"
    plan = resolve(client).json()
    selected = plan["assignments"][0]["seat"]
    roster[:] = [replace(c, weights_id="replaced-weights") if c.seat == selected else c for c in roster]
    response = invoke(client, plan)
    assert response.status_code == 409, response.text
    assert calls == []


def test_plan_templates_and_invalid_json_are_readable(plan_app):
    client, _, _ = plan_app
    client.headers["Authorization"] = "Bearer operator"
    templates = client.get("/v1/seat-plans/templates")
    assert templates.status_code == 200
    assert "glass" in templates.text and "council" in templates.text
    assert client.post("/v1/seat-plans/resolve", content="bad-json").status_code == 400


def test_pinned_stream_exposes_receipt_and_records_terminal_execution(plan_app, monkeypatch):
    import json
    client, _, calls = plan_app
    client.headers["Authorization"] = "Bearer operator"
    plan = resolve(client).json()

    def stream(provider, model, key, request, *, max_tokens, on_done, **kwargs):
        calls.append((provider, model, request))
        yield b'data: {"choices":[{"delta":{"content":"Done"}}]}\n\n'
        on_done(P.extract(provider, model, {
            "choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1}}))
        yield b'data: [DONE]\n\n'

    monkeypatch.setattr(P, "stream", stream)
    response = invoke(client, plan, request={
        "messages": [{"role": "user", "content": "stream private task"}], "stream": True})
    assert response.status_code == 200, response.text
    assert "[DONE]" in response.text
    assert response.headers["x-router-plan"] == plan["plan_id"]
    assert response.headers["x-router-weights"] == plan["assignments"][0]["weights_id"]
    stored = client.get(f'/v1/seat-plans/{plan["plan_id"]}').json()
    assert stored["receipts"][-1]["status"] == "ANSWERED"
    assert stored["assignments"][0]["state"] == "ANSWERED"
    assert "stream private task" not in json.dumps(stored)


@pytest.mark.parametrize("stream", [False, True])
def test_identity_change_between_preparation_and_execution_never_calls_provider(plan_app, monkeypatch, stream):
    from dataclasses import replace
    client, roster, calls = plan_app
    client.headers["Authorization"] = "Bearer operator"
    plan = resolve(client).json()
    selected = plan["assignments"][0]["seat"]
    reads = 0

    def changing_roster(lane="interactive"):
        nonlocal reads
        reads += 1
        return list(roster) if reads == 1 else [replace(c, weights_id="changed") if c.seat == selected else c for c in roster]

    monkeypatch.setattr(client.app.state.router.gather, "roster", changing_roster)
    response = invoke(client, plan, request={
        "messages": [{"role": "user", "content": "hi"}], "stream": stream})
    assert response.status_code == 422, response.text
    assert calls == []
    stored = client.get(f'/v1/seat-plans/{plan["plan_id"]}').json()
    assert stored["receipts"][-1]["status"] == "FAILED"
    decision = client.get("/router/decisions").json()[0]
    evidence = client.get("/router/decisions/" + decision["id"]).json()
    assert evidence["choice"]["confidence"]["level"] == "unknown"


@pytest.mark.parametrize("routing", [{"models": ["other"]}, {"route": "fallback"}, {"plugins": []}])
def test_plan_rejects_native_upstream_model_substitution_controls(plan_app, routing):
    client, _, calls = plan_app
    client.headers["Authorization"] = "Bearer operator"
    plan = resolve(client).json()
    response = invoke(client, plan, request={"messages": [{"role": "user", "content": "hi"}], **routing})
    assert response.status_code in (400, 409)
    assert calls == []


def test_custom_slot_id_uses_encoded_invocation_path(plan_app):
    from urllib.parse import unquote
    client, _, calls = plan_app
    client.headers["Authorization"] = "Bearer operator"
    result = resolve(client, slots=[{
        "id": "review / 二 %", "role": "review", "binding": {"kind": "auto"}}], constraints=[])
    assert result.status_code == 200, result.text
    plan = result.json()
    assignment = plan["assignments"][0]
    assert "%2F" in assignment["invoke"]["path"]
    response = client.post(assignment["invoke"]["path"], json={
        "version": plan["version"], "context": plan["context"],
        "request": {"messages": [{"role": "user", "content": "hi"}]}})
    assert response.status_code == 200, response.text
    assert unquote(response.headers["x-router-slot"]) == assignment["slot_id"]
    assert len(calls) == 1

