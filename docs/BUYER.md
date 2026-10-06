# Calling the router

One model-router API with Chat Completions and a text/function-tool Responses path. You send
a request; the router picks a model, calls it, and tells you what it picked, why, and what
you were charged.

**Base URL:** `https://chloe.blumenkraft.cloud/modelrouter/v1`
**Key:** sent to you separately. It starts with `mr_`. Treat it like a password.
**Budget:** your key has a hard spend cap (in USD). A call that could take you over it is
refused before anything is spent.

## First call

Python (`pip install openai`):
```python
from openai import OpenAI

client = OpenAI(
    base_url="https://chloe.blumenkraft.cloud/modelrouter/v1",
    api_key="mr_...",
    default_headers={"X-Router-Lane": "background"},
)
r = client.chat.completions.create(
    model="auto",
    max_tokens=300,
    messages=[{"role": "user", "content": "Explain tides in two sentences."}],
)
print(r.choices[0].message.content)
print(r.model_extra["router"])      # which model answered, why, what you were charged
```

curl:
```bash
curl https://chloe.blumenkraft.cloud/modelrouter/v1/chat/completions \
  -H "Authorization: Bearer mr_..." -H "Content-Type: application/json" \
  -H "X-Router-Lane: background" \
  -d '{"model":"auto","max_tokens":300,"messages":[{"role":"user","content":"Explain tides in two sentences."}]}'
```

The API exposes these routes only; it does not promise that every OpenAI API feature or
client works. Automated tests use FastAPI's test client. The repository has no direct tests
for the official OpenAI Python or Node SDKs, or Codex CLI. A September 30, 2026 Codex CLI
0.159 test used a local adapter before native Responses support was added.

For a Responses API request, use `model="auto"` and set `X-Router-Lane: background` for a
long coding task. Text input and function tools are supported; include the full conversation
history on each call. Hosted tools, multimodal input, `previous_response_id`, and background
polling are not supported.

## Choosing how it routes

| you send | effect |
|---|---|
| `model: "auto"` | someone is waiting: fastest-enough, cheapest answer |
| `model: "auto:batch"` (or `auto:background`) | nobody is waiting: cheaper, slower capacity |
| `model: "provider:model"` | that model only; copy a current id from `GET /v1/models` |
| header `X-Router-Answer-Tokens: 5` | "the answer is about this long" — sharpens the cost choice for short answers |
| header `X-Router-Session: <any id>` | keeps a conversation on one provider while its prompt cache is warm (cheaper) |

`max_tokens` is always applied: if you leave it out, your key's default (1024) is used.
Some models reason before answering; the router won't hand them a budget too small to
answer in, and if it has to raise it, it says so.

## What you get back

Both endpoints include routing details in a `router` field. Chat Completions returns a chat
completion; Responses returns a Responses-shaped object with `message` and `function_call`
output items. The router fields are:
```
seat               the model that answered, e.g. "provider:current-model-id"
because            why it was chosen over the others
charged_usd        what this call took from your budget
key_remaining_usd  what is left
```
Your balance at any time: `GET /v1/usage` with your key.

## When it says no

| status | meaning | what to do |
|---|---|---|
| 402 `spend_cap_reached` | this call could exceed what's left on your key | lower `max_tokens`, or ask for a higher cap |
| 422 `router_abstained` | no model can answer this honestly (the message says why) | usually: raise `max_tokens` |
| 429 | rate limit (60 requests/min) or providers at capacity | retry after the `Retry-After` seconds |
| 502 `routed_call_failed` | the chosen models failed or came back empty | retry; you are charged only what the attempts cost |
| 401 | key wrong or revoked | check the key |

## What happens to your prompts

They are sent to the provider of the model that answers (io.net, Sail, AkashML, Venice,
Nous or OpenRouter) and are subject to that provider's terms. The router itself keeps no
prompt or answer text: it records token counts, costs and which model answered. Free-tier
models, which may log prompts, are never used.
