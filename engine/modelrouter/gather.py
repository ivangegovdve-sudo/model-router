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

import httpx

from . import providers as P
from .config import Config
from .decision import Candidate
from .secrets import Keyring
from .store import Store

log = logging.getLogger("modelrouter.gather")
CATALOGUE_TTL_S = 600
DASHBOARD_TTL_S = 1800
BLOCK_S = 900          # an account-level refusal benches the provider this long
QUOTA_S = 600          # a quota refusal, unless the provider says when it resets
UNAVAILABLE_S = 300    # unreachable / CDN bot check: retry soon, never blame the key


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
                if st.state in ("NO_KEY", "CATALOGUE_FAILED"):
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
                dash = self._dashboard(provider)
                windows = self._window_prices(provider)
                contexts = self._context_lengths(provider)
                st.models = len(listings)
                sources = set()
                for li in listings:
                    seat = "%s:%s" % (provider, li.model)
                    pr, co, src, available = li.prompt, li.completion, "provider-catalogue", True
                    window = None
                    cards = windows.get(li.model) or li.windows or {}
                    for w in P.LANE_WINDOWS.get(lane, ("asap",)):
                        if w in cards:
                            pr, co, _cached = cards[w]
                            window, src = w, "provider pricing page"
                            break
                    row = (dash or {}).get(li.model)
                    if row:
                        pp = row.get("pricing") or {}
                        dp, dc = P._per_m(pp.get("promptUsdPerToken")), \
                            P._per_m(pp.get("completionUsdPerToken"))
                        if dp is not None and dc is not None:
                            pr, co, src = dp, dc, "dashboard"
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
                        list_prompt=pr, list_completion=co, price_source=src,
                        measured_usd_per_mtok=prof.measured_usd_per_mtok if prof else None,
                        measured_basis=prof.measured_basis if prof else "", window=window,
                        emits=prof.emits if prof else None,
                        min_max_tokens=prof.min_max_tokens if prof else None,
                        reasoning_overhead_tokens=prof.reasoning_overhead_tokens if prof else None,
                        floor_evidence=prof.floor_evidence if prof else "",
                        max_reasoning_tokens=prof.max_reasoning_tokens if prof else None,
                        probe_age_s=prof.age_s if prof else None,
                        latency_s=prof.latency_s if prof else None))
                st.price_source = "mixed" if len(sources) > 1 else (next(iter(sources), ""))
        return out

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


def call_cost(li: P.Listing | None, res: P.Result) -> tuple[float | None, str]:
    """What one call cost: billed when the provider says, else usage x this model's
    own rate card (cached prompt tokens at the cached rate when published)."""
    if res.billed_usd is not None:
        return res.billed_usd, "billed"
    if li is None or li.prompt is None or li.completion is None \
            or res.prompt_tokens is None or res.completion_tokens is None:
        return None, "unknown"
    cached = res.cached_tokens or 0
    cached_rate = li.cached_prompt if li.cached_prompt is not None else li.prompt
    usd = ((res.prompt_tokens - cached) * li.prompt + cached * cached_rate
           + res.completion_tokens * li.completion) / 1e6
    return usd, "computed"
