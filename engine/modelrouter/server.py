"""HTTP: the OpenAI-compatible face, plus the legibility endpoints.

    POST /v1/chat/completions      model "auto" routes; "provider:model" is judged alone
    GET  /v1/models                "auto" and every live seat
    GET  /health                   no auth; what is configured, what is missing
    GET  /router/setup             the setup surface: keys by NAME, sources, problems
    GET  /router/roster            every candidate with the facts the decision reads
    POST /router/explain           the decision for a request, without making the call
    POST /router/probe             buy behavioural facts (spends money, capped)
    GET  /router/decisions         recent decisions
    GET  /router/decisions/{id}    one decision: considered, chosen, why, what it cost

Auth: a bearer token (the client's "API key"). Its value is read like a provider key
-- from the configured secret -- and compared in constant time. With no token the
server refuses to start, unless bound to loopback with no_auth = true.
"""
from __future__ import annotations

import argparse
import logging
import secrets as pysecrets
import shutil
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .config import Config, load
from .router import LANES, Router, lane_of
from .secrets import Keyring
from .store import Store

log = logging.getLogger("modelrouter.server")
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    """OpenAI-shaped error, so clients surface the message instead of choking."""
    return JSONResponse({"error": {"message": message, "type": code, "code": code, **extra}},
                        status_code=status)


def build(cfg: Config) -> FastAPI:
    keys = Keyring(cfg.source, dict(cfg.secrets), cfg.gcp_project)
    store = Store(cfg.state_dir / "modelrouter.sqlite3")
    router = Router(cfg, keys, store)
    token_keys = Keyring(cfg.source, {"_token": cfg.token_secret} if cfg.token_secret else {},
                         cfg.gcp_project)
    app = FastAPI(title="modelrouter", version=__version__)
    app.state.router = router

    def auth(authorization: str | None = Header(default=None)) -> None:
        if cfg.no_auth:
            return
        want = token_keys.get("_token")
        got = (authorization or "").removeprefix("Bearer ").strip()
        if not want or not got or not pysecrets.compare_digest(got.encode(), want.encode()):
            raise HTTPException(401, "invalid or missing router token",
                                headers={"WWW-Authenticate": "Bearer"})

    def ceiling(h: str | None) -> float | None:
        if not h:
            return None
        try:
            return float(h)
        except ValueError:
            raise HTTPException(400, "X-Router-Max-Usd-Per-M must be a number")

    def setup() -> dict:
        ks = [k.__dict__ for k in keys.status()]
        tok = token_keys.status()
        return {
            "config": str(cfg.path), "config_problems": cfg.problems,
            "secrets_source": cfg.source, "gcp_project": cfg.gcp_project,
            "gcloud_installed": bool(shutil.which("gcloud")),
            "keys": ks,
            "auth": "disabled (loopback only)" if cfg.no_auth else (
                "token " + ("read" if tok and tok[0].present else "UNREADABLE: " +
                            (tok[0].detail if tok else "token_secret not set"))),
            "dashboard": cfg.dashboard_url or None,
            "dashboard_status": router.gather.dashboard_status,
            "ready": bool(any(k["present"] for k in ks)) and not cfg.problems,
        }

    @app.get("/health")
    def health():
        s = setup()
        return {"status": "ok" if s["ready"] else "not_ready", "service": "modelrouter",
                "version": __version__, "providers_with_keys":
                    [k["provider"] for k in s["keys"] if k["present"]],
                "problems": s["config_problems"] + ["%s key: %s" % (k["provider"], k["detail"])
                                                    for k in s["keys"] if not k["present"]]}

    @app.get("/router/setup", dependencies=[Depends(auth)])
    def router_setup():
        return setup()

    @app.get("/v1/models", dependencies=[Depends(auth)])
    def models():
        seats = router.gather.roster()
        data = [{"id": "auto" if lane == "interactive" else "auto:" + lane, "object": "model",
                 "owned_by": "modelrouter"} for lane in LANES]
        data += [{"id": c.seat, "object": "model", "owned_by": c.provider} for c in seats
                 if c.provider_state == "OK" and c.available is not False]
        return {"object": "list", "data": data}

    @app.get("/router/roster", dependencies=[Depends(auth)])
    def roster(provider: str | None = None, measured_only: bool = False):
        seats = router.gather.roster()
        rows = []
        for c in seats:
            if provider and c.provider != provider:
                continue
            if measured_only and c.emits is None:
                continue
            d = dict(c.__dict__)
            d["emits"] = c.emits.value if c.emits else None
            rows.append(d)
        return {"providers": router.providers_public(),
                "dashboard": router.gather.dashboard_status, "seats": rows}

    @app.post("/router/explain", dependencies=[Depends(auth)])
    async def explain(request: Request,
                      x_router_max_usd_per_m: str | None = Header(default=None),
                      x_router_lane: str | None = Header(default=None)):
        req = await request.json()
        try:
            lane = lane_of(req, x_router_lane)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return await run_in_threadpool(router.explain, req, ceiling(x_router_max_usd_per_m), lane)

    @app.post("/router/probe", dependencies=[Depends(auth)])
    async def probe(request: Request):
        body = await request.json()
        seats = body.get("seats") or None
        cheapest = int(body.get("cheapest") or 0)
        if not seats and not cheapest:
            raise HTTPException(422, "give `seats` or `cheapest`")
        budget = body.get("budget_usd")
        budget = min(float(budget), cfg.probe_budget_usd) if budget is not None else None
        return await run_in_threadpool(router.probe, seats, cheapest, budget)

    @app.get("/router/decisions", dependencies=[Depends(auth)])
    def decisions(limit: int = 50):
        return store.recent(min(max(limit, 1), 500))

    @app.get("/router/decisions/{did}", dependencies=[Depends(auth)])
    def decision(did: str):
        d = store.get(did)
        if not d:
            raise HTTPException(404, "no such decision")
        return d

    @app.get("/router/observations/{seat:path}", dependencies=[Depends(auth)])
    def observations(seat: str):
        return store.observations(seat)

    @app.post("/v1/chat/completions", dependencies=[Depends(auth)])
    async def chat(request: Request, user_agent: str | None = Header(default=None),
                   x_router_max_usd_per_m: str | None = Header(default=None),
                   x_router_lane: str | None = Header(default=None)):
        try:
            req = await request.json()
        except ValueError:
            return _error(400, "invalid_request", "body is not JSON")
        if not isinstance(req.get("messages"), list) or not req["messages"]:
            return _error(400, "invalid_request", "`messages` must be a non-empty list")
        lim = ceiling(x_router_max_usd_per_m)
        client = (user_agent or "")[:80]
        try:
            lane = lane_of(req, x_router_lane)
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        if req.get("stream"):
            routed, it = await run_in_threadpool(
                lambda: router.route_stream(req, client=client, ceiling=lim, lane=lane))
            if it is None:
                return _abstain(routed)
            return StreamingResponse(it, media_type="text/event-stream",
                                     headers={"X-Router-Decision": routed.decision_id,
                                              "X-Router-Seat": routed.choice.seat or "",
                                              "Cache-Control": "no-cache"})
        routed = await run_in_threadpool(lambda: router.route(req, client=client, ceiling=lim,
                                                                 lane=lane))
        if routed.status == "ABSTAINED":
            return _abstain(routed)
        res = routed.result
        if routed.status != "ANSWERED" or res is None or res.body is None:
            return _error(502, "routed_call_failed",
                          "every routed attempt failed or returned no content: " +
                          "; ".join("%s -> %s" % (a["seat"], a["detail"])
                                    for a in routed.doc["attempts"]),
                          decision_id=routed.decision_id)
        body = dict(res.body)
        body["router"] = {"decision_id": routed.decision_id, "seat": routed.choice.seat,
                          "because": routed.choice.because,
                          "cost_usd": routed.doc["result"]["cost_usd"],
                          "cost_basis": routed.doc["result"]["cost_basis"],
                          "attempts": len(routed.doc["attempts"])}
        return JSONResponse(body, headers={"X-Router-Decision": routed.decision_id,
                                           "X-Router-Seat": routed.choice.seat or ""})

    def _abstain(routed) -> JSONResponse:
        ch = routed.choice
        return _error(422, "router_abstained",
                      "no model can serve this request honestly: " + ch.because,
                      decision_id=routed.decision_id, unknown=list(ch.unknown))

    return app


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser("modelrouter-server")
    ap.add_argument("--config", type=Path)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load(a.config)
    if cfg.problems and not cfg.path.exists():
        raise SystemExit("refusing to start: " + "; ".join(cfg.problems))
    if cfg.no_auth and cfg.bind not in LOOPBACK:
        raise SystemExit("refusing to start: no_auth = true is allowed only on loopback")
    if not cfg.no_auth:
        if not cfg.token_secret:
            raise SystemExit("refusing to start: server.token_secret is empty (clients need "
                             "a token; or bind loopback with no_auth = true)")
        tk = Keyring(cfg.source, {"_token": cfg.token_secret}, cfg.gcp_project).load("_token")
        if not tk.present:
            raise SystemExit("refusing to start: router token %s unreadable (%s)"
                             % (tk.name, tk.detail))
    for p in cfg.problems:
        log.warning("config: %s", p)
    uvicorn.run(build(cfg), host=cfg.bind, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
