"""The decision layer, on the facts measured on AkashML on 2026-09-25."""
from dataclasses import replace

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
    assert "rate card" in ch.because     # computed cost = the model's own rate card, split


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


def _seat(seat, p, c, emits, floor=1, oh=None, lat=0.8, tps=150.0, **kw):
    prov, _, model = seat.partition(":")
    return Candidate(seat=seat, provider=prov, model=model, list_prompt=p, list_completion=c,
                     emits=emits, min_max_tokens=floor, reasoning_overhead_tokens=oh,
                     latency_s=lat, decode_tps=tps, context_length=128000, supports_tools=True,
                     **kw)


def test_interactive_gate_is_predicted_from_decode_speed_not_a_short_probe():
    """2026-09-26: AkashML answered one word in 0.6s yet decoded at 24 tok/s (13.8s for 400)."""
    ionet = _seat("ionet:gpt-oss-120b", 0.10, 0.50, Emits.CONTENT, lat=0.9, tps=260)
    akash = _seat("akashml:gpt-oss-120b", 0.03, 0.17, Emits.CONTENT, lat=0.6, tps=24)
    long_ask = Ask(prompt_tokens=40, max_tokens=400, interactive_max_latency_s=8.0)
    ch = decide(long_ask, [ionet, akash])
    assert ch.seat == "ionet:gpt-oss-120b"
    why = {a.seat: a.because for a in ch.considered}["akashml:gpt-oss-120b"]
    assert "predicted 17.3s for 400 tokens" in why and "24 tok/s" in why
    # the same slow decoder is fine for a one-word answer, and for the batch lane
    assert decide(replace(long_ask, max_tokens=8), [ionet, akash]).seat == "akashml:gpt-oss-120b"
    assert decide(replace(long_ask, lane="batch"), [ionet, akash]).seat == "akashml:gpt-oss-120b"


def test_unmeasured_decode_speed_is_unknown_in_the_interactive_lane_only():
    s = _seat("p:m", 0.1, 0.1, Emits.CONTENT, tps=None)
    ask = Ask(prompt_tokens=40, max_tokens=50, interactive_max_latency_s=8.0)
    assert assess(ask, s).unknown == ("decode_tps",)
    assert assess(replace(ask, lane="background"), s).verdict is Verdict.QUALIFIES


def test_no_reasoning_field_honours_the_callers_budget_as_is():
    s = _seat("ionet:DeepSeek-V3.2", 0.2, 0.4, Emits.CONTENT, has_reasoning_field=False)
    ch = decide(Ask(prompt_tokens=40, max_tokens=4, allow_clamp=True), [s])
    assert ch.max_tokens == 4 and "budget honoured as-is" in ch.considered[0].because


def test_second_attempt_after_running_out_escalates_to_the_safe_ceiling():
    glm = _seat("ionet:glm", 0.1, 0.4, Emits.REASONING_THEN_CONTENT, floor=36, oh=35)
    ask = Ask(prompt_tokens=40, max_tokens=8, allow_clamp=True, only_seat="ionet:glm")
    assert decide(ask, [glm]).max_tokens == 8 + 2 * 35
    assert decide(replace(ask, escalate=("ionet:glm",)), [glm]).max_tokens == 1024


def test_model_blocked_by_catalogue_is_excluded_with_its_reason():
    s = _seat("ionet:MiniMax-M2.7", 0.1, 0.1, Emits.CONTENT,
              blocked="io.net access tier 3 required; this key's tier is lower")
    a = assess(Ask(prompt_tokens=10), s)
    assert a.verdict is Verdict.EXCLUDED and "tier 3" in a.because


def test_no_reasoning_short_answer_beats_cheaper_per_token_reasoning_model():
    """Gemma-4-12B shape: 2 tokens, no reasoning tax, wins a short answer on its own merits."""
    gemma = _seat("sail:google/gemma-4-12B-it", 0.30, 2.00, Emits.CONTENT)
    glm = _seat("sail:zai-org/GLM-5.3-Flash", 0.11, 0.35, Emits.REASONING_THEN_CONTENT,
                floor=37, oh=36)
    ch = decide(Ask(prompt_tokens=40, max_tokens=4), [gemma, glm])
    assert ch.seat == "sail:google/gemma-4-12B-it"


def test_small_budget_prefers_rerouting_to_a_no_reasoning_model_over_clamping():
    ask = Ask(prompt_tokens=40, max_tokens=16, allow_clamp=True)
    ch = decide(ask, akash())
    assert ch.seat == "akashml:meta-llama/Llama-3.3-70B-Instruct" and ch.max_tokens == 16


def test_clamps_visibly_when_no_model_answers_within_the_budget():
    ask = Ask(prompt_tokens=40, max_tokens=16, allow_clamp=True,
              only_seat="akashml:openai/gpt-oss-20b")
    ch = decide(ask, akash())
    assert ch.outcome is Outcome.ROUTE
    assert ch.max_tokens == 16 + 2 * 255              # caller's room + 2x the reasoning seen
    assert "raised to 526" in ch.because and ch.facts["max_tokens_raised_from"] == 16
    # never silent: without clamping the same request is refused
    assert decide(replace(ask, allow_clamp=False), akash()).outcome is Outcome.ABSTAIN


def test_billed_provider_keeps_its_measured_price():
    billed = _seat("venice:x", 0.01, 0.01, Emits.CONTENT, measured_usd_per_mtok=0.5,
                   measured_basis="billed")
    a = assess(Ask(prompt_tokens=40, max_tokens=10), billed)
    assert a.price_basis == "measured, billed" and a.usd_per_mtok == 0.5


def test_named_model_skips_the_lane_latency_gate():
    slow = _seat("sail:m", 0.05, 0.20, Emits.CONTENT, lat=2.4)
    ask = Ask(prompt_tokens=40, max_tokens=50, interactive_max_latency_s=1.2, only_seat="sail:m")
    assert decide(ask, [slow]).seat == "sail:m"


# --- the multi-variable decision (2026-09-26) -------------------------------------------
from decimal import Decimal as D

from modelrouter.decision import decide_ensemble


def test_cost_per_answer_not_per_token_inverts_the_rate_card():
    """$2.00/M output in 2 tokens beats $0.10/M that burns 200 reasoning tokens first."""
    gemma = _seat("sail:google/gemma-4-12B-it", "0.30", "2.00", Emits.CONTENT,
                  has_reasoning_field=False)
    cheap = _seat("x:reasoner", "0.05", "0.10", Emits.REASONING_THEN_CONTENT, floor=201, oh=200)
    # Declared answer length: 2 tokens. Gemma burns 2; the reasoner burns 200 + 2.
    ch = decide(Ask(prompt_tokens=20, max_tokens=250, answer_tokens=2), [gemma, cheap])
    assert ch.seat == "sail:google/gemma-4-12B-it"
    by = {a.seat: a.expected_usd for a in ch.considered}
    assert by["sail:google/gemma-4-12B-it"] == D("0.00001")        # (20*0.30 + 2*2.00) / 1e6
    assert by["x:reasoner"] == D("0.0000212")                       # (20*0.05 + 202*0.10) / 1e6
    # Priced at the cap instead, 250 answer tokens make the $0.10 reasoner genuinely cheaper:
    # the inversion is about tokens burned, so the answer length has to be an input.
    assert decide(Ask(prompt_tokens=20, max_tokens=250), [gemma, cheap]).seat == "x:reasoner"


def test_money_is_exact_decimal_from_price_strings():
    s = _seat("akashml:gpt-oss-120b", D("0.03"), D("0.17"), Emits.CONTENT)
    a = assess(Ask(prompt_tokens=1000, max_tokens=1000), s)
    assert a.expected_usd == D("0.00020")          # (1000*0.03 + 1000*0.17) / 1e6, no float drift
    assert isinstance(a.expected_usd, D)


def test_warm_prompt_cache_makes_the_same_seat_cheaper_so_routing_stays_sticky():
    warm = _seat("sail:kimi", "2.50", "12.50", Emits.CONTENT, list_cached="0.25")
    cold = _seat("ionet:kimi", "2.00", "12.50", Emits.CONTENT, list_cached="1.00")
    ask = Ask(prompt_tokens=8000, max_tokens=100)
    assert decide(ask, [warm, cold]).seat == "ionet:kimi"            # cold: cheaper input wins
    ch = decide(replace(ask, warm=(("sail:kimi", 7900),)), [warm, cold])
    assert ch.seat == "sail:kimi" and "7900 prompt tokens warm" in ch.because


def test_stale_price_is_unknown_not_fact():
    s = _seat("sail:m", "0.1", "0.1", Emits.CONTENT, price_age_s=3 * 86400)
    a = assess(Ask(prompt_tokens=10, max_price_age_s=172800), s)
    assert a.verdict is Verdict.UNKNOWN and "stale" in a.because


def test_load_factor_scales_predicted_latency():
    busy = _seat("ionet:m", "0.1", "0.1", Emits.CONTENT, lat=1.0, tps=100, load_factor=1.6,
                 in_flight=63)
    a = assess(Ask(prompt_tokens=10, max_tokens=500, interactive_max_latency_s=8.0), busy)
    assert a.verdict is Verdict.EXCLUDED and "x1.60 at 63 in flight" in a.because


def test_ensemble_hook_picks_cheapest_per_family_only():
    seats = [_seat("a:qwen/x", "0.1", "0.1", Emits.CONTENT), _seat("b:qwen/y", "0.2", "0.2", Emits.CONTENT),
             _seat("c:meta/z", "0.3", "0.3", Emits.CONTENT), _seat("d:google/w", "0.4", "0.4", Emits.CONTENT)]
    got = [a.seat for a in decide_ensemble(Ask(prompt_tokens=10, max_tokens=10), seats, k=3)]
    assert got == ["a:qwen/x", "c:meta/z", "d:google/w"]


def test_kimi_k3_has_no_single_cheapest_provider_it_depends_on_the_token_split():
    """Measured 2026-09-26. Sail flex 1.25/6.25; Nous 1.0301/9.043 ($/M in/out, live API).
    Nous wins iff 1.0301P + 9.043C < 1.25P + 6.25C, i.e. P/C > 2.793/0.2199 = 12.70."""
    sail = _seat("sail:moonshotai/Kimi-K3", "1.25", "6.25", Emits.CONTENT, window="flex")
    nous = _seat("nous:moonshotai/kimi-k3", "1.0301", "9.043", Emits.CONTENT)
    rag = Ask(prompt_tokens=20000, max_tokens=1000, answer_tokens=1000, lane="batch")    # 20:1
    gen = Ask(prompt_tokens=2000, max_tokens=2000, answer_tokens=2000, lane="batch")     # 1:1
    assert decide(rag, [sail, nous]).seat == "nous:moonshotai/kimi-k3"
    assert decide(gen, [sail, nous]).seat == "sail:moonshotai/Kimi-K3"
    by = {a.seat: a.expected_usd for a in decide(rag, [sail, nous]).considered}
    assert by["nous:moonshotai/kimi-k3"] == D("0.029645")         # (20000*1.0301 + 1000*9.043)/1e6
    assert by["sail:moonshotai/Kimi-K3"] == D("0.03125")          # (20000*1.25 + 1000*6.25)/1e6
    near = Ask(prompt_tokens=12700, max_tokens=1000, answer_tokens=1000, lane="batch")    # the knee
    a = {x.seat: x.expected_usd for x in decide(near, [sail, nous]).considered}
    assert abs(a["nous:moonshotai/kimi-k3"] - a["sail:moonshotai/Kimi-K3"]) < D("0.00001")


def test_upstream_fault_takes_out_correlated_providers_not_just_the_one_that_failed():
    orr = _seat("openrouter:m", "0.10", "0.10", Emits.CONTENT, correlated_with=("nous",))
    nous = _seat("nous:m", "0.05", "0.05", Emits.CONTENT, correlated_with=("openrouter",))
    indep = _seat("ionet:m", "0.20", "0.20", Emits.CONTENT)
    ask = Ask(prompt_tokens=10, max_tokens=10)
    assert decide(ask, [orr, nous, indep]).seat == "nous:m"              # cheaper, yes
    after = replace(ask, upstream_down=(("nous", "HTTP 502 bad gateway"),))
    ch = decide(after, [orr, nous, indep])
    assert ch.seat == "ionet:m"                                           # redundant, no
    why = {a.seat: a.because for a in ch.considered}["openrouter:m"]
    assert "correlated with nous" in why and "not an independent second chance" in why


def test_billed_provider_keeps_the_card_split_scaled_by_its_measured_billing_ratio():
    nous = _seat("nous:moonshotai/kimi-k3", "1.0301", "9.043", Emits.CONTENT,
                 measured_usd_per_mtok=D("3.0"), measured_basis="billed", billing_ratio=D("1.1"))
    a = assess(Ask(prompt_tokens=20000, max_tokens=1000, answer_tokens=1000), nous)
    assert a.expected_usd == D("0.029645") * D("1.1")        # split preserved, then scaled
    assert "x1.1 billed/card measured" in a.price_basis


def test_kimi_k3_with_nous_billed_rates_sail_flex_wins_every_split():
    sail = _seat("sail:moonshotai/Kimi-K3", "1.25", "6.25", Emits.CONTENT, window="flex")
    nous = _seat("nous:moonshotai/kimi-k3", "1.0301", "9.043", Emits.CONTENT,
                 billed_prompt=D("3"), billed_completion=D("15"),
                 billed_evidence="solved from 4 billed calls")
    for p, c in ((20000, 1000), (2000, 2000), (100000, 100)):
        ch = decide(Ask(prompt_tokens=p, max_tokens=c, answer_tokens=c, lane="batch"), [sail, nous])
        assert ch.seat == "sail:moonshotai/Kimi-K3"
    why = {a.seat: a.price_basis for a in ch.considered}["nous:moonshotai/kimi-k3"]
    assert "billed rates 3/15 per M (solved from 4 billed calls)" in why
