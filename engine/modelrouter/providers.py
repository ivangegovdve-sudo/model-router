"""Providers: where models are served, how each catalogue reads, how a call is made.

Every provider speaks the OpenAI chat-completions shape for calls. Catalogues differ
in how they publish prices, so each has a parser; a price a parser cannot read is
None (UNKNOWN), never 0.

call() never raises and never echoes credentials. A call's cost is:
  billed    the provider reported what it charged for THIS generation
  computed  usage x this model's own rate card (the provider reports no charge)
  unknown   neither is available
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import httpx

PROVIDERS: dict[str, dict[str, Any]] = {
    "openrouter": {"base": "https://openrouter.ai/api/v1", "catalogue": "/models",
                   "parse": "openrouter"},
    "akashml": {"base": "https://api.akashml.com/v1", "catalogue": "/models",
                "parse": "akashml"},
    "venice": {"base": "https://api.venice.ai/api/v1", "catalogue": "/models?type=text",
               "parse": "venice"},
    "nous": {"base": "https://inference-api.nousresearch.com/v1", "catalogue": "/models",
             "parse": "openrouter"},
    "sail": {"base": "https://api.sailresearch.com/v1", "catalogue": "/models",
             "parse": "bare"},                  # publishes no prices: UNKNOWN
}
UA = "modelrouter/0.1 (+https://github.com/ivangegovdve-sudo/model-router)"
_ACCOUNT = re.compile(r"budget|credit|balance|insufficient|payment|billing|unauthori|"
                      r"invalid api key|no auth|forbidden", re.I)
_QUOTA = re.compile(r"quota|rate.?limit|per.?day|too many|limit exceeded", re.I)
_NOT_TEXT = re.compile(r"(embed|rerank|moderation|guard|whisper|tts|transcri|image|"
                       r"flux|sdxl|video|audio|ocr)", re.I)


def _f(v) -> float | None:
    """A price field -> float, or None. Negative means 'variable' upstream: UNKNOWN."""
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x >= 0 else None


def _per_m(v) -> float | None:
    x = _f(v)
    return None if x is None else x * 1e6


@dataclass
class Listing:
    """One model as its provider's catalogue describes it."""

    provider: str
    model: str
    prompt: float | None            # USD / M tokens
    completion: float | None
    cached_prompt: float | None = None
    context_length: int | None = None
    supports_tools: bool | None = None
    reasoning: bool | None = None
    text_out: bool | None = True


def parse_catalogue(provider: str, rows: list[dict]) -> list[Listing]:
    shape = PROVIDERS[provider]["parse"]
    out: list[Listing] = []
    for m in rows:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        if shape in ("openrouter", "akashml"):
            p = m.get("pricing") or {}
            if shape == "openrouter":
                pr, co, ca = _per_m(p.get("prompt")), _per_m(p.get("completion")), \
                    _per_m(p.get("input_cache_read"))
                params = m.get("supported_parameters")
                tools = None if params is None else ("tools" in params)
                reasoning = None if params is None else ("reasoning" in params
                                                         or "include_reasoning" in params)
                arch = m.get("architecture") or {}
                outs = arch.get("output_modalities")
            else:
                pr, co, ca = _per_m(p.get("input")), _per_m(p.get("output")), \
                    _per_m(p.get("input_cache_read"))
                feats = m.get("supported_features")
                tools = None if feats is None else ("tools" in feats)
                reasoning = None if feats is None else ("reasoning" in feats)
                outs = m.get("output_modalities")
            ctx = m.get("context_length")
            text_out = None if outs is None else ("text" in outs)
        elif shape == "venice":
            spec = m.get("model_spec") or {}
            pp = spec.get("pricing") or {}
            pr = _f((pp.get("input") or {}).get("usd"))
            co = _f((pp.get("output") or {}).get("usd"))
            ca = None
            ctx = spec.get("availableContextTokens")
            caps = spec.get("capabilities") or {}
            tools = caps.get("supportsFunctionCalling")
            reasoning = caps.get("supportsReasoning")
            text_out = m.get("type", "text") == "text"
        else:
            pr = co = ca = None
            ctx = m.get("context_length")
            tools = reasoning = None
            text_out = None
        if _NOT_TEXT.search(mid) or text_out is False:
            continue
        try:
            ctx = int(ctx) if ctx is not None else None
        except (TypeError, ValueError):
            ctx = None
        out.append(Listing(provider, mid, pr, co, ca, ctx, tools, reasoning, text_out))
    return out


def fetch_catalogue(provider: str, key: str, timeout: float = 30) -> list[Listing]:
    spec = PROVIDERS[provider]
    r = httpx.get(spec["base"] + spec["catalogue"], timeout=timeout,
                  headers={"Authorization": "Bearer " + key, "User-Agent": UA})
    r.raise_for_status()
    d = r.json()
    rows = (d.get("data") if isinstance(d, dict) else d) or []
    return parse_catalogue(provider, rows)


@dataclass
class Result:
    ok: bool                         # True only when usable output came back
    status: int                      # upstream HTTP status (0: no response)
    provider: str
    model: str
    content: str = ""
    reasoning: str = ""              # reasoning_content / reasoning, if emitted
    tool_calls: int = 0
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    billed_usd: float | None = None
    latency_s: float = 0.0
    detail: str = ""
    scope: str = "model"             # on failure: model | provider | quota
    ratelimit: dict[str, str] = field(default_factory=dict)
    body: dict | None = None         # the upstream JSON, returned to the client as-is

    @property
    def empty(self) -> bool:
        return self.status == 200 and not self.content.strip() and not self.tool_calls


def classify_failure(status: int, msg: str) -> str:
    if status in (401, 402):
        return "provider"
    if status == 429 or _QUOTA.search(msg or ""):
        return "quota"
    if status == 403 and _ACCOUNT.search(msg or ""):
        return "provider"
    return "model"


def _ratelimit(headers: httpx.Headers) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items() if "ratelimit" in k.lower()}


def extract(provider: str, model: str, payload: dict) -> Result:
    """Read one non-streamed completion. Content and reasoning are kept apart:
    text in reasoning_content is NOT an answer."""
    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    if isinstance(content, list):  # content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    usage = payload.get("usage") or {}
    billed = usage.get("cost")
    if billed is None and isinstance(payload.get("cost"), dict):   # Venice
        billed = payload["cost"].get("usd")
    details = usage.get("prompt_tokens_details") or {}
    return Result(
        ok=bool(str(content).strip()) or bool(msg.get("tool_calls")), status=200,
        provider=provider, model=model, content=str(content), reasoning=str(reasoning),
        tool_calls=len(msg.get("tool_calls") or []), finish_reason=choice.get("finish_reason"),
        prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
        cached_tokens=details.get("cached_tokens"),
        billed_usd=_f(billed), body=payload)


def _body(provider: str, model: str, req: dict, max_tokens: int | None) -> dict:
    body = {k: v for k, v in req.items() if k not in ("model", "max_tokens",
                                                      "max_completion_tokens")}
    body["model"] = model
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if provider == "openrouter":
        body["usage"] = {"include": True}      # ask for the billed cost
    elif provider == "venice" and "venice_parameters" not in body:
        # Venice prepends its own ~1.5k-token system prompt unless told not to: the
        # client would pay for a prompt it never sent. Measured 2026-09-26 ("hi" billed
        # 1507 prompt tokens).
        body["venice_parameters"] = {"include_venice_system_prompt": False}
    return body


def _headers(key: str) -> dict:
    return {"Authorization": "Bearer " + key, "User-Agent": UA,
            "HTTP-Referer": "https://github.com/ivangegovdve-sudo/model-router",
            "X-Title": "modelrouter"}


def _error(r: httpx.Response, key: str) -> str:
    try:
        err = r.json().get("error") or {}
        msg = str(err.get("message", err) if isinstance(err, dict) else err)
    except Exception:
        msg = ""
    return msg.replace(key, "***")[:160] if key else msg[:160]


def call(provider: str, model: str, key: str, req: dict, *, max_tokens: int | None,
         timeout: float = 180) -> Result:
    t0 = time.time()

    def fail(status: int, detail: str, scope: str = "model", rl=None) -> Result:
        return Result(False, status, provider, model, latency_s=time.time() - t0,
                      detail=detail, scope=scope, ratelimit=rl or {})

    spec = PROVIDERS.get(provider)
    if not spec:
        return fail(0, "unknown provider", "provider")
    if not key:
        return fail(0, "no key", "provider")
    body = _body(provider, model, req, max_tokens)
    body.pop("stream", None)
    body.pop("stream_options", None)
    try:
        r = httpx.post(spec["base"] + "/chat/completions", json=body, timeout=timeout,
                       headers=_headers(key))
    except httpx.TimeoutException:
        return fail(0, "timeout after %.0fs" % (time.time() - t0))
    except Exception as exc:                                    # noqa: BLE001
        return fail(0, type(exc).__name__)          # never str(exc): it can echo headers
    rl = _ratelimit(r.headers)
    if r.status_code != 200:
        msg = _error(r, key)
        return fail(r.status_code, "HTTP %d %s" % (r.status_code, msg),
                    classify_failure(r.status_code, msg), rl)
    try:
        res = extract(provider, model, r.json())
    except Exception:
        return fail(200, "unparseable response envelope", rl=rl)
    res.latency_s = time.time() - t0
    res.ratelimit = rl
    if res.empty:
        res.detail = "HTTP 200 but empty content (finish_reason=%s%s)" % (
            res.finish_reason, ", text went to reasoning" if res.reasoning.strip() else "")
    else:
        res.detail = "ok"
    return res


def stream(provider: str, model: str, key: str, req: dict, *, max_tokens: int | None,
           on_done: Callable[[Result], None], timeout: float = 180) -> Iterator[bytes]:
    """Forward an SSE stream verbatim while tallying what it carried.

    Usage arrives in the last chunk when requested; content and reasoning deltas are
    counted separately so an all-reasoning stream is recorded as empty.
    """
    t0 = time.time()
    body = _body(provider, model, req, max_tokens)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    res = Result(False, 0, provider, model)
    content: list[str] = []
    reasoning_chars = 0
    try:
        with httpx.stream("POST", PROVIDERS[provider]["base"] + "/chat/completions",
                          json=body, timeout=timeout, headers=_headers(key)) as r:
            res.status = r.status_code
            res.ratelimit = _ratelimit(r.headers)
            if r.status_code != 200:
                r.read()
                msg = _error(r, key)
                res.detail = "HTTP %d %s" % (r.status_code, msg)
                res.scope = classify_failure(r.status_code, msg)
                err = {"error": {"message": "upstream %s refused: %s" % (provider, res.detail),
                                 "type": "upstream_error"}}
                yield ("data: %s\n\n" % json.dumps(err)).encode()
                return
            for line in r.iter_lines():
                yield (line + "\n").encode()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    continue
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("content"):
                        content.append(d["content"])
                    reasoning_chars += len(d.get("reasoning_content") or d.get("reasoning") or "")
                    if d.get("tool_calls"):
                        res.tool_calls += 1
                    if ch.get("finish_reason"):
                        res.finish_reason = ch["finish_reason"]
                u = ev.get("usage")
                if u:
                    res.prompt_tokens = u.get("prompt_tokens")
                    res.completion_tokens = u.get("completion_tokens")
                    res.cached_tokens = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
                    res.billed_usd = _f(u.get("cost"))
    except httpx.TimeoutException:
        res.detail = "timeout mid-stream"
    except Exception as exc:                                    # noqa: BLE001
        res.detail = type(exc).__name__
    finally:
        res.content = "".join(content)
        res.reasoning = "x" * reasoning_chars       # length only; the text went to the client
        res.latency_s = time.time() - t0
        res.ok = res.status == 200 and (bool(res.content.strip()) or res.tool_calls > 0)
        if res.status == 200 and not res.detail:
            res.detail = "ok" if res.ok else "HTTP 200 but empty content (streamed)"
        on_done(res)
