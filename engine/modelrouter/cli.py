"""modelrouter -- setup and operation from a terminal.

    modelrouter init              write a config template (secret NAMES only)
    modelrouter doctor            what is configured, what is readable, what is missing
    modelrouter serve             run the proxy
    modelrouter probe --cheapest 3   buy behavioural facts for the likeliest winners
    modelrouter probe --refresh-days 3   re-measure before facts expire (7 days)
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
    if not (a.seat or a.cheapest or a.refresh_days is not None or a.generation):
        print("nothing to probe: give --seat, --cheapest N or --refresh-days D")
        return 1
    if a.generation:
        out = r.probe_generation(a.seat or None, measured=True if not a.seat else False,
                                 budget_usd=a.budget,
                                 progress=lambda row: print("  %-55s tokens=%s %.1fs %s tok/s %s"
                                                            % (row["seat"], row["completion_tokens"],
                                                               row["latency_s"], row["tok_per_s"],
                                                               row["detail"]), flush=True))
        print(json.dumps({"spent_usd": out["spent_usd"], "budget_usd": out["budget_usd"]}))
        return 0
    out = r.probe(a.seat or None, a.cheapest, a.budget, progress=show,
                  refresh_older_than_s=a.refresh_days * 86400 if a.refresh_days is not None
                  else None)
    print(json.dumps({"spent_usd": out["spent_usd"], "budget_usd": out["budget_usd"],
                      "profiles": {p["seat"]: p.get("profile") for p in out["probed"]}},
                     indent=1))
    return 0


def _store_key_value(cfg, target: str, value: str) -> str:
    """Hand a new caller key to the operator's secret store. The value goes over stdin or
    into a file the operator chose; it is never printed, logged or put on a command line."""
    import os
    import subprocess
    kind, _, where = target.partition(":")
    if kind == "gcp":
        if not cfg.gcp_project:
            raise SystemExit("secrets.gcp_project is not set in the config")
        exe = shutil.which("gcloud")
        if not exe:
            raise SystemExit("gcloud not installed")
        r = subprocess.run([exe, "secrets", "create", where, "--replication-policy=automatic",
                            "--project=" + cfg.gcp_project, "--data-file=-"],
                           input=value, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError("gcloud secrets create %s failed (exit %d)" % (where, r.returncode))
        return "GCP Secret Manager %s (project %s)" % (where, cfg.gcp_project)
    if kind == "file":
        path = Path(where)
        if path.exists():
            raise RuntimeError("refusing to overwrite %s" % path)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        return "file %s (mode 600)" % path
    raise SystemExit("--store must be gcp:<secret-name> or file:<path>")


def cmd_keys(a) -> int:
    from decimal import Decimal
    from .clientkeys import ClientKeys
    cfg = load(a.config)
    ck = ClientKeys(cfg.state_dir / "modelrouter.sqlite3")
    if a.keys_cmd == "create":
        value, rec = ck.create(a.name, Decimal(a.cap_usd),
                               max_usd_per_mtok=Decimal(a.max_usd_per_mtok), rpm=a.rpm,
                               default_max_tokens=a.default_max_tokens, note=a.note)
        try:
            where = _store_key_value(cfg, a.store, value)
        except Exception as exc:                                # noqa: BLE001
            ck.revoke(rec.id)          # a key nobody can retrieve must not stay valid
            print("NOT CREATED: storing the key failed (%s); key %s revoked" % (exc, rec.id))
            return 2
        finally:
            value = None
        print("created caller key %s (%s): cap $%s, <= $%s/M, %d req/min, default max_tokens %d"
              % (rec.id, rec.name, rec.cap_usd, rec.max_usd_per_mtok, rec.rpm,
                 rec.default_max_tokens))
        print("value stored in %s -- deliver it to the caller out of band" % where)
        return 0
    if a.keys_cmd == "list":
        print(json.dumps([k.public() for k in ck.list()], indent=1))
        return 0
    if a.keys_cmd == "revoke":
        print("revoked" if ck.revoke(a.id) else "no active key %s" % a.id)
        return 0
    if a.keys_cmd == "set-cap":
        print("cap set" if ck.set_cap(a.id, Decimal(a.cap_usd)) else "no key %s" % a.id)
        return 0
    if a.keys_cmd == "usage":
        k = ck.get(a.id)
        print(json.dumps({**k.public(), "recent_charges": ck.charges(a.id)}, indent=1) if k
              else "no key %s" % a.id)
        return 0
    return 1


def cmd_import(a) -> int:
    import json as _json
    cfg = load(a.config)
    r = _router(cfg)
    rows = _json.load(open(a.rows, encoding="utf-8")) if a.rows else None
    curve = _json.load(open(a.curve, encoding="utf-8")) if a.curve else None
    if isinstance(rows, dict):          # accept {"rows": [...]} as well as a bare list
        rows = next((v for v in rows.values() if isinstance(v, list)), [])
    print(json.dumps(r.import_measurements(rows, curve, a.note), indent=1))
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


def cmd_jev_spend(a) -> int:
    """Today's Jev spend against the hard daily cap. Reads the ledger; makes no call."""
    import json as _json

    from .jevguard import Guard
    st = Guard(load(a.config).guard_config(), lambda: "", fallback=None).status()
    if a.json:
        print(_json.dumps(st, indent=1))
        return 0
    print("Jev spend %s: $%s of $%s (remaining $%s, in flight $%s)" % (
        st["day"], st["spent_usd"], st["cap_usd"], st["remaining_usd"], st["in_flight_usd"]))
    print("  paid calls %d, input tokens %d, blocked by the cap %d, unknown-billing %d" % (
        st["paid_calls"], st["input_tokens"], st["blocked_calls"], st["unknown_billing_calls"]))
    print("  price %s; cap %s; resets in %d min" % (
        st["price"], "TRIPPED at " + st["cap_tripped_at"] if st["cap_tripped_at"] else "not tripped",
        st["resets_in_s"] // 60))
    print("  ledger %s" % st["ledger"])
    return 0


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
    p.add_argument("--generation", action="store_true",
                   help="measure decode speed on a ~400-token generation (every measured "
                        "seat without one, or the given --seat)")
    p.add_argument("--refresh-days", type=float,
                   help="re-probe measured seats last observed more than D days ago")
    p = sub.add_parser("keys", help="caller keys: spend-capped keys for people who are not the operator")
    ks = p.add_subparsers(dest="keys_cmd", required=True)
    c = ks.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--cap-usd", required=True, help="hard spend cap in USD, e.g. 5")
    c.add_argument("--store", required=True,
                   help="where the key value goes: gcp:<secret-name> or file:<new path>")
    c.add_argument("--max-usd-per-mtok", default="5", help="price ceiling for this key's calls")
    c.add_argument("--rpm", type=int, default=60)
    c.add_argument("--default-max-tokens", type=int, default=1024)
    c.add_argument("--note", default="")
    ks.add_parser("list")
    r = ks.add_parser("revoke"); r.add_argument("id")
    r = ks.add_parser("set-cap"); r.add_argument("id"); r.add_argument("cap_usd")
    r = ks.add_parser("usage"); r.add_argument("id")
    p = sub.add_parser("import-measurements")
    p.add_argument("--rows", help="one-word sweep rows (e.g. exp4_results.json)")
    p.add_argument("--curve", help="concurrency runs (e.g. exp5_results.json)")
    p.add_argument("--note", default="", help="provenance, recorded with each row")
    p = sub.add_parser("jev-spend", help="today's TypeSafe/Jev spend against the hard daily cap")
    p.add_argument("--json", action="store_true")
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
    return {"init": cmd_init, "doctor": cmd_doctor, "probe": cmd_probe, "jev-spend": cmd_jev_spend,
            "explain": cmd_explain, "import-measurements": cmd_import, "keys": cmd_keys}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
