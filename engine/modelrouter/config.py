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
[providers.venice]
secret = ""
[providers.nous]
secret = ""
[providers.sail]
secret = ""
[providers.ionet]
secret = ""

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
    ceiling_usd_per_mtok: float | None = 5.0
    probe_budget_usd: float = 0.02
    allow_free: bool = False
    dashboard_url: str = ""
    state_dir: Path = field(default_factory=lambda: Path.home() / ".modelrouter")
    problems: list[str] = field(default_factory=list)


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
    cfg.dashboard_url = (pol.get("dashboard_url", "") or "").rstrip("/")
    if cfg.source not in ("gcp", "env"):
        cfg.problems.append('secrets.source must be "gcp" or "env"')
    if cfg.source == "gcp" and not cfg.gcp_project:
        cfg.problems.append("secrets.gcp_project is empty")
    if not cfg.secrets:
        cfg.problems.append("no provider has a secret name -- nothing can be routed")
    return cfg
