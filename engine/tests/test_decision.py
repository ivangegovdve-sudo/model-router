"""The decision layer, on the facts measured on AkashML on 2026-09-25."""
from modelrouter.decision import Ask, Candidate, Emits, Outcome, Verdict, assess, decide


def akash(**over):
    """The three AkashML models as measured: two reasoning models, one plain."""
    base = dict(provider="akashml", provider_state="OK", available=True,
                context_length=131072, supports_tools=True, price_source="provider-catalogue")
    seats = [
        Candidate(seat="akashml:openai/gpt-oss-20b", model="openai/gpt-oss-20b",
                  list_prompt=0.02, list_completion=0.10, measured_usd_per_mtok=0.04,
                  emits=Emits.REASONING_THEN_CONTENT, min_max_tokens=256,
                  reasoning_overhead_tokens=100, **base),
        Candidate(seat="akashml:meta-llama/Llama-3.3-70B-Instruct",
                  model="meta-llama/Llama-3.3-70B-Instruct", list_prompt=0.20,
                  list_completion=0.52, measured_usd_per_mtok=0.23, emits=Emits.CONTENT,
                  min_max_tokens=1, **base),
        Candidate(seat="akashml:Qwen/Qwen3.8-27B", model="Qwen/Qwen3.8-27B",
                  list_prompt=0.25, list_completion=2.2, measured_usd_per_mtok=1.20,
                  emits=Emits.REASONING_THEN_CONTENT, min_max_tokens=1024,
                  reasoning_overhead_tokens=300, **base),
    ]
    return seats


def test_small_budget_excludes_reasoning_models_that_would_return_empty():
    # The measured failure: max_tokens=16 -> reasoning models return HTTP 200, empty.
    ch = decide(Ask(prompt_tokens=40, max_tokens=16), akash())
    assert ch.outcome is Outcome.ROUTE
    assert ch.seat == "akashml:meta-llama/Llama-3.3-70B-Instruct"
    why = {a.seat: a for a in ch.considered}
    assert why["akashml:openai/gpt-oss-20b"].verdict is Verdict.EXCLUDED
    assert "max_tokens >= 256" in why["akashml:openai/gpt-oss-20b"].because
    assert why["akashml:Qwen/Qwen3.8-27B"].verdict is Verdict.EXCLUDED


def test_price_table_alone_would_pick_the_empty_model():
    """What a list-price router does, to show the layer does not."""
    cheapest_by_list = min(akash(), key=lambda c: 2 * c.list_prompt + c.list_completion)
    assert cheapest_by_list.model == "openai/gpt-oss-20b"
    assert decide(Ask(prompt_tokens=40, max_tokens=16), akash()).seat != cheapest_by_list.seat


def test_large_budget_lets_the_cheap_reasoning_model_win_on_measured_cost():
    ch = decide(Ask(prompt_tokens=40, max_tokens=1024), akash())
    assert ch.seat == "akashml:openai/gpt-oss-20b"
    assert "measured" in ch.because


def test_never_measured_is_unknown_not_qualified_and_abstain_names_it():
    seats = [Candidate(seat="x:m", provider="x", model="m", list_prompt=0.01,
                       list_completion=0.01, context_length=8000)]
    ch = decide(Ask(prompt_tokens=10), seats)
    assert ch.outcome is Outcome.ABSTAIN
    assert "emits" in ch.unknown
    assert ch.considered[0].verdict is Verdict.UNKNOWN


def test_null_price_is_unknown_never_zero():
    c = Candidate(seat="sail:m", provider="sail", model="m", emits=Emits.CONTENT,
                  min_max_tokens=1, context_length=100000)
    a = assess(Ask(prompt_tokens=10), c)
    assert a.verdict is Verdict.UNKNOWN and a.unknown == ("price",)


def test_half_a_price_is_not_a_price():
    c = Candidate(seat="p:m", provider="p", model="m", list_prompt=0.0, list_completion=None,
                  emits=Emits.CONTENT, min_max_tokens=1, context_length=100000)
    assert assess(Ask(prompt_tokens=10), c).verdict is Verdict.UNKNOWN


def test_reasoning_only_is_excluded():
    c = Candidate(seat="p:m", provider="p", model="m", list_prompt=0.01, list_completion=0.01,
                  emits=Emits.REASONING_ONLY, context_length=100000)
    assert assess(Ask(prompt_tokens=10, max_tokens=4000), c).verdict is Verdict.EXCLUDED


def test_blocked_provider_and_retired_model_are_excluded_first():
    seats = akash()
    seats[1] = Candidate(**{**seats[1].__dict__, "provider_state": "BLOCKED",
                            "provider_detail": "HTTP 402"})
    ch = decide(Ask(prompt_tokens=40, max_tokens=16), seats)
    assert ch.outcome is Outcome.ABSTAIN
    assert any("provider BLOCKED" in a.because for a in ch.considered)


def test_ceiling_refuses_rather_than_falling_back_to_expensive():
    ch = decide(Ask(prompt_tokens=40, max_tokens=16, ceiling_usd_per_mtok=0.1), akash())
    assert ch.outcome is Outcome.ABSTAIN
    assert any("above the $0.1/M ceiling" in a.because for a in ch.considered)


def test_tools_unknown_is_unknown():
    seats = [Candidate(**{**akash()[1].__dict__, "supports_tools": None})]
    ch = decide(Ask(prompt_tokens=40, needs_tools=True), seats)
    assert ch.outcome is Outcome.ABSTAIN and "supports_tools" in ch.unknown


def test_named_model_is_judged_alone_and_can_be_refused():
    ch = decide(Ask(prompt_tokens=40, max_tokens=16, only_seat="akashml:Qwen/Qwen3.8-27B"),
                akash())
    assert ch.outcome is Outcome.ABSTAIN
    assert len(ch.considered) == 1


def test_failed_seat_is_not_chosen_again_for_the_same_request():
    ask = Ask(prompt_tokens=40, max_tokens=16,
              failed=(("akashml:meta-llama/Llama-3.3-70B-Instruct", "HTTP 500"),))
    ch = decide(ask, akash())
    assert ch.outcome is Outcome.ABSTAIN


def test_client_budget_is_forwarded_untouched():
    assert decide(Ask(prompt_tokens=40, max_tokens=16), akash()).max_tokens == 16
    assert decide(Ask(prompt_tokens=40), akash()).max_tokens is None


def test_choice_is_deterministic_and_serialisable():
    a = decide(Ask(prompt_tokens=40, max_tokens=2000), akash()).to_json()
    b = decide(Ask(prompt_tokens=40, max_tokens=2000), list(reversed(akash()))).to_json()
    assert a == b


def test_free_tier_is_excluded_by_default_and_allowed_by_policy():
    free = Candidate(seat="openrouter:x/y:free", provider="openrouter", model="x/y:free",
                     list_prompt=0.0, list_completion=0.0, emits=Emits.CONTENT,
                     min_max_tokens=1, context_length=100000)
    assert decide(Ask(prompt_tokens=10), akash() + [free]).seat != free.seat
    assert decide(Ask(prompt_tokens=10, allow_free=True), akash() + [free]).seat == free.seat


def test_abstain_names_the_reason_not_just_a_count():
    ch = decide(Ask(prompt_tokens=40, max_tokens=16, only_seat="akashml:Qwen/Qwen3.8-27B"),
                akash())
    assert "max_tokens >= 1024" in ch.because


def test_unmeasured_seats_kept_only_when_cheaper_than_winner():
    cheap = [Candidate(seat="p:u%d" % i, provider="p", model="u%d" % i, list_prompt=0.001,
                       list_completion=0.001, context_length=100000) for i in range(40)]
    dear = [Candidate(seat="p:d%d" % i, provider="p", model="d%d" % i, list_prompt=50.0,
                      list_completion=50.0, context_length=100000) for i in range(40)]
    ch = decide(Ask(prompt_tokens=40, max_tokens=2000), akash() + cheap + dear)
    unk = [a for a in ch.considered if a.verdict is Verdict.UNKNOWN]
    assert len(unk) == 25 and all(a.seat.startswith("p:u") for a in unk)
    assert ch.facts["unknown_not_listed"] == 55
