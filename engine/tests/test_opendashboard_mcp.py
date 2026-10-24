"""Opt-in integration against the actual published open-dashboard-mcp package.

Set OPENDASHBOARD_MCP_ENTRY to its installed build/index.js to run. The package
advises against a local measured fixture; no provider keys/calls are involved.
"""
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import socket
import threading
import time

import pytest
import uvicorn

from modelrouter.config import Config
from modelrouter.decision import Candidate, Emits
from modelrouter.server import build


def test_published_opendashboard_2_seat_tool_calls_router(tmp_path, monkeypatch):
    entry = os.environ.get("OPENDASHBOARD_MCP_ENTRY")
    if not entry or not Path(entry).is_file() or not shutil.which("node"):
        pytest.skip("set OPENDASHBOARD_MCP_ENTRY to installed open-dashboard-mcp@2.0.0 build/index.js")
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    monkeypatch.setenv("SEAT_PACKAGE_TEST_TOKEN", "local-package-test-token")
    app = build(Config(path=tmp_path / "config.toml", state_dir=tmp_path, source="env",
                       token_secret="SEAT_PACKAGE_TEST_TOKEN"))
    router = app.state.router
    candidate = Candidate(seat="openrouter:fixture-model", provider="openrouter", model="fixture-model",
                          list_prompt=Decimal("0.1"), list_completion=Decimal("0.2"),
                          context_length=32768, emits=Emits.CONTENT, supports_tools=True,
                          min_max_tokens=1, has_reasoning_field=False, observation_count=3)
    monkeypatch.setattr(router.gather, "roster", lambda lane="interactive": [candidate])
    def no_inference(*args, **kwargs):
        pytest.fail("dashboard seat advice made a provider call")
    monkeypatch.setattr("modelrouter.providers.call", no_inference)
    monkeypatch.setattr("modelrouter.providers.stream", no_inference)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started

    async def exchange():
        params = StdioServerParameters(command=shutil.which("node"), args=[entry], env={
            "MODEL_ROUTER_URL": f"http://127.0.0.1:{port}",
            "MODEL_ROUTER_TOKEN": "local-package-test-token"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                assert "dashboard_resolve_seat" in {tool.name for tool in listing.tools}
                async def call(arguments):
                    result = await session.call_tool("dashboard_resolve_seat", arguments)
                    payload = result.model_dump(by_alias=True)
                    assert not payload.get("isError", False), payload
                    return json.loads(next(item["text"] for item in payload["content"] if item["type"] == "text"))
                seat = await call({"consumer": "glass-solver", "role": "review",
                                   "needsTools": True, "minContext": 8192})
                assert seat["status"] == "seat" and seat["seat"] == "openrouter:fixture-model"
                assert seat["tier"] == 2 and seat["policy"] == "cheapest"
                assert seat["pool"]["order"] == ["sail", "codex", "antigravity", "local", "openrouter"]
                assert seat["jev"]["confidence"]["probability"] is None
                assert router.store.get(seat["decisionId"])["result"]["calls"] == 0
                refused = await call({"consumer": "public-council", "policy": "cheapest"})
                assert refused["status"] == "unavailable" and refused["policy"] == "free-only"
    try:
        asyncio.run(exchange())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
