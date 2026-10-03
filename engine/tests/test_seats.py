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
