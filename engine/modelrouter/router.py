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
from dataclasses import dataclass, replace
from typing import Callable, Iterator

from . import providers as P
from .config import Config
from .decision import Ask, Choice, Outcome, decide, is_free
from .gather import Gatherer, call_cost
from .secrets import Keyring
from .store import Store

log = logging.getLogger("modelrouter")
MAX_ATTEMPTS = 2
PROBE_PROMPT = [{"role": "user", "content": "Reply with exactly one word: Ready"}]
PROBE_LADDER = (32, 256, 1024, 2048)


def estimate_tokens(req: dict) -> int:
    """Rough prompt size: ~4 characters per token over messages and tool schemas."""
    n = len(json.dumps(req.get("messages") or [], ensure_ascii=False))
    if req.get("tools"):
        n += len(json.dumps(req["tools"], ensure_ascii=False))
    return max(1, n // 4)


def ask_from(req: dict, ceiling: float | None, allow_free: bool = False) -> Ask:
    model = str(req.get("model") or "auto")
    mt = req.get("max_completion_tokens", req.get("max_tokens"))
    return Ask(prompt_tokens=estimate_tokens(req),
               max_tokens=int(mt) if mt is not None else None,
               needs_tools=bool(req.get("tools")),
               ceiling_usd_per_mtok=ceiling,
               only_seat=None if model == "auto" or model.startswith("auto:") else model,
               allow_free=allow_free)


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

    # --- helpers -------------------------------------------------------------------------
    def _observe(self, seat: str, source: str, max_tokens: int | None, res: P.Result) -> tuple:
        usd, basis = call_cost(self.gather.listing(seat), res)
        if res.status == 200:
            self.store.observe(seat, source, max_tokens=max_tokens, status=res.status,
                               content_chars=len(res.content.strip()),
                               reasoning_chars=len(res.reasoning.strip()),
                               tool_calls=res.tool_calls, prompt_tokens=res.prompt_tokens,
                               completion_tokens=res.completion_tokens,
                               cached_tokens=res.cached_tokens, cost_usd=usd, cost_basis=basis,
                               latency_s=res.latency_s, detail=res.detail)
        self.gather.note_ratelimit(res.provider, res.ratelimit)
        if not res.ok and res.status != 200:
            self.gather.penalise(res.provider, res.model, res.scope, res.detail, res.ratelimit)
        return usd, basis

    @staticmethod
    def _attempt(seat: str, max_tokens: int | None, res: P.Result, usd, basis) -> dict:
        return {"seat": seat, "max_tokens": max_tokens, "http": res.status, "ok": res.ok,
                "detail": res.detail, "finish_reason": res.finish_reason,
                "content_chars": len(res.content.strip()),
                "reasoning_chars": len(res.reasoning.strip()), "tool_calls": res.tool_calls,
                "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens,
                "cached_tokens": res.cached_tokens, "cost_usd": usd, "cost_basis": basis,
                "latency_s": round(res.latency_s, 3), "scope": None if res.ok else res.scope}

    def providers_public(self) -> list[dict]:
        return [s.public() for s in self.gather.states.values()]

    # --- DECIDE only -------------------------------------------------------------------
    def explain(self, req: dict, ceiling: float | None = None) -> dict:
        ask = ask_from(req, ceiling if ceiling is not None else self.cfg.ceiling_usd_per_mtok,
                     self.cfg.allow_free)
        roster = self.gather.roster()
        ch = decide(ask, roster)
        return {"choice": ch.public(), "providers": self.providers_public(),
                "dashboard": self.gather.dashboard_status}

    # --- GATHER -> DECIDE -> ACT -------------------------------------------------------
    def route(self, req: dict, *, client: str = "", ceiling: float | None = None) -> Routed:
        t0 = time.time()
        ask = ask_from(req, ceiling if ceiling is not None else self.cfg.ceiling_usd_per_mtok,
                     self.cfg.allow_free)
        attempts: list[dict] = []
        decisions: list[dict] = []
        res: P.Result | None = None
        choice: Choice | None = None
        for _ in range(MAX_ATTEMPTS):
            choice = decide(ask, self.gather.roster())
            decisions.append(choice.public())
            if choice.abstained:
                break
            provider, _, model = choice.seat.partition(":")
            res = P.call(provider, model, self.keys.get(provider), req,
                         max_tokens=choice.max_tokens)
            usd, basis = self._observe(choice.seat, "traffic", choice.max_tokens, res)
            attempts.append(self._attempt(choice.seat, choice.max_tokens, res, usd, basis))
            if res.ok:
                break
            if ask.only_seat:
                break                  # the client named this model: no substitute
            ask = replace(ask, failed=ask.failed + ((choice.seat, res.detail),))
        assert choice is not None
        answered = bool(res and res.ok)
        status = "ANSWERED" if answered else ("ABSTAINED" if choice.abstained and not attempts
                                              else "FAILED")
        total = [a["cost_usd"] for a in attempts]
        doc = {
            "requested": str(req.get("model") or "auto"), "client": client,
            "ask": {"prompt_tokens": ask.prompt_tokens, "max_tokens": ask.max_tokens,
                    "needs_tools": ask.needs_tools, "ceiling_usd_per_mtok": ask.ceiling_usd_per_mtok,
                    "stream": False},
            "choice": decisions[-1], "earlier_decisions": decisions[:-1], "attempts": attempts,
            "providers": self.providers_public(), "dashboard": self.gather.dashboard_status,
            "status": status, "elapsed_s": round(time.time() - t0, 3),
            "result": {"cost_usd": sum(x for x in total if x is not None) if total else None,
                       "cost_basis": "/".join(sorted({a["cost_basis"] for a in attempts})) or None,
                       "calls": len(attempts),
                       "calls_unknown_cost": sum(1 for x in total if x is None)},
        }
        did = self.store.record(doc)
        return Routed(did, choice, res, status, doc)

    def route_stream(self, req: dict, *, client: str = "", ceiling: float | None = None
                     ) -> tuple[Routed, Iterator[bytes] | None]:
        """Decide now; stream the chosen model. A stream cannot be retried once bytes
        have gone to the client, so it is one attempt, recorded when it ends."""
        t0 = time.time()
        ask = ask_from(req, ceiling if ceiling is not None else self.cfg.ceiling_usd_per_mtok,
                     self.cfg.allow_free)
        choice = decide(ask, self.gather.roster())
        doc = {"requested": str(req.get("model") or "auto"), "client": client,
               "ask": {"prompt_tokens": ask.prompt_tokens, "max_tokens": ask.max_tokens,
                       "needs_tools": ask.needs_tools,
                       "ceiling_usd_per_mtok": ask.ceiling_usd_per_mtok, "stream": True},
               "choice": choice.public(), "earlier_decisions": [], "attempts": [],
               "providers": self.providers_public(), "dashboard": self.gather.dashboard_status,
               "status": "ABSTAINED" if choice.abstained else "STREAMING",
               "result": {"cost_usd": None, "cost_basis": None, "calls": 0,
                          "calls_unknown_cost": 0}}
        did = self.store.record(doc)
        routed = Routed(did, choice, None, doc["status"], doc)
        if choice.abstained:
            return routed, None
        provider, _, model = choice.seat.partition(":")

        def done(res: P.Result) -> None:
            usd, basis = self._observe(choice.seat, "traffic", choice.max_tokens, res)
            doc["attempts"] = [self._attempt(choice.seat, choice.max_tokens, res, usd, basis)]
            doc["status"] = "ANSWERED" if res.ok else "FAILED"
            doc["elapsed_s"] = round(time.time() - t0, 3)
            doc["result"] = {"cost_usd": usd, "cost_basis": basis, "calls": 1,
                             "calls_unknown_cost": 1 if usd is None else 0}
            doc["id"] = did
            self.store.record(doc)

        return routed, P.stream(provider, model, self.keys.get(provider), req,
                                max_tokens=choice.max_tokens, on_done=done)

    # --- probing: buying facts -----------------------------------------------------------
    def probe(self, seats: list[str] | None = None, cheapest: int = 0,
              budget_usd: float | None = None,
              progress: Callable[[dict], None] | None = None) -> dict:
        """Learn how models behave by calling them with a one-word task at rising
        budgets until content appears. Spends real money, capped by budget_usd.

        With `cheapest=N`, probes the N cheapest-by-list-price never-measured models
        per provider -- the ones most likely to win a route, and so the ones whose
        behaviour matters most.
        """
        budget = self.cfg.probe_budget_usd if budget_usd is None else budget_usd
        roster = {c.seat: c for c in self.gather.roster()}
        targets: list[str] = list(seats or [])
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
        spent, report = 0.0, []
        for seat in targets:
            c = roster.get(seat)
            if not c:
                report.append({"seat": seat, "skipped": "not in the live roster"})
                continue
            if c.provider_state != "OK":
                report.append({"seat": seat, "skipped": "provider " + c.provider_state})
                continue
            rungs = []
            for mt in PROBE_LADDER:
                if spent >= budget:
                    rungs.append({"max_tokens": mt, "skipped": "probe budget spent"})
                    break
                res = P.call(c.provider, c.model, self.keys.get(c.provider),
                             {"messages": PROBE_PROMPT, "temperature": 0}, max_tokens=mt,
                             timeout=90)
                usd, basis = self._observe(seat, "probe", mt, res)
                spent += usd or 0.0
                rung = self._attempt(seat, mt, res, usd, basis)
                rungs.append(rung)
                if progress:
                    progress(rung)
                if res.ok or res.status != 200:
                    break          # content found, or the call itself was refused
            report.append({"seat": seat, "rungs": rungs})
        profiles = self.store.profiles()
        for r in report:
            p = profiles.get(r["seat"])
            if p:
                r["profile"] = p.public()
        return {"spent_usd": round(spent, 8), "budget_usd": budget, "probed": report}
