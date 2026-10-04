"""HTTP: the OpenAI-compatible face, plus the legibility endpoints.

    POST /v1/chat/completions      model "auto" routes; "provider:model" is judged alone
    GET  /v1/models                "auto" and every live seat
    GET  /health                   no auth; what is configured, what is missing
    GET  /router/setup             the setup surface: keys by NAME, sources, problems
    GET  /router/roster            every candidate with the facts the decision reads
    POST /v1/seats/resolve         which seat fills a ROLE under a policy + family constraint
    POST /v1/systemone             Jev (TypeSafe System One) behind the hard daily spend cap
    GET  /v1/systemone/spend       today's Jev spend, cap, remaining, blocked calls
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
import uuid
from decimal import Decimal
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import __version__, jev, jevguard, seats
from .config import Config, load
from decimal import Decimal

from .clientkeys import ClientKeys, Refusal, worst_case
from .router import LANES, MAX_ATTEMPTS, Router, estimate_tokens, lane_of
from .secrets import Keyring
from .store import Store

log = logging.getLogger("modelrouter.server")
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    """OpenAI-shaped error, so clients surface the message instead of choking."""
    return JSONResponse({"error": {"message": message, "type": code, "code": code, **extra}},
                        status_code=status)


def _int_header(v: str | None) -> int | None:
    if not v:
        return None
    try:
        n = int(v)
    except ValueError:
        raise HTTPException(400, "X-Router-Answer-Tokens must be an integer")
    if n < 1:
        raise HTTPException(400, "X-Router-Answer-Tokens must be >= 1")
    return n


def build(cfg: Config) -> FastAPI:
    keys = Keyring(cfg.source, dict(cfg.secrets), cfg.gcp_project)
    store = Store(cfg.state_dir / "modelrouter.sqlite3")
    router = Router(cfg, keys, store)
    token_keys = Keyring(cfg.source, {"_token": cfg.token_secret} if cfg.token_secret else {},
                         cfg.gcp_project)
    app = FastAPI(title="modelrouter", version=__version__)
    app.state.router = router
    client_keys = ClientKeys(cfg.state_dir / "modelrouter.sqlite3")
    app.state.client_keys = client_keys

    @app.on_event("startup")
    async def _threads() -> None:
        # Starlette runs blocking handlers on anyio's default pool of 40 threads, which
        # would silently serialise callers beyond 40 concurrent requests. io.net and Sail
        # completed 64 concurrent generations with zero failures; the per-provider caps,
        # not the thread pool, are what should limit concurrency.
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = 256
        # Prices, catalogues, pricing pages and context pages are refreshed out of band:
        # a request never waits on one. The decision is a lookup plus a comparison.
        if cfg.secrets:
            router.gather.start_refresher()

    def principal(authorization: str | None = Header(default=None)):
        """Who is calling: ("client", ClientKey) for a caller key, ("admin", None) for the
        operator's router token (or anyone, on a no_auth loopback bind)."""
        got = (authorization or "").removeprefix("Bearer ").strip()
        if got.startswith("mr_"):
            key = client_keys.authenticate(got)
            if key is None:
                raise HTTPException(401, "invalid caller key", headers={"WWW-Authenticate": "Bearer"})
            if key.revoked:
                raise HTTPException(401, "this key has been revoked",
                                    headers={"WWW-Authenticate": "Bearer"})
            return ("client", key)
        if cfg.no_auth:
            return ("admin", None)
        want = token_keys.get("_token")
        if not want or not got or not pysecrets.compare_digest(got.encode(), want.encode()):
            raise HTTPException(401, "invalid or missing router token",
                                headers={"WWW-Authenticate": "Bearer"})
        return ("admin", None)

    def auth(who=Depends(principal)) -> None:
        """Operator-only routes: probes spend money, decisions show every caller's traffic."""
        if who[0] != "admin":
            raise HTTPException(403, "caller keys can use /v1/chat/completions, /v1/models and "
                                     "/v1/usage only")

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

    @app.get("/v1/usage")
    def usage(who=Depends(principal)):
        """A caller key's own spend: cap, spent, remaining, recent charges."""
        if who[0] != "client":
            raise HTTPException(400, "usage is per caller key; the operator reads /router/*")
        key = client_keys.get(who[1].id)
        return {**key.public(), "recent_charges": client_keys.charges(key.id, 20)}

    @app.get("/v1/models", dependencies=[Depends(principal)])
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

    jev_keys = Keyring(cfg.source, {"typesafe": cfg.jev_secret}, cfg.gcp_project)
    # The one door to the paid TypeSafe API: the seat advisor and /v1/systemone both use it.
    jev_guard = jevguard.Guard(cfg.guard_config(), lambda: jev_keys.get("typesafe"))
    app.state.jev_guard = jev_guard
    advisor = (jev.make_advisor(lambda: jev_keys.get("typesafe"),
                                min_prob=cfg.jev_min_probability, guard=jev_guard)
               if cfg.jev_enabled else None)

    @app.get("/v1/systemone/spend", dependencies=[Depends(auth)])
    def systemone_spend():
        return app.state.jev_guard.status()

    @app.post("/v1/systemone", dependencies=[Depends(auth)])
    async def systemone(request: Request, x_jev_caller: str | None = Header(default=None)):
        """TypeSafe's POST /v1/systemone, same body and same answer, behind the daily cap.
        A caller that points here instead of api.typesafe.ai holds no TypeSafe key and
        cannot overspend: at the cap the free local fallback answers, or it gets a 429."""
        if not cfg.jev_enabled:
            return _error(503, "jev_disabled", "[jev] enabled = false in the router config")
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "invalid_request", "body is not JSON")
        if (not isinstance(body, dict) or not isinstance(body.get("questions"), dict)
                or not body["questions"]):
            return _error(400, "invalid_request", "`questions` must be a non-empty object")
        # Only what System One reads leaves this machine: the model, the state, the questions.
        out = {"model": body.get("model") or jev.MODEL, "state": body.get("state"),
               "questions": body["questions"]}
        try:
            served = await run_in_threadpool(
                lambda: app.state.jev_guard.call(out, caller=(x_jev_caller or "gateway")[:60],
                                                 timeout=60.0))
        except jevguard.CapReached as exc:
            r = _error(429, "jev_daily_cap_reached", str(exc), guard=exc.status)
            r.headers["Retry-After"] = str(exc.retry_after_s)
            return r
        except jevguard.HttpStatus as exc:
            return _error(502, "jev_upstream_error", "TypeSafe answered %s" % exc)
        except Exception as exc:
            return _error(502, "jev_call_failed", "the Jev call failed: %s" % type(exc).__name__)
        resp = (dict(served.response) if isinstance(served.response, dict)
                else {"answers": served.response})
        resp["guard"] = served.public()
        return JSONResponse(resp, headers={"X-Jev-Served-By": served.served_by,
                                           "X-Jev-Spent-Usd": served.status["spent_usd"],
                                           "X-Jev-Cap-Usd": served.status["cap_usd"]})

    def pool_seats() -> list[seats.Seat]:
        out = [s for s in (seats.from_candidate(c) for c in router.gather.roster()) if s]
        out += seats.discover_local(cfg.ollama_url)
        out += seats.adapter_placeholders(cfg.declared_seats)
        out += [seats.from_declared(d) for d in cfg.declared_seats]
        return out

    @app.post("/v1/seats/resolve", dependencies=[Depends(auth)])
    async def seats_resolve(request: Request):
        """Which seat fills ROLE under POLICY and a cross-family constraint. Advisory: no
        inference on the chosen seat, no spend there (Jev's own call is the only cost)."""
        b = await request.json()
        consumer = b.get("consumer")
        if not isinstance(consumer, str) or consumer not in cfg.consumers:
            raise HTTPException(400, "unknown consumer %r; configured: %s -- add it under "
                                     "[consumers.<name>] with a policy" % (consumer, ", ".join(cfg.consumers)))
        configured = cfg.consumers[consumer]
        for field_ in ("exclude_families", "exclude_seats"):
            v = b.get(field_) or []
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise HTTPException(400, "%s must be a list of strings" % field_)
        try:
            # the request may lower the configured price ceiling, never raise it
            ceilings = [Decimal(str(x)) for x in (b.get("max_usd_per_m"), cfg.ceiling_usd_per_mtok)
                        if x is not None]
            req = seats.SeatRequest(
                role=str(b.get("role") or "general"), consumer=consumer,
                policy=b.get("policy"),
                exclude_families=tuple(b.get("exclude_families") or ()),
                exclude_seats=tuple(b.get("exclude_seats") or ()),
                min_context=(b.get("need") or {}).get("min_context"),
                needs_tools=bool((b.get("need") or {}).get("tools")),
                ceiling_usd_per_mtok=min(ceilings) if ceilings else None,
                task=str(b.get("task") or ""))
            res = await run_in_threadpool(
                lambda: seats.resolve(req, pool_seats(), configured_policy=configured,
                                      allow_subscription_free=cfg.free_only_allows_subscription,
                                      advisor=advisor))
        except (ValueError, ArithmeticError) as exc:
            raise HTTPException(400, str(exc))
        body = res.public()
        body["decision_id"] = uuid.uuid4().hex[:16]
        if res.outcome is seats.Outcome.UNAVAILABLE:
            raise HTTPException(422, detail={"type": "seat_unavailable", **body})
        return body

    @app.post("/router/explain", dependencies=[Depends(auth)])
    async def explain(request: Request,
                      x_router_max_usd_per_m: str | None = Header(default=None),
                      x_router_lane: str | None = Header(default=None),
                      x_router_session: str | None = Header(default=None),
                      x_router_answer_tokens: str | None = Header(default=None)):
        req = await request.json()
        try:
            lane = lane_of(req, x_router_lane)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return await run_in_threadpool(router.explain, req, ceiling(x_router_max_usd_per_m), lane,
                                       x_router_session, _int_header(x_router_answer_tokens))

    @app.post("/router/probe", dependencies=[Depends(auth)])
    async def probe(request: Request):
        body = await request.json()
        seats = body.get("seats") or None
        cheapest = int(body.get("cheapest") or 0)
        if not seats and not cheapest and not (body.get("generation") and body.get("measured")):
            raise HTTPException(422, "give `seats`, `cheapest`, or generation + measured")
        budget = body.get("budget_usd")
        budget = min(float(budget), cfg.probe_budget_usd) if budget is not None else None
        if body.get("generation"):
            return await run_in_threadpool(router.probe_generation, seats,
                                           bool(body.get("measured")), budget)
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

    @app.post("/v1/chat/completions")
    async def chat(request: Request, who=Depends(principal),
                   user_agent: str | None = Header(default=None),
                   x_router_max_usd_per_m: str | None = Header(default=None),
                   x_router_lane: str | None = Header(default=None),
                   x_router_session: str | None = Header(default=None),
                   x_router_ensemble: str | None = Header(default=None),
                   x_router_answer_tokens: str | None = Header(default=None)):
        try:
            req = await request.json()
        except ValueError:
            return _error(400, "invalid_request", "body is not JSON")
        if not isinstance(req.get("messages"), list) or not req["messages"]:
            return _error(400, "invalid_request", "`messages` must be a non-empty list")
        lim = ceiling(x_router_max_usd_per_m)
        client = (user_agent or "")[:80]
        key, held = None, Decimal(0)
        if who[0] == "client":
            key = who[1]
            client = "key:%s" % key.id
            # A caller key never runs open-ended: an unset budget gets the key's default,
            # and the price ceiling is the key's, however high the caller asks for.
            if req.get("max_tokens") is None and req.get("max_completion_tokens") is None:
                req["max_tokens"] = key.default_max_tokens
            key_lim = float(key.max_usd_per_mtok)
            lim = key_lim if lim is None else min(lim, key_lim)
            budget = int(req.get("max_completion_tokens") or req.get("max_tokens"))
            try:
                held = client_keys.reserve(key, worst_case(
                    estimate_tokens(req), budget, 1024 if cfg.clamp_max_tokens else 0,
                    MAX_ATTEMPTS, key.max_usd_per_mtok))
            except Refusal as r:
                return _error(r.status, r.code, r.message)
        try:
            lane = lane_of(req, x_router_lane)
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        if x_router_ensemble:
            return _error(501, "not_enabled", "ensemble routing (k cheap models from different "
                          "families + a verifier) is a reserved hook: the experiment that "
                          "decides whether it pays has not run")
        # Conversation identity for prompt-cache warmth: explicit header, else OpenAI `user`.
        session = (x_router_session or str(req.get("user") or "") or None)
        answer = _int_header(x_router_answer_tokens)
        if req.get("stream"):
            routed, it = await run_in_threadpool(
                lambda: router.route_stream(req, client=client, ceiling=lim, lane=lane,
                                            session=session, answer_tokens=answer))
            if it is None:
                if key:
                    client_keys.release(key, held)
                return _abstain(routed)
            if key:
                it = _settling(it, key, held, routed)
            return StreamingResponse(it, media_type="text/event-stream",
                                     headers={"X-Router-Decision": routed.decision_id,
                                              "X-Router-Seat": routed.choice.seat or "",
                                              "Cache-Control": "no-cache"})
        routed = await run_in_threadpool(lambda: router.route(req, client=client, ceiling=lim,
                                                                 lane=lane, session=session,
                                                                 answer_tokens=answer))
        charged = None
        if key:
            if not routed.doc["attempts"]:
                client_keys.release(key, held)          # nothing was called: nothing to pay
            else:
                charged = client_keys.settle(key, held, _exact(routed.doc), routed.decision_id,
                                             routed.doc["result"]["cost_basis"] or "unknown")
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
        headers = {"X-Router-Decision": routed.decision_id,
                   "X-Router-Seat": routed.choice.seat or ""}
        if key:
            left = client_keys.get(key.id)
            body["router"]["charged_usd"] = str(charged)
            body["router"]["key_remaining_usd"] = str(left.remaining)
            headers["X-Router-Key-Charged"] = str(charged)
            headers["X-Router-Key-Remaining"] = str(left.remaining)
        return JSONResponse(body, headers=headers)

    def _exact(doc: dict):
        r = doc.get("result") or {}
        if r.get("calls_unknown_cost"):
            return None                    # charged at the reservation, the worst case
        v = r.get("cost_usd_exact")
        return Decimal(v) if v is not None else None

    def _settling(it, key, held, routed):
        try:
            yield from it
        finally:
            # providers.stream records the result before this runs (its own finally).
            client_keys.settle(key, held, _exact(routed.doc), routed.decision_id,
                               (routed.doc.get("result") or {}).get("cost_basis") or "unknown")

    def _abstain(routed) -> JSONResponse:
        ch = routed.choice
        busy = [a for a in ch.considered if "AT_CAPACITY" in a.because]
        excluded = [a for a in ch.considered if a.verdict.value == "EXCLUDED"]
        raced = "slot taken between decision and call" in ch.because
        if raced or busy and len(busy) == len(excluded) and not any(
                a.verdict.value == "UNKNOWN" for a in ch.considered):
            # Only capacity stood in the way: that clears in seconds. 429 + Retry-After is
            # what OpenAI clients already retry on, instead of treating it as a hard refusal.
            r = _error(429, "router_at_capacity",
                       "every eligible model's provider is at its measured concurrency cap: "
                       + ch.because, decision_id=routed.decision_id)
            r.headers["Retry-After"] = "1"
            return r
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
