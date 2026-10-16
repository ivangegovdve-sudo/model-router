"""The operator-local Clef decision endpoint is registered, not a chat executor."""

from decimal import Decimal

from modelrouter import providers as P
from modelrouter.config import load
from modelrouter.secrets import Keyring


def test_clef_catalogue_is_explicitly_free_and_keeps_local_identity_metadata():
    rows = P.parse_catalogue("clef", [{
        "id": "clef-flash-local",
        "owned_by": "llamacpp",
        "architecture": {"input_modalities": ["text"], "output_modalities": ["decisions"]},
        "meta": {"n_ctx": 8192},
    }])

    assert len(rows) == 1
    clef = rows[0]
    assert clef.model == "clef-flash-local"
    assert clef.prompt == Decimal("0") and clef.completion == Decimal("0")
    assert clef.context_length == 8192
    assert clef.family == "clef"
    assert clef.output_modalities == ("decisions",)
    assert clef.text_out is False
    assert "decision-only" in clef.blocked


def test_clef_chat_call_fails_closed_without_contacting_the_decision_endpoint(monkeypatch):
    monkeypatch.setattr(P.httpx, "post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    result = P.call("clef", "clef-flash-local", "", {
        "messages": [{"role": "user", "content": "choose a route"}],
    }, max_tokens=32)

    assert not result.ok
    assert result.status == 422
    assert result.detail == "decision-only endpoint; use /v1/systemone"


def test_config_enables_clef_without_turning_a_local_endpoint_into_an_executor(tmp_path):
    config = tmp_path / "router.toml"
    config.write_text('[secrets]\nsource = "env"\n[providers.clef]\nenabled = true\n')

    cfg = load(config)
    status = Keyring(cfg.source, cfg.secrets).load("clef")

    assert cfg.secrets["clef"] == "__local__"
    assert status.present and status.detail == "local"
    assert cfg.seat_app_executors == {}
