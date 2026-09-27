"""Caller keys: a stranger can make real calls without being able to overspend."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from modelrouter import providers as P
from modelrouter.clientkeys import ClientKeys, worst_case
from modelrouter.config import Config
from modelrouter.server import build
from tests.test_router import CATALOGUE, SECRET, Upstream


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AKASH_KEY", SECRET)
    monkeypatch.setenv("ROUTER_TOKEN", "admin-tok")
    up = Upstream()
    monkeypatch.setattr(P, "call", up)
    monkeypatch.setattr(P, "fetch_catalogue", lambda provider, key, timeout=30: list(CATALOGUE))
    cfg = Config(path=tmp_path / "c.toml", source="env", secrets={"akashml": "AKASH_KEY"},
                 token_secret="ROUTER_TOKEN", state_dir=tmp_path, dashboard_url="",
                 probe_budget_usd=1.0, interactive_max_latency_s=None)
    app = build(cfg)
    admin = TestClient(app)
    admin.headers["Authorization"] = "Bearer admin-tok"
    admin.post("/router/probe", json={"cheapest": 3})
    ck: ClientKeys = app.state.client_keys
    value, rec = ck.create("friend", Decimal("0.05"))
    friend = TestClient(app)
    friend.headers["Authorization"] = "Bearer " + value
    return admin, friend, ck, rec, value, up


def ask(c, **kw):
    return c.post("/v1/chat/completions", json={"model": "auto", "max_tokens": 16,
                                                "messages": [{"role": "user", "content": "hi"}], **kw})


def test_caller_key_makes_a_real_call_and_is_charged_its_actual_cost(env):
    admin, friend, ck, rec, value, up = env
    r = ask(friend)
    assert r.status_code == 200, r.text
    charged = Decimal(r.headers["X-Router-Key-Charged"])
    assert charged > 0 and Decimal(r.headers["X-Router-Key-Remaining"]) == Decimal("0.05") - charged
    u = friend.get("/v1/usage").json()
    assert u["calls"] == 1 and Decimal(u["spent_usd"]) == charged
    assert u["recent_charges"][0]["basis"] == "computed"


def test_a_call_that_could_overrun_the_cap_is_refused_before_any_provider_is_called(env):
    admin, friend, ck, rec, value, up = env
    up.calls.clear()
    r = ask(friend, max_tokens=4000)            # worst case 2x(prompt+4000+1024) at $5/M > $0.05
    assert r.status_code == 402 and r.json()["error"]["type"] == "spend_cap_reached"
    assert up.calls == []
    assert Decimal(friend.get("/v1/usage").json()["spent_usd"]) == 0


def test_unset_max_tokens_gets_the_keys_default_not_open_ended(env):
    admin, friend, ck, rec, value, up = env
    ck.set_cap(rec.id, Decimal("1"))
    up.calls.clear()
    r = friend.post("/v1/chat/completions", json={"model": "auto",
                    "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert up.calls[-1][1] == 1024


def test_caller_key_cannot_reach_operator_routes(env):
    admin, friend, ck, rec, value, up = env
    for path in ("/router/decisions", "/router/roster", "/router/setup"):
        assert friend.get(path).status_code == 403
    assert friend.post("/router/probe", json={"cheapest": 1}).status_code == 403
    assert admin.get("/router/decisions").status_code == 200


def test_unknown_cost_is_charged_at_the_reservation_not_zero(env, monkeypatch):
    admin, friend, ck, rec, value, up = env
    ck.set_cap(rec.id, Decimal("1"))
    import modelrouter.server as S
    monkeypatch.setattr("modelrouter.router.call_cost", lambda li, res: (None, "unknown"))
    r = ask(friend)
    assert r.status_code == 200
    charged = Decimal(r.headers["X-Router-Key-Charged"])
    assert charged > Decimal("0.0001")          # the reservation, not zero
    assert friend.get("/v1/usage").json()["recent_charges"][0]["basis"].startswith("reserved")


def test_revoked_key_and_wrong_key_are_refused(env):
    admin, friend, ck, rec, value, up = env
    bad = TestClient(friend.app)
    bad.headers["Authorization"] = "Bearer mr_%s_wrong" % rec.id
    assert ask(bad).status_code == 401
    ck.revoke(rec.id)
    assert ask(friend).status_code == 401


def test_rate_limit_per_key(env):
    admin, friend, ck, rec, value, up = env
    import sqlite3
    ck._db.execute("UPDATE client_keys SET rpm=2, cap_usd='1' WHERE id=?", (rec.id,)); ck._db.commit()
    codes = [ask(friend).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_stream_is_settled_when_it_ends(env, monkeypatch):
    admin, friend, ck, rec, value, up = env
    ck.set_cap(rec.id, Decimal("1"))
    def fake_stream(provider, model, key, req, *, max_tokens, on_done, timeout=180, window=None):
        yield b'data: {"choices":[{"delta":{"content":"Ready"}}]}\n\n'
        res = P.Result(True, 200, provider, model, content="Ready", prompt_tokens=10,
                       completion_tokens=2, latency_s=0.5, detail="ok")
        on_done(res)
    monkeypatch.setattr(P, "stream", fake_stream)
    r = ask(friend, stream=True)
    assert r.status_code == 200 and "Ready" in r.text
    u = friend.get("/v1/usage").json()
    assert u["calls"] == 1 and Decimal(u["spent_usd"]) > 0


def test_key_value_never_appears_in_any_response(env):
    admin, friend, ck, rec, value, up = env
    ask(friend)
    for r in (friend.get("/v1/usage"), admin.get("/router/decisions"), friend.get("/v1/models"),
              admin.get("/router/setup")):
        assert value not in r.text
    import sqlite3
    dump = "\n".join(str(tuple(row)) for row in ck._db.execute("SELECT * FROM client_keys"))
    assert value not in dump                    # only the hash is stored
