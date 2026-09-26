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
    # Sail's /models lists ids only. Its prices -- one rate card per completion
    # window -- are published on its docs pricing page, read live like a catalogue.
    "sail": {"base": "https://api.sailresearch.com/v1", "catalogue": "/models",
             "parse": "bare", "prices": "https://docs.sailresearch.com/pricing.md",
             "specs": "https://docs.sailresearch.com/models.md", "windows": True},
    "ionet": {"base": "https://api.intelligence.io.solutions/api/v1", "catalogue": "/models",
              "parse": "ionet"},
}
# Every provider call carries a real User-Agent. io.net sits behind Cloudflare, which
# answers a request with no recognisable client (Python urllib's default) with
# "403 error code: 1010" -- a bot check that looks exactly like a rejected key.
UA = "modelrouter/0.1 (+https://github.com/ivangegovdve-sudo/model-router)"
_EDGE = re.compile(r"error code:\s*10\d\d|cloudflare|attention required|cf-ray|"
                   r"just a moment", re.I)
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


#: Lane -> the scheduling windows it may buy, most preferred first. Sail names them
#: asap (low latency), balanced (wider scheduling), flex (best effort).
LANE_WINDOWS = {"interactive": ("asap",), "background": ("balanced", "asap"),
                "batch": ("flex", "balanced", "asap")}
_WINDOW_LABEL = {"default (asap)": "asap", "asap": "asap", "balanced": "balanced",
                 "flex": "flex"}
_PRICE_ARIA = re.compile(
    r'aria-label="[^"]*?\s(Default \(ASAP\)|ASAP|Balanced|Flex) pricing: input \$([\d.]+), '
    r'cached \$([\d.]+), output \$([\d.]+) per 1M tokens\.?"', re.I)


def parse_window_prices(page: str) -> dict[str, dict[str, tuple[float, float, float]]]:
    """Sail pricing page -> {model_id: {window: (prompt, completion, cached)}} in $/M.

    Keyed by the page's own `data-model` ids, so nothing here names a model. A model
    or window the page does not price is simply absent: UNKNOWN, never zero."""
    out: dict[str, dict[str, tuple[float, float, float]]] = {}
    for chunk in page.split("<tbody")[1:]:
        m = re.search(r'data-model="([^"]+)"', chunk[:400])
        if not m:
            continue
        for w, inp, cached, outp in _PRICE_ARIA.findall(chunk):
            win = _WINDOW_LABEL.get(w.lower())
            if win:
                out.setdefault(m.group(1), {})[win] = (float(inp), float(outp), float(cached))
    return out


_CTX = re.compile(r'cap-expand-key">Context</span>\s*<span className="cap-expand-val">\s*'
                  r'([\d.]+)\s*([KkMm]?)\s*</span>')
_CODE_ID = re.compile(r"<code>([^<\s]+/[^<\s]+)</code>")


def parse_context_lengths(page: str) -> dict[str, int]:
    """Sail models page -> {model_id: context tokens}. Each model's "Context" value
    precedes its `<code>org/model</code>` id in the page; pair each with the next id."""
    out: dict[str, int] = {}
    for m in _CTX.finditer(page):
        ident = _CODE_ID.search(page, m.end())
        if not ident or ident.group(1) in out:
            continue
        n = float(m.group(1)) * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
        out[ident.group(1)] = int(n)
    return out


def fetch_context_lengths(provider: str, timeout: float = 30) -> dict[str, int]:
    url = PROVIDERS[provider].get("specs")
    if not url:
        return {}
    r = httpx.get(url, timeout=timeout, headers={"User-Agent": UA})
    r.raise_for_status()
    return parse_context_lengths(r.text)


def fetch_window_prices(provider: str, timeout: float = 30) -> dict:
    url = PROVIDERS[provider].get("prices")
    if not url:
        return {}
    r = httpx.get(url, timeout=timeout, headers={"User-Agent": UA})
    r.raise_for_status()
    return parse_window_prices(r.text)


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
    windows: dict[str, tuple[float, float, float]] | None = None   # window -> (p, c, cached)
    blocked: str = ""               # the catalogue itself says this key cannot use it


def parse_catalogue(provider: str, rows: list[dict]) -> list[Listing]:
    shape = PROVIDERS[provider]["parse"]
    out: list[Listing] = []
    for m in rows:
        blocked_why = ""
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
        elif shape == "ionet":
            pr, co, ca = _per_m(m.get("input_token_price")), _per_m(m.get("output_token_price")),                 _per_m(m.get("cache_read_token_price"))
            ctx = m.get("context_window") or m.get("max_model_len")
            tools = m.get("supports_tools")
            reasoning = m.get("supports_reasoning")
            outs = m.get("output_modalities")
            text_out = None if outs is None else ("text" in outs)
            if m.get("higher_tier_required") is True:
                tier = m.get("min_access_tier")
                blocked_why = "io.net access tier %s required; this key's tier is lower" % tier \
                    if tier is not None else "a higher io.net access tier is required"
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
        out.append(Listing(provider, mid, pr, co, ca, ctx, tools, reasoning, text_out,
                           blocked=blocked_why))
    return out


class CatalogueError(Exception):
    def __init__(self, status: int, scope: str, msg: str):
        super().__init__("HTTP %d" % status)
        self.status, self.scope, self.msg = status, scope, msg


def fetch_catalogue(provider: str, key: str, timeout: float = 30) -> list[Listing]:
    spec = PROVIDERS[provider]
    r = httpx.get(spec["base"] + spec["catalogue"], timeout=timeout,
                  headers={"Authorization": "Bearer " + key, "User-Agent": UA})
    if r.status_code != 200:
        msg, raw = _error(r, key)
        raise CatalogueError(r.status_code, classify_failure(r.status_code, msg, raw, r.headers),
                             msg)
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
    reasoning_field: bool | None = None  # the response HAS a reasoning field (even empty)
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


def is_edge_block(status: int, body: str, headers: httpx.Headers | None = None) -> bool:
    """A CDN/bot-check refusal: it says nothing about the key."""
    if status not in (403, 429, 503, 520, 521, 522, 523, 524, 525, 526):
        return False
    if _EDGE.search(body or ""):
        return True
    return bool(headers) and "cf-ray" in headers and         "text/html" in (headers.get("content-type") or "")


def classify_failure(status: int, msg: str, raw: str = "",
                     headers: httpx.Headers | None = None) -> str:
    """model: try another model | provider: the account cannot be used (auth, money)
    | quota: a rate window | edge: a CDN refused HOW we called, not WHO we are --
    the credential is not suspect and must not be rotated over it."""
    if is_edge_block(status, raw or msg, headers):
        return "edge"
    # io.net answers 402 for ONE model the key's tier cannot use ("requires a higher IO
    # Intelligence tier"). That is the model, not the account: benching the provider over
    # it would take every other io.net model down with it.
    if status == 402 and re.search(r"tier", (msg or "") + (raw or ""), re.I):
        return "model"
    if status in (401, 402):
        return "provider"
    # Money before rate: "Budget limit exceeded (monthly limit)" is an account that
    # cannot pay, not a rate window that will reopen in a minute.
    if status in (403, 429) and _ACCOUNT.search(msg or ""):
        return "provider"
    if status == 429 or _QUOTA.search(msg or ""):
        return "quota"
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
    has_field = "reasoning_content" in msg or "reasoning" in msg
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
        billed_usd=_f(billed), body=payload, reasoning_field=has_field)


def _body(provider: str, model: str, req: dict, max_tokens: int | None,
          window: str | None = None) -> dict:
    body = {k: v for k, v in req.items() if k not in ("model", "max_tokens",
                                                      "max_completion_tokens")}
    body["model"] = model
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if window and PROVIDERS.get(provider, {}).get("windows"):
        # The lane travels with the call: priced at this window, so scheduled in it.
        body["metadata"] = {**(body.get("metadata") or {}), "completion_window": window}
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


def _error(r: httpx.Response, key: str) -> tuple[str, str]:
    """(message for display, raw body head for classification) -- both scrubbed."""
    raw = ""
    try:
        raw = r.text[:600]
    except Exception:
        pass
    try:
        d = r.json()
        err = d.get("error") or d.get("detail") or {}
        msg = str(err.get("message", err) if isinstance(err, dict) else err)
    except Exception:
        msg = raw.strip().splitlines()[0] if raw.strip() else ""
    if key:
        msg, raw = msg.replace(key, "***"), raw.replace(key, "***")
    return msg[:160], raw


def _describe(status: int, msg: str, scope: str) -> str:
    if scope == "edge":
        return "HTTP %d %s -- CDN bot check, not a credential failure" % (status, msg)
    return "HTTP %d %s" % (status, msg)


def call(provider: str, model: str, key: str, req: dict, *, max_tokens: int | None,
         timeout: float = 180, window: str | None = None) -> Result:
    t0 = time.time()

    def fail(status: int, detail: str, scope: str = "model", rl=None) -> Result:
        return Result(False, status, provider, model, latency_s=time.time() - t0,
                      detail=detail, scope=scope, ratelimit=rl or {})

    spec = PROVIDERS.get(provider)
    if not spec:
        return fail(0, "unknown provider", "provider")
    if not key:
        return fail(0, "no key", "provider")
    body = _body(provider, model, req, max_tokens, window)
    body.pop("stream", None)
    body.pop("stream_options", None)
    try:
        r = httpx.post(spec["base"] + "/chat/completions", json=body, timeout=timeout,
                       headers=_headers(key))
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # The provider could not be reached at all: UNAVAILABLE, not a bad key.
        return fail(0, "unreachable (%s)" % type(exc).__name__, "transport")
    except httpx.TimeoutException:
        return fail(0, "timeout after %.0fs" % (time.time() - t0))
    except Exception as exc:                                    # noqa: BLE001
        return fail(0, type(exc).__name__)          # never str(exc): it can echo headers
    rl = _ratelimit(r.headers)
    if r.status_code != 200:
        msg, raw = _error(r, key)
        scope = classify_failure(r.status_code, msg, raw, r.headers)
        return fail(r.status_code, _describe(r.status_code, msg, scope), scope, rl)
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
           on_done: Callable[[Result], None], timeout: float = 180,
           window: str | None = None) -> Iterator[bytes]:
    """Forward an SSE stream verbatim while tallying what it carried.

    Usage arrives in the last chunk when requested; content and reasoning deltas are
    counted separately so an all-reasoning stream is recorded as empty.
    """
    t0 = time.time()
    body = _body(provider, model, req, max_tokens, window)
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
                msg, raw = _error(r, key)
                res.scope = classify_failure(r.status_code, msg, raw, r.headers)
                res.detail = _describe(r.status_code, msg, res.scope)
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
                    if "reasoning_content" in d or "reasoning" in d:
                        res.reasoning_field = True
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
