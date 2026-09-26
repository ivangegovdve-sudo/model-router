"""GATHER -> DECIDE -> ACT with a fake upstream: probes, retry-by-redecision, refusals."""
import json
from decimal import Decimal
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

    def __call__(self, provider, model, key, req, *, max_tokens, timeout=180, window=None):
        assert key == SECRET
        self.calls.append((model, max_tokens))
        need = {"openai/gpt-oss-20b": 200, "Qwen/Qwen3.8-27B": 900}.get(model, 0)
        if model.startswith("meta-llama") and self.fail_llama:
            return P.Result(False, 500, provider, model, detail="HTTP 500 boom", scope="model")
        budget = max_tokens or 4096
        if budget < need:
            content, reasoning, ct = "", "thinking " * 20, budget
        else:
            content, reasoning, ct = "Ready", ("thinking " * 20 if need else ""), need + 1
        payload = {"choices": [{"message": {"content": content, "reasoning_content": reasoning},
                                "finish_reason": "length" if not content else "stop"}],
                   "usage": {"prompt_tokens": 40, "completion_tokens": ct}}
        r = P.extract(provider, model, payload)
        r.detail = "ok" if r.ok else "HTTP 200 but empty content"
        r.latency_s = 0.5
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
                 probe_budget_usd=1.0, interactive_max_latency_s=None)   # lane gate tested apart
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
    assert prof["akashml:openai/gpt-oss-20b"]["min_max_tokens"] == 201   # measured spend, not the 256 rung
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
    assert "max_tokens >= 201" in v["akashml:openai/gpt-oss-20b"]["because"]
    assert "200 reasoning tokens before its answer (probe)" in v["akashml:openai/gpt-oss-20b"]["because"]


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
    # Llama failed. Nothing else answers within 16, so the redecision raises the budget
    # for the cheapest reasoning model -- on the record -- rather than passing 16 through.
    assert r.status_code == 200, r.text
    assert up.calls == [("meta-llama/Llama-3.3-70B-Instruct", 16), ("openai/gpt-oss-20b", 416)]
    d = client.get("/router/decisions/" + r.json()["router"]["decision_id"]).json()
    assert d["status"] == "ANSWERED" and len(d["earlier_decisions"]) == 1
    assert d["choice"]["facts"]["max_tokens_raised_from"] == 16


def test_failed_call_with_clamping_off_fails_loudly(app, monkeypatch):
    client, up = app
    client.app.state.router.cfg.clamp_max_tokens = False
    client.post("/router/probe", json={"cheapest": 3})
    up.fail_llama = True
    r = chat(client, max_tokens=16)
    assert r.status_code == 502
    d = client.get("/router/decisions/" + r.json()["error"]["decision_id"]).json()
    assert d["status"] == "FAILED" and d["choice"]["outcome"] == "ABSTAIN"


def test_named_reasoning_model_small_budget_is_clamped_visibly_never_passed_through(app):
    client, up = app
    client.post("/router/probe", json={"cheapest": 3})
    up.calls.clear()
    r = chat(client, model="akashml:openai/gpt-oss-20b", max_tokens=16)
    assert r.status_code == 200, r.text
    assert up.calls == [("openai/gpt-oss-20b", 16 + 2 * 200)]   # caller's 16 + 2x reasoning seen
    assert r.json()["choices"][0]["message"]["content"] == "Ready"
    d = client.get("/router/decisions/" + r.json()["router"]["decision_id"]).json()
    assert d["choice"]["facts"]["max_tokens_raised_from"] == 16
    assert "raised to 416" in d["choice"]["because"]


def test_clamp_beyond_the_cap_is_refused_not_silently_expensive(app):
    client, up = app
    client.post("/router/probe", json={"cheapest": 3})
    up.calls.clear()
    r = chat(client, model="akashml:Qwen/Qwen3.8-27B", max_tokens=16)   # 2x900 > 1024 cap
    assert r.status_code == 422 and up.calls == []
    assert "max_tokens >= 901" in r.json()["error"]["message"]


def test_unknown_lane_is_an_error_not_a_default(app):
    client, up = app
    r = chat(client, model="auto:whenever", max_tokens=16)
    assert r.status_code == 400 and "unknown lane" in r.text


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
    assert rows[0].prompt == Decimal("0.2") and rows[0].completion is None
    rows = P.parse_catalogue("openrouter", [{"id": "x/y", "pricing": {"prompt": "-1",
                                                                       "completion": "0"}}])
    assert rows[0].prompt is None
    assert P.parse_catalogue("sail", [{"id": "s/t"}])[0].prompt is None
    assert P.parse_catalogue("openrouter", [{"id": "x/text-embedding-3"}]) == []


def test_floor_is_per_model_measured_spend_not_a_global_threshold(tmp_path):
    """io.net 2026-09-26: GLM-5.3-Flash spent 91 tokens on one word, DeepSeek-V4.1-Flash 18."""
    s = Store(tmp_path / "s.db")
    def obs(seat, mt, content, ct):
        s.observe(seat, "probe", max_tokens=mt, status=200, content_chars=content,
                  reasoning_chars=200 if ct else 0, tool_calls=0, prompt_tokens=20,
                  completion_tokens=ct, cached_tokens=0, cost_usd=1e-6, cost_basis="computed",
                  latency_s=1.0, detail="")
    obs("ionet:glm", 16, 0, 16); obs("ionet:glm", 256, 5, 91)
    obs("ionet:ds", 16, 0, 16); obs("ionet:ds", 256, 5, 18)
    p = s.profiles()
    assert p["ionet:glm"].min_max_tokens == 91          # 90 spent before 'ready'
    assert p["ionet:ds"].min_max_tokens == 18           # 16 was empty; 17 spent
    assert "empty at max_tokens=16" in p["ionet:ds"].floor_evidence


def test_cloudflare_1010_is_an_edge_block_not_a_bad_key():
    import httpx
    assert P.classify_failure(403, "error code: 1010", "error code: 1010") == "edge"
    html = httpx.Headers({"cf-ray": "x", "content-type": "text/html"})
    assert P.classify_failure(403, "", "<html>Attention Required!</html>", html) == "edge"
    assert P.classify_failure(401, "Invalid API Key", '{"detail":"Invalid API Key"}') == "provider"


def test_edge_block_marks_provider_unavailable_not_blocked(app, monkeypatch):
    client, up = app
    def refuse(provider, key, timeout=30):
        raise P.CatalogueError(403, "edge", "error code: 1010")
    monkeypatch.setattr(P, "fetch_catalogue", refuse)
    r = client.get("/router/roster").json()
    st = r["providers"][0]
    assert st["state"] == "UNAVAILABLE" and "not a credential failure" in st["detail"]


def test_ionet_catalogue_parses_per_token_prices():
    rows = P.parse_catalogue("ionet", [{"id": "deepseek-ai/DeepSeek-V4.1-Flash",
        "input_token_price": 2.95e-07, "output_token_price": 1.15e-06,
        "cache_read_token_price": 1.475e-07, "context_window": 262124,
        "supports_tools": True, "supports_reasoning": True, "output_modalities": ["text"]}])
    li = rows[0]
    assert li.prompt == Decimal("0.295") and li.completion == Decimal("1.15")
    assert li.context_length == 262124 and li.supports_tools is True


def test_sail_pricing_page_parses_per_window_and_keys_by_page_id():
    page = ('<tbody className="pricing-model-group" data-model="google/gemma-4-12B-it">'
            '<tr aria-label="Gemma 4 12B IT Default (ASAP) pricing: input $0.30, cached $0.15, '
            'output $2.00 per 1M tokens."></tr><tr aria-label="Gemma 4 12B IT Flex pricing: '
            'input $0.05, cached $0.02, output $1.00 per 1M tokens."></tr></tbody>')
    w = P.parse_window_prices(page)
    assert w == {"google/gemma-4-12B-it": {"asap": (Decimal("0.30"), Decimal("2.00"), Decimal("0.15")),
                                            "flex": (Decimal("0.05"), Decimal("1.00"), Decimal("0.02"))}}


def test_sail_models_page_context_pairs_with_the_following_id():
    page = ('<span className="cap-expand-key">Context</span>\n<span className="cap-expand-val">16K</span>'
            ' ... <code>google/gemma-4-12B-it</code> <code>none</code>'
            '<span className="cap-expand-key">Context</span><span className="cap-expand-val">1M</span>'
            '<code>zai-org/GLM-5.3</code>')
    assert P.parse_context_lengths(page) == {"google/gemma-4-12B-it": 16000,
                                             "zai-org/GLM-5.3": 1000000}


def test_window_is_sent_as_sail_metadata_only_to_windowed_providers():
    assert P._body("sail", "m", {"messages": []}, 8, "flex")["metadata"] == {"completion_window": "flex"}
    assert "metadata" not in P._body("akashml", "m", {"messages": []}, 8, "flex")


def test_ionet_tier_402_is_the_model_not_the_account():
    msg = "Model 'MiniMaxAI/MiniMax-M2.7' requires a higher IO Intelligence tier"
    assert P.classify_failure(402, msg, '{"detail":"%s"}' % msg) == "model"
    assert P.classify_failure(402, "Insufficient credits", "") == "provider"
    rows = P.parse_catalogue("ionet", [{"id": "MiniMaxAI/MiniMax-M2.7", "input_token_price": 1e-7,
                                        "output_token_price": 1e-7, "higher_tier_required": True,
                                        "min_access_tier": 3}])
    assert "tier 3" in rows[0].blocked


def test_import_measurements_sets_floors_field_flag_decode_speed_and_caps(app):
    client, up = app
    router = client.app.state.router
    rows = [
        {"provider": "akash", "model": "Qwen/Qwen3.8-27B", "budget": 16, "latency_s": 0.6,
         "error": None, "content_len": 0, "has_reasoning_field": True, "reasoning_len": 60,
         "finish_reason": "length", "billed_prompt": 15, "billed_completion": 16},
        {"provider": "akash", "model": "Qwen/Qwen3.8-27B", "budget": 64, "latency_s": 0.8,
         "error": None, "content_len": 5, "has_reasoning_field": True, "reasoning_len": 90,
         "finish_reason": "stop", "billed_prompt": 15, "billed_completion": 29},
        {"provider": "akash", "model": "meta-llama/Llama-3.3-70B-Instruct", "budget": 16,
         "latency_s": 0.5, "error": None, "content_len": 5, "has_reasoning_field": False,
         "reasoning_len": 0, "finish_reason": "stop", "billed_prompt": 15, "billed_completion": 1},
        {"provider": "akash", "model": "x/errors", "budget": 16, "error": "HTTP 400"},
    ]
    curve = [{"provider": "akash", "model": "openai/gpt-oss-20b", "concurrency": n,
              "succeeded": n - f, "failed": f, "median_latency_s": 13.78,
              "aggregate_tok_per_s": 23.95} for n, f in ((1, 0), (4, 0), (16, 0), (64, 34))]
    out = router.import_measurements(rows, curve, "test")
    assert out == {"observations": 3, "skipped_errors": 1, "decode_runs": 1,
                   "concurrency_caps": {"akashml": 16}}
    prof = router.store.profiles()
    assert prof["akashml:Qwen/Qwen3.8-27B"].min_max_tokens == 29       # 28 spent, empty at 16
    assert prof["akashml:meta-llama/Llama-3.3-70B-Instruct"].has_reasoning_field is False
    assert round(prof["akashml:openai/gpt-oss-20b"].decode_tps) == 24
    st = {p["provider"]: p for p in client.get("/router/roster").json()["providers"]}["akashml"]
    assert st["max_concurrency"] == 16 and "0 failures at n=16" in st["concurrency_source"]


def test_provider_at_its_measured_concurrency_cap_is_routed_around(app):
    client, up = app
    router = client.app.state.router
    router.store.set_provider_fact("akashml", "max_ok_concurrency", 1, "test")
    router.gather.acquire("akashml")
    try:
        st = {p["provider"]: p for p in client.get("/router/roster").json()["providers"]}
        assert st["akashml"]["state"] == "AT_CAPACITY"
    finally:
        router.gather.release("akashml")
    st = {p["provider"]: p for p in client.get("/router/roster").json()["providers"]}
    assert st["akashml"]["state"] == "OK"


def test_empty_long_generation_does_not_raise_the_short_task_floor(tmp_path):
    s = Store(tmp_path / "s.db")
    s.observe("ionet:ds", "probe", max_tokens=256, status=200, content_chars=5, reasoning_chars=60,
              tool_calls=0, prompt_tokens=20, completion_tokens=18, cached_tokens=0, cost_usd=None,
              cost_basis="unknown", latency_s=1.0, detail="", reasoning_field=True)
    s.observe("ionet:ds", "gen", max_tokens=1024, status=200, content_chars=0, reasoning_chars=4000,
              tool_calls=0, prompt_tokens=30, completion_tokens=1024, cached_tokens=0,
              cost_usd=None, cost_basis="unknown", latency_s=4.1, detail="", reasoning_field=True)
    p = s.profiles()["ionet:ds"]
    assert p.min_max_tokens == 18                 # not 1025
    assert round(p.decode_tps) == 250             # but decode speed still counts


def test_named_seat_at_capacity_is_429_with_retry_after_and_names_the_cap(app):
    client, up = app
    router = client.app.state.router
    client.post("/router/probe", json={"cheapest": 3})
    router.store.set_provider_fact("akashml", "max_ok_concurrency", 1, "test cap")
    router.gather.acquire("akashml")
    try:
        r = chat(client, model="akashml:meta-llama/Llama-3.3-70B-Instruct", max_tokens=16)
    finally:
        router.gather.release("akashml")
    assert r.status_code == 429 and r.headers["Retry-After"] == "1"
    assert "AT_CAPACITY" in r.json()["error"]["message"] and "test cap" in r.json()["error"]["message"]


def test_mirror_detection_finds_a_catalogue_copied_with_identical_prices():
    from modelrouter.gather import detect_mirrors
    def L(p, m, pr): return P.Listing(p, m, Decimal(pr), Decimal(pr))
    orig = [L("openrouter", "m%d" % i, "0.%d" % (i + 1)) for i in range(10)]
    copy = [L("nous", "m%d" % i, "0.%d" % (i + 1)) for i in range(9)]
    indep = [L("ionet", "m%d" % i, "0.9") for i in range(9)]            # same ids, own prices
    assert detect_mirrors({"openrouter": orig, "nous": copy, "ionet": indep}) ==         {"nous": ("openrouter", 1.0), "ionet": ("openrouter", pytest.approx(1 / 9))}  # own prices
    reprice = [L("nous", "m%d" % i, "0.%d" % (i + 1) if i < 7 else "0.05") for i in range(9)]
    assert detect_mirrors({"openrouter": orig, "nous": reprice})["nous"][1] == pytest.approx(7 / 9)


def test_groq_null_pricing_is_unknown_not_free():
    rows = P.parse_catalogue("groq", [
        {"id": "allam-2-7b", "pricing": None, "context_window": 4096, "active": True},
        {"id": "openai/gpt-oss-20b", "pricing": {"prompt": "0.000000075", "completion": "0.0000003",
         "input_cache_read": "0.0000000375"}, "context_window": 131072, "active": True}])
    assert rows[0].prompt is None and rows[0].completion is None
    assert rows[1].prompt == Decimal("0.075") and rows[1].cached_prompt == Decimal("0.0375")


def test_curve_import_records_latency_ratios_and_load_factor_interpolates(app):
    client, up = app
    router = client.app.state.router
    curve = [{"provider": "ionet", "model": "m", "concurrency": n, "succeeded": n, "failed": 0,
              "median_latency_s": lat, "per_request_tok_per_s": 200}
             for n, lat in ((1, 1.11), (4, 1.26), (16, 1.72), (64, 1.80))]
    router.import_measurements(None, curve, "t")
    facts = router.store.provider_facts()["ionet"]
    assert facts["latency_ratio_n64"]["value"] == pytest.approx(1.622, abs=1e-3)
    lf = router.gather._load_factor(facts, in_flight=9)                  # n=10, between 4 and 16
    assert 1.135 < lf < 1.55


def test_ensemble_header_is_reserved_not_silently_ignored(app):
    client, up = app
    r = client.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user",
                    "content": "x"}]}, headers={"X-Router-Ensemble": "3"})
    assert r.status_code == 501 and "has not run" in r.text


def test_same_open_weights_at_independent_prices_is_not_correlation(app):
    client, up = app
    g = client.app.state.router.gather
    def L(p, m, pr): return P.Listing(p, m, Decimal(pr), Decimal(pr))
    g._cat = {"akashml": (0, [L("akashml", "m%d" % i, "0.1") for i in range(6)])}
    g.states["ionet"] = type(g.states["akashml"])("ionet")
    g._cat["ionet"] = (0, [L("ionet", "m%d" % i, "0.3") for i in range(30)])
    from modelrouter.gather import detect_mirrors, CORRELATED_SAME_PRICE
    share = detect_mirrors({p: v[1] for p, v in g._cat.items()})["akashml"][1]
    assert share < CORRELATED_SAME_PRICE     # same ids, own prices: independent host
