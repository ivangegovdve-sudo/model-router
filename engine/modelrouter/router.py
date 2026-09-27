"""GATHER -> DECIDE -> ACT, and the record that makes each call legible.

A routed call is at most two attempts. When the first comes back empty or refused,
the refusal is written into the facts (an observation, or a provider penalty) and the
decision is taken AGAIN over the updated roster -- not a pinned fallback list. If the
second decision abstains, or its call also fails, the client gets a loud error with
the decision attached. An empty 200 is never passed off as an answer.
"""
from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from dataclasses import dataclass, replace
from typing import Callable, Iterator

from . import providers as P
from .config import Config
from .decision import Ask, Choice, Outcome, decide, is_free
from .gather import Gatherer, call_cost, card_cost
from .secrets import Keyring
from .store import Store

log = logging.getLogger("modelrouter")
MAX_ATTEMPTS = 2
#: How long a provider keeps a prompt prefix cached is its own business; five minutes is the
#: common floor. Past it the router assumes the cache is cold.
WARM_TTL_S = 300
PROBE_PROMPT = [{"role": "user", "content": "Reply with exactly one word: Ready"}]
#: A generation long enough to measure decode, not connection + prefill (~400 tokens).
GEN_PROMPT = [{"role": "user", "content": "Write about 300 words on the history of the "
               "printing press. Plain prose, no headings."}]
GEN_MAX_TOKENS = 1024                 # the measured universal safe ceiling
PROBE_LADDER = (32, 256, 1024, 2048)
PROBE_CONFIRMATIONS = 2    # extra samples at the budget where a reasoning model answered


def estimate_tokens(req: dict) -> int:
    """Rough prompt size: ~4 characters per token over messages and tool schemas."""
    n = len(json.dumps(req.get("messages") or [], ensure_ascii=False))
    if req.get("tools"):
        n += len(json.dumps(req["tools"], ensure_ascii=False))
    return max(1, n // 4)


LANES = ("interactive", "background", "batch")


def lane_of(req: dict, header: str | None = None) -> str:
    """`auto` is interactive; `auto:background` / `auto:batch` (or X-Router-Lane) are not.
    An unknown lane is an error, not a silent default."""
    model = str(req.get("model") or "auto")
    lane = (header or (model.split(":", 1)[1] if model.startswith("auto:") else "")
            or "interactive").strip().lower()
    if lane not in LANES:
        raise ValueError("unknown lane %r -- use one of %s" % (lane, ", ".join(LANES)))
    return lane


def ask_from(req: dict, ceiling: float | None, allow_free: bool = False, *,
             lane: str = "interactive", interactive_max_latency_s: float | None = None,
             allow_clamp: bool = False) -> Ask:
    model = str(req.get("model") or "auto")
    mt = req.get("max_completion_tokens", req.get("max_tokens"))
    return Ask(prompt_tokens=estimate_tokens(req),
               max_tokens=int(mt) if mt is not None else None,
               needs_tools=bool(req.get("tools")),
               ceiling_usd_per_mtok=ceiling,
               only_seat=None if model == "auto" or model.startswith("auto:") else model,
               allow_free=allow_free, lane=lane,
               interactive_max_latency_s=interactive_max_latency_s,
               allow_clamp=allow_clamp)


@dataclass
class Routed:
    decision_id: str
    choice: Choice
    result: P.Result | None
    status: str          # ANSWERED | ABSTAINED | FAILED
    doc: dict


class Router:
    def __init__(self, cfg: Config, keys: Keyring, store: Store):
        self.cfg, self.keys, self.store = cfg, keys, store
        self.gather = Gatherer(cfg, keys, store)
        self._sessions: dict[str, tuple[str, int, float]] = {}   # session -> (seat, tokens, t)

    def _warm(self, session: str | None) -> tuple[tuple[str, int], ...]:
        if not session:
            return ()
        hit = self._sessions.get(session)
        if not hit or time.time() - hit[2] > WARM_TTL_S:
            return ()
        return ((hit[0], hit[1]),)

    def _remember(self, session: str | None, seat: str, prompt_tokens: int) -> None:
        if session:
            self._sessions[session] = (seat, prompt_tokens, time.time())
            if len(self._sessions) > 10000:          # bound memory: drop the oldest half
                for k, _ in sorted(self._sessions.items(), key=lambda kv: kv[1][2])[:5000]:
                    self._sessions.pop(k, None)

    # --- helpers -------------------------------------------------------------------------
    def _ask(self, req: dict, ceiling: float | None, lane: str, session: str | None = None,
             answer_tokens: int | None = None) -> Ask:
        a = ask_from(req, ceiling if ceiling is not None else self.cfg.ceiling_usd_per_mtok,
                     self.cfg.allow_free, lane=lane,
                     interactive_max_latency_s=self.cfg.interactive_max_latency_s,
                     allow_clamp=self.cfg.clamp_max_tokens)
        return replace(a, warm=self._warm(session), max_price_age_s=self.cfg.max_price_age_s,
                       answer_tokens=answer_tokens)

    @staticmethod
    def _window(roster: list, seat: str | None) -> str | None:
        for c in roster:
            if c.seat == seat:
                return c.window
        return None

    def _observe(self, seat: str, source: str, max_tokens: int | None, res: P.Result,
                 window: str | None = None) -> tuple:
        li = self.gather.listing(seat, window)
        usd, basis = call_cost(li, res)
        card = card_cost(li, res)
        if window not in (None, "asap"):
            # A call scheduled in a slow window says nothing about interactive latency.
            res.latency_s = 0.0
        if res.status == 200:
            self.store.observe(seat, source, max_tokens=max_tokens, status=res.status,
                               content_chars=len(res.content.strip()),
                               reasoning_chars=len(res.reasoning.strip()),
                               tool_calls=res.tool_calls, prompt_tokens=res.prompt_tokens,
                               completion_tokens=res.completion_tokens,
                               cached_tokens=res.cached_tokens, cost_usd=usd, cost_basis=basis,
                               latency_s=res.latency_s, detail=res.detail,
                               reasoning_field=res.reasoning_field, card_usd=card)
        self.gather.note_ratelimit(res.provider, res.ratelimit)
        if not res.ok and res.status != 200:
            self.gather.penalise(res.provider, res.model, res.scope, res.detail, res.ratelimit)
        return usd, basis

    @staticmethod
    def _attempt(seat: str, max_tokens: int | None, res: P.Result, usd, basis,
                 window: str | None = None) -> dict:
        return {"seat": seat, "window": window, "max_tokens": max_tokens, "http": res.status,
                "ok": res.ok,
                "detail": res.detail, "finish_reason": res.finish_reason,
                "content_chars": len(res.content.strip()),
                "reasoning_chars": len(res.reasoning.strip()), "tool_calls": res.tool_calls,
                "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens,
                "cached_tokens": res.cached_tokens,
                # float for display; the exact Decimal string is what sums are made from
                "cost_usd": None if usd is None else float(usd),
                "cost_usd_exact": None if usd is None else str(usd), "cost_basis": basis,
                "latency_s": round(res.latency_s, 3), "scope": None if res.ok else res.scope}

    def providers_public(self) -> list[dict]:
        return [s.public() for s in self.gather.states.values()]

    # --- DECIDE only -------------------------------------------------------------------
    def explain(self, req: dict, ceiling: float | None = None, lane: str = "interactive",
                session: str | None = None,
                answer_tokens: int | None = None) -> dict:
        t0 = time.perf_counter()
        ask = self._ask(req, ceiling, lane, session, answer_tokens)
        roster = self.gather.roster(lane)
        ch = decide(ask, roster)
        return {"choice": ch.public(), "providers": self.providers_public(),
                "dashboard": self.gather.dashboard_status,
                "decide_ms": round((time.perf_counter() - t0) * 1000, 2)}

    # --- GATHER -> DECIDE -> ACT -------------------------------------------------------
    def route(self, req: dict, *, client: str = "", ceiling: float | None = None,
              lane: str = "interactive", session: str | None = None,
              answer_tokens: int | None = None) -> Routed:
        t0 = time.time()
        ask = self._ask(req, ceiling, lane, session, answer_tokens)
        decide_ms: list[float] = []
        attempts: list[dict] = []
        decisions: list[dict] = []
        res: P.Result | None = None
        choice: Choice | None = None
        for _ in range(MAX_ATTEMPTS):
            d0 = time.perf_counter()
            roster = self.gather.roster(lane)
            choice = decide(ask, roster)
            decide_ms.append(round((time.perf_counter() - d0) * 1000, 2))
            decisions.append(choice.public())
            if choice.abstained:
                break
            provider, _, model = choice.seat.partition(":")
            window = self._window(roster, choice.seat)
            if not self.gather.try_acquire(provider):
                # Lost the race for the provider's last slot: rule the seat out and decide
                # again rather than exceed the concurrency it was measured to survive.
                why = "provider AT_CAPACITY: slot taken between decision and call"
                ask = replace(ask, failed=ask.failed + ((choice.seat, why),))
                choice = replace(choice, outcome=Outcome.ABSTAIN, seat=None,
                                 because="%s -- %s" % (choice.seat, why))
                decisions[-1] = choice.public()     # if no attempt follows, this stands
                continue
            try:
                res = P.call(provider, model, self.keys.get(provider), req,
                             max_tokens=choice.max_tokens, window=window)
            finally:
                self.gather.release(provider)
            usd, basis = self._observe(choice.seat, "traffic", choice.max_tokens, res, window)
            attempts.append(self._attempt(choice.seat, choice.max_tokens, res, usd, basis, window))
            if res.ok:
                break
            # Empty because the budget ran out while it reasoned: the observation just
            # recorded raises this model's floor, so deciding again may raise the budget
            # for the SAME model. Any other failure rules the seat out for this request.
            ran_out = res.status == 200 and res.finish_reason == "length" and res.reasoning.strip()
            if ask.only_seat and not ran_out:
                break                  # the client named this model: no substitute
            if not ran_out:
                ask = replace(ask, failed=ask.failed + ((choice.seat, res.detail),))
            else:
                ask = replace(ask, escalate=ask.escalate + (choice.seat,))
            # An UPSTREAM fault (unreachable, CDN, timeout, 5xx) -- not an account refusal,
            # which is per-account (OpenRouter's budget 403 never stopped Nous) -- takes out
            # every provider that shares the upstream. Retrying on a correlated provider is
            # the same bet twice.
            # A plain 500 is usually the one model; gateway codes are the upstream.
            if res.scope in ("transport", "edge") or res.status in (502, 503, 504) or                     (res.status == 0 and "timeout" in res.detail):
                ask = replace(ask, upstream_down=ask.upstream_down + ((provider, res.detail),))
        assert choice is not None
        answered = bool(res and res.ok)
        status = "ANSWERED" if answered else ("ABSTAINED" if choice.abstained and not attempts
                                              else "FAILED")
        total = [None if a["cost_usd_exact"] is None else Decimal(a["cost_usd_exact"])
                 for a in attempts]
        spent = sum((x for x in total if x is not None), Decimal(0)) if total else None
        doc = {
            "requested": str(req.get("model") or "auto"), "client": client,
            "ask": {"prompt_tokens": ask.prompt_tokens, "max_tokens": ask.max_tokens,
                    "needs_tools": ask.needs_tools, "ceiling_usd_per_mtok": ask.ceiling_usd_per_mtok,
                    "stream": False, "lane": lane},
            "choice": decisions[-1], "earlier_decisions": decisions[:-1], "attempts": attempts,
            "providers": self.providers_public(), "dashboard": self.gather.dashboard_status,
            "status": status, "elapsed_s": round(time.time() - t0, 3),
            "decide_ms": decide_ms, "session_warm": [s for s, _ in ask.warm],
            "result": {"cost_usd": None if spent is None else float(spent),
                       "cost_usd_exact": None if spent is None else str(spent),
                       "cost_basis": "/".join(sorted({a["cost_basis"] for a in attempts})) or None,
                       "calls": len(attempts),
                       "calls_unknown_cost": sum(1 for x in total if x is None)},
        }
        did = self.store.record(doc)
        if answered and choice.seat:
            self._remember(session, choice.seat, ask.prompt_tokens)
        return Routed(did, choice, res, status, doc)

    def route_stream(self, req: dict, *, client: str = "", ceiling: float | None = None,
                     lane: str = "interactive", session: str | None = None,
                     answer_tokens: int | None = None) -> tuple[Routed, Iterator[bytes] | None]:
        """Decide now; stream the chosen model. A stream cannot be retried once bytes
        have gone to the client, so it is one attempt, recorded when it ends."""
        t0 = time.time()
        ask = self._ask(req, ceiling, lane, session, answer_tokens)
        d0 = time.perf_counter()
        roster = self.gather.roster(lane)
        choice = decide(ask, roster)
        decide_ms = round((time.perf_counter() - d0) * 1000, 2)
        window = self._window(roster, choice.seat)
        doc = {"requested": str(req.get("model") or "auto"), "client": client,
               "ask": {"prompt_tokens": ask.prompt_tokens, "max_tokens": ask.max_tokens,
                       "needs_tools": ask.needs_tools,
                       "ceiling_usd_per_mtok": ask.ceiling_usd_per_mtok, "stream": True,
                       "lane": lane},
               "choice": choice.public(), "earlier_decisions": [], "attempts": [],
               "providers": self.providers_public(), "dashboard": self.gather.dashboard_status,
               "status": "ABSTAINED" if choice.abstained else "STREAMING",
               "decide_ms": [decide_ms],
               "result": {"cost_usd": None, "cost_basis": None, "calls": 0,
                          "calls_unknown_cost": 0}}
        did = self.store.record(doc)
        routed = Routed(did, choice, None, doc["status"], doc)
        if choice.abstained:
            return routed, None
        provider, _, model = choice.seat.partition(":")

        if not self.gather.try_acquire(provider):
            doc["status"] = "ABSTAINED"
            doc["choice"] = replace(choice, outcome=Outcome.ABSTAIN, seat=None,
                                    because="provider AT_CAPACITY: slot taken between decision "
                                    "and call").public()
            self.store.record({**doc, "id": did})
            return Routed(did, replace(choice, outcome=Outcome.ABSTAIN, because=doc["choice"]
                                       ["because"]), None, "ABSTAINED", doc), None

        def done(res: P.Result) -> None:
            self.gather.release(provider)
            usd, basis = self._observe(choice.seat, "traffic", choice.max_tokens, res, window)
            doc["attempts"] = [self._attempt(choice.seat, choice.max_tokens, res, usd, basis,
                                             window)]
            doc["status"] = "ANSWERED" if res.ok else "FAILED"
            if res.ok:
                self._remember(session, choice.seat, ask.prompt_tokens)
            doc["elapsed_s"] = round(time.time() - t0, 3)
            doc["result"] = {"cost_usd": None if usd is None else float(usd),
                             "cost_usd_exact": None if usd is None else str(usd),
                             "cost_basis": basis, "calls": 1,
                             "calls_unknown_cost": 1 if usd is None else 0}
            doc["id"] = did
            self.store.record(doc)

        return routed, P.stream(provider, model, self.keys.get(provider), req,
                                max_tokens=choice.max_tokens, on_done=done, window=window)

    # --- probing: buying facts -----------------------------------------------------------
    def probe(self, seats: list[str] | None = None, cheapest: int = 0,
              budget_usd: float | None = None,
              progress: Callable[[dict], None] | None = None,
              refresh_older_than_s: float | None = None) -> dict:
        """Learn how models behave by calling them with a one-word task at rising
        budgets until content appears. Spends real money, capped by budget_usd.

        With `cheapest=N`, probes the N cheapest-by-list-price never-measured models
        per provider -- the ones most likely to win a route, and so the ones whose
        behaviour matters most. With `refresh_older_than_s`, re-probes measured seats
        whose newest observation is older than that, before their profile expires and
        routes start abstaining on them.
        """
        budget = Decimal(str(self.cfg.probe_budget_usd if budget_usd is None else budget_usd))
        roster = {c.seat: c for c in self.gather.roster()}
        targets: list[str] = list(seats or [])
        if refresh_older_than_s is not None:
            stale = [c for c in roster.values() if c.emits is not None
                     and (c.probe_age_s or 0) > refresh_older_than_s]
            targets += [c.seat for c in sorted(stale, key=lambda c: c.seat)
                        if c.seat not in targets]
        if cheapest:
            by_p: dict[str, list] = {}
            for c in roster.values():
                if c.provider_state == "OK" and c.emits is None and c.available is not False \
                        and (self.cfg.allow_free or not is_free(c)) \
                        and c.list_prompt is not None and c.list_completion is not None:
                    by_p.setdefault(c.provider, []).append(c)
            for cs in by_p.values():
                cs.sort(key=lambda c: (2 * c.list_prompt + c.list_completion, c.seat))
                targets += [c.seat for c in cs[:cheapest] if c.seat not in targets]
        spent, report = Decimal(0), []
        for seat in targets:
            c = roster.get(seat)
            if not c:
                report.append({"seat": seat, "skipped": "not in the live roster"})
                continue
            if c.provider_state != "OK":
                report.append({"seat": seat, "skipped": "provider " + c.provider_state})
                continue
            rungs = []
            queue = list(PROBE_LADDER)
            confirming = False
            while queue:
                mt = queue.pop(0)
                if spent >= budget:
                    rungs.append({"max_tokens": mt, "skipped": "probe budget spent"})
                    break
                res = P.call(c.provider, c.model, self.keys.get(c.provider),
                             {"messages": PROBE_PROMPT, "temperature": 0}, max_tokens=mt,
                             timeout=90)
                usd, basis = self._observe(seat, "probe", mt, res)
                if usd is None:
                    # Unknown cost is charged against the budget at the worst price the
                    # policy allows, so a provider that publishes no prices cannot
                    # probe for free.
                    toks = (res.prompt_tokens or 0) + (res.completion_tokens or mt)
                    usd = toks * Decimal(str(self.cfg.ceiling_usd_per_mtok or 5.0)) / Decimal(1_000_000)
                spent += usd
                rung = self._attempt(seat, mt, res, usd, basis)
                rungs.append(rung)
                if progress:
                    progress(rung)
                if res.status != 200:
                    break          # the call itself was refused
                if res.ok and not confirming:
                    if res.reasoning.strip():
                        # One sample understates a reasoning model's floor: the same
                        # model and prompt spent 35 and 90 tokens on io.net an hour
                        # apart. Resample at this budget; the floor takes the maximum.
                        queue = [mt] * PROBE_CONFIRMATIONS
                        confirming = True
                    else:
                        break
                elif confirming and not queue:
                    break
            report.append({"seat": seat, "rungs": rungs})
        profiles = self.store.profiles()
        for r in report:
            p = profiles.get(r["seat"])
            if p:
                r["profile"] = p.public()
        return {"spent_usd": float(spent), "budget_usd": float(budget), "probed": report}

    def probe_generation(self, seats: list[str] | None = None, measured: bool = False,
                         budget_usd: float | None = None,
                         progress: Callable[[dict], None] | None = None) -> dict:
        """Measure decode speed on a real ~400-token generation -- the number the
        interactive lane gates on. With `measured`, every seat that has behaviour facts
        but no decode speed yet. Spends real money, capped by budget_usd."""
        budget = Decimal(str(self.cfg.probe_budget_usd if budget_usd is None else budget_usd))
        roster = {c.seat: c for c in self.gather.roster()}
        targets = list(seats or [])
        if measured:
            targets += sorted(c.seat for c in roster.values()
                              if c.emits is not None and c.decode_tps is None
                              and c.provider_state == "OK" and not c.blocked
                              and (self.cfg.allow_free or not is_free(c))
                              and c.seat not in targets)
        spent, report = Decimal(0), []
        for seat in targets:
            c = roster.get(seat)
            if not c or c.provider_state != "OK":
                report.append({"seat": seat, "skipped": "not routable now"})
                continue
            if spent >= budget:
                report.append({"seat": seat, "skipped": "probe budget spent"})
                continue
            self.gather.acquire(c.provider)
            try:
                res = P.call(c.provider, c.model, self.keys.get(c.provider),
                             {"messages": GEN_PROMPT, "temperature": 0},
                             max_tokens=GEN_MAX_TOKENS, timeout=180)
            finally:
                self.gather.release(c.provider)
            usd, basis = self._observe(seat, "gen", GEN_MAX_TOKENS, res)
            if usd is None:
                usd = ((res.prompt_tokens or 0) + (res.completion_tokens or GEN_MAX_TOKENS)) \
                    * Decimal(str(self.cfg.ceiling_usd_per_mtok or 5.0)) / Decimal(1_000_000)
            spent += usd
            row = self._attempt(seat, GEN_MAX_TOKENS, res, usd, basis)
            row["tok_per_s"] = round(res.completion_tokens / res.latency_s, 1) \
                if res.completion_tokens and res.latency_s else None
            report.append(row)
            if progress:
                progress(row)
        return {"spent_usd": float(spent), "budget_usd": float(budget), "measured": report}

    def import_measurements(self, rows: list[dict] | None = None,
                            curve: list[dict] | None = None, source_note: str = "") -> dict:
        """Import measurements taken outside the router as observations -- the same facts
        a probe would record, so floors, emits and decode speed derive the same way.

        rows   one-word sweeps: {provider, model, budget, latency_s, error, content_len,
               has_reasoning_field, reasoning_len, finish_reason, billed_prompt,
               billed_completion}  -> source "clamp-table"
        curve  concurrency runs: {provider, model, concurrency, succeeded, failed,
               median_latency_s, per_request_tok_per_s | aggregate_tok_per_s}
               -> a decode-speed observation from the n=1 run, and the provider's
               max_ok_concurrency (largest concurrency with zero failures)
        """
        alias = {"akash": "akashml", "io.net": "ionet", "io": "ionet"}
        n_rows = n_skipped = 0
        for r in rows or []:
            provider = alias.get(r["provider"], r["provider"])
            seat = "%s:%s" % (provider, r["model"])
            if r.get("error"):
                n_skipped += 1          # a refusal is not a behaviour fact about the model
                continue
            self.store.observe(
                seat, "clamp-table", max_tokens=r.get("budget"), status=200,
                content_chars=int(r.get("content_len") or 0),
                reasoning_chars=int(r.get("reasoning_len") or 0), tool_calls=0,
                prompt_tokens=r.get("billed_prompt"), completion_tokens=r.get("billed_completion"),
                cached_tokens=None, cost_usd=None, cost_basis="unknown",
                latency_s=float(r.get("latency_s") or 0), detail="imported " + source_note,
                reasoning_field=r.get("has_reasoning_field"))
            n_rows += 1
        caps: dict[str, int] = {}
        n_gen = 0
        for r in curve or []:
            provider = alias.get(r["provider"], r["provider"])
            if int(r.get("failed") or 0) == 0:
                caps[provider] = max(caps.get(provider, 0), int(r["concurrency"]))
            if int(r["concurrency"]) == 1 and r.get("succeeded"):
                tps = r.get("per_request_tok_per_s") or r.get("aggregate_tok_per_s")
                lat = r.get("median_latency_s")
                if tps and lat:
                    self.store.observe(
                        "%s:%s" % (provider, r["model"]), "gen-import", max_tokens=None,
                        status=200, content_chars=1, reasoning_chars=0, tool_calls=0,
                        prompt_tokens=None, completion_tokens=int(round(tps * lat)),
                        cached_tokens=None, cost_usd=None, cost_basis="unknown",
                        latency_s=float(lat), detail="imported concurrency n=1 " + source_note)
                    n_gen += 1
        base: dict[str, float] = {}
        for r in curve or []:
            if int(r["concurrency"]) == 1 and r.get("median_latency_s"):
                base[alias.get(r["provider"], r["provider"])] = float(r["median_latency_s"])
        for r in curve or []:
            provider = alias.get(r["provider"], r["provider"])
            n, lat = int(r["concurrency"]), r.get("median_latency_s")
            if n > 1 and lat and base.get(provider) and int(r.get("failed") or 0) == 0:
                self.store.set_provider_fact(provider, "latency_ratio_n%d" % n,
                                             round(float(lat) / base[provider], 3),
                                             "measured: median %.2fs at n=%d vs %.2fs at n=1 %s"
                                             % (lat, n, base[provider], source_note))
        for provider, cap in caps.items():
            self.store.set_provider_fact(provider, "max_ok_concurrency", cap,
                                         "measured: 0 failures at n=%d %s" % (cap, source_note))
        return {"observations": n_rows, "skipped_errors": n_skipped, "decode_runs": n_gen,
                "concurrency_caps": caps}
