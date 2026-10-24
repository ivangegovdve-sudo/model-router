"""Configuration: a TOML file holding secret NAMES and policy, never a secret value.

Located at --config, else $MODELROUTER_CONFIG, else ~/.modelrouter/config.toml.
`modelrouter init` writes the template below; `modelrouter doctor` says what is missing.
"""
from __future__ import annotations

import os
import hashlib
import json
import math
import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .providers import PROVIDERS
from .secrets import LOCAL_SECRET

TEMPLATE = """\
# modelrouter configuration. This file holds secret NAMES, never secret values.

[secrets]
# "gcp": read each key from GCP Secret Manager with this machine's gcloud credentials.
# "env": read each key from the named environment variable.
source = "gcp"
gcp_project = ""            # e.g. "my-project"

# One table per provider you want routed. Omit a provider to leave it out entirely.
# `secret` is the Secret Manager secret name (source = "gcp") or the variable name (source = "env").
[providers.openrouter]
secret = ""
[providers.akashml]
secret = ""
# max_concurrency = 16    # optional; otherwise the measured cap (import-measurements) applies
[providers.venice]
secret = ""
[providers.nous]
secret = ""
[providers.sail]
secret = ""
[providers.ionet]
secret = ""
[providers.groq]
secret = ""
# Operator-local Clef llama.cpp decision seat. It never reads a secret and is
# enabled only when this flag is true. External app executors remain separate.
[providers.clef]
enabled = false
# Not supported: Nous (its /models is an OpenRouter mirror -- same ids, same prices to the
# digit; detected and excluded as MIRROR if configured) and Cerebras (no prices in its API).

[server]
bind = "127.0.0.1"
port = 7480
# Clients send this as their API key. Name of the secret (or env var) that holds it.
# Leave empty only with bind = "127.0.0.1" and no_auth = true.
token_secret = ""
no_auth = false

[policy]
# Refuse any model whose price (measured, else list) is above this, in USD per million tokens.
ceiling_usd_per_mtok = 5.0
# Lanes. `model: "auto"` is the interactive lane: someone is waiting, so a seat whose
# measured latency is above this is excluded. `auto:background` and `auto:batch` ignore
# latency and buy cheaper windows where a provider sells them (Sail balanced / flex).
# Predicted from each model's measured short-call latency + decode speed on a real
# generation (`modelrouter probe --generation`), for THIS request's token count.
interactive_max_latency_s = 8.0
# When no model answers within a caller's small max_tokens, raise it for the cheapest
# reasoning model (recorded in the decision) instead of refusing. Never silent.
clamp_max_tokens = true
# A price read longer ago than this is UNKNOWN, not fact (Sail's is scraped from docs).
max_price_age_s = 172800
# A provider whose catalogue is >= 80% another's with identical prices is a MIRROR: comparing
# it against the original would score the provider against itself, and one fault takes out
# both. Excluded unless allowed.
allow_mirrors = false
# Free tiers (":free", or $0 list price) may log prompts and have their own daily quota.
allow_free = false
# Probing spends real money to learn how a model behaves. Hard cap per probe run.
probe_budget_usd = 0.02
# Where prices come from first. Set to "" to use only each provider's own catalogue.
dashboard_url = "https://openrouter-github-dashboard.vercel.app"

# Exact model weights, independent of provider aliases or quantization labels.
# Alternatively, each registry can be supplied as a complete JSON object through
# MODELROUTER_SEAT_MODEL_IDENTITIES, MODELROUTER_SEAT_REFERENCE_MODELS, or
# MODELROUTER_SEAT_APP_EXECUTORS; an environment value replaces its TOML table.
# [seat_model_identities."openrouter:vendor/model"]
# weights_id = "weights:operator-verified-revision"
# identity_evidence_id = "operator-proof:v1"  # optional; a content-bound ID is generated
# family = "broad-family"                   # optional; never an exact identity
# [seat_reference_models."author-proof"]
# models = ["openrouter:vendor/model"]
# revision = "exact-pr-head-sha"
# [seat_reference_models."glass-author-config"]
# consumer = "glass-solver"
# source = "operator-configured"
# allow_consumer_models = true  # trust this consumer's configured, head-bound provenance
# [seat_app_executors."registered-local-executor"]
# consumer = "private-council"
# models = ["local:configured-model"]
# [seat_app_executors."registered-local-executor".model_metadata."local:configured-model"]
# supports_tools = true
# list_prompt = 0.0
# list_completion = 0.0
"""


@dataclass
class Config:
    path: Path
    source: str = "gcp"
    gcp_project: str | None = None
    secrets: dict[str, str] = field(default_factory=dict)       # provider -> secret name
    bind: str = "127.0.0.1"
    port: int = 7480
    token_secret: str = ""
    no_auth: bool = False
    ceiling_usd_per_mtok: float | None = 5.0     # converted to Decimal at the decision
    probe_budget_usd: float = 0.02
    allow_free: bool = False
    interactive_max_latency_s: float | None = 8.0
    max_concurrency: dict[str, int] = field(default_factory=dict)   # provider -> cap
    clamp_max_tokens: bool = True
    max_price_age_s: float | None = 172800
    allow_mirrors: bool = False
    dashboard_url: str = ""
    seat_consumer_policies: dict[str, str] = field(default_factory=lambda: {
        "glass-solver": "cheapest", "private-council": "cheapest", "public-council": "free-only"})
    seat_consumer_excluded: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "glass-solver": ("cerebras",)})
    seat_model_identities: dict[str, dict[str, Any]] = field(default_factory=dict)
    seat_reference_models: dict[str, dict[str, Any]] = field(default_factory=dict)
    seat_app_executors: dict[str, dict[str, Any]] = field(default_factory=dict)
    state_dir: Path = field(default_factory=lambda: Path.home() / ".modelrouter")
    problems: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.validate_seat_configuration()

    def validate_seat_configuration(self) -> None:
        """Malformed trust configuration is named and removed, never treated as proof."""
        for name, validator in (("seat_model_identities", _model_identity),
                                ("seat_reference_models", _reference_models),
                                ("seat_app_executors", _app_executor)):
            value = getattr(self, name)
            if not isinstance(value, dict):
                self.problems.append("%s must be a table" % name)
                setattr(self, name, {})
                continue
            normalized = {}
            for key, entry in value.items():
                try:
                    _text(key, name + " key")
                    normalized[key] = validator(key, entry)
                except ValueError as exc:
                    self.problems.append("%s.%s: %s" % (name, key, exc))
            setattr(self, name, normalized)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value \
            or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError("%s must be a nonempty identifier" % name)
    return value


def _model_ref(value: Any) -> str:
    _text(value, "model reference")
    provider, separator, model = value.partition(":")
    if not separator or not re.fullmatch(r"[a-z][a-z0-9_.-]*", provider) or not model \
            or any(c.isspace() for c in model):
        raise ValueError("model reference must be provider:model")
    return value


def _entry(value: Any, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValueError("entry must be a table")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("unsupported fields: " + ", ".join(sorted(str(k) for k in unknown)))
    return value


def _models(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("models must be a nonempty list of provider:model references")
    return list(dict.fromkeys(_model_ref(model) for model in value))


def _model_identity(seat: str, entry: Any) -> dict:
    _model_ref(seat)
    raw = _entry(entry, {"weights_id", "identity_evidence_id", "family"})
    out = {"weights_id": _text(raw.get("weights_id"), "weights_id")}
    if raw.get("family") is not None:
        out["family"] = _text(raw["family"], "family")
    if raw.get("identity_evidence_id") is not None:
        out["identity_evidence_id"] = _text(raw["identity_evidence_id"], "identity_evidence_id")
    else:
        out["identity_evidence_id"] = _operator_evidence_id({"seat": seat, **out})
    return out


def _operator_evidence_id(claim: dict) -> str:
    serialized = json.dumps(claim, sort_keys=True, separators=(",", ":"))
    return "operator-config:" + hashlib.sha256(serialized.encode()).hexdigest()


def _reference_models(evidence_id: str, entry: Any) -> dict:
    _text(evidence_id, "reference evidence ID")
    raw = _entry(entry, {"models", "revision", "verified", "consumer", "source", "allow_consumer_models"})
    delegated = any(key in raw for key in ("consumer", "allow_consumer_models"))
    if delegated:
        if set(raw) != {"consumer", "source", "allow_consumer_models"}:
            raise ValueError("delegated reference needs only consumer, source, and allow_consumer_models")
        if raw["source"] != "operator-configured" or raw["allow_consumer_models"] is not True:
            raise ValueError("delegated reference must explicitly allow consumer models from operator-configured provenance")
        return {"consumer": _text(raw["consumer"], "consumer"),
                "source": "operator-configured", "allow_consumer_models": True}
    out = {"models": _models(raw.get("models")), "source": "operator-configured"}
    if raw.get("source", "operator-configured") != "operator-configured":
        raise ValueError("configured reference source must be operator-configured")
    if raw.get("revision") is not None:
        out["revision"] = _text(raw["revision"], "revision")
    verified = raw.get("verified", True)
    if not isinstance(verified, bool):
        raise ValueError("verified must be true or false")
    out["verified"] = verified
    return out


_APP_FLAGS = {"supports_tools", "supports_editing", "supports_reasoning"}
_APP_ARRAYS = {"reasoning_efforts", "supported_parameters", "input_modalities", "output_modalities"}
_APP_PRICES = {"list_prompt", "list_completion", "billed_prompt", "billed_completion"}


def _app_model_metadata(value: Any) -> dict:
    raw = _entry(value, _APP_FLAGS | _APP_ARRAYS | _APP_PRICES |
                 {"context_length", "weights_id", "identity_evidence_id", "family"})
    out = {}
    for key, item in raw.items():
        if item is None:
            out[key] = None
        elif key in _APP_FLAGS:
            if not isinstance(item, bool):
                raise ValueError("%s must be true, false, or null" % key)
            out[key] = item
        elif key in _APP_ARRAYS:
            if not isinstance(item, list):
                raise ValueError("%s must be a list of strings" % key)
            out[key] = list(dict.fromkeys(_text(v, key) for v in item))
            if key == "reasoning_efforts" and any(v not in ("none", "low", "medium", "high") for v in out[key]):
                raise ValueError("reasoning_efforts must use none, low, medium, or high")
        elif key in _APP_PRICES:
            price = _app_price(item)
            if price is None:
                raise ValueError("%s must be a finite nonnegative price" % key)
            out[key] = price
        elif key == "context_length":
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise ValueError("context_length must be an integer >= 1")
            out[key] = item
        else:
            out[key] = _text(item, key)
    if out.get("identity_evidence_id") is not None and out.get("weights_id") is None:
        raise ValueError("identity_evidence_id needs weights_id")
    return out


def _app_price(value: Any) -> str | None:
    """Keep exact configured rates as JSON-safe decimal strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        representable = math.isfinite(float(number))
    except (ValueError, OverflowError, InvalidOperation):
        return None
    return str(number) if number.is_finite() and number >= 0 and representable else None


def _app_executor(executor_id: str, entry: Any) -> dict:
    _text(executor_id, "executor ID")
    raw = _entry(entry, {"consumer", "models", "model_metadata"})
    out = {"consumer": _text(raw.get("consumer"), "consumer"), "models": _models(raw.get("models"))}
    metadata = raw.get("model_metadata", {})
    if not isinstance(metadata, dict) or any(model not in out["models"] for model in metadata):
        raise ValueError("model_metadata must be a table keyed by this executor's exact models")
    out["model_metadata"] = {model: _app_model_metadata(value) for model, value in metadata.items()}
    for model, facts in out["model_metadata"].items():
        if facts.get("weights_id") is not None and facts.get("identity_evidence_id") is None:
            claim = {"executor_id": executor_id, "model_ref": model,
                     "weights_id": facts["weights_id"]}
            if facts.get("family") is not None:
                claim["family"] = facts["family"]
            facts["identity_evidence_id"] = _operator_evidence_id(claim)
    return out


def _load_seat_configuration(cfg: Config, data: dict) -> None:
    for name in ("seat_model_identities", "seat_reference_models", "seat_app_executors"):
        environment_name = "MODELROUTER_" + name.upper()
        value = data.get(name, {})
        if environment_name in os.environ:
            try:
                value = json.loads(os.environ[environment_name])
                if not isinstance(value, dict):
                    raise ValueError("not an object")
            except (ValueError, TypeError):
                cfg.problems.append("%s must contain a JSON object" % environment_name)
                value = {}
        setattr(cfg, name, value)
    cfg.validate_seat_configuration()


def default_path() -> Path:
    env = os.environ.get("MODELROUTER_CONFIG")
    return Path(env) if env else Path.home() / ".modelrouter" / "config.toml"


def load(path: Path | None = None) -> Config:
    path = path or default_path()
    cfg = Config(path=path, state_dir=Path(os.environ.get("MODELROUTER_STATE", path.parent)))
    if not path.exists():
        cfg.problems.append("no config file at %s -- run `modelrouter init`" % path)
        _load_seat_configuration(cfg, {})
        return cfg
    with open(path, "rb") as fh:
        d = tomllib.load(fh)
    s = d.get("secrets", {})
    cfg.source = s.get("source", "gcp")
    cfg.gcp_project = s.get("gcp_project") or None
    for name, spec in (d.get("providers") or {}).items():
        if name not in PROVIDERS:
            cfg.problems.append("unknown provider [providers.%s] -- known: %s"
                                % (name, ", ".join(PROVIDERS)))
            continue
        if PROVIDERS[name].get("local"):
            if isinstance(spec, dict) and spec.get("enabled") is True:
                cfg.secrets[name] = LOCAL_SECRET
            continue
        if (spec or {}).get("secret"):
            cfg.secrets[name] = spec["secret"]
        if (spec or {}).get("max_concurrency"):
            cfg.max_concurrency[name] = int(spec["max_concurrency"])
    srv = d.get("server", {})
    cfg.bind = srv.get("bind", cfg.bind)
    cfg.port = int(srv.get("port", cfg.port))
    cfg.token_secret = srv.get("token_secret", "")
    cfg.no_auth = bool(srv.get("no_auth", False))
    pol = d.get("policy", {})
    c = pol.get("ceiling_usd_per_mtok", cfg.ceiling_usd_per_mtok)
    cfg.ceiling_usd_per_mtok = float(c) if c not in (None, "", 0) else None
    cfg.probe_budget_usd = float(pol.get("probe_budget_usd", cfg.probe_budget_usd))
    cfg.allow_free = bool(pol.get("allow_free", False))
    lat = pol.get("interactive_max_latency_s", cfg.interactive_max_latency_s)
    cfg.interactive_max_latency_s = float(lat) if lat not in (None, "", 0) else None
    cfg.clamp_max_tokens = bool(pol.get("clamp_max_tokens", True))
    age = pol.get("max_price_age_s", cfg.max_price_age_s)
    cfg.max_price_age_s = float(age) if age not in (None, "", 0) else None
    cfg.allow_mirrors = bool(pol.get("allow_mirrors", False))
    cfg.dashboard_url = (pol.get("dashboard_url", "") or "").rstrip("/")
    consumers = d.get("seat_consumers", {})
    if not isinstance(consumers, dict):
        cfg.problems.append("seat_consumers must be a table of consumer names and policies")
    else:
        for name, policy in consumers.items():
            if not isinstance(name, str) or not name or policy not in ("cheapest", "free-only"):
                cfg.problems.append("seat_consumers.%s must be cheapest or free-only" % name)
                continue
            cfg.seat_consumer_policies[name] = policy
    excluded = d.get("seat_consumer_excluded", {})
    if not isinstance(excluded, dict):
        cfg.problems.append("seat_consumer_excluded must be a table of consumer -> excluded providers/families")
    else:
        for name, items in excluded.items():
            if (not isinstance(name, str) or not name or not isinstance(items, list)
                    or not all(isinstance(x, str) and x for x in items)):
                cfg.problems.append("seat_consumer_excluded.%s must be a list of names" % name)
                continue
            if "cerebras" not in {x.casefold() for x in items}:
                cfg.problems.append("seat_consumer_excluded.%s must keep cerebras (reserved for Chloe)" % name)
                continue
            cfg.seat_consumer_excluded[name] = tuple(x.casefold() for x in items)
    _load_seat_configuration(cfg, d)
    if cfg.source not in ("gcp", "env"):
        cfg.problems.append('secrets.source must be "gcp" or "env"')
    if cfg.source == "gcp" and not cfg.gcp_project:
        cfg.problems.append("secrets.gcp_project is empty")
    if not cfg.secrets:
        cfg.problems.append("no provider has a secret name -- nothing can be routed")
    return cfg
