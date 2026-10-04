"""The hard daily Jev spend cap: exact cost, blocks at the cap, falls back, resets at midnight."""
import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from modelrouter import jev, jevguard
from modelrouter.jevguard import CapReached, Guard, GuardConfig, HttpStatus
from modelrouter.seats import Outcome, SeatRequest, resolve

BODY = {"model": "jev-latest", "state": {"t": "x" * 400},
        "questions": {"q": {"type": "choice", "instructions": "pick", "criteria": {"a": "A", "b": "B"}}}}
ANSWER = {"q": {"type": "choice", "choice": "a", "confidence": 0.9, "probabilities": {"a": 0.9, "b": 0.1}}}


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 4, 12, 0, tzinfo=timezone(timedelta(hours=3)))

    def __call__(self):
        return self.t


def nbytes(body):
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


# A price that makes one BODY call cost about 4 cents, so a test crosses the cap in a few
# calls. The fake bills one token per request byte: the most a real request can be billed.
PRICE = "200"
PER_CALL = D(nbytes(BODY)) * D(PRICE) / 10 ** 6


class Paid:
    """A fake TypeSafe: counts the calls that would have been billed."""

    def __init__(self, input_tokens=None, usage=True):
        self.calls, self.input_tokens, self.usage = 0, input_tokens, usage

    def __call__(self, key, body, timeout):
        assert key == "k"
        self.calls += 1
        r = {"model": "jev-1.13.0", "answers": ANSWER}
        if self.usage:
            r["usage"] = {"input_tokens": self.input_tokens or nbytes(body), "output_tokens": 18}
        return r


def guard(tmp_path, paid, *, cap="1.00", fallback=None, clock=None, alerts=None, **kw):
    kw.setdefault("usd_per_mtok_in", PRICE)
    cfg = GuardConfig(cap_usd=cap, ledger=tmp_path / "jev_spend.sqlite3", fallback="defer", **kw)
    return Guard(cfg, lambda: "k", post=paid, fallback=fallback, now=clock or Clock(),
                 alert=(alerts.append if alerts is not None else lambda e: None))


def test_cost_is_typesafes_published_price_input_only():
    cfg = GuardConfig()
    assert cfg.cost_nano(1_000_000, 50_000) == 42_000_000            # $0.042 per Mtok in, output free
    assert jevguard.usd(cfg.cost_nano(7_000, 144)) == "0.000294"     # one ~7k-token call
    assert cfg.cost_nano(1, 0) == 42                                 # 42 nano-USD per token, exact


def test_a_call_is_charged_its_actual_usage(tmp_path):
    g = guard(tmp_path, Paid(input_tokens=7_000), usd_per_mtok_in="0.042")
    big = {**BODY, "state": {"t": "x" * 40_000}}
    s = g.call(big, caller="t")
    assert s.served_by == "jev" and s.blocked is False and s.cost_usd == "0.000294"
    st = g.status()
    assert st["spent_usd"] == "0.000294" and st["paid_calls"] == 1 and st["input_tokens"] == 7_000


def test_the_cap_blocks_the_paid_call_and_the_free_fallback_answers(tmp_path):
    paid, alerts, free = Paid(), [], []

    def laya(body, timeout):
        free.append(body)
        return {"model": "laya-local", "answers": ANSWER}

    g = guard(tmp_path, paid, fallback=laya, alerts=alerts)
    served = [g.call(BODY, caller="pipeline") for _ in range(40)]
    n = int(D("1.00") / PER_CALL)                    # the calls that fit under $1.00
    assert 5 < n < 35 and paid.calls == n            # the next one could cross $1.00: not made
    assert [s.served_by for s in served[:n]] == ["jev"] * n
    assert all(s.blocked and s.served_by.startswith("fallback:") and s.cost_usd == "0" for s in served[n:])
    assert len(free) == 40 - n and served[-1].response["model"] == "laya-local"
    st = g.status()
    assert D(st["spent_usd"]) == n * PER_CALL <= D("1.00") < (n + 1) * PER_CALL
    assert st["blocked_calls"] == 40 - n and st["cap_tripped_at"]
    assert len(alerts) == 1 and alerts[0]["event"] == "jev_daily_cap_tripped"     # once, not 17 times


def test_with_no_fallback_the_call_is_deferred_not_paid(tmp_path):
    paid = Paid()
    g = guard(tmp_path, paid, cap="0.15")
    g.call(BODY)
    with pytest.raises(CapReached) as e:
        g.call(BODY)
    assert paid.calls == 1 and e.value.retry_after_s == 12 * 3600 and "cap $0.15 reached" in str(e.value)


def test_a_failing_fallback_defers_and_never_pays(tmp_path):
    paid = Paid()

    def broken(body, timeout):
        raise OSError("model not loaded")
    g = guard(tmp_path, paid, cap="0.00001", fallback=broken)
    with pytest.raises(CapReached):
        g.call(BODY)
    assert paid.calls == 0


def test_the_cap_resets_at_local_midnight_and_persists_across_processes(tmp_path):
    clock, paid = Clock(), Paid()
    g = guard(tmp_path, paid, cap="0.15", clock=clock)
    g.call(BODY)
    again = guard(tmp_path, paid, cap="0.15", clock=clock)       # a second process, same ledger
    with pytest.raises(CapReached):
        again.call(BODY)
    clock.t += timedelta(hours=11, minutes=59)                    # 23:59 local: still today
    with pytest.raises(CapReached):
        again.call(BODY)
    clock.t += timedelta(minutes=2)                               # 00:01 local: a new day
    assert again.call(BODY).served_by == "jev" and paid.calls == 2
    assert again.status()["day"] == "2026-10-05"


def test_unknown_billing_is_charged_the_worst_case_never_zero(tmp_path):
    def timeout(key, body, t):
        raise TimeoutError
    g = guard(tmp_path, timeout)
    with pytest.raises(TimeoutError):
        g.call(BODY)
    worst = g.cfg.worst_case_nano(BODY)
    assert D(g.status()["spent_usd"]) == D(worst) / 10 ** 9 > 0
    g2 = guard(tmp_path / "b", Paid(usage=False))                 # answered, but no usage object
    g2.call(BODY)
    assert g2.status()["unknown_billing_calls"] == 1 and D(g2.status()["spent_usd"]) > 0


def test_an_http_refusal_is_not_billed(tmp_path):
    def refused(key, body, t):
        raise HttpStatus(401)
    g = guard(tmp_path, refused)
    with pytest.raises(HttpStatus):
        g.call(BODY)
    assert g.status()["spent_usd"] == "0"


def test_the_reservation_is_an_upper_bound_on_the_real_cost(tmp_path):
    # 2026-10-01, glass-mem ledger: 2,150,541 state chars were billed as 961,466 input tokens.
    # One token per UTF-8 byte is therefore a safe ceiling, also for Cyrillic (2 bytes a letter).
    cfg = GuardConfig()
    body = {"state": "Ж" * 1000, "questions": {"q": {"type": "noul", "instructions": "да?"}}}
    assert cfg.worst_case_nano(body) >= cfg.cost_nano(2000, 0)


def test_concurrent_callers_cannot_jointly_overshoot_the_cap(tmp_path):
    paid, lock = Paid(), threading.Lock()

    def slow(key, body, t):
        with lock:
            return paid(key, body, t)
    guards = [guard(tmp_path, slow, cap="0.50") for _ in range(8)]
    errs = []

    def run(g):
        for _ in range(6):
            try:
                g.call(BODY)
            except CapReached:
                errs.append(1)
    ts = [threading.Thread(target=run, args=(g,)) for g in guards]
    [t.start() for t in ts]
    [t.join() for t in ts]
    n = int(D("0.50") / PER_CALL)
    assert paid.calls == n and len(errs) == 48 - n
    assert D(guards[0].status()["spent_usd"]) == n * PER_CALL <= D("0.50")


def test_config_is_validated_and_env_overrides_it(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        GuardConfig(cap_usd="-1")
    with pytest.raises(ValueError):
        GuardConfig(fallback="pay-anyway")
    with pytest.raises(ValueError):
        jevguard.url_fallback("https://api.typesafe.ai/v1/systemone")     # a fallback must be free
    monkeypatch.setenv("JEV_DAILY_CAP_USD", "0.25")
    assert GuardConfig.from_env().cap_usd == D("0.25")
    from modelrouter import config
    p = tmp_path / "c.toml"
    p.write_text(config.TEMPLATE.replace('gcp_project = ""', 'gcp_project = "p"'), encoding="utf-8")
    monkeypatch.delenv("JEV_DAILY_CAP_USD")
    g = config.load(p).guard_config()
    assert g.cap_usd == D("1.00") and g.usd_per_mtok_in == D("0.042") and g.usd_per_mtok_out == 0
    assert g.ledger == tmp_path / "jev_spend.sqlite3"


def test_the_alert_is_written_to_the_alerts_file(tmp_path):
    cfg = GuardConfig(cap_usd="0", ledger=tmp_path / "jev_spend.sqlite3", fallback="defer")
    g = Guard(cfg, lambda: "k", post=Paid(), fallback=None, now=Clock())
    with pytest.raises(CapReached):
        g.call(BODY, caller="kalo-canon")
    ev = json.loads((tmp_path / "jev_guard_alerts.jsonl").read_text("utf-8").splitlines()[0])
    assert ev["event"] == "jev_daily_cap_tripped" and ev["caller"] == "kalo-canon" and ev["cap_usd"] == "0"


# ---- the seat advisor goes through the guard ------------------------------------------------
def _pool():
    from tests.test_seats import POOL
    return POOL


def test_the_seat_advisor_stops_paying_at_the_cap_and_keeps_deciding(tmp_path):
    paid = Paid()
    paid_answer = paid.__call__

    def post(key, body, t):
        r = paid_answer(key, body, t)
        seat = "sail:Qwen/Qwen3-32B"
        r["answers"] = {"seat": {"choice": seat, "probabilities": {seat: 0.95}}}
        return r
    g = guard(tmp_path, post, cap="0.30")
    adv = jev.make_advisor(lambda: "k", guard=g)
    first = resolve(SeatRequest(task="t"), _pool(), configured_policy="cheapest", advisor=adv)
    second = resolve(SeatRequest(task="t"), _pool(), configured_policy="cheapest", advisor=adv)
    assert first.jev["used"] is True and first.jev["served_by"] == "jev"
    assert second.outcome is Outcome.SEAT and second.jev["used"] is False
    assert "daily cap reached" in second.jev["why"] and paid.calls == 1


def test_an_unguarded_paid_advisor_is_refused():
    with pytest.raises(ValueError):
        jev.make_advisor(lambda: "k")


def test_the_advisor_payload_does_not_send_price_facts_twice():
    body = jev.build_request(SeatRequest(role="review", task="t"), _pool()[:3])
    assert set(body["state"]["seats"][0]) == {"seat", "family", "context"}
    crit = body["questions"]["seat"]["criteria"]
    assert all("tier" in v for v in crit.values()) and any("$" in v for v in crit.values())


# ---- the HTTP gateway -------------------------------------------------------------------
@pytest.fixture
def gateway(tmp_path):
    from fastapi.testclient import TestClient
    from modelrouter import config, server
    p = tmp_path / "c.toml"
    p.write_text('[secrets]\nsource="env"\n[providers.openrouter]\nsecret="NOPE"\n'
                 '[server]\nno_auth=true\n[policy]\ndashboard_url=""\n'
                 '[jev]\nenabled=true\ndaily_cap_usd=0.25\nusd_per_mtok_in=200\nfallback="defer"\n'
                 '[local]\nollama_url="http://127.0.0.1:9"\n', encoding="utf-8")
    cfg = config.load(p)
    cfg.state_dir = tmp_path
    app = server.build(cfg)
    paid = Paid()
    app.state.jev_guard = Guard(cfg.guard_config(), lambda: "k", post=paid, fallback=None)
    return TestClient(app), paid, app


def test_gateway_serves_jev_then_returns_429_at_the_cap(gateway):
    client, paid, _ = gateway
    ok = [client.post("/v1/systemone", json=BODY, headers={"X-Jev-Caller": "kalo-canon"}) for _ in range(2)]
    assert [r.status_code for r in ok] == [200, 200] and D(ok[1].json()["guard"]["spent_usd"]) == 2 * PER_CALL
    assert ok[0].json()["answers"] == ANSWER and ok[0].headers["X-Jev-Served-By"] == "jev"
    blocked = client.post("/v1/systemone", json=BODY)
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "jev_daily_cap_reached"
    assert int(blocked.headers["Retry-After"]) > 0 and paid.calls == 2
    spend = client.get("/v1/systemone/spend").json()
    assert D(spend["spent_usd"]) == 2 * PER_CALL <= D("0.25") and spend["blocked_calls"] == 1


def test_gateway_falls_back_to_the_free_model_at_the_cap(gateway):
    client, paid, app = gateway
    g = app.state.jev_guard
    app.state.jev_guard = Guard(g.cfg, lambda: "k", post=paid,
                                fallback=lambda body, t: {"model": "laya-local", "answers": ANSWER})
    rs = [client.post("/v1/systemone", json=BODY) for _ in range(4)]
    assert [r.status_code for r in rs] == [200] * 4 and paid.calls == 2
    assert [r.json()["guard"]["served_by"] for r in rs] == ["jev", "jev", "fallback:custom", "fallback:custom"]
    assert rs[3].json()["model"] == "laya-local" and rs[3].json()["guard"]["blocked"] is True


def test_gateway_forwards_only_model_state_questions_and_rejects_bad_bodies(gateway):
    client, paid, app = gateway
    seen = []

    def post(key, body, t):
        seen.append(body)
        return paid(key, body, t)
    app.state.jev_guard = Guard(app.state.jev_guard.cfg, lambda: "k", post=post, fallback=None)
    client.post("/v1/systemone", json={**BODY, "api_key": "leak", "debug": {"big": "x" * 5000}})
    assert set(seen[0]) == {"model", "state", "questions"}
    assert client.post("/v1/systemone", json={"state": "s"}).status_code == 400
