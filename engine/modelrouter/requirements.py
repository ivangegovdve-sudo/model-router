"""Validated routing requirements and their explicit catalogue evidence.

This module does not inspect prompt text or infer capabilities from model names.
Missing catalogue facts remain unknown whenever a requirement needs them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import math
import re
from typing import Any


MODEL_TYPES = ("text", "reasoning", "vision")
CAPABILITIES = ("tools", "reasoning", "structured_output", "editing")
THINKING_LEVELS = ("none", "low", "medium", "high")


@dataclass(frozen=True)
class RoutingRequirements:
    providers: tuple[str, ...] = ()
    model_types: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    thinking_level: str | None = None
    editable: bool | None = None
    tool_calls: bool | None = None
    min_context_tokens: int | None = None
    max_usd_per_mtok: Decimal | None = None

    def public(self) -> dict:
        doc = asdict(self)
        for key in ("providers", "model_types", "required_capabilities"):
            doc[key] = list(doc[key])
        if self.max_usd_per_mtok is not None:
            doc["max_usd_per_mtok"] = float(self.max_usd_per_mtok)
        return doc


def finite_price(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("%s must be a finite nonnegative number" % name)
    price = Decimal(str(value))
    try:
        representable = math.isfinite(float(price))
    except (OverflowError, ValueError):
        representable = False
    if not price.is_finite() or price < 0 or not representable:
        raise ValueError("%s must be a finite nonnegative number" % name)
    return price


def lower_ceiling(*values: Any) -> Decimal | None:
    prices = [finite_price(v, "price ceiling") for v in values if v is not None]
    return min(prices) if prices else None


def reasoning_effort_of(reasoning: Any) -> str | None:
    """The supported native reasoning object contains only an optional effort.

    Enable flags and token budgets would need separate capability evidence and
    conflict semantics. Reject them before a translator can drop or forward them.
    """
    if reasoning is None:
        return None
    if not isinstance(reasoning, dict):
        raise ValueError("`reasoning` must be an object")
    unsupported = set(reasoning) - {"effort"}
    if unsupported:
        raise ValueError("unsupported reasoning controls: %s; only reasoning.effort is supported" %
                         ", ".join(sorted(unsupported)))
    effort = reasoning.get("effort")
    if effort is not None and (not isinstance(effort, str) or effort not in THINKING_LEVELS):
        raise ValueError("reasoning.effort must use: " + ", ".join(THINKING_LEVELS))
    return effort


def parse_requirements(req: dict) -> RoutingRequirements:
    if "router_requirements" not in req:
        raw = {}
    else:
        raw = req["router_requirements"]
        if not isinstance(raw, dict):
            raise ValueError("`router_requirements` must be an object")
    unknown = set(raw) - set(RoutingRequirements.__dataclass_fields__)
    if unknown:
        raise ValueError("unknown router_requirements field: " + ", ".join(sorted(unknown)))
    values = {}
    for key, allowed in (("providers", None), ("model_types", MODEL_TYPES),
                         ("required_capabilities", CAPABILITIES)):
        value = raw.get(key, [])
        if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
            raise ValueError("router_requirements.%s must be a list of nonempty strings" % key)
        if allowed is not None and any(x not in allowed for x in value):
            raise ValueError("router_requirements.%s must use: %s" % (key, ", ".join(allowed)))
        if key == "providers" and any(not re.fullmatch(r"[a-z][a-z0-9_.-]*", x) for x in value):
            raise ValueError("router_requirements.providers must contain provider identifiers")
        values[key] = tuple(dict.fromkeys(value))
    for key in ("editable", "tool_calls"):
        value = raw.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError("router_requirements.%s must be true, false, or null" % key)
        values[key] = value
    effort = raw.get("thinking_level")
    if effort is not None and (not isinstance(effort, str) or effort not in THINKING_LEVELS):
        raise ValueError("router_requirements.thinking_level must use: " + ", ".join(THINKING_LEVELS))
    native_effort = req.get("reasoning_effort")
    object_effort = reasoning_effort_of(req.get("reasoning"))
    if object_effort is not None:
        if native_effort is not None and native_effort != object_effort:
            raise ValueError("reasoning.effort conflicts with reasoning_effort")
        native_effort = object_effort
    if native_effort is not None:
        if not isinstance(native_effort, str) or native_effort not in THINKING_LEVELS:
            raise ValueError("`reasoning_effort` must use: " + ", ".join(THINKING_LEVELS))
        if effort is not None and effort != native_effort:
            raise ValueError("router_requirements.thinking_level conflicts with reasoning_effort")
        effort = native_effort
    values["thinking_level"] = effort
    minimum = raw.get("min_context_tokens")
    if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, int)
                                or minimum < 1):
        raise ValueError("router_requirements.min_context_tokens must be an integer >= 1")
    values["min_context_tokens"] = minimum
    values["max_usd_per_mtok"] = finite_price(raw.get("max_usd_per_mtok"),
                                             "router_requirements.max_usd_per_mtok")
    requirements = RoutingRequirements(**values)
    if requirements.tool_calls is False and (req.get("tools") or req.get("functions")):
        raise ValueError("router_requirements.tool_calls=false conflicts with supplied tools")
    if requirements.tool_calls is False and "tools" in requirements.required_capabilities:
        raise ValueError("router_requirements.tool_calls=false conflicts with required capability tools")
    if requirements.editable is False and "editing" in requirements.required_capabilities:
        raise ValueError("router_requirements.editable=false conflicts with required capability editing")
    return requirements


def capability_fact(candidate, capability: str) -> bool | None:
    parameters = candidate.supported_parameters
    if capability == "tools":
        return candidate.supports_tools if candidate.supports_tools is not None else (
            None if parameters is None else "tools" in parameters)
    if capability == "editing":
        return candidate.supports_editing
    if capability == "reasoning":
        if candidate.supports_reasoning is not None:
            return candidate.supports_reasoning
        if candidate.reasoning_efforts is not None:
            return True
        if parameters is not None and {"reasoning", "reasoning_effort"} & set(parameters):
            return True
        # Missing effort controls do not establish that a model cannot reason by
        # default. A negative reasoning fact needs an explicit catalogue flag.
        return None
    if capability == "structured_output":
        return None if parameters is None else bool(
            {"response_format", "structured_outputs"} & set(parameters))
    raise ValueError("unknown capability %s" % capability)


def model_type_fact(candidate, model_type: str) -> bool | None:
    if model_type == "reasoning":
        return capability_fact(candidate, "reasoning")
    if model_type == "vision":
        return None if candidate.input_modalities is None else "image" in candidate.input_modalities
    if model_type == "text":
        inputs, outputs = candidate.input_modalities, candidate.output_modalities
        if (inputs is not None and "text" not in inputs) or (outputs is not None and "text" not in outputs):
            return False
        return None if inputs is None or outputs is None else True
    raise ValueError("unknown model type %s" % model_type)


def effort_fact(candidate, effort: str) -> bool | None:
    """Effort is controllable only with explicit named levels or a supported parameter."""
    parameters, efforts = candidate.supported_parameters, candidate.reasoning_efforts
    generic = parameters is not None and ("reasoning_effort" in parameters or (
        candidate.provider in ("openrouter", "nous") and "reasoning" in parameters))
    if efforts:
        control = effort in efforts
    elif generic:
        control = True
    else:
        control = False if parameters is not None else None
    if effort == "none":
        return control
    reasoning = capability_fact(candidate, "reasoning")
    if reasoning is False or control is False:
        return False
    return True if reasoning is True and control is True else None


@dataclass(frozen=True)
class RequirementResult:
    name: str
    supported: bool | None
    because: str
    unknown_fact: str


def check_requirements(requirements: RoutingRequirements, candidate) -> tuple[RequirementResult, ...]:
    checks = []
    def add(name, supported, because, fact):
        checks.append(RequirementResult(name, supported, because, fact))
    if requirements.providers:
        add("providers", candidate.provider in requirements.providers,
            "provider is outside the requested provider list", "provider")
    if requirements.model_types:
        matches = [model_type_fact(candidate, kind) for kind in requirements.model_types]
        verdict = True if True in matches else None if None in matches else False
        add("model_types", verdict, "model does not match requested model types",
            "model_types")
    for capability in requirements.required_capabilities:
        add("required_capabilities." + capability, capability_fact(candidate, capability),
            "required capability %s is unsupported" % capability, capability)
    if requirements.thinking_level is not None:
        add("thinking_level", effort_fact(candidate, requirements.thinking_level),
            "requested thinking level %s is unsupported" % requirements.thinking_level,
            "reasoning_effort")
    for key, capability in (("tool_calls", "tools"), ("editable", "editing")):
        required = getattr(requirements, key)
        if required is not None:
            actual = capability_fact(candidate, capability)
            add(key, None if actual is None else actual == required,
                "%s support must be %s" % (capability, "required" if required else "excluded"),
                capability)
    if requirements.min_context_tokens is not None:
        add("min_context_tokens", None if candidate.context_length is None else (
            candidate.context_length >= requirements.min_context_tokens),
            "model context is below requested minimum %d" % requirements.min_context_tokens,
            "context_length")
    return tuple(checks)


def provider_request(req: dict, requirements: RoutingRequirements, candidate) -> dict:
    """Strip router-only controls and send effort only after support was established."""
    body = {key: value for key, value in req.items()
            if not key.startswith("router_") and key not in ("reasoning_effort", "reasoning")}
    if requirements.thinking_level is not None:
        if effort_fact(candidate, requirements.thinking_level) is not True:
            raise ValueError("selected model does not explicitly support requested thinking level")
        body["reasoning_effort"] = requirements.thinking_level
    return body
