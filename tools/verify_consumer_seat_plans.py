"""Run both real consumer adapters against a real HTTP router with fake inference.

Usage: python tools/verify_consumer_seat_plans.py --council /path/to/council
       --glass /path/to/glass-pr-solver
Requires the router test dependencies and the two local consumer checkouts.
No provider, vault, GitHub or paid inference is contacted.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import importlib.util
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import httpx
import uvicorn


def run(council: Path, glass: Path):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "engine"))
    sys.path.insert(0, str(glass / "engine"))
    stream_dir = council / "deployed" / "council-private-stream"
    sys.path.insert(0, str(stream_dir))
    from modelrouter.config import Config
    from modelrouter.decision import Candidate, Emits
    from modelrouter.server import build
    from modelrouter import providers
    from glass_solver.config import merged
    from glass_solver.facts import Observed
    from glass_solver.seat_plans import PlanClient, request_for
    from council_seat_plans import RouterSeatPlanClient

    calls = []
    seats = ["openrouter:fixture/" + letter for letter in "ABCDEF"]

    def fake_call(provider, model, key, request, *, max_tokens, **kwargs):
        calls.append({"seat": provider + ":" + model, "tools": request.get("tools"),
                      "provider_filter": request.get("provider")})
        return providers.extract(provider, model, {
            "model": model,
            "choices": [{"message": {"content": "Fixture answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    def no_network(*args, **kwargs):
        raise AssertionError("a provider/network refresh or probe was attempted")

    providers.call = fake_call
    providers.fetch_catalogue = no_network
    with tempfile.TemporaryDirectory(prefix="seat-integration-") as temporary:
        state = Path(temporary)
        identities = {seat: {"weights_id": "fixture-weights-" + seat[-1]} for seat in seats}
        cfg = Config(path=state / "router.toml", state_dir=state, source="env",
                     no_auth=True, interactive_max_latency_s=None,
                     seat_model_identities=identities,
                     seat_reference_models={"integration-authors": {
                         "consumer": "glass-solver", "source": "operator-configured",
                         "allow_consumer_models": True}})
        app = build(cfg)
        roster = [Candidate(seat=seat, provider="openrouter", model=seat.split(":", 1)[1],
                    weights_id=identities[seat]["weights_id"],
                    identity_evidence_id=cfg.seat_model_identities[seat]["identity_evidence_id"],
                    emits=Emits.CONTENT, has_reasoning_field=False, observation_count=3,
                    supports_tools=True, context_length=65536,
                    list_prompt=Decimal("0.1"), list_completion=Decimal("0.2")) for seat in seats]
        app.state.router.gather.roster = lambda lane="background": list(roster)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        base = "http://127.0.0.1:" + str(sock.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise AssertionError("local router failed to start")
                time.sleep(0.02)

            os.environ.update(COUNCIL_ROUTER_ENABLED="1", COUNCIL_ROUTER_URL=base,
                              COUNCIL_MODE="private", COUNCIL_AUTH_PASSWORD="fixture-password",
                              COUNCIL_LOG_DIR=str(state / "council-logs"), COUNCIL_MEMORY_DIR="")
            spec = importlib.util.spec_from_file_location("integration_council", stream_dir / "council_service.py")
            service = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = service
            spec.loader.exec_module(service)
            logging.getLogger("httpx").setLevel(logging.WARNING)
            service._router_plan_client = lambda: RouterSeatPlanClient(base)
            council_result = service.run_council(service.AskRequest(
                question="Local fixture: deliberate about a change", seat_plan={"member_count": 4,
                    "context": {"run_id": "integration-round"}}))
            assert len(council_result["proposals"]) == len(council_result["critiques"]) == 4
            assert len({s["weights_id"] for s in council_result["slot_assignments"]}) == 4
            assert len(calls) == 10, "all Council stages must execute pinned assignments"

            head = "a" * 40
            glass_cfg = merged({"repos": ["fixture/repo"], "seat_plans": {
                "enabled": True, "base_url": base, "secret": "fixture-router-token",
                "author_models": [{"repo": "fixture/repo", "number": 1, "head_sha": head,
                    "models": [seats[0]], "evidence_id": "integration-authors"}]},
                "reviewers": [{"name": "fixture-worker", "plan_slot": "*", "command": "fixture"}],
                "queues": {"seat_secret_project": "fixture-project",
                    "seat_upstreams": [{"url": base + "/v1/chat/completions", "secret": "fixture-router-token"}]}})
            observed = Observed("fixture/repo", 1, head, {}, {}, {})
            with httpx.Client(trust_env=False) as client:
                plan_client = PlanClient(glass_cfg, key=lambda: ("fixture-value", ""))
                request = request_for(glass_cfg, "fixture/repo#1", observed)
                plan = plan_client.resolve(request)
                reviewers = [s for s in plan["assignments"] if "review" in s["roles"]]
                assert len(reviewers) == 2 and len({s["weights_id"] for s in reviewers}) == 2
                assert all(s["weights_id"] != identities[seats[0]]["weights_id"] for s in reviewers)
                for assignment in reviewers:
                    with plan_client.gateway(assignment, plan) as gateway:
                        gateway_url, gateway_token = gateway.start()
                        response = client.post(gateway_url + "/chat/completions",
                            headers={"Authorization": "Bearer " + gateway_token}, json={
                                "model": "untrusted-worker-choice", "messages": [{"role": "user", "content": "fixture review"}]})
                        assert response.status_code == 200, response.text
                        assert response.json()["router"]["seat_plan"]["executed_seat"] == assignment["seat"]
                        assert gateway.executed_receipts, "actual pinned execution must be observed"
                stored = client.get(base + "/v1/seat-plans/" + plan["plan_id"]).json()
                assert len([r for r in stored["receipts"] if r["status"] == "ANSWERED"]) == 2
                assert "fixture review" not in json.dumps(stored)
                assert all(c["provider_filter"]["ignore"] == ["Cerebras", "Groq"] for c in calls[-2:])
                plan_client.close()
            print(json.dumps({"council": {"members": 4, "pinned_calls": 10},
                "glass": {"review_slots": 2, "distinct_weights": True, "excluded_author": True,
                          "verified_pinned_calls": 2}, "live_provider_calls": 0}, indent=2))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--council", type=Path, required=True)
    parser.add_argument("--glass", type=Path, required=True)
    args = parser.parse_args()
    run(args.council.resolve(), args.glass.resolve())
