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


# A price that makes one BODY call cost about 8 cents, so a test crosses the cap in a few
# calls. The fake bills one token per request byte: the most a real request can be billed.
PRICE = "50"
BOUND = nbytes(BODY) + jevguard.REQUEST_OVERHEAD_TOKENS + jevguard.QUESTION_OVERHEAD_TOKENS
PER_CALL = D(BOUND) * D(PRICE) / 10 ** 6


class Paid:
    """A fake TypeSafe: counts the calls that would have been billed."""

    def __init__(self, input_tokens=None, usage=True):
        self.calls, self.input_tokens, self.usage = 0, input_tokens, usage

    def __call__(self, key, body, timeout):
        assert key == "k"
        self.calls += 1
        r = {"model": "jev-1.13.0", "answers": ANSWER}
        if self.usage:
            full = nbytes(body) + jevguard.REQUEST_OVERHEAD_TOKENS + jevguard.QUESTION_OVERHEAD_TOKENS * len(body["questions"])
            r["usage"] = {"input_tokens": self.input_tokens or full, "output_tokens": 18}
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


def test_the_cap_resets_at_local_midnight_and_is_read_from_the_ledger_not_memory(tmp_path):
    clock, paid = Clock(), Paid()
    g = guard(tmp_path, paid, cap="0.15", clock=clock)
    g.call(BODY)
    again = guard(tmp_path, paid, cap="0.15", clock=clock)       # a second Guard, same ledger file
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
    g = guard(tmp_path, post, cap="0.12")
    adv = jev.make_advisor(lambda: "k", guard=g)
    first = resolve(SeatRequest(task="t"), _pool(), configured_policy="cheapest", advisor=adv)
    second = resolve(SeatRequest(task="t"), _pool(), configured_policy="cheapest", advisor=adv)
    assert first.jev["used"] is True and first.jev["served_by"] == "jev"
    assert second.outcome is Outcome.SEAT and second.jev["used"] is False
    assert "daily cap reached" in second.jev["why"] and paid.calls == 1


def test_an_unguarded_paid_advisor_is_refused():
    with pytest.raises(TypeError):
        jev.make_advisor(lambda: "k")                                # no guard: no advisor
    with pytest.raises(TypeError):
        jev.make_advisor(lambda: "k", guard=None)
    with pytest.raises(TypeError):                                   # the old test-only bypass is gone
        jev.make_advisor(lambda: "k", post=lambda key, body, t: {})


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
                 '[jev]\nenabled=true\ndaily_cap_usd=0.20\nusd_per_mtok_in=50\nfallback="defer"\n'
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
    assert D(spend["spent_usd"]) == 2 * PER_CALL <= D("0.20") and spend["blocked_calls"] == 1


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


# ---- findings of the cross-family review (Codex, PR #8) -------------------------------------
def test_a_5xx_may_have_been_billed_and_is_charged_the_worst_case(tmp_path):
    def five_hundred(key, body, t):
        raise HttpStatus(500)
    g = guard(tmp_path, five_hundred)
    with pytest.raises(HttpStatus):
        g.call(BODY)
    st = g.status()
    assert D(st["spent_usd"]) == PER_CALL and st["unknown_billing_calls"] == 1
    for code in (401, 403):                                      # the key was refused: free
        def refused(key, body, t, code=code):
            raise HttpStatus(code)
        g2 = guard(tmp_path / str(code), refused)
        with pytest.raises(HttpStatus):
            g2.call(BODY)
        assert g2.status()["spent_usd"] == "0"


def test_repeated_5xx_cannot_buy_unlimited_calls(tmp_path):
    sent = []

    def five_hundred(key, body, t):
        sent.append(1)
        raise HttpStatus(502)
    g = guard(tmp_path, five_hundred, cap="0.50")
    for _ in range(30):
        with pytest.raises((HttpStatus, CapReached)):
            g.call(BODY)
    assert len(sent) == int(D("0.50") / PER_CALL) and D(g.status()["spent_usd"]) <= D("0.50")


def test_the_price_cannot_be_configured_below_the_published_one_nor_the_cap_absurdly_high(monkeypatch):
    for bad in ({"usd_per_mtok_in": "0"}, {"usd_per_mtok_in": "0.0419"}, {"usd_per_mtok_in": "NaN"},
                {"usd_per_mtok_out": "-1"}, {"cap_usd": "1e100"}, {"cap_usd": "100.01"},
                {"cap_usd": "Infinity"}):
        with pytest.raises(ValueError):
            GuardConfig(**bad)
    monkeypatch.setenv("JEV_USD_PER_MTOK_IN", "0")               # the env cannot make calls free
    with pytest.raises(ValueError):
        GuardConfig.from_env()
    assert GuardConfig(cap_usd="100", usd_per_mtok_in="0.05").cost_nano(10 ** 6, 0) == 50_000_000


def test_every_failure_after_the_reservation_settles_it(tmp_path):
    def no_key():
        raise OSError("vault unreachable")
    paid = Paid()
    cfg = GuardConfig(cap_usd="1", usd_per_mtok_in=PRICE, ledger=tmp_path / "l.sqlite3", fallback="defer")
    g = Guard(cfg, no_key, post=paid, fallback=None, now=Clock(), alert=lambda e: None)
    with pytest.raises(OSError):
        g.call(BODY)
    st = g.status()                                               # nothing sent: free, not stranded
    assert paid.calls == 0 and st["spent_usd"] == "0" and st["in_flight_usd"] == "0"

    for usage in ("lots", {"input_tokens": "9", "output_tokens": 1}, {"input_tokens": True, "output_tokens": 1},
                  {"input_tokens": -5, "output_tokens": 0}, None):
        d = tmp_path / str(abs(hash(str(usage))))
        g2 = guard(d, lambda key, body, t, u=usage: {"answers": {}, "usage": u})
        g2.call(BODY)
        st = g2.status()
        assert D(st["spent_usd"]) == PER_CALL and st["unknown_billing_calls"] == 1 and st["in_flight_usd"] == "0"


def test_the_reservation_bounds_any_answer_the_api_can_give(tmp_path):
    cfg = GuardConfig(usd_per_mtok_in="0.042", usd_per_mtok_out="3")
    n = jevguard.request_bytes(BODY)
    assert cfg.worst_case_nano(BODY) == cfg.cost_nano(BOUND, jevguard.MAX_OUTPUT_TOKENS)
    assert cfg.worst_case_nano(BODY) >= cfg.cost_nano(n, 50_000)          # a very long answer
    with pytest.raises(ValueError):                                       # too large to bound
        cfg.worst_case_nano({"state": "x" * (jevguard.MAX_REQUEST_BYTES + 1), "questions": {}})


def test_separate_processes_share_one_cap(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    worker = Path(__file__).with_name("_jev_worker.py")
    ledger = tmp_path / "shared.sqlite3"
    procs = [subprocess.Popen([sys.executable, str(worker), str(ledger), "0.50", PRICE, "6"],
                              stdout=subprocess.PIPE, text=True) for _ in range(4)]
    paid = sum(json.loads(p.communicate(timeout=120)[0])["paid"] for p in procs)
    assert all(p.returncode == 0 for p in procs)
    n = int(D("0.50") / PER_CALL)
    assert paid == n                                              # 24 attempts, 4 processes, n paid
    st = Guard(GuardConfig(cap_usd="0.50", usd_per_mtok_in=PRICE, ledger=ledger, fallback="defer"),
               lambda: "k", fallback=None).status()
    assert D(st["spent_usd"]) == n * PER_CALL <= D("0.50") and st["blocked_calls"] == 24 - n


def test_the_fallback_must_be_loopback_and_redirects_are_not_followed(tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    for bad in ("https://api.typesafe.ai/v1/systemone", "http://proxy.internal:8080/x",
                "http://127.0.0.1.evil.example/x", "ftp://127.0.0.1/x", "http://10.0.0.5/x"):
        with pytest.raises(ValueError):
            jevguard.url_fallback(bad)
        with pytest.raises(ValueError):
            GuardConfig(fallback="url", fallback_url=bad)
    hits = []

    class Target(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)

    class Redirector(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:%d/paid" % target.server_address[1])
            self.end_headers()

        def log_message(self, *a):
            pass

    red = HTTPServer(("127.0.0.1", 0), Redirector)
    for srv in (target, red):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    fb = jevguard.url_fallback("http://127.0.0.1:%d/v1/systemone" % red.server_address[1])
    with pytest.raises(HttpStatus) as e:
        fb(BODY, 5.0)
    assert e.value.code == 307 and hits == []                    # the redirect target was never called
    g = guard(tmp_path, Paid(), cap="0", fallback=fb)
    with pytest.raises(CapReached):                               # fallback failed: deferred, not paid
        g.call(BODY)
    target.shutdown()
    red.shutdown()


def test_gateway_refuses_a_request_too_large_to_bound(gateway):
    client, paid, _ = gateway
    big = {**BODY, "state": "x" * (jevguard.MAX_REQUEST_BYTES + 1)}
    r = client.post("/v1/systemone", json=big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "jev_request_too_large" and paid.calls == 0


# ---- second review round ---------------------------------------------------------------------
def test_a_429_is_not_assumed_free(tmp_path):
    def limited(key, body, t):
        raise HttpStatus(429)
    g = guard(tmp_path, limited)
    with pytest.raises(HttpStatus):
        g.call(BODY)
    assert D(g.status()["spent_usd"]) == PER_CALL and jevguard.NOT_BILLED_STATUSES == (401, 403)


def test_a_call_settled_after_midnight_is_spend_of_the_new_day(tmp_path):
    clock, paid = Clock(), Paid()
    clock.t = clock.t.replace(hour=23, minute=59, second=59)
    g = guard(tmp_path, paid, cap="0.15", clock=clock)
    worst = g.cfg.worst_case_nano(BODY)
    held, _, _ = g.ledger.reserve("straddler", worst, g.cfg.cap_nano, 1)
    clock.t += timedelta(seconds=5)                               # 00:00:04, and it lands now
    g.ledger.settle(held, "OK", worst, 500, 0)
    st = g.status()
    assert st["day"] == "2026-10-05" and D(st["spent_usd"]) == PER_CALL and st["in_flight_usd"] == "0"
    with pytest.raises(CapReached):                               # the new day already carries it
        g.call(BODY)
    assert paid.calls == 0


def test_the_guard_caps_the_timeout_so_no_call_outlives_its_hold(tmp_path):
    seen = []

    def post(key, body, t):
        seen.append(t)
        return {"answers": {}, "usage": {"input_tokens": 1, "output_tokens": 0}}
    guard(tmp_path, post).call(BODY, timeout=86_400)
    assert seen == [jevguard.MAX_TIMEOUT_S]


def test_a_ledger_from_before_the_ts_column_keeps_its_in_flight_holds(tmp_path):
    import sqlite3
    clock = Clock()
    p = tmp_path / "jev_spend.sqlite3"
    db = sqlite3.connect(p)
    db.execute("""CREATE TABLE jev_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
        at TEXT NOT NULL, caller TEXT NOT NULL, status TEXT NOT NULL, reserved_nano INTEGER NOT NULL,
        cost_nano INTEGER, input_tokens INTEGER, output_tokens INTEGER, request_bytes INTEGER NOT NULL,
        served_by TEXT, detail TEXT)""")
    made = clock.t - timedelta(minutes=10)
    worst = GuardConfig(usd_per_mtok_in=PRICE).worst_case_nano(BODY)
    db.execute("INSERT INTO jev_calls(day, at, caller, status, reserved_nano, request_bytes) "
               "VALUES ('2026-10-03', ?, 'old-process', 'RESERVED', ?, 1)", (made.isoformat(timespec="seconds"), worst))
    db.commit()
    db.close()
    paid = Paid()
    g = guard(tmp_path, paid, cap="0.15", clock=clock)            # the upgrade happens here
    ts = g.ledger._conn().execute("SELECT ts FROM jev_calls").fetchone()[0]
    assert ts == made.timestamp()                                 # aged from `at`, not from 1970
    assert D(g.status()["in_flight_usd"]) == PER_CALL
    with pytest.raises(CapReached):                               # the old hold still counts
        g.call(BODY)
    assert paid.calls == 0


def test_what_is_measured_is_what_is_sent(monkeypatch):
    sent = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"answers": {}}'

    def fake_open(req, timeout):
        sent["data"] = req.data
        return Resp()
    monkeypatch.setattr(jevguard._OPENER, "open", fake_open)
    body = {**BODY, "state": {"t": "Жълтата дюля беше щастлива. " * 20, "q": 'a "quoted" \\ value'}}
    jevguard._post("http://127.0.0.1:1/x", None, body, 1.0)
    assert len(sent["data"]) == jevguard.request_bytes(body) and json.loads(sent["data"]) == body


def test_only_a_literal_loopback_ip_is_a_fallback():
    for bad in ("http://localhost:8080/x", "http://LOCALHOST/x", "http://my-laya/x", "http://[::2]/x"):
        with pytest.raises(ValueError):
            jevguard.url_fallback(bad)
    assert jevguard.url_fallback("http://127.0.0.1:8660/v1/systemone")
    assert jevguard.url_fallback("http://[::1]:8660/v1/systemone")


# ---- third review round ----------------------------------------------------------------------
def test_the_bound_covers_what_the_live_api_billed():
    """Every (request bytes, questions, billed input tokens) triple measured live on 2026-10-04.
    The first two are tiny requests the bare one-token-per-byte bound did NOT cover."""
    cfg = GuardConfig()
    live = [(90, 1, 269), (124, 1, 303), (726, 16, 404), (1270, 16, 948), (3261, 1, 1060),
            (3975, 8, 1256), (5621, 24, 1704), (2278, 1, 1173), (2058, 1, 1021), (1824, 1, 891)]
    for nb, nq, billed in live:
        bound = nb + jevguard.REQUEST_OVERHEAD_TOKENS + jevguard.QUESTION_OVERHEAD_TOKENS * nq
        assert billed <= bound, (nb, nq, billed)
    tiny = {"model": "jev-latest", "state": "x", "questions": {"q0": {"type": "noul", "instructions": "ok?"}}}
    assert jevguard.request_bytes(tiny) == 90
    assert cfg.worst_case_nano(tiny) >= cfg.cost_nano(269, 0) * 4          # 4x the measured overhead


def test_a_swept_hold_is_charged_to_the_day_of_the_sweep(tmp_path):
    clock = Clock()
    clock.t = clock.t.replace(hour=23, minute=30)
    g = guard(tmp_path, Paid(), cap="0.15", clock=clock)
    g.ledger.reserve("crashed", g.cfg.worst_case_nano(BODY), g.cfg.cap_nano, 1)
    clock.t += timedelta(minutes=61)                              # 00:31 next day, past the TTL
    with pytest.raises(CapReached):                               # converted INTO today: still counts
        g.call(BODY)
    row = g.ledger._conn().execute("SELECT day, status FROM jev_calls WHERE caller = 'crashed'").fetchone()
    assert row == ("2026-10-05", "UNKNOWN_BILLING")
    clock.t += timedelta(days=1)                                  # it is spend of that day only
    assert g.call(BODY).served_by == "jev"


def test_an_overshoot_widens_the_holds_already_in_flight_in_the_same_transaction(tmp_path):
    alerts = []
    g = guard(tmp_path, Paid(input_tokens=10 ** 9), cap="100", alerts=alerts)
    base = g.cfg.worst_case_nano(BODY)
    a, _, _ = g.ledger.reserve("in-flight-a", base, g.cfg.cap_nano, 1)
    b, _, _ = g.ledger.reserve("in-flight-b", base, g.cfg.cap_nano, 1)
    c, _, _ = g.ledger.reserve("overshooter", base, g.cfg.cap_nano, 1)
    charged, factor = g.ledger.settle(c, "OK", 3 * base, 3, 0)    # billed 3x its reservation
    assert charged == 3 * base
    assert factor >= 3000 and g.ledger.bound_factor() == factor
    held = [r[0] for r in g.ledger._conn().execute(
        "SELECT reserved_nano FROM jev_calls WHERE status = 'RESERVED' ORDER BY id")]
    assert len(held) == 2 and all(h >= 3 * base for h in held)   # both live holds widened at once
    assert D(g.status()["spent_usd"]) * 10 ** 9 >= 3 * base + 2 * 3 * base
    other = guard(tmp_path, Paid(), cap="100")                    # another process: sees the factor
    other.call(BODY)
    last = other.ledger._conn().execute("SELECT reserved_nano FROM jev_calls ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert last >= 3 * base


# ---- fourth review round ---------------------------------------------------------------------
def test_a_call_past_its_deadline_keeps_its_hold_and_settles_itself_late(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(jevguard, "MAX_TIMEOUT_S", 0.2)
    release, sent, alerts = threading.Event(), [], []

    def drip(key, body, t):                                       # a transport that ignores its timeout
        sent.append(1)
        release.wait(20)
        return {"answers": {}, "usage": {"input_tokens": 3 * BOUND, "output_tokens": 0}}   # and overbills
    g = guard(tmp_path, drip, cap="0.15", alerts=alerts)
    t0 = time.time()
    with pytest.raises(TimeoutError):
        g.call(BODY, timeout=3600)
    assert time.time() - t0 < 5 and sent == [1]
    st = g.status()                                               # still HELD: not settled, not dropped
    assert D(st["in_flight_usd"]) == PER_CALL == D(st["spent_usd"]) and st["unknown_billing_calls"] == 0
    with pytest.raises(CapReached):                               # so it cannot make room for another
        g.call(BODY)
    assert sent == [1]                                            # the blocked call sent nothing
    release.set()                                                 # the late answer arrives
    for _ in range(100):
        if g.status()["in_flight_usd"] == "0":
            break
        time.sleep(0.05)
    st = g.status()
    assert D(st["spent_usd"]) == 3 * PER_CALL and st["in_flight_usd"] == "0"       # settled by the worker
    assert [a["event"] for a in alerts if a["event"] != "jev_daily_cap_tripped"] == ["jev_reservation_exceeded"]
    assert g.ledger.bound_factor() >= 3000                        # the late overbilling was detected


def test_the_bound_factor_is_read_under_the_reservation_lock(tmp_path):
    g = guard(tmp_path, Paid(), cap="100")
    base = g.cfg.worst_case_nano(BODY)
    stale = g.ledger.bound_factor()                               # what a caller read a moment ago
    other = guard(tmp_path, Paid(), cap="100")                    # another process widens it meanwhile
    c, reserved, _ = other.ledger.reserve("overshooter", base, other.cfg.cap_nano, 1)
    other.ledger.settle(c, "OK", 3 * base, 3, 0)
    cid, reserved, _ = g.ledger.reserve("late-reader", base, g.cfg.cap_nano, 1)
    assert stale == 1000 and reserved >= 3 * base                 # the fresh factor, not the stale one
    assert g.ledger._conn().execute("SELECT reserved_nano FROM jev_calls WHERE id = ?", (cid,)).fetchone()[0] == reserved


def test_reserve_reads_the_day_after_the_sweep_from_one_clock_snapshot(tmp_path):
    clock = Clock()
    clock.t = clock.t.replace(hour=23, minute=59, second=59)
    g = guard(tmp_path, Paid(), cap="0.15", clock=clock)
    clock.t -= timedelta(hours=2)
    g.ledger.reserve("crashed", g.cfg.worst_case_nano(BODY), g.cfg.cap_nano, 1)     # an old hold
    clock.t += timedelta(hours=2)
    ticks = []

    def ticking():                                                # midnight passes during reserve()
        ticks.append(1)
        return clock.t + timedelta(seconds=2 * (len(ticks) - 1))
    g.ledger._now = ticking
    cid, worst, spent = g.ledger.reserve("at-midnight", g.cfg.worst_case_nano(BODY), g.cfg.cap_nano, 1)
    assert len(ticks) == 1                                        # one snapshot for sweep + day + insert
    assert cid is None and spent == worst                         # the swept hold counted in ITS day's sum


def test_a_swept_row_is_terminal_a_late_result_cannot_lower_or_move_its_charge(tmp_path):
    clock = Clock()
    g = guard(tmp_path, Paid(), cap="100", clock=clock)
    worst = g.cfg.worst_case_nano(BODY)
    cid, _, _ = g.ledger.reserve("slow", worst, g.cfg.cap_nano, 1)
    clock.t += timedelta(minutes=61)
    g.ledger.reserve("trigger-sweep", worst, g.cfg.cap_nano, 1)
    clock.t += timedelta(days=1)
    _, factor = g.ledger.settle(cid, "OK", 10, 5, 0)              # a cheap late result
    assert factor is None
    row = g.ledger._conn().execute("SELECT day, status, cost_nano, detail FROM jev_calls WHERE id = ?", (cid,)).fetchone()
    assert row[:3] == ("2026-10-04", "UNKNOWN_BILLING", worst) and "late result: OK" in row[3]
    _, factor = g.ledger.settle(cid, "OK", 2 * worst, 9, 0)       # a late result ABOVE it does raise it
    assert factor
    assert g.ledger._conn().execute("SELECT cost_nano FROM jev_calls WHERE id = ?", (cid,)).fetchone()[0] == 2 * worst


# ---- fifth review round ----------------------------------------------------------------------
def test_unknown_billing_charges_the_hold_as_widened_not_as_first_captured(tmp_path):
    g = guard(tmp_path, Paid(), cap="100")
    base = g.cfg.worst_case_nano(BODY)
    a, worst_a, _ = g.ledger.reserve("in-flight", base, g.cfg.cap_nano, 1)        # captures W
    b, _, _ = g.ledger.reserve("overshooter", base, g.cfg.cap_nano, 1)
    g.ledger.settle(b, "OK", 3 * base, 3, 0)                      # widens A's hold to >= 3W
    widened = g.ledger._conn().execute("SELECT reserved_nano FROM jev_calls WHERE id = ?", (a,)).fetchone()[0]
    assert worst_a == base and widened >= 3 * base
    g.ledger.settle(a, "UNKNOWN_BILLING", worst_a, None, None, "HTTP 500")        # A fails, passing stale W
    cost = g.ledger._conn().execute("SELECT cost_nano FROM jev_calls WHERE id = ?", (a,)).fetchone()[0]
    assert cost == widened                                        # charged the widened hold
    c, _, _ = g.ledger.reserve("free", base, g.cfg.cap_nano, 1)   # NOT_BILLED still settles at zero
    g.ledger.settle(c, "NOT_BILLED", 0, None, None, "HTTP 401")
    assert g.ledger._conn().execute("SELECT cost_nano FROM jev_calls WHERE id = ?", (c,)).fetchone()[0] == 0


def test_a_live_call_is_never_swept_and_stays_widenable_however_long_it_takes(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(jevguard, "MAX_TIMEOUT_S", 0.2)
    clock, release, sent = Clock(), threading.Event(), []

    def stuck(key, body, t):
        sent.append(1)
        release.wait(20)
        return {"answers": {}, "usage": {"input_tokens": 10, "output_tokens": 0}}
    g = guard(tmp_path, stuck, cap="100", clock=clock)
    base = g.cfg.worst_case_nano(BODY)
    with pytest.raises(TimeoutError):
        g.call(BODY)
    clock.t += timedelta(hours=3)                                 # far past the TTL, thread still alive
    assert g.ledger.beat() == 1                                   # the heartbeat re-stamps its hold
    other = guard(tmp_path, Paid(), cap="100", clock=clock)       # another process sweeps
    c, _, _ = other.ledger.reserve("overshooter", base, other.cfg.cap_nano, 1)
    row = other.ledger._conn().execute("SELECT status FROM jev_calls ORDER BY id LIMIT 1").fetchone()
    assert row == ("RESERVED",)                                   # not swept: still a live hold
    other.ledger.settle(c, "OK", 3 * base, 3, 0)                  # and it IS widened with the bound
    assert other.ledger._conn().execute("SELECT reserved_nano FROM jev_calls ORDER BY id LIMIT 1").fetchone()[0] >= 3 * base
    release.set()
    for _ in range(100):
        if g.status()["in_flight_usd"] == "0":
            break
        time.sleep(0.05)
    assert g.ledger.beat() == 0 and g.ledger._live == set()       # finished: nothing left to beat


def test_a_dead_process_stops_beating_and_its_hold_is_swept(tmp_path):
    clock = Clock()
    g = guard(tmp_path, Paid(), cap="100", clock=clock)
    cid, _, _ = g.ledger.reserve("crashed-process", g.cfg.worst_case_nano(BODY), g.cfg.cap_nano, 1)
    clock.t += timedelta(hours=3)
    assert g.ledger.beat() == 0                                   # nobody claims it as alive
    g.ledger.reserve("trigger-sweep", 1, g.cfg.cap_nano, 1)
    assert g.ledger._conn().execute("SELECT status FROM jev_calls WHERE id = ?", (cid,)).fetchone() == ("UNKNOWN_BILLING",)


def test_the_heartbeat_thread_runs_by_itself(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(jevguard, "HEARTBEAT_S", 0.05)
    clock = Clock()
    g = guard(tmp_path, Paid(), cap="100", clock=clock)
    cid, _, _ = g.ledger.reserve("live", g.cfg.worst_case_nano(BODY), g.cfg.cap_nano, 1)
    g.ledger.alive(cid, True)
    clock.t += timedelta(hours=5)
    want = clock.t.timestamp()
    for _ in range(100):
        if g.ledger._conn().execute("SELECT ts FROM jev_calls WHERE id = ?", (cid,)).fetchone()[0] == want:
            break
        time.sleep(0.05)
    assert g.ledger._conn().execute("SELECT ts FROM jev_calls WHERE id = ?", (cid,)).fetchone()[0] == want
    g.ledger.alive(cid, False)


def test_the_caller_waits_exactly_the_deadline_not_longer(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(jevguard, "MAX_TIMEOUT_S", 0.3)
    release = threading.Event()
    g = guard(tmp_path, lambda key, body, t: release.wait(20) and {}, cap="100")
    t0 = time.time()
    with pytest.raises(TimeoutError):
        g.call(BODY, timeout=999)
    assert 0.3 <= time.time() - t0 < 1.0
    release.set()
