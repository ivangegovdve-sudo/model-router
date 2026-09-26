"""GATHER -> DECIDE -> ACT with a fake upstream: probes, retry-by-redecision, refusals."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modelrouter import providers as P
from modelrouter.config import Config
from modelrouter.decision import Emits
from modelrouter.server import build
from modelrouter.store import Store, derive

SECRET = "sk-test-VALUE-that-must-never-leak"

CATALOGUE = [
    P.Listing("akashml", "openai/gpt-oss-20b", 0.02, 0.10, None, 131072, True, True),
    P.Listing("akashml", "meta-llama/Llama-3.3-70B-Instruct", 0.20, 0.52, 0.10, 131072, True, False),
    P.Listing("akashml", "Qwen/Qwen3.8-27B", 0.25, 2.2, None, 262144, True, True),
]


class Upstream:
    """Behaves like the measured AkashML models."""

    def __init__(self):
        self.calls = []
        self.fail_llama = False

    def __call__(self, provider, model, key, req, *, max_tokens, timeout=180):
        assert key == SECRET
        self.calls.append((model, max_tokens))
        need = {"openai/gpt-oss-20b": 200, "Qwen/Qwen3.8-27B": 900}.get(model, 0)
        if model.startswith("meta-llama") and self.fail_llama:
            return P.Result(False, 500, provider, model, detail="HTTP 500 boom", scope="model")
        budget = max_tokens or 4096
        if budget < need:
            content, reasoning, ct = "", "thinking " * 20, budget
        else:
            content, reasoning, ct = "Ready", ("thinking " * 20 if need else ""), need + 2
        payload = {"choices": [{"message": {"content": content, "reasoning_content": reasoning},
                                "finish_reason": "length" if not content else "stop"}],
                   "usage": {"prompt_tokens": 40, "completion_tokens": ct}}
        r = P.extract(provider, model, payload)
        r.detail = "ok" if r.ok else "HTTP 200 but empty content"
        return r


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("AKASH_KEY", SECRET)
    monkeypatch.setenv("ROUTER_TOKEN", "tok")
    up = Upstream()
    monkeypatch.setattr(P, "call", up)
    monkeypatch.setattr(P, "fetch_catalogue", lambda provider, key, timeout=30: list(CATALOGUE))
    cfg = Config(path=tmp_path / "c.toml", source="env", secrets={"akashml": "AKASH_KEY"},
                 token_secret="ROUTER_TOKEN", state_dir=tmp_path, dashboard_url="",
                 probe_budget_usd=1.0)
    client = TestClient(build(cfg))
    client.headers["Authorization"] = "Bearer tok"
    return client, up


def chat(client, **kw):
    body = {"model": "auto", "messages": [{"role": "user", "content": "hi"}], **kw}
    return client.post("/v1/chat/completions", json=body)


def test_unmeasured_roster_abstains_loudly(app):
    client, up = app
    r = chat(client, max_tokens=16)
    assert r.status_code == 422
    assert r.json()["error"]["type"] == "router_abstained"
    assert up.calls == []                     # refused without spending anything


def test_probe_then_route_small_budget_avoids_empty_reasoning_models(app):
    client, up = app
    p = client.post("/router/probe", json={"cheapest": 3}).json()
    prof = {x["seat"]: x["profile"] for x in p["probed"]}
    assert prof["akashml:openai/gpt-oss-20b"]["emits"] == "reasoning_then_content"
    assert prof["akashml:openai/gpt-oss-20b"]["min_max_tokens"] == 256
    assert prof["akashml:meta-llama/Llama-3.3-70B-Instruct"]["emits"] == "content"
    up.calls.clear()
    r = chat(client, max_tokens=16)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "Ready"
    assert body["router"]["seat"] == "akashml:meta-llama/Llama-3.3-70B-Instruct"
    assert body["router"]["cost_basis"] == "computed"
    d = client.get("/router/decisions/" + body["router"]["decision_id"]).json()
    v = {c["seat"]: c for c in d["choice"]["considered"]}
    assert "max_tokens >= 256" in v["akashml:openai/gpt-oss-20b"]["because"]


def test_large_budget_routes_to_cheap_reasoning_model(app):
    client, up = app
    client.post("/router/probe", json={"cheapest": 3})
    r = chat(client, max_tokens=2000)
    assert r.json()["router"]["seat"] == "akashml:openai/gpt-oss-20b"


def test_failed_call_is_redecided_not_pinned_fallback(app):
    client, up = app
    client.post("/router/probe", json={"cheapest": 3})
    up.fail_llama = True
    up.calls.clear()
    r = chat(client, max_tokens=16)
    # Llama failed; the others need more budget than 16, so the redecision abstains
    # and the client gets a loud error -- not an empty 200, not an expensive fallback.
    assert r.status_code == 502
    assert [m for m, _ in up.calls] == ["meta-llama/Llama-3.3-70B-Instruct"]
    d = client.get("/router/decisions/" + r.json()["error"]["decision_id"]).json()
    assert d["status"] == "FAILED" and len(d["earlier_decisions"]) == 1
    assert d["choice"]["outcome"] == "ABSTAIN"


def test_named_model_refused_when_budget_too_small(app):
    client, up = app
    client.post("/router/probe", json={"cheapest": 3})
    r = chat(client, model="akashml:Qwen/Qwen3.8-27B", max_tokens=16)
    assert r.status_code == 422
    assert "max_tokens >= 1024" in json.dumps(
        client.get("/router/decisions/" + r.json()["error"]["decision_id"]).json())


def test_auth_required_and_secret_never_in_any_response(app):
    client, up = app
    assert client.post("/v1/chat/completions", json={}, headers={"Authorization": ""}).status_code == 401
    client.post("/router/probe", json={"cheapest": 3})
    chat(client, max_tokens=16)
    for path in ("/health", "/router/setup", "/router/roster", "/router/decisions", "/v1/models"):
        assert SECRET not in client.get(path).text
    s = client.get("/router/setup").json()
    assert s["keys"][0]["name"] == "AKASH_KEY" and s["keys"][0]["present"]


def test_missing_key_is_named_not_silent(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    cfg = Config(path=tmp_path / "c.toml", source="env", secrets={"akashml": "NOPE"},
                 no_auth=True, state_dir=tmp_path, dashboard_url="")
    c = TestClient(build(cfg))
    h = c.get("/health").json()
    assert h["status"] == "not_ready"
    assert any("variable unset" in p for p in h["problems"])
    r = c.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 422


def test_derive_reasoning_only_after_large_empty_budget(tmp_path):
    s = Store(tmp_path / "s.db")
    for mt in (32, 256, 1024, 2048):
        s.observe("p:m", "probe", max_tokens=mt, status=200, content_chars=0, reasoning_chars=99,
                  tool_calls=0, prompt_tokens=40, completion_tokens=mt, cached_tokens=0,
                  cost_usd=0.0001, cost_basis="computed", latency_s=1.0, detail="")
    assert s.profiles()["p:m"].emits is Emits.REASONING_ONLY


def test_classify_failure():
    assert P.classify_failure(402, "") == "provider"
    assert P.classify_failure(429, "") == "quota"
    assert P.classify_failure(403, "Budget limit exceeded (monthly limit). Contact your org admin.") == "provider"
    assert P.classify_failure(429, "Rate limit exceeded: free-models-per-day") == "quota"
    assert P.classify_failure(500, "internal") == "model"


def test_catalogue_parsers_keep_unknown_prices_unknown():
    rows = P.parse_catalogue("akashml", [{"id": "a/b", "pricing": {"input": "0.0000002",
                                                                     "output": None}}])
    assert rows[0].prompt == pytest.approx(0.2) and rows[0].completion is None
    rows = P.parse_catalogue("openrouter", [{"id": "x/y", "pricing": {"prompt": "-1",
                                                                       "completion": "0"}}])
    assert rows[0].prompt is None
    assert P.parse_catalogue("sail", [{"id": "s/t"}])[0].prompt is None
    assert P.parse_catalogue("openrouter", [{"id": "x/text-embedding-3"}]) == []
