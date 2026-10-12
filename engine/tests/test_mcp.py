"""Real MCP initialize/list/call exchanges against a local, non-spending router contract."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from modelrouter.mcp_server import summarise_choice


CHOICE = {
    "outcome": "ROUTE", "seat": "test:vision", "because": "known vision support",
    "max_tokens": 64, "expected_usd": 0.00001, "expected_usd_exact": "0.000010",
    "considered": [{"seat": "test:vision", "verdict": "QUALIFIES", "because": "known"},
                   {"seat": "test:text", "verdict": "EXCLUDED", "because": "no vision"},
                   {"seat": "test:unknown", "verdict": "UNKNOWN", "because": "unknown"}],
    "unknown": ["capabilities"],
    "facts": {"requirements": {"required_capabilities": ["tools"]},
              "capability_evidence": "test catalogue"},
    "confidence": {"basis": "catalogue and observations", "unknown_count": 1},
}

# Optional override exercises one SDK's client against the other SDK's server.
SERVER_PYTHON = os.environ.get("MODELROUTER_MCP_TEST_SERVER_PYTHON", sys.executable)


def test_choice_summary_preserves_decision_evidence():
    summary = summarise_choice(CHOICE)
    assert summary.get("facts") == CHOICE["facts"]
    assert summary.get("confidence") == CHOICE["confidence"]
    assert summary.get("expected_usd_exact") == "0.000010"
    assert summary["unknown_count"] == 1
    assert summary["unknown_facts"] == ["capabilities"]
    assert summary["excluded"][0]["because"] == "no vision"


def test_choice_summary_counts_unknown_candidates_omitted_by_router():
    choice = {**CHOICE, "facts": {**CHOICE["facts"], "unknown_candidates": 12,
                                  "unknown_not_listed": 11}}
    assert summarise_choice(choice)["unknown_count"] == 12


@contextmanager
def router_contract():
    """A local router HTTP contract; no provider endpoints exist in this fixture."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, data):
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body, dict(self.headers)))
            if self.path == "/router/explain":
                self.respond({"choice": CHOICE, "providers": [{"provider": "test"}]})
            elif self.path == "/v1/seat-plans/resolve":
                self.respond({"schema": "seat-plan/1", "outcome": "RESOLVED",
                              "plan_id": "plan-test", "version": 1, "assignments": [],
                              "constraint_evidence": [{"id": "reviewers", "satisfied": True}]})
            elif self.path == "/v1/seat-plans/plan-test/reselect":
                self.respond({"schema": "seat-plan/1", "outcome": "RESOLVED",
                              "plan_id": "plan-test", "version": 2})
            else:
                self.send_error(404, "model calls are forbidden in this test")

        def do_GET(self):
            requests.append((self.path, None, dict(self.headers)))
            if self.path == "/router/roster?measured_only=false":
                self.respond({"seats": [{"seat": "test:vision", "supports_tools": True,
                                         "input_modalities": ["text", "image"],
                                         "supported_parameters": ["tools"]}]})
            elif self.path == "/router/setup":
                self.respond({"ready": True, "keys": []})
            elif self.path == "/router/decisions?limit=2":
                self.respond([{"id": "decision-test", "status": "ANSWERED"}])
            elif self.path == "/router/decisions/decision-test":
                self.respond({"id": "decision-test", "choice": CHOICE,
                              "earlier_decisions": [], "attempts": []})
            elif self.path == "/v1/seat-plans/templates":
                self.respond({"schema": "seat-plan/1", "templates": [{"id": "council"}]})
            elif self.path == "/v1/seat-plans/plan-test":
                self.respond({"plan_id": "plan-test", "version": 1})
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def server_environment(url):
    return {**os.environ, "MODELROUTER_URL": url, "MODELROUTER_TOKEN": "local-test-token"}


@contextmanager
def http_server(transport, env, tmp_path):
    with socket.socket() as free_port:
        free_port.bind(("127.0.0.1", 0))
        port = free_port.getsockname()[1]
    with (tmp_path / (transport + ".log")).open("w+") as log:
        process = subprocess.Popen(
            [SERVER_PYTHON, "-m", "modelrouter.mcp_server", "--transport", transport,
             "--port", str(port)], env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail("MCP server exited before listening: " + log.read())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail("MCP HTTP transport never listened")
            yield f"http://127.0.0.1:{port}"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def tool_json(result):
    data = result.model_dump(by_alias=True)
    assert not data.get("isError", False), data
    return json.loads(next(item["text"] for item in data["content"] if item["type"] == "text"))


async def exercise_tools(streams):
    async with ClientSession(streams[0], streams[1]) as session:
        initialized = (await session.initialize()).model_dump(by_alias=True)
        assert initialized["serverInfo"]["name"] == "modelrouter"
        assert initialized["protocolVersion"]  # SDK negotiates the date-version protocol.
        tools = {tool.name: tool.model_dump(by_alias=True)
                 for tool in (await session.list_tools()).tools}
        assert set(tools) == {"explain_route", "recent_decisions", "get_decision",
                              "router_setup", "router_roster", "seat_plan_templates",
                              "resolve_seat_plan", "get_seat_plan", "reselect_seat_plan"}
        assert "requirements" in tools["explain_route"]["inputSchema"]["properties"]
        result = tool_json(await session.call_tool("explain_route", {
            "prompt": "Describe this picture",
            "requirements": {"required_capabilities": ["tools"]},
            "max_tokens": 64, "needs_tools": True, "answer_tokens": 16,
            "ceiling_usd_per_mtok": 2.5, "lane": "background"}))
        assert result["seat"] == "test:vision"
        assert result["facts"] == CHOICE["facts"]
        assert result["confidence"] == CHOICE["confidence"]
        roster = tool_json(await session.call_tool("router_roster", {}))
        assert roster["seats"][0]["input_modalities"] == ["text", "image"]
        assert roster["seats"][0]["supports_tools"] is True
        assert roster["seats"][0]["supported_parameters"] == ["tools"]
        assert tool_json(await session.call_tool("router_setup", {}))["ready"] is True
        recent = tool_json(await session.call_tool("recent_decisions", {"limit": 2}))
        assert recent[0]["id"] == "decision-test"
        decision = tool_json(await session.call_tool("get_decision", {"decision_id": "decision-test"}))
        assert decision["choice"]["facts"] == CHOICE["facts"]
        templates = tool_json(await session.call_tool("seat_plan_templates", {}))
        assert templates["templates"][0]["id"] == "council"
        plan = tool_json(await session.call_tool("resolve_seat_plan", {
            "plan": {"schema": "seat-plan/1", "consumer": "private-council", "slots": []}}))
        assert plan["outcome"] == "RESOLVED"
        assert plan["constraint_evidence"][0]["satisfied"] is True
        stored = tool_json(await session.call_tool("get_seat_plan", {"plan_id": "plan-test"}))
        assert stored["version"] == 1
        updated = tool_json(await session.call_tool("reselect_seat_plan", {
            "plan_id": "plan-test", "request": {"version": 1, "context": {}, "failed_slots": []}}))
        assert updated["version"] == 2


@pytest.mark.parametrize("transport", ["stdio", "sse", "streamable-http"])
def test_mcp_transport_round_trip(transport, tmp_path):
    with router_contract() as (url, requests):
        env = server_environment(url)
        if transport == "stdio":
            async def run():
                params = StdioServerParameters(command=SERVER_PYTHON,
                                                args=["-m", "modelrouter.mcp_server"], env=env)
                async with stdio_client(params) as streams:
                    await asyncio.wait_for(exercise_tools(streams), timeout=20)
            asyncio.run(run())
        else:
            with http_server(transport, env, tmp_path) as mcp_url:
                async def run():
                    client = (sse_client(mcp_url + "/sse") if transport == "sse" else
                              streamable_http_client(mcp_url + "/mcp"))
                    async with client as streams:
                        await asyncio.wait_for(exercise_tools(streams), timeout=20)
                asyncio.run(run())
        path, body, headers = requests[0]
        assert path == "/router/explain"
        assert body["router_requirements"] == {"required_capabilities": ["tools"]}
        assert body["messages"] == [{"role": "user", "content": "Describe this picture"}]
        assert body["tools"][0]["function"]["name"] == "tool"
        assert headers["Authorization"] == "Bearer local-test-token"
        assert headers["X-Router-Lane"] == "background"
        assert headers["X-Router-Answer-Tokens"] == "16"
        assert headers["X-Router-Max-Usd-Per-M"] == "2.5"
        assert len(requests) == 9


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "::", "example.com"])
def test_http_cli_rejects_public_bind(host):
    result = subprocess.run([sys.executable, "-m", "modelrouter.mcp_server",
                             "--transport", "streamable-http", "--host", host],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "loopback" in result.stderr
