"""GATHER: the live roster for one request -- prices, capability, behaviour, quota.

Sources, in order of authority for each fact:
  price        open-dashboard live-models row for (provider, model), else the
               provider's own catalogue. Which one was used is recorded.
  available    present in the provider's live catalogue right now; the dashboard
               can additionally mark a model `disappeared`.
  behaviour    this router's own observations of the model (probes and traffic).
  quota/state  the provider's last answers: an account-level refusal BLOCKS the
               provider for a while; a 429/quota answer marks it QUOTA_EXHAUSTED.

Nothing is pinned. A slug that is retired drops out of the catalogue and so out of
the roster; it cannot be chosen because it is not there to choose.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import httpx

from . import providers as P
from .config import Config
from dataclasses import replace

from .decision import Candidate, money
from .secrets import Keyring
from .store import Store

log = logging.getLogger("modelrouter.gather")
CATALOGUE_TTL_S = 600
DASHBOARD_TTL_S = 1800
BLOCK_S = 900          # an account-level refusal benches the provider this long
QUOTA_S = 600          # a quota refusal, unless the provider says when it resets
UNAVAILABLE_S = 300    # unreachable / CDN bot check: retry soon, never blame the key
REFRESH_S = 240        # out-of-band refresh, well inside the TTLs: the request path reads cache
MIRROR_OVERLAP = 0.8   # share of a catalogue found in another's ...
MIRROR_SAME_PRICE = 0.95   # ... with prices identical to the digit: MIRROR
CORRELATED_SAME_PRICE = 0.5   # ... mostly identical: CORRELATED (shared upstream)
# Below that, the same ids are just the same open weights hosted independently: AkashML's
# six models are all on io.net, and not one price matches (2026-09-26).


def _iso_age(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return time.time() - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def detect_mirrors(cats: dict[str, list]) -> dict[str, tuple[str, float]]:
    """{copy: (original, share of shared models priced identically)} for every provider
    whose catalogue is mostly another's (>= MIRROR_OVERLAP of its ids).

    Two kinds, told apart by price:
      MIRROR      >= MIRROR_SAME_PRICE identical: comparing it scores a provider against
                  itself. Excluded.
      CORRELATED  same catalogue, its own prices: a real alternative for price, but not
                  independent for fallback. Measured 2026-09-26: Nous carries 97% of its
                  ids from OpenRouter, but 51 of 368 shared models are priced differently
                  (DeepSeek-V4.1-Flash $0.035/$0.29 vs $0.30/$1.20), and it served billed
                  calls while every OpenRouter key was refused for the org budget."""
    priced = {p: {li.model: (li.prompt, li.completion) for li in ls} for p, ls in cats.items() if ls}
    out: dict[str, tuple[str, float]] = {}
    for b, pb in priced.items():
        best = None
        for a, pa in priced.items():
            if a == b or not pb:
                continue
            # the copy is the one contained in the other; equal size -> the later name
            if not (len(pb) < len(pa) or (len(pb) == len(pa) and b > a)):
                continue
            common = set(pa) & set(pb)
            if len(common) / len(pb) < MIRROR_OVERLAP:
                continue
            same = sum(1 for m in common if pb[m] == pa[m] and pb[m][0] is not None) / len(common)
            key = (same, len(common) / len(pb))
            if best is None or key > best[0]:
                best = (key, a)
        if best:
            out[b] = (best[1], best[0][0])
    return out


@dataclass
class ProviderState:
    provider: str
    key: str = "UNREAD"              # KeyStatus.detail
    # OK | NO_KEY | BLOCKED (the account: auth, money) | QUOTA_EXHAUSTED (a rate window)
    # | UNAVAILABLE (cannot be reached, or a CDN refused how we called: the key is NOT suspect)
    # | CATALOGUE_FAILED
    state: str = "OK"
    detail: str = ""
    until: float = 0.0
    models: int = 0
    price_source: str = ""           # dashboard | provider-catalogue | mixed
    ratelimit: dict[str, str] = field(default_factory=dict)
    free_quota_until: float = 0.0    # OpenRouter's free-model daily quota, separate from credit
    in_flight: int = 0               # calls this router has open to the provider right now
    max_concurrency: int | None = None   # measured (or configured) concurrency it survives
    concurrency_source: str = ""
    mirror_of: str = ""              # its catalogue is this provider's (MIRROR or CORRELATED)
    correlated_with: list = field(default_factory=list)   # shared upstream, both directions
    same_price_share: float | None = None

    def public(self) -> dict:
        d = dict(self.__dict__)
        d["until"] = self.until or None
        d["free_quota_until"] = self.free_quota_until or None
        return d


class Gatherer:
    def __init__(self, cfg: Config, keys: Keyring, store: Store):
        self.cfg, self.keys, self.store = cfg, keys, store
        self._cat: dict[str, tuple[float, list[P.Listing]]] = {}
        self._dash: dict[str, tuple[float, dict[str, dict] | None]] = {}
        self._win: dict[str, tuple[float, dict]] = {}
        self._ctx: dict[str, tuple[float, dict]] = {}
        self.states: dict[str, ProviderState] = {p: ProviderState(p) for p in cfg.secrets}
        # provider -> ok | not carried | unreachable (...) ; the whole map is shown as-is
        self.dashboard_status: dict[str, str] | str = (
            "not configured" if not cfg.dashboard_url else {})
        self._lock = threading.Lock()

    # --- sources -----------------------------------------------------------------
    def _catalogue(self, provider: str, key: str) -> list[P.Listing]:
        hit = self._cat.get(provider)
        if hit and time.time() - hit[0] < CATALOGUE_TTL_S:
            return hit[1]
        rows = P.fetch_catalogue(provider, key)
        self._cat[provider] = (time.time(), rows)
        return rows

    def _dashboard(self, provider: str) -> dict[str, dict] | None:
        """The dashboard's live-models rows for one provider, by model id; None if it
        does not carry that provider (then the provider's own catalogue prices)."""
        if not self.cfg.dashboard_url:
            return None
        hit = self._dash.get(provider)
        if hit and time.time() - hit[0] < DASHBOARD_TTL_S:
            return hit[1]
        rows: dict[str, dict] = {}
        cursor = None
        try:
            for _ in range(20):
                params = {"provider": provider, "limit": 500}
                if cursor:
                    params["cursor"] = cursor
                r = httpx.get(self.cfg.dashboard_url + "/api/public/v2/live-models",
                              params=params, timeout=30, headers={"User-Agent": P.UA})
                if r.status_code == 400 and not rows:
                    # The dashboard validates `provider` against the set it collects:
                    # 400 means it does not carry this provider (yet), not that it is down.
                    self.dashboard_status[provider] = "not carried"
                    self._dash[provider] = (time.time(), None)
                    return None
                r.raise_for_status()
                d = r.json()
                for row in d.get("data") or []:
                    if row.get("provider") == provider:
                        rows[str(row.get("id"))] = row
                cursor = d.get("cursor")
                if not cursor or not d.get("data"):
                    break
            self.dashboard_status[provider] = "ok (%d rows)" % len(rows)
        except Exception as exc:                                # noqa: BLE001
            self.dashboard_status[provider] = "unreachable (%s)" % type(exc).__name__
            self._dash[provider] = (time.time(), None)
            return None
        out = rows or None
        self._dash[provider] = (time.time(), out)
        return out

    def _window_prices(self, provider: str) -> dict:
        """Per-window rate cards for providers that publish them (Sail). A failed read
        leaves them UNKNOWN for this run -- never a stale copy, never zero."""
        if not P.PROVIDERS[provider].get("prices"):
            return {}
        hit = self._win.get(provider)
        if hit and time.time() - hit[0] < CATALOGUE_TTL_S:
            return hit[1]
        try:
            prices = P.fetch_window_prices(provider)
            self.states[provider].price_source = "provider pricing page"
        except Exception as exc:                                # noqa: BLE001
            prices = {}
            self.states[provider].detail = "pricing page unreadable (%s)" % type(exc).__name__
        self._win[provider] = (time.time(), prices)
        return prices

    def _context_lengths(self, provider: str) -> dict:
        if not P.PROVIDERS[provider].get("specs"):
            return {}
        hit = self._ctx.get(provider)
        if hit and time.time() - hit[0] < CATALOGUE_TTL_S:
            return hit[1]
        try:
            ctx = P.fetch_context_lengths(provider)
        except Exception:                                       # noqa: BLE001
            ctx = {}                                            # stays UNKNOWN this run
        self._ctx[provider] = (time.time(), ctx)
        return ctx

    # --- concurrency ---------------------------------------------------------------
    def acquire(self, provider: str) -> None:
        with self._lock:
            if provider in self.states:
                self.states[provider].in_flight += 1

    def try_acquire(self, provider: str) -> bool:
        """Reserve a slot atomically. Checking capacity when deciding and reserving when
        calling let 17 calls through a cap of 16 under 20-way load (2026-09-26)."""
        with self._lock:
            st = self.states.get(provider)
            if st is None:
                return True
            if st.max_concurrency and st.in_flight >= st.max_concurrency:
                return False
            st.in_flight += 1
            return True

    def release(self, provider: str) -> None:
        with self._lock:
            if provider in self.states:
                self.states[provider].in_flight = max(0, self.states[provider].in_flight - 1)

    def _caps(self) -> None:
        """Concurrency caps: configured beats measured; measured = the largest
        concurrency run with zero failures (AkashML dropped 34 of 64 on 2026-09-26)."""
        facts = self.store.provider_facts()
        for p, st in self.states.items():
            conf = self.cfg.max_concurrency.get(p)
            if conf:
                st.max_concurrency, st.concurrency_source = int(conf), "config"
            elif "max_ok_concurrency" in facts.get(p, {}):
                f = facts[p]["max_ok_concurrency"]
                st.max_concurrency, st.concurrency_source = int(f["value"]), f["source"]

    # --- feedback from ACT -----------------------------------------------------------
    def penalise(self, provider: str, model: str, scope: str, detail: str,
                 ratelimit: dict[str, str] | None = None) -> None:
        st = self.states.get(provider)
        if not st:
            return
        now = time.time()
        if ratelimit:
            st.ratelimit = ratelimit
        if scope == "provider":
            st.state, st.detail, st.until = "BLOCKED", detail, now + BLOCK_S
        elif scope in ("edge", "transport"):
            st.state, st.detail, st.until = "UNAVAILABLE", detail, now + UNAVAILABLE_S
        elif scope == "quota":
            if provider == "openrouter" and model.endswith(":free"):
                # The free-model daily quota is its own limit; paid models still work.
                st.free_quota_until = now + 6 * 3600
                st.detail = "free-model daily quota: " + detail
            else:
                st.state, st.detail, st.until = "QUOTA_EXHAUSTED", detail, now + QUOTA_S

    def note_ratelimit(self, provider: str, ratelimit: dict[str, str]) -> None:
        if provider in self.states and ratelimit:
            self.states[provider].ratelimit = ratelimit

    # --- the roster --------------------------------------------------------------------
    def roster(self, lane: str = "interactive") -> list[Candidate]:
        profiles = self.store.profiles()
        self._caps()
        facts = self.store.provider_facts()
        out: list[Candidate] = []
        with self._lock:
            for provider, st in self.states.items():
                ks = self.keys.load(provider)
                st.key = ks.detail
                if not ks.present:
                    st.state, st.detail, st.models = "NO_KEY", "%s %s: %s" % (
                        ks.source, ks.name or "(unset)", ks.detail), 0
                    continue
                if st.state in ("BLOCKED", "QUOTA_EXHAUSTED", "UNAVAILABLE")                         and time.time() >= st.until:
                    st.state, st.detail, st.until = "OK", "", 0.0
                if st.state in ("NO_KEY", "CATALOGUE_FAILED", "AT_CAPACITY"):
                    st.state, st.detail = "OK", ""
                try:
                    listings = self._catalogue(provider, self.keys.get(provider))
                except P.CatalogueError as exc:
                    if exc.scope in ("edge", "transport"):
                        st.state, st.until = "UNAVAILABLE", time.time() + UNAVAILABLE_S
                        st.detail = ("catalogue HTTP %d: CDN bot check, not a credential "
                                     "failure" % exc.status)
                    elif exc.scope == "provider" or exc.status in (401, 402, 403):
                        st.state, st.until = "BLOCKED", time.time() + BLOCK_S
                        st.detail = "catalogue HTTP %d %s" % (exc.status, exc.msg)
                    else:
                        st.state = "CATALOGUE_FAILED"
                        st.detail = "catalogue HTTP %d %s" % (exc.status, exc.msg)
                    continue
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    st.state, st.until = "UNAVAILABLE", time.time() + UNAVAILABLE_S
                    st.detail = "unreachable (%s)" % type(exc).__name__
                    continue
                except Exception as exc:                        # noqa: BLE001
                    st.state, st.detail = "CATALOGUE_FAILED", type(exc).__name__
                    continue
                if st.max_concurrency and st.in_flight >= st.max_concurrency:
                    # Above what it was measured to survive it drops requests, which looks
                    # like intermittent model failure upstream. Route elsewhere instead.
                    st.state = "AT_CAPACITY"
                    st.detail = "%d calls in flight; measured to survive %d (%s)" % (
                        st.in_flight, st.max_concurrency, st.concurrency_source)
                dash = self._dashboard(provider)
                windows = self._window_prices(provider)
                contexts = self._context_lengths(provider)
                st.models = len(listings)
                sources = set()
                for li in listings:
                    seat = "%s:%s" % (provider, li.model)
                    pr, co, src, available = li.prompt, li.completion, "provider-catalogue", True
                    ca = li.cached_prompt
                    age = time.time() - li.read_at if li.read_at else None
                    window = None
                    blocked = li.blocked
                    cards = windows.get(li.model) or li.windows or {}
                    for w in P.LANE_WINDOWS.get(lane, ("asap",)):
                        if w in cards:
                            pr, co, ca = cards[w]
                            window, src = w, "provider pricing page"
                            age = time.time() - self._win[provider][0] \
                                if provider in self._win else age
                            break
                    else:
                        if cards and not blocked:
                            # Priced, but not in any window this lane may buy (Sail's
                            # Qwen3.6-35B-A3B is flex-only and answers ASAP calls with 400).
                            pr = co = None
                            blocked = "not sold in the %s lane's windows (offered: %s)" % (
                                lane, ", ".join(sorted(cards)))
                    row = (dash or {}).get(li.model)
                    if row:
                        pp = row.get("pricing") or {}
                        dp, dc = P._per_m(pp.get("promptUsdPerToken")), \
                            P._per_m(pp.get("completionUsdPerToken"))
                        # The provider's own catalogue, read this run, beats the dashboard's
                        # daily copy; the dashboard fills a price the provider did not give.
                        if (pr is None or co is None) and dp is not None and dc is not None:
                            pr, co, src = dp, dc, "dashboard"
                            age = _iso_age(row.get("lastConfirmedAt") or row.get("lastSeenAt"))
                        if row.get("availability") == "disappeared":
                            available = False
                    sources.add(src)
                    state, detail = st.state, st.detail
                    if state == "OK" and li.model.endswith(":free") and \
                            time.time() < st.free_quota_until:
                        state, detail = "QUOTA_EXHAUSTED", "free-model daily quota"
                    prof = profiles.get(seat)
                    out.append(Candidate(
                        seat=seat, provider=provider, model=li.model,
                        provider_state=state, provider_detail=detail, available=available,
                        context_length=li.context_length or contexts.get(li.model),
                        supports_tools=li.supports_tools,
                        list_prompt=pr, list_completion=co, list_cached=ca, price_age_s=age,
                        price_source=src, in_flight=st.in_flight,
                        load_factor=self._load_factor(facts.get(provider, {}), st.in_flight),
                        measured_usd_per_mtok=prof.measured_usd_per_mtok if prof else None,
                        measured_basis=prof.measured_basis if prof else "", window=window,
                        blocked=blocked,
                        has_reasoning_field=prof.has_reasoning_field if prof else None,
                        billing_ratio=prof.billing_ratio if prof else None,
                        billed_prompt=prof.billed_prompt if prof else None,
                        billed_completion=prof.billed_completion if prof else None,
                        billed_evidence=prof.billed_evidence if prof else "",
                        decode_tps=prof.decode_tps if prof else None,
                        emits=prof.emits if prof else None,
                        min_max_tokens=prof.min_max_tokens if prof else None,
                        reasoning_overhead_tokens=prof.reasoning_overhead_tokens if prof else None,
                        floor_evidence=prof.floor_evidence if prof else "",
                        max_reasoning_tokens=prof.max_reasoning_tokens if prof else None,
                        probe_age_s=prof.age_s if prof else None,
                        latency_s=prof.latency_s if prof else None))
                st.price_source = "mixed" if len(sources) > 1 else (next(iter(sources), ""))
            mirrors = detect_mirrors({p: self._cat[p][1] for p in self.states if p in self._cat})
            excluded = set()
            for st in self.states.values():
                st.correlated_with = []
            for copy, (original, same) in mirrors.items():
                st = self.states[copy]
                if same < CORRELATED_SAME_PRICE:
                    continue                    # same open weights, independent host
                st.mirror_of = original
                st.same_price_share = round(same, 3)
                st.correlated_with = sorted(set(st.correlated_with) | {original})
                if original in self.states:
                    o = self.states[original]
                    o.correlated_with = sorted(set(o.correlated_with) | {copy})
                if same >= MIRROR_SAME_PRICE and not self.cfg.allow_mirrors and st.state == "OK":
                    st.state = "MIRROR"
                    st.detail = ("mirror of %s: %.0f%% of shared models priced identically -- "
                                 "not an independent provider" % (original, 100 * same))
                    excluded.add(copy)
                elif same >= CORRELATED_SAME_PRICE and st.state == "OK":
                    st.detail = ("CORRELATED with %s: shares its catalogue, own prices on %.0f%% "
                                 "of shared models -- not independent for fallback" % (
                                     original, 100 * (1 - same)))
            out = [replace(c, correlated_with=tuple(self.states[c.provider].correlated_with))
                   if self.states[c.provider].correlated_with else c for c in out]
            if excluded:
                out = [c if c.provider not in excluded else
                       replace(c, provider_state="MIRROR",
                               provider_detail=self.states[c.provider].detail)
                       for c in out]
        return out

    @staticmethod
    def _load_factor(facts: dict, in_flight: int) -> float:
        """Latency multiplier with this call added to what is already open, interpolated
        from the provider's measured curve (median latency at n / at 1). Sail stays flat
        (2.70 -> 3.23s at 64); io.net degrades 60% (1.11 -> 1.80s)."""
        pts = sorted((int(k[len("latency_ratio_n"):]), f["value"]) for k, f in facts.items()
                     if k.startswith("latency_ratio_n"))
        if not pts:
            return 1.0
        n = in_flight + 1
        prev = (1, 1.0)
        for x, y in pts:
            if n <= x:
                return prev[1] + (y - prev[1]) * (n - prev[0]) / max(1, x - prev[0])
            prev = (x, y)
        return pts[-1][1]

    # --- out-of-band refresh --------------------------------------------------------
    def refresh(self) -> None:
        """Re-read every live source now. Run on a timer so a request never waits on a
        catalogue: the per-request decision is a lookup plus a comparison."""
        for provider in list(self.states):
            if not self.keys.load(provider).present:
                continue
            try:
                self._cat[provider] = (time.time(), P.fetch_catalogue(provider,
                                                                      self.keys.get(provider)))
            except Exception:                                   # noqa: BLE001
                pass                    # the next roster() records why, on the request path
            self._dash.pop(provider, None)
            self._win.pop(provider, None)
            self._ctx.pop(provider, None)
            for fn in (self._dashboard, self._window_prices, self._context_lengths):
                try:
                    fn(provider)
                except Exception:                               # noqa: BLE001
                    pass
        self.last_refresh = time.time()

    def start_refresher(self, every_s: float = REFRESH_S) -> None:
        def loop():
            while True:
                try:
                    self.refresh()
                except Exception:                               # noqa: BLE001
                    log.exception("refresh failed")
                time.sleep(every_s)
        threading.Thread(target=loop, daemon=True, name="modelrouter-refresh").start()

    def listing(self, seat: str, window: str | None = None) -> P.Listing | None:
        """The seat's rate card -- for a windowed provider, the card of the window the
        call was actually scheduled in, so its computed cost is the one it incurred."""
        provider, _, model = seat.partition(":")
        hit = self._cat.get(provider)
        for li in (hit[1] if hit else []):
            if li.model == model:
                card = (self._win.get(provider, (0, {}))[1].get(model) or {}).get(window or "asap")
                if card:
                    return P.Listing(provider, model, card[0], card[1], card[2],
                                     li.context_length, li.supports_tools, li.reasoning)
                return li
        return None


def card_cost(li: P.Listing | None, res: P.Result) -> Decimal | None:
    """What this model's own rate card says the call cost."""
    if li is None or li.prompt is None or li.completion is None \
            or res.prompt_tokens is None or res.completion_tokens is None:
        return None
    p, c = money(li.prompt), money(li.completion)
    cached = res.cached_tokens or 0
    cached_rate = money(li.cached_prompt) if li.cached_prompt is not None else p
    return ((res.prompt_tokens - cached) * p + cached * cached_rate
            + res.completion_tokens * c) / Decimal(1_000_000)


def call_cost(li: P.Listing | None, res: P.Result) -> tuple[Decimal | None, str]:
    """What one call cost: billed when the provider says, else usage x this model's
    own rate card (cached prompt tokens at the cached rate when published)."""
    if res.billed_usd is not None:
        return Decimal(str(res.billed_usd)), "billed"
    if li is None or li.prompt is None or li.completion is None \
            or res.prompt_tokens is None or res.completion_tokens is None:
        return None, "unknown"
    cached = res.cached_tokens or 0
    p, c = money(li.prompt), money(li.completion)
    cached_rate = money(li.cached_prompt) if li.cached_prompt is not None else p
    usd = ((res.prompt_tokens - cached) * p + cached * cached_rate
           + res.completion_tokens * c) / Decimal(1_000_000)
    return usd, "computed"
