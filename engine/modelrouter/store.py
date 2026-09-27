"""Durable state: every decision, and every observation of a model's behaviour.

Nothing here holds a prompt or a completion. A decision records the facts that
produced it and what the call cost; an observation records what a model DID with a
budget (where its text went, how many tokens, what it cost). A model's profile --
the behavioural facts the decision layer reads -- is derived from observations, so
it is always explainable by pointing at the calls that produced it.
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
import statistics
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .decision import Emits

PROFILE_TTL_S = 7 * 86400
#: A model that returned no content at this budget or above is REASONING_ONLY.
REASONING_ONLY_AT = 2048

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, seat TEXT NOT NULL, t REAL NOT NULL, source TEXT NOT NULL,
  max_tokens INTEGER, status INTEGER, content_chars INTEGER, reasoning_chars INTEGER,
  tool_calls INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, cached_tokens INTEGER,
  cost_usd REAL, cost_basis TEXT, latency_s REAL, detail TEXT);
CREATE INDEX IF NOT EXISTS obs_seat ON observations(seat, t);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, t REAL NOT NULL, requested TEXT, outcome TEXT, seat TEXT,
  because TEXT, cost_usd REAL, cost_basis TEXT, status TEXT, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS dec_t ON decisions(t);
CREATE TABLE IF NOT EXISTS provider_facts (
  provider TEXT NOT NULL, key TEXT NOT NULL, value REAL, source TEXT, t REAL,
  PRIMARY KEY (provider, key));
"""
#: Sources that ran the fixed one-word task, so their reasoning spend defines a floor.
ONE_WORD_SOURCES = ("probe", "clamp-table")
#: A generation this long measures decode speed, not connection + prefill.
DECODE_MIN_TOKENS = 150
#: A call this short measures time-to-answer overhead (connection, prefill, first token).
SHORT_MAX_TOKENS = 64


@dataclass
class Profile:
    seat: str
    observations: int
    emits: Emits | None
    min_max_tokens: int | None
    reasoning_overhead_tokens: int | None
    measured_usd_per_mtok: Decimal | None
    measured_basis: str                  # billed | computed | billed+computed | ""
    latency_s: float | None
    age_s: float | None
    floor_evidence: str = ""             # why min_max_tokens is what it is
    max_reasoning_tokens: int | None = None  # most reasoning seen before an answer, any task
    has_reasoning_field: bool | None = None  # False: the response has no field to strand text in
    billing_ratio: Decimal | None = None     # billed / rate-card cost over the same calls
    billed_prompt: Decimal | None = None     # $/M actually charged, solved from its own bills
    billed_completion: Decimal | None = None
    billed_evidence: str = ""
    decode_tps: float | None = None          # tokens/s on a real generation (>= 150 tokens)

    def public(self) -> dict:
        d = dict(self.__dict__)
        d["emits"] = self.emits.value if self.emits else None
        return d


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(observations)")}
        if "reasoning_field" not in cols:            # migrate stores from before 2026-09-26
            self._db.execute("ALTER TABLE observations ADD COLUMN reasoning_field INTEGER")
            self._db.commit()
        if "card_usd" not in cols:                   # what the rate card said the call cost
            self._db.execute("ALTER TABLE observations ADD COLUMN card_usd TEXT")
            self._db.commit()

    # --- observations ---------------------------------------------------------
    def observe(self, seat: str, source: str, *, max_tokens: int | None, status: int,
                content_chars: int, reasoning_chars: int, tool_calls: int,
                prompt_tokens: int | None, completion_tokens: int | None,
                cached_tokens: int | None, cost_usd: float | None, cost_basis: str,
                latency_s: float, detail: str, reasoning_field: bool | None = None,
                t: float | None = None, card_usd=None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO observations (seat,t,source,max_tokens,status,content_chars,"
                "reasoning_chars,tool_calls,prompt_tokens,completion_tokens,cached_tokens,"
                "cost_usd,cost_basis,latency_s,detail,reasoning_field,card_usd) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (seat, t or time.time(), source, max_tokens, status, content_chars,
                 reasoning_chars, tool_calls, prompt_tokens, completion_tokens, cached_tokens,
                 None if cost_usd is None else str(cost_usd), cost_basis, latency_s, detail[:200],
                 None if reasoning_field is None else int(reasoning_field),
                 None if card_usd is None else str(card_usd)))
            self._db.commit()
            self._profiles = None           # observations changed: re-derive on next read

    # --- provider facts (e.g. the concurrency a provider survives) ---------------------
    def set_provider_fact(self, provider: str, key: str, value: float, source: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO provider_facts VALUES (?,?,?,?,?)",
                             (provider, key, value, source, time.time()))
            self._db.commit()

    def provider_facts(self) -> dict[str, dict[str, dict]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM provider_facts").fetchall()
        out: dict[str, dict[str, dict]] = {}
        for r in rows:
            out.setdefault(r["provider"], {})[r["key"]] = {"value": r["value"],
                                                          "source": r["source"], "t": r["t"]}
        return out

    def observations(self, seat: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM observations WHERE seat=? ORDER BY t DESC "
                                    "LIMIT ?", (seat, limit)).fetchall()
        return [dict(r) for r in rows]

    def profiles(self) -> dict[str, Profile]:
        """Derived per seat, cached until the next observation -- the per-request decision
        path is a lookup, not a table scan."""
        hit = getattr(self, "_profiles", None)
        if hit is not None and time.time() - hit[0] < 60:
            return hit[1]
        out = self._derive_all()
        self._profiles = (time.time(), out)
        return out

    def _derive_all(self) -> dict[str, Profile]:
        cutoff = time.time() - PROFILE_TTL_S
        with self._lock:
            rows = self._db.execute("SELECT * FROM observations WHERE t>=? AND status=200 "
                                    "ORDER BY t", (cutoff,)).fetchall()
        by: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by.setdefault(r["seat"], []).append(r)
        return {seat: derive(seat, obs) for seat, obs in by.items()}

    # --- decisions --------------------------------------------------------------
    def record(self, doc: dict) -> str:
        did = doc.get("id") or time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        doc["id"] = did
        doc.setdefault("t", time.time())
        final = doc.get("result") or {}
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO decisions (id,t,requested,outcome,seat,because,cost_usd,"
                "cost_basis,status,doc) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, doc["t"], doc.get("requested"), doc["choice"]["outcome"],
                 doc["choice"].get("seat"), doc["choice"]["because"], final.get("cost_usd"),
                 final.get("cost_basis"), doc.get("status"), json.dumps(doc)))
            self._db.commit()
        return did

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT id,t,requested,outcome,seat,because,cost_usd,"
                                    "cost_basis,status FROM decisions ORDER BY t DESC LIMIT ?",
                                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get(self, did: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT doc FROM decisions WHERE id=?", (did,)).fetchone()
        return json.loads(r["doc"]) if r else None


def derive(seat: str, obs: list) -> Profile:
    """Behavioural facts from observations. Every field is traceable to rows."""
    # Decode-speed generations ("gen") are a different, long task: an empty answer at 1024
    # there means THAT task needs more (nine reasoning models billed 1024 tokens and returned
    # nothing on a 300-word essay, 2026-09-26) -- not that a one-word answer does. They feed
    # decode speed only, never the floor or the emits verdict.
    obs_all, obs = obs, [o for o in obs if o["source"] not in ("gen", "gen-import")]
    ok = [o for o in obs if (o["content_chars"] or 0) > 0 or (o["tool_calls"] or 0) > 0]
    empty = [o for o in obs if o not in ok]
    reasoned = any((o["reasoning_chars"] or 0) > 0 for o in obs)
    largest_empty = max((o["max_tokens"] or 0 for o in empty), default=0)

    if ok:
        emits = Emits.REASONING_THEN_CONTENT if (reasoned or empty) else Emits.CONTENT
    elif empty and largest_empty >= REASONING_ONLY_AT:
        emits = Emits.REASONING_ONLY
    elif empty:
        emits = Emits.REASONING_THEN_CONTENT     # empty so far, but only at small budgets
    else:
        emits = None

    min_mt: int | None
    floor_evidence = ""
    if emits is Emits.CONTENT:
        min_mt = 1
    elif emits is Emits.REASONING_THEN_CONTENT:
        # The floor is what THIS model was measured to spend before its answer began,
        # not a rung of the probe ladder: GLM-5.3-Flash spent 91 tokens on one word,
        # DeepSeek-V4.1-Flash 18. A budget at or below the reasoning spend returns
        # nothing, so the floor is one token above the largest spend seen on a probe
        # (the fixed one-word task), and above every budget that came back empty.
        # It is a LOWER bound: a long task can reason for thousands of tokens more.
        probes = [o for o in ok if o["source"] in ONE_WORD_SOURCES
                  and o["completion_tokens"] is not None]
        pool = probes or [o for o in ok if o["completion_tokens"] is not None]
        spent = [_spent_before_answer(o) for o in pool]
        if spent:
            min_mt = max(max(spent) + 1, largest_empty + 1)
            floor_evidence = "%d reasoning tokens before its answer (%s)" % (
                max(spent), "probe" if probes else "traffic")
            if largest_empty:
                floor_evidence += "; empty at max_tokens=%d" % largest_empty
        else:
            # No token counts: fall back to the smallest budget that produced content.
            above = [o["max_tokens"] for o in ok if o["max_tokens"] and o["max_tokens"] > largest_empty]
            min_mt = min(above) if above else None
            if min_mt:
                floor_evidence = "content first appeared at max_tokens=%d" % min_mt
    else:
        min_mt = None

    overhead = None
    spent_all: list[int] = []
    if emits is Emits.REASONING_THEN_CONTENT:
        spent_all = [_spent_before_answer(o) for o in ok if o["completion_tokens"] is not None]
        overhead = int(statistics.median(spent_all)) if spent_all else None
    max_reasoning = max(spent_all) if emits is Emits.REASONING_THEN_CONTENT and spent_all else None

    priced = [o for o in obs if o["cost_usd"] is not None and o["prompt_tokens"] is not None
              and o["completion_tokens"] is not None]
    tokens = sum(o["prompt_tokens"] + o["completion_tokens"] for o in priced)
    usd = sum((Decimal(str(o["cost_usd"])) for o in priced), Decimal(0))
    measured = usd / tokens * Decimal(1_000_000) if tokens else None
    basis = "+".join(sorted({o["cost_basis"] for o in priced})) if priced else ""
    # Billed against what the rate card said, on the same calls: keeps the input/output
    # split of the card (which decides Kimi-K3 between Sail and Nous) while still catching
    # what a card misses (Venice's injected system prompt billed 1507 tokens for "hi").
    both = [o for o in priced if o["cost_basis"] == "billed" and "card_usd" in o.keys()
            and o["card_usd"] not in (None, "", "0")]
    card = sum((Decimal(o["card_usd"]) for o in both), Decimal(0))
    billing_ratio = (sum((Decimal(str(o["cost_usd"])) for o in both), Decimal(0)) / card
                     if card else None)
    # Two different latencies. A short call measures connection + prefill + first token;
    # only a real generation measures decode. A 16-token probe called AkashML "fastest"
    # while its decode ran at 24 tok/s against io.net's 260 (2026-09-26).
    lat = [o["latency_s"] for o in ok if o["latency_s"]
           and (o["completion_tokens"] or 0) <= SHORT_MAX_TOKENS]
    tps = [o["completion_tokens"] / o["latency_s"] for o in obs_all
           if o["latency_s"] and (o["completion_tokens"] or 0) >= DECODE_MIN_TOKENS]
    fields = [o["reasoning_field"] for o in obs if "reasoning_field" in o.keys()
              and o["reasoning_field"] is not None]
    has_field = (True if any(fields) else False) if fields else None
    if has_field is False and ok:
        # No reasoning field at all: there is nowhere for text to be stranded, so the
        # caller's budget can be honoured as-is. Any empty reply was something else.
        emits, min_mt, overhead, max_reasoning = Emits.CONTENT, 1, None, None
        floor_evidence = "no reasoning field in any response: budget honoured as-is"
    bp, bc, bev = solve_billed_rates(priced)
    return Profile(seat, len(obs_all), emits, min_mt, overhead, measured, basis,
                   statistics.median(lat) if lat else None,
                   time.time() - max(o["t"] for o in obs_all) if obs_all else None, floor_evidence,
                   max_reasoning, has_reasoning_field=has_field,
                   decode_tps=statistics.median(tps) if tps else None,
                   billing_ratio=billing_ratio, billed_prompt=bp, billed_completion=bc,
                   billed_evidence=bev)


def solve_billed_rates(obs: list) -> tuple[Decimal | None, Decimal | None, str]:
    """The input and output rates a provider ACTUALLY charged, solved from its own bills.

    Each billed call is one equation: billed = prompt x a + completion x b. Two calls with
    different prompt:completion splits pin both rates (least squares over all of them). An
    advertised price is a claim; this is the bill. Measured 2026-09-26: Nous advertised
    Kimi-K3 at $1.03 / $9.04 per M (API and portal alike) and billed $3.00 / $15.00 --
    OpenRouter's list -- on the nous-fleet key.
    """
    rows = [(Decimal(o["prompt_tokens"]), Decimal(o["completion_tokens"]),
             Decimal(str(o["cost_usd"]))) for o in obs
            if o["cost_basis"] == "billed" and not (o["cached_tokens"] or 0)
            and o["prompt_tokens"] and o["completion_tokens"] is not None]
    if len({(p, c) for p, c, _ in rows}) < 2:
        return None, None, ""
    spp = sum(p * p for p, _, _ in rows)
    scc = sum(c * c for _, c, _ in rows)
    spc = sum(p * c for p, c, _ in rows)
    spy = sum(p * y for p, _, y in rows)
    scy = sum(c * y for _, c, y in rows)
    det = spp * scc - spc * spc
    # Near-collinear splits (every call the same shape) cannot separate the two rates.
    if det <= 0 or det / (spp * scc) < Decimal("1e-4"):
        return None, None, ""
    a = (spy * scc - scy * spc) / det
    b = (scy * spp - spy * spc) / det
    if a < 0 or b < 0:
        return None, None, ""
    m = Decimal(1_000_000)
    q = Decimal("0.0001")
    return (a * m).quantize(q), (b * m).quantize(q), "solved from %d billed calls" % len(rows)


def _spent_before_answer(o) -> int:
    """Completion tokens not accounted for by the answer: ~4 characters a token,
    rounded DOWN (min 1) so the floor errs high -- 'ready' is one token, not two."""
    chars = o["content_chars"] or 0
    answer = max(1, chars // 4) if chars else 0
    return max(0, (o["completion_tokens"] or 0) - answer)
