# model-router

An OpenAI-compatible proxy that picks the model for each request with a typed decision you
can read — and refuses, loudly, when no model can serve the request honestly.

Point any OpenAI client at `http://<host>:7480/v1`, ask for model `auto`, and the router
chooses a model, forwards the call, and returns the response with a `router` field that says
which model answered, why, and what it cost. Every decision is recorded: which models were
considered, their live and measured prices, which were ruled out and on what fact.

## Why price tables are not enough

Measured on one provider (AkashML), same one-word task:

| model | billed | returned |
|---|---|---|
| gpt-oss-20b | ~$0 | **empty** |
| Llama-3.3-70B-Instruct | $0.00001 | `Ready` |
| Qwen3.8-27B | $0.00004 | **empty** |

Two are reasoning models: their text goes to `reasoning_content`, and a small `max_tokens` is
spent on reasoning before any answer appears — HTTP 200, tokens charged, empty string. A router
that picks the cheapest model from a price table picks the one that returns nothing and reports
success. This router qualifies a model only on **measured behaviour** (where it emits, how many
tokens it needs before content appears) and ranks it on its **own measured price** plus the
reasoning tokens it actually spends.

## The decision: many variables, no model calls

Per request the router compares **expected cost per completed answer** — rate × tokens actually
burned — not advertised $/M. A $2.00/M model that answers in 2 tokens beats a $0.10/M model that
reasons for 200 first; priced at a 250-token cap, the $0.10 one genuinely wins. So answer length is
an input: send `X-Router-Answer-Tokens` when you know it (otherwise the cap is used, and the
decision says so).

The inputs, all read live or measured, none pinned: input / cached-input / output rates per
scheduling window (Decimal, from the providers' own strings); each rate's read time (a price older
than `max_price_age_s` is UNKNOWN); reasoning tokens burned before an answer; the minimum viable
`max_tokens`; whether a reasoning field exists at all; decode speed on a real generation;
latency growth under the provider's current load (from its measured concurrency curve); the
concurrency it survives; context length; lane; and prompt-cache warmth — a conversation's last
seat is priced with its cache-read rate, so staying put wins exactly when it is cheaper
(session = `X-Router-Session` or the OpenAI `user` field).

Catalogues, pricing pages and context pages refresh out of band every 4 minutes; the decision
itself is a lookup plus a comparison — measured 17–24 ms over ~1,000 seats, no network, no model.

## Three stages

```
GATHER   live catalogues and prices, measured behaviour, provider quota/budget state
DECIDE   a Choice over the live roster -- or ABSTAIN
ACT      forward the call; an empty answer is a failure, never a success
```

- **Nothing is pinned.** Candidates come from each provider's live catalogue per request. A
  retired model drops out of the catalogue and so cannot be chosen.
- **Unknown is unknown.** A null price is UNKNOWN, never $0. A model never measured is UNKNOWN,
  never assumed to work.
- **ABSTAIN is the feature.** If nothing qualifies, the client gets HTTP 422 `router_abstained`
  with the reasons — not a silent fallback to something expensive.
- **One retry, by re-deciding.** If the chosen model fails or comes back empty, that fact is
  recorded and the decision is taken again over the updated roster. If that abstains or fails,
  the client gets HTTP 502 with the decision id.
- **`max_tokens` is a correctness parameter, gated per model.** On reasoning-first providers
  (AkashML, io.net) most models spend part of the budget reasoning before any answer: io.net's
  GLM-5.3-Flash spent 36–90 tokens to say one word, DeepSeek-V4.1-Flash 18. The router gates each
  model on its own measured spend (resampled, maximum taken), never on a global threshold, and
  refuses a request whose budget is below it.
- **Lanes: someone waiting vs nobody waiting.** `auto` is interactive and excludes seats whose
  *predicted* latency for the request (short-call latency + tokens ÷ decode speed measured on a
  real generation) is above `interactive_max_latency_s` (default 8 s). `auto:background` /
  `auto:batch` buy Sail's `balanced` / `flex` windows; one gemma-4-12B answer measured $0.0000127
  asap, $0.0000061 balanced, $0.0000035 flex.
- **A small budget is never passed to a reasoning model.** The router prefers a no-reasoning model
  that fits; otherwise it raises the budget (caller's + 2× the most reasoning observed), on the record.
- **Nothing pinned, including prices.** Sail's `/models` lists ids only, so its per-window prices
  and context lengths are read live from its docs pricing and models pages each run; if a page
  cannot be read, those facts are UNKNOWN for that run.
- **Concurrency caps from measurement.** A provider is capped at the largest concurrency it
  completed with zero failures (AkashML dropped 34 of 64; io.net and Sail completed 64). At the cap
  it is `AT_CAPACITY`: `auto` routes elsewhere, a named model gets 429 + `Retry-After`.
  `modelrouter import-measurements --rows … --curve …` loads sweeps taken outside the router.
- **A CDN refusal is not a bad key.** Every call carries a real User-Agent; a Cloudflare
  `403 error code: 1010` marks the provider UNAVAILABLE and says the credential is not suspect.
- **Account-level refusals bench the provider once** (budget, auth: 15 min; rate/quota: 10 min;
  OpenRouter's free-model daily quota only benches free models).

## Install (your keys, your machine)

Requires Python 3.11+. Keys are read at runtime from **GCP Secret Manager** (with your machine's
`gcloud` credentials) or from **environment variables** — never from a file. The config file
holds secret *names* only.

```
pip install "git+https://github.com/ivangegovdve-sudo/model-router.git#subdirectory=engine"
modelrouter init
```

`init` writes `~/.modelrouter/config.toml` (or `$MODELROUTER_CONFIG`). Choose
`source = "gcp"` (set `gcp_project`) or `"env"`, and give each provider you use the name of the
secret or variable holding its key. Supported: `openrouter`, `akashml`, `venice`, `nous`, `sail`, `ionet` (io.net Intelligence),
`groq`. A provider whose catalogue is mostly another's is detected by measurement: identical prices
→ MIRROR (excluded); mostly identical → CORRELATED (routable, not independent for fallback). Nous:
97% of its ids are OpenRouter's, 86% at identical prices — CORRELATED. Cerebras is not supported
(no prices in its API).

```
modelrouter doctor
modelrouter probe --cheapest 3
modelrouter probe --generation      # decode speed: the interactive lane needs it
modelrouter serve
```

`doctor` lists every key by NAME — readable or not, and why — plus provider states, and exits
non-zero naming what is missing (a key it cannot read, `gcloud` missing or not logged in, no router
token). The server refuses to start without a token unless bound to loopback with `no_auth = true`.

Until something is measured, every request **abstains** — by design. `probe` buys the facts: it
asks each model for a one-word answer at rising `max_tokens` (32, 256, 1024, 2048) until content
appears, under a hard budget (`policy.probe_budget_usd`). Real traffic keeps refining the same facts.

### Use it

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:7480/v1", api_key="<router token>")
r = client.chat.completions.create(model="auto", max_tokens=400,
                                   messages=[{"role": "user", "content": "..."}])
print(r.choices[0].message.content, r.model_extra["router"])
```

`model="provider:model"` (e.g. `akashml:meta-llama/Llama-3.3-70B-Instruct`) asks for one model; it
is still judged, and refused if it cannot serve the request (for example a reasoning model given
too small a `max_tokens`). Header `X-Router-Max-Usd-Per-M` lowers the price ceiling for one call.

### See why

- `GET /router/decisions`, `GET /router/decisions/{id}` — every candidate considered, verdict and
  reason, each attempt, and the cost with its basis (`billed` by the provider, or `computed` from
  usage × the model's own rate card).
- `POST /router/explain` — the decision for a request, without making the call.
- The Windows/Android app (`app/`) renders all of it.
- The MCP server (`modelrouter-mcp`, install with `[mcp]`) exposes the same decision as an advisory
  tool. An MCP server cannot switch a client's model — the client chose it before any tool runs —
  so it advises; the proxy routes.

Full contract: [`docs/CONTRACT.md`](docs/CONTRACT.md).

## Policy (`[policy]` in the config)

| key | default | meaning |
|---|---|---|
| `ceiling_usd_per_mtok` | 5.0 | refuse any model priced above this (measured, else list) |
| `interactive_max_latency_s` | 8.0 | the interactive lane (`auto`) excludes seats predicted slower for the request |
| `clamp_max_tokens` | true | raise a too-small budget for a reasoning model instead of refusing |
| `allow_free` | false | free tiers may log prompts and run on their own daily quota |
| `probe_budget_usd` | 0.02 | hard cap per probe run |
| `dashboard_url` | open-dashboard | price source for providers it carries; else each provider's own catalogue |

## Develop

```
cd engine && pip install -e ".[test,mcp]" && pytest
```
