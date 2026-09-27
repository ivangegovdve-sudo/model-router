"""MCP: the same decision, as an advisory tool.

An MCP server cannot switch a client's model -- the client chose it before calling
any tool. So this face only ADVISES: "what would the router pick for this, and
why". It is a thin client of the running proxy's HTTP contract (docs/CONTRACT.md):
same engine, same facts, same answer. Provider keys never come near it.

Config (env):
  MODELROUTER_URL     default http://127.0.0.1:7480
  MODELROUTER_TOKEN   the router token, if the proxy requires one
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server.mcpserver import MCPServer

URL = os.environ.get("MODELROUTER_URL", "http://127.0.0.1:7480").rstrip("/")

mcp = MCPServer("modelrouter", instructions=(
    "Ask which model the router would choose for a request and why -- every candidate "
    "considered, its live and measured price, and the fact that decided. Advisory only: "
    "to actually route, point an OpenAI-compatible client at the proxy with model 'auto'."))


def _client() -> httpx.Client:
    tok = os.environ.get("MODELROUTER_TOKEN", "").strip()
    return httpx.Client(base_url=URL, timeout=120,
                        headers={"Authorization": "Bearer " + tok} if tok else {})


def _get(path: str, **params) -> dict | list:
    with _client() as c:
        r = c.get(path, params=params)
        if r.status_code != 200:
            return {"error": "HTTP %d" % r.status_code, "detail": r.text[:300]}
        return r.json()


def summarise_choice(ch: dict, top: int = 8) -> dict:
    ranked = [c for c in ch["considered"] if c["verdict"] == "QUALIFIES"][:top]
    excluded = [c for c in ch["considered"] if c["verdict"] == "EXCLUDED"][:top]
    unknown = [c for c in ch["considered"] if c["verdict"] == "UNKNOWN"]
    return {"outcome": ch["outcome"], "seat": ch["seat"], "because": ch["because"],
            "max_tokens": ch["max_tokens"], "expected_usd": ch["expected_usd"],
            "qualifying": ranked, "excluded": excluded,
            "unknown_count": len(unknown), "unknown_facts": ch["unknown"]}


@mcp.tool()
def explain_route(prompt: str, max_tokens: int | None = None, needs_tools: bool = False,
                  model: str = "auto", ceiling_usd_per_mtok: float | None = None,
                  answer_tokens: int | None = None, lane: str = "interactive") -> str:
    """Which model would the router choose for this request, and why? Makes no model
    call and spends nothing.

    Returns the outcome (ROUTE or ABSTAIN), the chosen seat ("provider:model"), the
    fact that decided, the qualifying candidates ranked by expected cost, and the
    excluded ones with the reason each was ruled out (e.g. a reasoning model whose
    answer needs more max_tokens than the request allows). answer_tokens is the expected
    answer length: cost per answer is rate x tokens actually burned, so a 2-token reply
    can make a $2/M model cheaper than a $0.10/M one that reasons first. lane:
    interactive | background | batch. ABSTAIN means no model can
    serve the request honestly -- read `because`.
    """
    req: dict = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if max_tokens is not None:
        req["max_tokens"] = max_tokens
    if needs_tools:
        req["tools"] = [{"type": "function", "function": {"name": "tool", "parameters": {}}}]
    headers = {"X-Router-Lane": lane}
    if answer_tokens is not None:
        headers["X-Router-Answer-Tokens"] = str(answer_tokens)
    if ceiling_usd_per_mtok is not None:
        headers["X-Router-Max-Usd-Per-M"] = str(ceiling_usd_per_mtok)
    with _client() as c:
        r = c.post("/router/explain", json=req, headers=headers)
        if r.status_code != 200:
            return json.dumps({"error": "HTTP %d" % r.status_code, "detail": r.text[:300]})
        d = r.json()
    return json.dumps({**summarise_choice(d["choice"]), "providers": d["providers"]}, indent=1)


@mcp.tool()
def recent_decisions(limit: int = 15) -> str:
    """Recent routed calls: which seat served each, why, what it cost, and the status
    (ANSWERED, ABSTAINED, FAILED)."""
    return json.dumps(_get("/router/decisions", limit=limit), indent=1)


@mcp.tool()
def get_decision(decision_id: str) -> str:
    """One routed call in full: every candidate considered, each attempt, the cost."""
    d = _get("/router/decisions/" + decision_id)
    if isinstance(d, dict) and "choice" in d:
        d = {**d, "choice": summarise_choice(d["choice"])}
        d.pop("earlier_decisions", None)
    return json.dumps(d, indent=1)


@mcp.tool()
def router_setup() -> str:
    """Is the router ready? Which provider keys are readable (by name only), which
    providers are blocked or out of quota, and any configuration problem."""
    return json.dumps(_get("/router/setup"), indent=1)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
