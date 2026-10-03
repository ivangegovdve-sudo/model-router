"""Seat selection: pool order, exclusions, cross-family, free-only, Jev fail-open."""
from decimal import Decimal as D

import pytest

from modelrouter import jev, seats
from modelrouter.seats import Outcome, Seat, SeatRequest, resolve


def S(provider, model, price="1", kind="priced", family="auto", ctx=128000, tools=True, ok=True):
    fam = seats.family_of(model) if family == "auto" else family
    return Seat(seat=f"{provider}:{model}", provider=provider, model=model, family=fam,
                tier=seats.TIER.get(provider, 99), kind=kind,
                usd_per_mtok=None if price is None else D(price), context_length=ctx,
                supports_tools=tools, available=ok, detail="" if ok else "down")


POOL = [
    S("openrouter", "mistralai/mistral-large", "2"),
    S("local", "llama3.1:8b", "0", "local"),
    S("sail", "Qwen/Qwen3-32B", "0.30"),
    S("codex", "gpt-5.6-sol", "0", "subscription"),
    S("antigravity", "gemini-3-pro", "0", "subscription"),
]


def test_pool_order_then_cheapest_within_tier():
    pool = POOL + [S("sail", "meta-llama/Llama-3.3-70B", "0.20")]
    r = resolve(SeatRequest(role="review"), pool, configured_policy="cheapest")
    assert r.seat.seat == "sail:meta-llama/Llama-3.3-70B"        # tier 0, cheaper of two


def test_gpu_above_openrouter_and_openrouter_last():
    pool = [S("openrouter", "mistralai/m", "0.01"), S("local", "llama3.1:8b", "0", "local")]
    assert resolve(SeatRequest(), pool, configured_policy="cheapest").seat.provider == "local"
    only = [S("openrouter", "mistralai/m", "0.01")]
    assert resolve(SeatRequest(), only, configured_policy="cheapest").seat.provider == "openrouter"


def test_claude_and_cerebras_never_selected_even_alone_and_even_when_cheapest():
    pool = [S("cerebras", "llama3.1-8b", "0"), S("claude", "claude-opus-5-5", "0"),
            S("anthropic", "claude-sonnet-5-5", "0"),
            S("openrouter", "anthropic/claude-opus-5-5", "0.001"),     # Claude via OpenRouter
            S("sail", "x/Claude-distill-7b", "0.001")]
    for pol in ("cheapest", "free-only"):
        r = resolve(SeatRequest(), pool, configured_policy=pol)
        assert r.outcome is Outcome.UNAVAILABLE and r.seat is None
        assert all(a.verdict.value == "EXCLUDED" for a in r.considered)


def test_providers_outside_the_pool_are_not_selected():
    r = resolve(SeatRequest(), [S("akashml", "Qwen/Q", "0.01")], configured_policy="cheapest")
    assert r.outcome is Outcome.UNAVAILABLE and "not in the pool" in r.considered[0].because


def test_cross_family_excludes_author_family_and_unknown_family_is_a_conflict():
    r = resolve(SeatRequest(role="review", exclude_families=("openai",)), POOL,
                configured_policy="cheapest")
    assert r.seat.provider == "sail"
    pool = [S("codex", "gpt-5.6-sol", "0", "subscription"),
            S("sail", "mystery/model-x", "0.1", family=None)]
    r = resolve(SeatRequest(exclude_families=("openai",)), pool, configured_policy="cheapest")
    assert r.outcome is Outcome.UNAVAILABLE           # openai excluded, unknown != different
    assert "family unknown" in r.considered[1].because
    # no constraint: unknown family is allowed
    assert resolve(SeatRequest(), pool[1:], configured_policy="cheapest").seat.provider == "sail"


def test_base_weight_lineage():
    assert seats.family_of("NousResearch/Hermes-4-70B") == "meta"
    assert seats.family_of("google/gemma-3-27b") == seats.family_of("gemini-3-pro") == "google"
    assert seats.family_of("gpt-oss-120b") == "openai"
    assert seats.family_of("totally-new-thing") is None


def test_free_only_selects_only_free_and_never_falls_back_to_paid():
    r = resolve(SeatRequest(consumer="public-council"), POOL, configured_policy="free-only")
    assert r.seat.seat == "local:llama3.1:8b"         # subscriptions are paid plans
    paid_only = [S("sail", "Qwen/Qwen3-32B", "0.30"), S("codex", "gpt-5.6-sol", "0", "subscription")]
    r = resolve(SeatRequest(), paid_only, configured_policy="free-only")
    assert r.outcome is Outcome.UNAVAILABLE
    assert "never falls back to a paid seat" in r.because


def test_free_only_openrouter_free_tier_and_unknown_price_is_not_free():
    pool = [S("openrouter", "meta-llama/llama-3.3-70b-instruct:free", "0", "free"),
            S("sail", "Qwen/Q", None)]
    r = resolve(SeatRequest(), pool, configured_policy="free-only")
    assert r.seat.kind == "free"
    r = resolve(SeatRequest(), [S("sail", "Qwen/Q", None)], configured_policy="cheapest")
    assert r.outcome is Outcome.UNAVAILABLE and "unknown is not free" in r.considered[0].because


def test_request_can_tighten_but_never_loosen_policy():
    assert seats.effective_policy("cheapest", "free-only") == "free-only"
    assert seats.effective_policy("free-only", "cheapest") == "free-only"
    assert seats.effective_policy(None, "free-only") == "free-only"
    with pytest.raises(ValueError):
        seats.effective_policy(None, None)
    with pytest.raises(ValueError):
        seats.effective_policy(None, "anything")


def test_subscription_counts_as_free_only_when_operator_allows():
    r = resolve(SeatRequest(), POOL[3:4], configured_policy="free-only",
                allow_subscription_free=True)
    assert r.seat.provider == "codex"


def test_unavailable_seat_is_skipped_and_named():
    pool = [S("sail", "Qwen/Q", "0.1", ok=False), S("openrouter", "mistralai/m", "1")]
    r = resolve(SeatRequest(), pool, configured_policy="cheapest")
    assert r.seat.provider == "openrouter"
    assert "unavailable" in r.considered[0].because


def test_context_tools_and_ceiling_gates():
    pool = [S("sail", "Qwen/Q", "0.1", ctx=8000), S("openrouter", "mistralai/m", "9", ctx=200000)]
    r = resolve(SeatRequest(min_context=50000), pool, configured_policy="cheapest")
    assert r.seat.provider == "openrouter"
    r = resolve(SeatRequest(min_context=50000, ceiling_usd_per_mtok=D(5)), pool,
                configured_policy="cheapest")
    assert r.outcome is Outcome.UNAVAILABLE
    r = resolve(SeatRequest(needs_tools=True), [S("sail", "Qwen/Q", "0.1", tools=None)],
                configured_policy="cheapest")
    assert r.outcome is Outcome.UNAVAILABLE


def test_exclude_seats():
    r = resolve(SeatRequest(exclude_seats=("sail:Qwen/Qwen3-32B",)), POOL,
                configured_policy="cheapest")
    assert r.seat.provider == "codex"


# ---- Jev: advises among eligible seats only, fails open ---------------------------------
def advisor_returning(choice, prob):
    return jev.make_advisor(
        lambda: "k", min_prob=0.6,
        post=lambda key, body, t: {"answers": {"seat": {
            "choice": choice, "confidence": prob, "probabilities": {choice: prob}}}})


def test_jev_can_switch_to_a_stronger_eligible_seat():
    adv = advisor_returning("codex:gpt-5.6-sol", 0.9)
    r = resolve(SeatRequest(role="review", task="subtle concurrency bug"), POOL,
                configured_policy="cheapest", advisor=adv)
    assert r.seat.seat == "codex:gpt-5.6-sol" and r.jev["used"] is True
    assert "chosen by Jev" in r.because


def test_jev_cannot_pick_an_excluded_seat_or_low_confidence():
    r = resolve(SeatRequest(task="x", exclude_families=("openai",)), POOL,
                configured_policy="cheapest", advisor=advisor_returning("codex:gpt-5.6-sol", 0.99))
    assert r.seat.provider != "codex" and r.jev["used"] is False   # not eligible: ignored
    r = resolve(SeatRequest(task="x"), POOL, configured_policy="cheapest",
                advisor=advisor_returning("codex:gpt-5.6-sol", 0.4))
    assert r.seat.provider == "sail" and "below floor" in r.jev["why"]


def test_jev_in_free_only_only_sees_free_seats():
    seen = {}

    def post(key, body, t):
        seen.update(body["questions"]["seat"]["criteria"])
        return {"answers": {"seat": {"choice": "local:llama3.1:8b",
                                     "probabilities": {"local:llama3.1:8b": 0.9}}}}
    pool = POOL + [S("openrouter", "x/y:free", "0", "free")]
    resolve(SeatRequest(task="t"), pool, configured_policy="free-only",
            advisor=jev.make_advisor(lambda: "k", post=post))
    assert set(seen) == {"local:llama3.1:8b", "openrouter:x/y:free"}


def test_jev_failures_fail_open():
    def boom(key, body, t):
        raise TimeoutError
    for adv in (jev.make_advisor(lambda: "k", post=boom), jev.make_advisor(lambda: "")):
        r = resolve(SeatRequest(task="t"), POOL, configured_policy="cheapest", advisor=adv)
        assert r.outcome is Outcome.SEAT and r.seat.provider == "sail" and r.jev["used"] is False

    def raising(req, el):
        raise RuntimeError
    assert resolve(SeatRequest(task="t"), POOL, configured_policy="cheapest",
                   advisor=raising).seat.provider == "sail"


def test_jev_not_called_without_task_or_single_candidate():
    def never(key, body, t):
        raise AssertionError("called")
    adv = jev.make_advisor(lambda: "k", post=never)
    resolve(SeatRequest(), POOL, configured_policy="cheapest", advisor=adv)
    resolve(SeatRequest(task="t"), POOL[:1], configured_policy="cheapest", advisor=adv)


def test_task_is_bounded_and_goes_in_state_not_instructions():
    body = jev.build_request(SeatRequest(task="ZZQ" * 3000), POOL[:2])
    assert len(body["state"]["task"]) == jev.TASK_MAX_CHARS
    assert "ZZQ" not in body["questions"]["seat"]["instructions"]


def test_discover_local_reads_live_tags_and_skips_non_chat_and_bench_copies():
    tags = {"models": [{"name": "qwen3:8b"}, {"name": "nomic-embed-text:latest"},
                       {"name": "bench-qwen3-coder-30b:latest"}, {"name": "gemma4:12b"}]}
    got = seats.discover_local(get=lambda base: tags)
    assert [s.model for s in got] == ["qwen3:8b", "gemma4:12b"]
    assert all(s.kind == "local" and s.free and s.tier == seats.TIER["local"] for s in got)


def test_discover_local_unreachable_is_an_unavailable_seat_not_silence():
    def down(base):
        raise OSError
    got = seats.discover_local(get=down)
    assert len(got) == 1 and not got[0].available and "unreachable" in got[0].detail
    r = resolve(SeatRequest(), got, configured_policy="free-only")
    assert r.outcome is Outcome.UNAVAILABLE


def test_undeclared_codex_and_antigravity_show_up_unavailable_and_declared_ones_do_not():
    ph = seats.adapter_placeholders([])
    assert {s.provider for s in ph} == {"codex", "antigravity"} and not any(s.available for s in ph)
    assert seats.adapter_placeholders([{"provider": "codex", "model": "gpt-5.6-sol"}])[0].provider == "antigravity"


def test_config_template_parses_and_carries_the_consumer_policies(tmp_path):
    from modelrouter import config
    p = tmp_path / "c.toml"
    p.write_text(config.TEMPLATE, encoding="utf-8")
    cfg = config.load(p)
    assert cfg.consumers["public-council"] == "free-only"
    assert cfg.consumers["glass-solver"] == "cheapest" and cfg.jev_secret == "typesafe-api-key"
    assert not [x for x in cfg.problems if "consumers" in x or "seats" in x]


# ---- regressions from the cross-family review of PR #7 (Codex, 2026-10-03) --------------
def test_forbidden_backend_cannot_hide_behind_a_name_or_invoke_metadata():
    hidden = seats.from_declared({"provider": "sail", "model": "innocuous-7b", "available": True,
                                  "invoke": {"kind": "openai-compat", "base_url": "https://api.anthropic.com/v1"}})
    cere = seats.from_declared({"provider": "openrouter", "model": "plain", "available": True,
                                "invoke": {"base_url": "https://api.cerebras.ai/v1"}})
    alias = S("local", "sonnet", "0", "local")
    alias2 = S("local", "opus:latest", "0", "local")
    for s in (hidden, cere, alias, alias2):
        r = resolve(SeatRequest(), [s], configured_policy="cheapest")
        assert r.outcome is Outcome.UNAVAILABLE, s.seat


def test_declared_kind_never_overrides_a_price():
    paid = seats.from_declared({"provider": "openrouter", "model": "x/y", "kind": "free",
                                "usd_per_mtok": 5, "available": True})
    assert paid.kind == "priced" and paid.usd_per_mtok == 5 and not paid.free
    assert resolve(SeatRequest(), [paid], configured_policy="free-only").outcome is Outcome.UNAVAILABLE
    nopr = seats.from_declared({"provider": "openrouter", "model": "x/y", "kind": "free", "available": True})
    assert nopr.usd_per_mtok is None and not nopr.free
    sub = seats.from_declared({"provider": "codex", "model": "gpt-5.6-sol", "usd_per_mtok": 3, "available": True})
    assert sub.kind == "priced"
    assert resolve(SeatRequest(), [sub], configured_policy="free-only",
                   allow_subscription_free=True).outcome is Outcome.UNAVAILABLE


def test_family_comparison_is_case_and_space_insensitive():
    s = seats.from_declared({"provider": "sail", "model": "m", "family": " OpenAI ", "available": True})
    assert s.family == "openai"
    r = resolve(SeatRequest(exclude_families=("OpenAI ",)), [s], configured_policy="cheapest")
    assert r.outcome is Outcome.UNAVAILABLE


def test_invoke_is_an_allowlist_without_credentials():
    pub = seats.public_invoke({"kind": "openai-compat", "base_url": "https://u:p@h/v1", "model": "m",
                               "token": "SECRET", "headers": {"authorization": "Bearer SECRET"}})
    assert pub == {"kind": "openai-compat", "model": "m"}
    assert "SECRET" not in str(seats.public_invoke({"base_url": "http://h/v1?key=SECRET"}))
    assert seats.public_invoke({"base_url": "http://127.0.0.1:11434/v1"})["base_url"].endswith("/v1")


def test_jev_rejects_nonfinite_and_out_of_range_probabilities_and_unlisted_seats():
    for bad in (float("nan"), 1.5, -0.1):
        r = resolve(SeatRequest(task="t"), POOL, configured_policy="cheapest",
                    advisor=advisor_returning("codex:gpt-5.6-sol", bad))
        assert r.seat.provider == "sail" and r.jev["used"] is False
    many = [S("sail", f"m{i}", str(i + 1)) for i in range(jev.MAX_SEATS)] + [S("openrouter", "late/model", "50")]
    r = resolve(SeatRequest(task="t"), many, configured_policy="cheapest",
                advisor=advisor_returning("openrouter:late/model", 0.99))
    assert r.seat.seat == "sail:m0" and r.jev["used"] is False       # was never shown to Jev


# ---- the HTTP endpoint -------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient
    from modelrouter import config, server
    p = tmp_path / "c.toml"
    p.write_text('[secrets]\nsource="env"\n[providers.openrouter]\nsecret="NOPE"\n'
                 '[server]\nno_auth=true\n[policy]\nceiling_usd_per_mtok=5\ndashboard_url=""\n'
                 '[jev]\nenabled=false\n[local]\nollama_url="http://127.0.0.1:9"\n'
                 '[[seats]]\nprovider="sail"\nmodel="Qwen/Qwen3-32B"\navailable=true\nusd_per_mtok=0.3\n'
                 '[[seats]]\nprovider="sail"\nmodel="deepseek-ai/Big"\navailable=true\nusd_per_mtok=9\n',
                 encoding="utf-8")
    cfg = config.load(p)
    cfg.state_dir = tmp_path
    return TestClient(server.build(cfg))


def test_endpoint_unknown_or_mistyped_consumer_is_refused_even_with_a_policy(client):
    for c in ("zzz", "public-council ", None, 7):
        r = client.post("/v1/seats/resolve", json={"consumer": c, "policy": "cheapest"})
        assert r.status_code == 400, c


def test_endpoint_scalar_families_are_refused_not_split_into_characters(client):
    r = client.post("/v1/seats/resolve", json={"consumer": "glass-solver", "exclude_families": "openai"})
    assert r.status_code == 400


def test_endpoint_applies_the_global_ceiling_and_a_request_can_only_lower_it(client):
    r = client.post("/v1/seats/resolve", json={"consumer": "glass-solver", "exclude_seats": ["sail:Qwen/Qwen3-32B"]})
    assert r.status_code == 422                       # the $9/M seat is above the configured $5/M
    r = client.post("/v1/seats/resolve", json={"consumer": "glass-solver", "max_usd_per_m": 100})
    assert r.status_code == 200 and r.json()["seat"] == "sail:Qwen/Qwen3-32B"


def test_endpoint_public_council_gets_unavailable_not_a_paid_seat(client):
    r = client.post("/v1/seats/resolve", json={"consumer": "public-council", "role": "council"})
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["type"] == "seat_unavailable" and d["policy"] == "free-only" and d["seat"] is None
    r = client.post("/v1/seats/resolve", json={"consumer": "public-council", "policy": "cheapest"})
    assert r.status_code == 422                        # cannot loosen


def test_pool_matches_the_fixture_shared_with_open_dashboard_mcp():
    import json
    from pathlib import Path
    fx = json.loads((Path(__file__).parents[2] / "docs" / "fixtures" / "seat-pool.json").read_text("utf-8"))
    assert list(seats.POOL) == fx["order"]
    assert sorted(seats.EXCLUDED_PROVIDERS) == sorted(fx["excluded"])
    assert list(seats.ROLES) == fx["roles"] and list(seats.POLICIES) == fx["policies"]
