"""Configuration: a TOML file holding secret NAMES and policy, never a secret value.

Located at --config, else $MODELROUTER_CONFIG, else ~/.modelrouter/config.toml.
`modelrouter init` writes the template below; `modelrouter doctor` says what is missing.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .providers import PROVIDERS

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
# Not supported: Nous (its /models is an OpenRouter mirror -- same ids, same prices to the
# digit; detected and excluded as MIRROR if configured) and Cerebras (no prices in its API).

[server]
bind = "127.0.0.1"
port = 7480
# Clients send this as their API key. Name of the secret (or env var) that holds it.
# Leave empty only with bind = "127.0.0.1" and no_auth = true.
token_secret = ""
no_auth = false

[consumers.glass-solver]
policy = "cheapest"          # pool order, cheapest live price within a tier
[consumers.private-council]
policy = "cheapest"
[consumers.public-council]
policy = "free-only"         # only free seats; UNAVAILABLE rather than a paid fallback

[local]
ollama_url = "http://127.0.0.1:11434"    # local-GPU seats are read live from /api/tags

# Codex / Antigravity seats: declare one once its call path is verified live.
# [[seats]]
# provider = "codex"
# model = "gpt-5.6-sol"
# available = true
# invoke = { kind = "cli", ... }

[jev]
enabled = true
secret = "typesafe-api-key"  # GCP SM name; the switching layer, fail-open
min_probability = 0.6
# HARD daily cap on what is paid to TypeSafe, enforced here before each call (TypeSafe has
# no server-side budget). Resets at local midnight. At the cap the paid call is not made:
# the free local fallback answers, or the call is deferred. Every Jev caller that goes
# through the guard (or POST /v1/systemone) shares this one cap.
daily_cap_usd = 1.00
usd_per_mtok_in = 0.042      # TypeSafe's published price for jev-1.13.0; output is free
usd_per_mtok_out = 0.0
timezone = ""                # IANA name, e.g. "Europe/Sofia"; "" = this machine's local time
fallback = "laya"            # "laya" (in-process, pip install laya) | "url" | "defer"
fallback_url = ""            # a Jev-compatible LOCAL endpoint, for fallback = "url"
alert_url = ""               # optional webhook, POSTed once per day when the cap trips

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
    # Seat selection (seats.py). consumer -> policy; declared codex/antigravity/local seats.
    consumers: dict[str, str] = field(default_factory=lambda: {
        "glass-solver": "cheapest", "private-council": "cheapest", "public-council": "free-only"})
    declared_seats: list[dict] = field(default_factory=list)
    free_only_allows_subscription: bool = False
    ollama_url: str = "http://127.0.0.1:11434"
    jev_enabled: bool = True
    jev_secret: str = "typesafe-api-key"
    jev_min_probability: float = 0.6
    jev_guard: dict = field(default_factory=dict)   # [jev] cap / price / fallback keys, as read
    state_dir: Path = field(default_factory=lambda: Path.home() / ".modelrouter")
    problems: list[str] = field(default_factory=list)

    def guard_config(self):
        """The Jev spend guard's settings: [jev] in the file, then JEV_* environment overrides.
        The ledger lives in the state dir so every process of this install shares one cap."""
        from .jevguard import GuardConfig
        g = self.jev_guard
        base = GuardConfig(
            cap_usd=g.get("daily_cap_usd", "1.00"), usd_per_mtok_in=g.get("usd_per_mtok_in", "0.042"),
            usd_per_mtok_out=g.get("usd_per_mtok_out", "0"), timezone=g.get("timezone", ""),
            ledger=self.state_dir / "jev_spend.sqlite3", fallback=g.get("fallback", "laya"),
            fallback_url=g.get("fallback_url", ""), alert_url=g.get("alert_url", ""))
        return GuardConfig.from_env(base)


def default_path() -> Path:
    env = os.environ.get("MODELROUTER_CONFIG")
    return Path(env) if env else Path.home() / ".modelrouter" / "config.toml"


def load(path: Path | None = None) -> Config:
    path = path or default_path()
    cfg = Config(path=path, state_dir=Path(os.environ.get("MODELROUTER_STATE", path.parent)))
    if not path.exists():
        cfg.problems.append("no config file at %s -- run `modelrouter init`" % path)
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
    for name, spec in (d.get("consumers") or {}).items():
        pol = (spec or {}).get("policy")
        if pol not in ("cheapest", "free-only"):
            cfg.problems.append('[consumers.%s] policy must be "cheapest" or "free-only"' % name)
        else:
            cfg.consumers[name] = pol
    for sd in d.get("seats") or []:
        if not sd.get("provider") or not sd.get("model"):
            cfg.problems.append("[[seats]] entry needs provider and model")
        else:
            cfg.declared_seats.append(dict(sd))
    sp = d.get("seat_policy") or {}
    cfg.free_only_allows_subscription = bool(sp.get("free_only_allows_subscription", False))
    cfg.ollama_url = (d.get("local") or {}).get("ollama_url", cfg.ollama_url)
    jv = d.get("jev") or {}
    cfg.jev_enabled = bool(jv.get("enabled", True))
    cfg.jev_secret = jv.get("secret", cfg.jev_secret)
    cfg.jev_min_probability = float(jv.get("min_probability", cfg.jev_min_probability))
    cfg.jev_guard = {k: jv[k] for k in ("daily_cap_usd", "usd_per_mtok_in", "usd_per_mtok_out",
                                        "timezone", "fallback", "fallback_url", "alert_url")
                     if k in jv}
    try:
        cfg.guard_config()
    except Exception as exc:
        cfg.problems.append("[jev] %s" % exc)
    if cfg.source not in ("gcp", "env"):
        cfg.problems.append('secrets.source must be "gcp" or "env"')
    if cfg.source == "gcp" and not cfg.gcp_project:
        cfg.problems.append("secrets.gcp_project is empty")
    if not cfg.secrets:
        cfg.problems.append("no provider has a secret name -- nothing can be routed")
    return cfg
