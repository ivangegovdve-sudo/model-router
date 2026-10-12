from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from modelrouter import providers as P
from modelrouter import dashboard as D
from modelrouter.config import Config
from modelrouter.gather import Gatherer
from modelrouter.secrets import Keyring
from modelrouter.store import Store


def point(unit, amount, condition=None, date="2026-10-12T04:30:00Z"):
    return {"unit": unit, "amount": amount, "condition": condition,
            "source": {"url": "https://source.test/models", "readAt": date},
            "provenance": "published"}


def test_canonical_prices_keep_decimal_precision_and_source_age():
    row = {"pricingState": "published", "pricePoints": [
        point("token_in", "0.0000001250"), point("token_out", "0.0000005000"),
        point("token_cached", "0.0000000100")]}
    card = D.prices(row)
    assert card.prompt == Decimal("0.1250")
    assert card.completion == Decimal("0.5000")
    assert card.cached == Decimal("0.0100")
    assert card.read_at == "2026-10-12T04:30:00Z"


def test_legacy_prices_still_work_and_null_is_not_free():
    row = {"pricing": {"promptUsdPerToken": "0", "completionUsdPerToken": None},
           "lastConfirmedAt": "2026-10-12T04:30:00Z"}
    assert D.prices(row).prompt == Decimal(0)
    assert D.prices(row).completion is None


def test_legacy_scheduling_window_is_never_interpreted_as_flat_price():
    row = {"pricing": {"promptUsdPerToken": "0.000001", "completionUsdPerToken": "0.000002"},
           "pricingWindow": "Flex", "lastConfirmedAt": "2026-10-12T04:30:00Z"}
    assert D.prices(row).prompt is None
    assert D.prices(row, "asap").prompt is None
    assert D.prices(row, "flex").completion == Decimal(2)
    assert D.prices(row, "flex").window == "flex"


@pytest.mark.parametrize("confirmed", [None, "bad-date"])
def test_legacy_price_requires_a_confirmed_quote_timestamp(confirmed):
    row = {"pricing": {"promptUsdPerToken": "0.000001", "completionUsdPerToken": "0.000002"},
           "lastConfirmedAt": confirmed, "lastSeenAt": "2026-10-12T04:30:00Z"}
    assert D.prices(row).prompt is None


@pytest.mark.parametrize("amount", [None, True, "-1", "NaN", "Infinity", "bad"])
def test_invalid_prices_stay_unknown(amount):
    assert D.prices({"pricingState": "published", "pricePoints": [
        point("token_in", amount), point("token_out", "1")]}).prompt is None


def test_conditional_and_non_token_prices_are_not_used_as_flat_prices():
    band = {"kind": "time_band", "name": "peak"}
    row = {"pricingState": "published", "pricePoints": [
        point("token_in", "1", band), point("token_out", "1", band),
        point("image", "0.04")]}
    assert D.prices(row).prompt is None
    assert D.prices(row).completion is None


def test_scheduling_conditions_require_matching_window_and_do_not_mix_cards():
    row = {"pricingState": "published", "pricePoints": [
        point("token_in", "0.000001", {"kind": "latency_window", "name": "Flex"}),
        point("token_out", "0.000002", {"kind": "latency_window", "name": "Flex"}),
        point("token_in", "0.000005", {"kind": "latency_window", "name": "ASAP"})]}
    assert D.prices(row).prompt is None
    assert D.prices(row, "flex").completion == Decimal(2)
    assert D.prices(row, "asap").completion is None


def test_unknown_canonical_price_set_does_not_resurrect_legacy_aliases():
    assert D.prices({"pricingState": "unknown", "pricePoints": [],
                     "pricing": {"promptUsdPerToken": "0"}}).prompt is None


def test_ambiguous_or_unsourced_points_stay_unknown():
    row = {"pricingState": "published", "pricePoints": [
        point("token_in", "1"), point("token_in", "2"), point("token_out", "1")]}
    assert D.prices(row).prompt is None
    row["pricePoints"] = [point("token_in", "1", date="bad-date")]
    assert D.prices(row).prompt is None


@pytest.mark.parametrize("source", [None, 1, ["bad"], "bad"])
def test_malformed_source_stays_unknown(source):
    price = {**point("token_in", "1"), "source": source}
    assert D.prices({"pricingState": "published", "pricePoints": [price]}).prompt is None


def test_price_point_without_a_source_url_is_unknown():
    price = point("token_in", "1")
    price["source"].pop("url")
    assert D.prices({"pricingState": "published", "pricePoints": [price]}).prompt is None


def test_pagination_keeps_opaque_cursor_and_rejects_incomplete_results():
    requests = []
    def serve(req):
        requests.append(req)
        cursor = req.url.params.get("cursor")
        return httpx.Response(200, json={"schemaVersion": "2.0", "data": [
            {"id": "one" if cursor is None else "two", "provider": "openrouter"}],
            "cursor": "opaque+/=" if cursor is None else None})
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        rows = D.fetch_rows(client, "https://dashboard.test", "live-models", {"provider": "openrouter"})
    assert [r["id"] for r in rows] == ["one", "two"]
    assert requests[1].url.params["cursor"] == "opaque+/="
    assert requests[1].url.params["provider"] == "openrouter"
    def stuck(req):
        return httpx.Response(200, json={"data": [{"id": "one"}], "cursor": "same"})
    with httpx.Client(transport=httpx.MockTransport(stuck)) as client:
        with pytest.raises(ValueError, match="cursor"):
            D.fetch_rows(client, "https://dashboard.test", "live-models", {})


def test_provider_capabilities_preserve_explicit_empty_and_unknown_values():
    raw = {"id": "vendor/model", "pricing": {}, "context_length": 10000,
           "supported_parameters": ["tools", "reasoning_effort", "response_format"],
           "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}}
    listing = P.parse_catalogue("openrouter", [raw])[0]
    assert listing.supported_parameters == ("tools", "reasoning_effort", "response_format")
    assert listing.input_modalities == ("text", "image")
    assert listing.output_modalities == ("text",)
    assert listing.reasoning is True
    assert listing.supports_editing is None
    empty = P.parse_catalogue("openrouter", [{**raw, "supported_parameters": []}])[0]
    assert empty.supported_parameters == () and empty.reasoning is None
    negative = P.parse_catalogue("openrouter", [{**raw, "supports_reasoning": False}])[0]
    assert negative.reasoning is False


def test_dashboard_capability_evidence_joins_by_exact_model_id():
    row = {"contextLength": "1048576", "reasoningEfforts": [], "outputModalities": ["text"]}
    native = {"supportedParameters": ["tools", "reasoning_effort"],
              "architecture": {"input_modalities": ["text", "image"]}}
    facts = D.capabilities(row, native)
    assert facts["supports_tools"] is True
    assert facts["supports_reasoning"] is True
    assert facts["reasoning_efforts"] == ()
    assert facts["input_modalities"] == ("text", "image")
    assert facts["context_length"] == 1048576
    assert facts["supports_editing"] is None
    assert D.capabilities({"reasoningEfforts": None}, {})["supports_reasoning"] is None


def test_provider_body_strips_filters_and_adapts_openrouter_thinking_effort():
    body = P._body("openrouter", "vendor/model", {"messages": [],
                   "router_requirements": {"thinking_level": "high"}, "reasoning_effort": "high"}, 100)
    assert "router_requirements" not in body
    assert "reasoning_effort" not in body
    assert body["reasoning"] == {"effort": "high"}


def test_gather_preserves_native_price_and_fills_dashboard_capability_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TEST_KEY", "test")
    cfg = Config(path=tmp_path / "config.toml", source="env",
                 secrets={"openrouter": "DASHBOARD_TEST_KEY"}, state_dir=tmp_path,
                 dashboard_url="https://dashboard.test")
    store = Store(tmp_path / "state.sqlite3")
    gather = Gatherer(cfg, Keyring(cfg.source, cfg.secrets), store)
    listing = P.Listing("openrouter", "vendor/model", Decimal(1), Decimal(2))
    monkeypatch.setattr(P, "fetch_catalogue", lambda *a, **kw: [listing])
    monkeypatch.setattr(gather, "_dashboard", lambda provider: {"vendor/model": {
        "pricingState": "published", "pricePoints": [point("token_in", "0.000009"),
        point("token_out", "0.000009")], "reasoningEfforts": [], "contextLength": "32000",
        "outputModalities": ["text"]}})
    monkeypatch.setattr(gather, "_dashboard_models", lambda: {"vendor/model": {
        "supportedParameters": ["tools", "reasoning_effort"],
        "architecture": {"input_modalities": ["text", "image"]}}})
    candidate = gather.roster()[0]
    assert candidate.list_prompt == Decimal(1)
    assert candidate.price_source == "provider-catalogue"
    assert candidate.supports_tools is True and candidate.supports_reasoning is True
    assert candidate.context_length == 32000
    assert candidate.input_modalities == ("text", "image")
    assert candidate.supports_editing is None
    assert isinstance(gather.dashboard_status, dict)
    listing.completion = None
    partial = gather.roster()[0]
    assert partial.list_prompt == Decimal(1)
    assert partial.list_completion == Decimal(9)
    assert "mixed" in partial.price_source
