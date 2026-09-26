"""modelrouter -- setup and operation from a terminal.

    modelrouter init              write a config template (secret NAMES only)
    modelrouter doctor            what is configured, what is readable, what is missing
    modelrouter serve             run the proxy
    modelrouter probe --cheapest 3   buy behavioural facts for the likeliest winners
    modelrouter explain "prompt" [--max-tokens N] [--tools]   the decision, no call
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .config import TEMPLATE, default_path, load


def _router(cfg):
    from .router import Router
    from .secrets import Keyring
    from .store import Store
    return Router(cfg, Keyring(cfg.source, dict(cfg.secrets), cfg.gcp_project),
                  Store(cfg.state_dir / "modelrouter.sqlite3"))


def cmd_init(a) -> int:
    path = a.config or default_path()
    if path.exists() and not a.force:
        print("config already exists: %s  (use --force to overwrite)" % path)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    print("wrote %s\nnext: fill in secret names, then run `modelrouter doctor`" % path)
    return 0


def cmd_doctor(a) -> int:
    cfg = load(a.config)
    ok = True
    print("config        %s" % cfg.path)
    for p in cfg.problems:
        ok = False
        print("  PROBLEM     %s" % p)
    print("secrets from  %s%s" % (cfg.source, (" (project %s)" % cfg.gcp_project)
                                  if cfg.source == "gcp" else ""))
    if cfg.source == "gcp":
        print("gcloud        %s" % ("found" if shutil.which("gcloud") else "NOT FOUND -- install "
                                    "the Google Cloud SDK and run `gcloud auth application-default login`"))
    r = _router(cfg)
    any_key = False
    for ks in r.keys.status():
        any_key |= ks.present
        print("key %-10s %-8s %s  (%s)" % (ks.provider, "OK" if ks.present else "MISSING",
                                           ks.name or "-", ks.detail))
    if not cfg.no_auth:
        if cfg.token_secret:
            from .secrets import Keyring
            tk = Keyring(cfg.source, {"_token": cfg.token_secret}, cfg.gcp_project).load("_token")
            print("router token  %s (%s)" % ("OK" if tk.present else "MISSING", tk.detail))
            ok &= tk.present
        else:
            ok = False
            print("router token  NOT SET -- set server.token_secret, or no_auth on loopback")
    if any_key:
        seats = r.gather.roster()
        for st in r.providers_public():
            print("provider %-10s %-16s %4d models  prices: %s %s" % (
                st["provider"], st["state"], st["models"], st["price_source"] or "-",
                st["detail"]))
        measured = sum(1 for c in seats if c.emits is not None)
        print("roster        %d seats, %d with measured behaviour" % (len(seats), measured))
        if not measured:
            print("  NOTE        nothing measured yet: every route will ABSTAIN until you run "
                  "`modelrouter probe --cheapest 3`")
        print("dashboard     %s" % r.gather.dashboard_status)
    else:
        ok = False
        print("  PROBLEM     no provider key is readable -- nothing can be routed")
    print("ready" if ok else "NOT READY")
    return 0 if ok else 2


def cmd_probe(a) -> int:
    cfg = load(a.config)
    r = _router(cfg)
    def show(rung):
        print("  %-55s max_tokens=%-5s http=%s content=%s reasoning=%s cost=%s (%s) %s" % (
            rung["seat"], rung["max_tokens"], rung["http"], rung["content_chars"],
            rung["reasoning_chars"], rung["cost_usd"], rung["cost_basis"], rung["detail"]),
            flush=True)
    out = r.probe(a.seat or None, a.cheapest, a.budget, progress=show)
    print(json.dumps({"spent_usd": out["spent_usd"], "budget_usd": out["budget_usd"],
                      "profiles": {p["seat"]: p.get("profile") for p in out["probed"]}},
                     indent=1))
    return 0


def cmd_explain(a) -> int:
    cfg = load(a.config)
    r = _router(cfg)
    req = {"model": a.model, "messages": [{"role": "user", "content": a.prompt}]}
    if a.max_tokens is not None:
        req["max_tokens"] = a.max_tokens
    if a.tools:
        req["tools"] = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
    ch = r.explain(req)["choice"]
    print("%s  %s\n  because: %s" % (ch["outcome"], ch["seat"] or "", ch["because"]))
    shown = [c for c in ch["considered"] if c["verdict"] != "UNKNOWN"][:a.top]
    for c in shown:
        print("  %-9s %-55s %s" % (c["verdict"], c["seat"], c["because"]))
    unk = sum(1 for c in ch["considered"] if c["verdict"] == "UNKNOWN")
    if unk:
        print("  UNKNOWN   %d more seats have a fact missing (mostly: never measured)" % unk)
    return 0 if ch["outcome"] == "ROUTE" else 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("modelrouter")
    ap.add_argument("--config", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--force", action="store_true")
    sub.add_parser("doctor")
    sub.add_parser("serve")
    p = sub.add_parser("probe")
    p.add_argument("--seat", action="append", help="provider:model (repeatable)")
    p.add_argument("--cheapest", type=int, default=0)
    p.add_argument("--budget", type=float)
    p = sub.add_parser("explain")
    p.add_argument("prompt")
    p.add_argument("--model", default="auto")
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--tools", action="store_true")
    p.add_argument("--top", type=int, default=12)
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        from .server import main as serve
        serve(["--config", str(a.config)] if a.config else [])
        return 0
    return {"init": cmd_init, "doctor": cmd_doctor, "probe": cmd_probe,
            "explain": cmd_explain}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
