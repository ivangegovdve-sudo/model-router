from decimal import Decimal

from fastapi.testclient import TestClient

from modelrouter.config import Config, load
from modelrouter.decision import Candidate, Emits
from modelrouter.server import build


def test_dashboard_seat_route_preserves_auth_and_advisory_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAT_TEST_TOKEN", "seat-test-token")
    cfg = Config(path=tmp_path / "config.toml", source="env", state_dir=tmp_path,
                 token_secret="SEAT_TEST_TOKEN")
    app = build(cfg)
    router = app.state.router
    candidate = Candidate(seat="openrouter:test", provider="openrouter", model="test",
                          list_prompt=Decimal("0.1"), list_completion=Decimal("0.2"),
                          context_length=8192, emits=Emits.CONTENT, supports_tools=True,
                          has_reasoning_field=False, observation_count=3)
    monkeypatch.setattr(router.gather, "roster", lambda lane="interactive": [candidate])
    with TestClient(app) as client:
        request = {"consumer": "glass-solver", "role": "review", "need": {"tools": True}}
        assert client.post("/v1/seats/resolve", json=request).status_code == 401
        client.headers["Authorization"] = "Bearer seat-test-token"
        response = client.post("/v1/seats/resolve", json=request)
        assert response.status_code == 200
        body = response.json()
        assert body["seat"] == "openrouter:test" and body["tier"] == 2
        record = client.get("/router/decisions/" + body["decision_id"]).json()
        assert record["status"] == "ADVISED" and record["result"]["calls"] == 0
        refusal = client.post("/v1/seats/resolve", json={"consumer": "public-council"})
        assert refusal.status_code == 422
        assert refusal.json()["detail"]["type"] == "seat_unavailable"
        assert refusal.json()["detail"]["policy"] == "free-only"
        assert client.post("/v1/seats/resolve", json={"consumer": "unknown"}).status_code == 400
        assert client.post("/v1/seats/resolve", content="invalid-json").status_code == 400


def test_consumer_policies_load_without_loosening_defaults_on_invalid_values(tmp_path):
    path = tmp_path / "router.toml"
    path.write_text('[secrets]\nsource="env"\n[seat_consumers]\n'
                    'custom="free-only"\npublic-council="invalid"\n')
    cfg = load(path)
    assert cfg.seat_consumer_policies["custom"] == "free-only"
    assert cfg.seat_consumer_policies["public-council"] == "free-only"
    assert any("seat_consumers.public-council" in error for error in cfg.problems)
