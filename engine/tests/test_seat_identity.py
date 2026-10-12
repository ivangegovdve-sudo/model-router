"""Operator-configured weight identity and explicit catalogue identity evidence."""
from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient

from modelrouter import providers as P
from modelrouter.config import Config, load
from modelrouter.decision import Ask, Candidate, Emits, decide
from modelrouter.gather import Gatherer
from modelrouter.secrets import Keyring
from modelrouter.store import Store


def config_file(tmp_path, extra=""):
    path = tmp_path / "router.toml"
    path.write_text('[secrets]\nsource="env"\n[providers.openrouter]\nsecret="TEST_KEY"\n' + extra)
    return path


def test_operator_aliases_share_exact_weights_but_keep_family_separate(tmp_path):
    cfg = Config(path=tmp_path / "router.toml", seat_model_identities={
        "openrouter:vendor/original": {"weights_id": "weights:one", "family": "vendor"},
        "sail:other/quantized": {"weights_id": "weights:one", "family": "vendor"},
        "ionet:vendor/different": {"weights_id": "weights:two", "family": "vendor"},
    })
    original = cfg.seat_model_identities["openrouter:vendor/original"]
    alias = cfg.seat_model_identities["sail:other/quantized"]
    assert original["weights_id"] == alias["weights_id"] == "weights:one"
    assert original["identity_evidence_id"].startswith("operator-config:")
    assert original["identity_evidence_id"] != alias["identity_evidence_id"]
    assert cfg.seat_model_identities["ionet:vendor/different"]["weights_id"] == "weights:two"
    assert not cfg.problems


def test_config_identity_evidence_changes_when_asserted_weights_change(tmp_path):
    one = Config(path=tmp_path / "c", seat_model_identities={"sail:m": {"weights_id": "one"}})
    same = Config(path=tmp_path / "c", seat_model_identities={"sail:m": {"weights_id": "one"}})
    other = Config(path=tmp_path / "c", seat_model_identities={"sail:m": {"weights_id": "two"}})
    first = one.seat_model_identities["sail:m"]["identity_evidence_id"]
    assert same.seat_model_identities["sail:m"]["identity_evidence_id"] == first
    assert other.seat_model_identities["sail:m"]["identity_evidence_id"] != first


def test_toml_loads_identity_reference_and_executor_registries(tmp_path):
    cfg = load(config_file(tmp_path, '''
[seat_model_identities."openrouter:vendor/m"]
weights_id="weights:stable"
identity_evidence_id="operator-proof:v1"
family="broad-family"
[seat_reference_models."author-v1"]
models=["openrouter:vendor/m"]
revision="head-sha"
verified=true
[seat_reference_models."glass-author-config"]
consumer="glass-solver"
source="operator-configured"
allow_consumer_models=true
[seat_app_executors."glass-codex"]
consumer="glass-solver"
models=["codex:configured-model"]
[seat_app_executors."glass-codex".model_metadata."codex:configured-model"]
supports_tools=true
context_length=32000
list_prompt=0.0
list_completion=0.0
'''))
    assert not cfg.problems
    assert cfg.seat_model_identities["openrouter:vendor/m"]["identity_evidence_id"] == "operator-proof:v1"
    assert cfg.seat_reference_models["author-v1"]["revision"] == "head-sha"
    assert cfg.seat_reference_models["glass-author-config"]["allow_consumer_models"] is True
    metadata = cfg.seat_app_executors["glass-codex"]["model_metadata"]["codex:configured-model"]
    assert metadata["supports_tools"] is True and Decimal(metadata["list_prompt"]) == 0


def test_json_environment_registry_overrides_and_validates_toml(tmp_path, monkeypatch):
    path = config_file(tmp_path, '[seat_model_identities."sail:m"]\nweights_id="old"\n')
    monkeypatch.setenv("MODELROUTER_SEAT_MODEL_IDENTITIES", json.dumps({"sail:m": {"weights_id": "new"}}))
    monkeypatch.setenv("MODELROUTER_SEAT_REFERENCE_MODELS", json.dumps({"author": {"models": ["sail:m"]}}))
    monkeypatch.setenv("MODELROUTER_SEAT_APP_EXECUTORS", json.dumps({
        "local": {"consumer": "private-council", "models": ["local:m"]}}))
    cfg = load(path)
    assert cfg.seat_model_identities["sail:m"]["weights_id"] == "new"
    assert cfg.seat_reference_models["author"]["models"] == ["sail:m"]
    assert cfg.seat_app_executors["local"]["consumer"] == "private-council"
    assert not cfg.problems


def test_environment_identity_registry_is_loaded_even_without_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELROUTER_SEAT_MODEL_IDENTITIES", '{"sail:m":{"weights_id":"one"}}')
    cfg = load(tmp_path / "missing.toml")
    assert cfg.seat_model_identities["sail:m"]["weights_id"] == "one"
    assert any("no config file" in issue for issue in cfg.problems)


@pytest.mark.parametrize("field,value", [
    ("seat_model_identities", []),
    ("seat_model_identities", {"bad-ref": {"weights_id": "one"}}),
    ("seat_model_identities", {"sail:m": {"weights_id": ""}}),
    ("seat_model_identities", {"sail:m": {"weights_id": True}}),
    ("seat_model_identities", {"sail:m": {"weights_id": "one", "identity_evidence_id": ""}}),
    ("seat_model_identities", {"sail:m": {"weights_id": "one", "unknown": True}}),
    ("seat_reference_models", {"author": {"models": []}}),
    ("seat_reference_models", {"author": {"models": ["not-a-seat"]}}),
    ("seat_reference_models", {"author": {"models": ["sail:m"], "verified": "yes"}}),
    ("seat_reference_models", {"author": {"consumer": "glass-solver", "source": "unverified", "allow_consumer_models": True}}),
    ("seat_reference_models", {"author": {"consumer": "glass-solver", "source": "operator-configured", "allow_consumer_models": False}}),
    ("seat_app_executors", {"executor": {"consumer": "", "models": ["local:m"]}}),
    ("seat_app_executors", {"executor": {"consumer": "private-council", "models": []}}),
    ("seat_app_executors", {"executor": {"consumer": "private-council", "models": ["local:m"],
                                         "model_metadata": {"local:other": {}}}}),
    ("seat_app_executors", {"executor": {"consumer": "private-council", "models": ["local:m"],
                                         "model_metadata": {"local:m": {"supports_tools": "yes"}}}}),
    ("seat_app_executors", {"executor": {"consumer": "private-council", "models": ["local:m"],
                                         "model_metadata": {"local:m": {"list_prompt": -1}}}}),
])
def test_malformed_registry_entries_are_removed_with_named_configuration_problem(tmp_path, field, value):
    cfg = Config(path=tmp_path / "c", **{field: value})
    assert getattr(cfg, field) == {}
    assert any(field in issue for issue in cfg.problems)


@pytest.mark.parametrize("value", ["not JSON", "[]", "null"])
def test_invalid_registry_environment_is_not_silently_ignored(tmp_path, monkeypatch, value):
    monkeypatch.setenv("MODELROUTER_SEAT_MODEL_IDENTITIES", value)
    cfg = load(config_file(tmp_path))
    assert cfg.seat_model_identities == {}
    assert any("MODELROUTER_SEAT_MODEL_IDENTITIES" in issue for issue in cfg.problems)


@pytest.mark.parametrize("provider", list(P.PROVIDERS))
def test_catalogue_accepts_only_explicit_weights_with_identity_evidence(provider):
    raw = {"id": "vendor/model", "weights_id": "canonical:weights", "identity_evidence_id": "published-proof:v1"}
    listing = P.parse_catalogue(provider, [raw])[0]
    assert listing.weights_id == "canonical:weights"
    assert listing.identity_evidence_id == "published-proof:v1"
    unproven = P.parse_catalogue(provider, [{"id": "vendor/model", "weights_id": "canonical:weights"}])[0]
    assert unproven.weights_id is None and unproven.identity_evidence_id is None


@pytest.mark.parametrize("extra", [
    {"canonical_slug": "vendor/base-model"},
    {"family": "known-family"},
    {"weights_id": "canonical", "identity_evidence_id": ""},
    {"weights_id": True, "identity_evidence_id": "proof"},
])
def test_names_family_and_malformed_metadata_do_not_establish_exact_weights(extra):
    listing = P.parse_catalogue("openrouter", [{"id": "vendor/base-model-Q4", **extra}])[0]
    assert listing.weights_id is None and listing.identity_evidence_id is None


def test_app_executor_prices_keep_exact_precision_and_unknowns(tmp_path):
    cfg = Config(path=tmp_path / "c", seat_app_executors={"local-runner": {
        "consumer": "private-council", "models": ["local:m"], "model_metadata": {"local:m": {
            "list_prompt": "0.0000001234567890123456789", "list_completion": None}}}})
    metadata = cfg.seat_app_executors["local-runner"]["model_metadata"]["local:m"]
    assert isinstance(metadata["list_prompt"], str)
    assert Decimal(metadata["list_prompt"]) == Decimal("0.0000001234567890123456789")
    assert metadata["list_completion"] is None


def test_configured_app_weight_claims_get_content_bound_executor_evidence(tmp_path):
    def configuration(executor, weight):
        return Config(path=tmp_path / "c", seat_app_executors={executor: {
            "consumer": "private-council", "models": ["local:m"], "model_metadata": {
                "local:m": {"weights_id": weight}}}}).seat_app_executors[executor]["model_metadata"]["local:m"]
    first = configuration("runner", "one")
    assert first["identity_evidence_id"].startswith("operator-config:")
    assert first["weights_id"] == "one"
    assert configuration("runner", "one")["identity_evidence_id"] == first["identity_evidence_id"]
    assert configuration("runner", "two")["identity_evidence_id"] != first["identity_evidence_id"]
    assert configuration("different-runner", "one")["identity_evidence_id"] != first["identity_evidence_id"]


def test_gather_maps_provider_aliases_to_same_exact_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("IDENTITY_KEY", "test-key")
    cfg = Config(path=tmp_path / "c", source="env", secrets={"openrouter": "IDENTITY_KEY", "ionet": "IDENTITY_KEY"},
                 seat_model_identities={
                     "openrouter:vendor/original": {"weights_id": "same:weights"},
                     "ionet:alias/model-Q4": {"weights_id": "same:weights"}})
    gather = Gatherer(cfg, Keyring(cfg.source, cfg.secrets), Store(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(P, "fetch_catalogue", lambda provider, *a, **kw: [P.Listing(
        provider, "vendor/original" if provider == "openrouter" else "alias/model-Q4", Decimal("0.1"), Decimal("0.2"))])
    candidates = gather.roster()
    assert len(candidates) == 2
    assert {c.weights_id for c in candidates} == {"same:weights"}
    assert all(c.identity_evidence_id.startswith("operator-config:") for c in candidates)
    assert all(c.family is None for c in candidates)


def test_gather_identity_requires_evidence_and_does_not_infer_from_similar_names(tmp_path, monkeypatch):
    monkeypatch.setenv("IDENTITY_KEY", "test-key")
    cfg = Config(path=tmp_path / "c", source="env", secrets={"openrouter": "IDENTITY_KEY"})
    gather = Gatherer(cfg, Keyring(cfg.source, cfg.secrets), Store(tmp_path / "state.sqlite3"))
    listings = [P.Listing("openrouter", "vendor/model", Decimal("0.1"), Decimal("0.2"),
                          weights_id="explicit", identity_evidence_id="catalogue:proof"),
                P.Listing("openrouter", "vendor/model-Q4", Decimal("0.1"), Decimal("0.2"),
                          weights_id="unproven", family="vendor")]
    monkeypatch.setattr(P, "fetch_catalogue", lambda *a, **kw: listings)
    by_model = {c.model: c for c in gather.roster()}
    assert by_model["vendor/model"].weights_id == "explicit"
    assert by_model["vendor/model"].identity_evidence_id == "catalogue:proof"
    assert by_model["vendor/model-Q4"].weights_id is None
    assert by_model["vendor/model-Q4"].identity_evidence_id is None
    assert by_model["vendor/model-Q4"].family == "vendor"


def test_windowed_listing_keeps_identity_and_capabilities(tmp_path):
    cfg = Config(path=tmp_path / "c")
    gather = Gatherer(cfg, Keyring(cfg.source, cfg.secrets), Store(tmp_path / "state.sqlite3"))
    listing = P.Listing("sail", "m", Decimal("1"), Decimal("2"), weights_id="weights",
                        identity_evidence_id="proof", supports_tools=True, supported_parameters=("tools",))
    gather._cat["sail"] = (0, [listing])
    gather._win["sail"] = (0, {"m": {"flex": (Decimal("0.1"), Decimal("0.2"), Decimal("0.01"))}})
    priced = gather.listing("sail:m", "flex")
    assert priced.prompt == Decimal("0.1") and priced.completion == Decimal("0.2")
    assert priced.weights_id == "weights" and priced.identity_evidence_id == "proof"
    assert priced.supported_parameters == ("tools",) and priced.supports_tools is True


def test_operator_mapping_overrides_upstream_and_is_exposed_without_changing_price(tmp_path, monkeypatch):
    from modelrouter.server import build
    monkeypatch.setenv("IDENTITY_KEY", "test-key")
    cfg = Config(path=tmp_path / "c", source="env", no_auth=True, state_dir=tmp_path,
                 secrets={"openrouter": "IDENTITY_KEY"}, seat_model_identities={
        "openrouter:vendor/alias": {"weights_id": "operator:weights", "identity_evidence_id": "operator:proof", "family": "operator-family"}})
    listing = P.Listing("openrouter", "vendor/alias", Decimal("0.1"), Decimal("0.2"),
                        context_length=4096, family="provider-family", weights_id="provider:weights",
                        identity_evidence_id="provider:proof")
    monkeypatch.setattr(P, "fetch_catalogue", lambda *a, **kw: [listing])
    client = TestClient(build(cfg))
    row = client.get("/router/roster").json()["seats"][0]
    assert row["weights_id"] == "operator:weights" and row["identity_evidence_id"] == "operator:proof"
    assert row["family"] == "operator-family"
    assert row["list_prompt"] == 0.1 and row["list_completion"] == 0.2
    assert row["price_source"] == "provider-catalogue"


def test_identity_does_not_change_existing_single_seat_cost_selection():
    base = dict(provider="sail", context_length=4096, emits=Emits.CONTENT,
                has_reasoning_field=False, input_modalities=("text",), output_modalities=("text",))
    cheap = Candidate(seat="sail:cheap", model="cheap", list_prompt=Decimal("0.1"),
                      list_completion=Decimal("0.1"), weights_id="one", identity_evidence_id="proof-one", **base)
    expensive = Candidate(seat="sail:expensive", model="expensive", list_prompt=Decimal("0.2"),
                          list_completion=Decimal("0.2"), weights_id="two", identity_evidence_id="proof-two", **base)
    assert decide(Ask(prompt_tokens=100, max_tokens=20), [expensive, cheap]).seat == cheap.seat
