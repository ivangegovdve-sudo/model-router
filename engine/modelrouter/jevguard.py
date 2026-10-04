"""Hard, client-side daily spend cap for TypeSafe / Jev (POST /v1/systemone).

WHY THIS EXISTS
---------------
On 2026-10-01 Jev was billed ~$10 in one day while a "$1/day cap" was believed to be in
place. The cap that existed (glass-mem `max_usd_per_day`) only counted calls whose cost was
KNOWN, Jev returns no price, and the per-token prices were unset -- so every Jev call was
"unknown cost" and the USD cap could never trip. The caller that actually spent the money
(the kalo-canon typed extraction, ~37k calls / ~250M input tokens that day) had no cap at
all. TypeSafe has no server-side budget, so the cap has to be ours.

WHAT IT GUARANTEES
------------------
Every paid call RESERVES its worst-case cost in one `BEGIN IMMEDIATE` SQLite transaction
before the request leaves, and settles to the actual cost afterwards. The reservation is an
upper bound (one input token per UTF-8 byte of the request, plus a fixed allowance for the
prompt the API wraps around it), so settled + in-flight spend stays under the cap -- across
threads and across processes sharing the ledger file. A crash between reserve and settle leaves the reservation
standing: a crash cannot buy calls. A call whose billing is unknown (timeout, a 5xx, no
`usage`) is charged its reservation, never $0; only a 401/403 refusal is free. A call still
in flight when local midnight passes keeps counting until it settles, and its cost lands in
the day it settles in. A hold is never dropped for being old: after RESERVATION_TTL_S it is
converted to a charge of its worst case. The price cannot be configured below TypeSafe's
published one, the cap cannot be configured above MAX_CAP_USD, and a request too large to
bound is refused.

THE ONE ASSUMPTION, and its limit. The reservation is a bound only as long as the provider
bills no more input tokens than: request bytes + REQUEST_OVERHEAD_TOKENS +
QUESTION_OVERHEAD_TOKENS per question. Measured live 2026-10-04: large requests bill 0.30-0.75
tokens per byte, and the API adds ~250 tokens of its own to every request (a 90-byte request
was billed 269), which is what the fixed allowance covers, four times over. The exact bytes
that were measured are the bytes that are sent. No client can stop a provider billing more
than it was sent: if a settlement ever exceeds its reservation the guard alerts and, in the
same transaction, widens every hold still in flight and every later reservation by the
observed ratio (persisted). The exposure is then the calls already on the wire at that
moment -- nothing new is admitted on the old bound.

A call has a hard wall-clock deadline (MAX_TIMEOUT_S). When it passes, the call is charged
its worst case and the caller gets TimeoutError, whatever the transport is still doing.

When the cap would be crossed the paid call is NOT made. The call is answered by the free
local System-1 fallback (Laya in-process, or a Jev-compatible local URL) when one is
available, else `CapReached` is raised so the caller can defer. The first block of a day
raises an alert (log, alerts file, optional webhook).

Cost is TypeSafe's published price, not a guess: jev-1.13.0 is $0.042 per million INPUT
tokens and output tokens are free (docs.typesafe.ai/models, read 2026-10-04), applied to the
`usage.input_tokens` / `usage.output_tokens` the API returns. Money is integer nano-USD.

This module is stdlib-only so any local Jev caller can import it and share the one ledger.
The TypeSafe key is never read here: the caller passes `get_key`, which reads it by NAME.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("modelrouter.jevguard")

API = "https://api.typesafe.ai/v1/systemone"
NANO = Decimal(10) ** 9
# TypeSafe's published prices (docs.typesafe.ai/models, jev-1.13.0, read 2026-10-04). They are
# FLOORS: config and environment may raise what a call is charged, never lower it -- a price of
# 0 would make every call free on paper and the cap meaningless.
PRICE_IN_FLOOR = Decimal("0.042")
PRICE_OUT_FLOOR = Decimal("0")
MAX_CAP_USD = Decimal("100")        # a "cap" above this is a typo or a way around the cap
# Jev accepts 64k tokens per request (same page), so an answer cannot be longer than that.
MAX_REQUEST_BYTES = 262_144
MAX_OUTPUT_TOKENS = 65_536
# What the API adds to the input it bills, beyond the bytes we send. Measured live 2026-10-04:
# a 90-byte one-question request was billed 269 input tokens, a 124-byte one 303, i.e. ~250
# tokens of fixed prompt per request; 16 questions in 726 bytes were billed 404. The allowances
# are ~4x what was measured.
REQUEST_OVERHEAD_TOKENS = 1024
QUESTION_OVERHEAD_TOKENS = 64
# A hold that has not settled after this long belongs to a crashed process. It is never simply
# dropped: it is converted to a charge of its worst case (UNKNOWN_BILLING) in the day the sweep
# runs. No call of a live process is still held by then: the guard enforces MAX_TIMEOUT_S as a
# hard wall-clock deadline and charges the worst case itself when it passes.
RESERVATION_TTL_S = 3600
MAX_TIMEOUT_S = 600.0
# Statuses that can only mean the key was refused before inference. Every other HTTP error
# (5xx, and 429 too: nothing guarantees a rate-limit answer is unbilled) may have been billed.
NOT_BILLED_STATUSES = (401, 403)


def _dec(v, what: str) -> Decimal:
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        raise ValueError("%s must be a number, got %r" % (what, v))
    if not d.is_finite() or d < 0:
        raise ValueError("%s must be a finite number >= 0, got %r" % (what, v))
    return d


@dataclass
class GuardConfig:
    cap_usd: Decimal = Decimal("1.00")
    usd_per_mtok_in: Decimal = Decimal("0.042")
    usd_per_mtok_out: Decimal = Decimal("0")
    timezone: str = ""                 # IANA name; "" = this machine's local time
    ledger: Path = field(default_factory=lambda: Path.home() / ".modelrouter" / "jev_spend.sqlite3")
    fallback: str = "laya"             # "laya" | "url" | "defer"
    fallback_url: str = ""             # Jev-compatible local endpoint, for fallback = "url"
    alert_url: str = ""                # optional webhook POSTed once per day when the cap trips

    def __post_init__(self):
        self.cap_usd = _dec(self.cap_usd, "daily_cap_usd")
        self.usd_per_mtok_in = _dec(self.usd_per_mtok_in, "usd_per_mtok_in")
        self.usd_per_mtok_out = _dec(self.usd_per_mtok_out, "usd_per_mtok_out")
        if self.usd_per_mtok_in < PRICE_IN_FLOOR or self.usd_per_mtok_out < PRICE_OUT_FLOOR:
            raise ValueError("Jev prices cannot be set below TypeSafe's published $%s in / $%s out "
                             "per Mtok" % (PRICE_IN_FLOOR, PRICE_OUT_FLOOR))
        if self.cap_usd > MAX_CAP_USD:
            raise ValueError("daily_cap_usd above $%s is refused" % MAX_CAP_USD)
        if self.fallback == "url":
            url_fallback(self.fallback_url)             # loopback only: raise now, not at the cap
        self.ledger = Path(self.ledger)
        if self.fallback not in ("laya", "url", "defer"):
            raise ValueError('fallback must be "laya", "url" or "defer"')
        if self.timezone:
            ZoneInfo(self.timezone)                     # raise now, not at midnight

    @classmethod
    def from_env(cls, base: "GuardConfig | None" = None) -> "GuardConfig":
        """Environment overrides, for callers without a config file. Never a key value."""
        b = base or cls()
        e = os.environ.get
        return cls(cap_usd=e("JEV_DAILY_CAP_USD", b.cap_usd),
                   usd_per_mtok_in=e("JEV_USD_PER_MTOK_IN", b.usd_per_mtok_in),
                   usd_per_mtok_out=e("JEV_USD_PER_MTOK_OUT", b.usd_per_mtok_out),
                   timezone=e("JEV_GUARD_TZ", b.timezone),
                   ledger=Path(e("JEV_GUARD_LEDGER", str(b.ledger))),
                   fallback=e("JEV_FALLBACK", b.fallback),
                   fallback_url=e("JEV_FALLBACK_URL", b.fallback_url),
                   alert_url=e("JEV_ALERT_URL", b.alert_url))

    @property
    def cap_nano(self) -> int:
        return int(self.cap_usd * NANO)

    def cost_nano(self, input_tokens: int, output_tokens: int) -> int:
        """Exact price of a settled call, rounded UP to a whole nano-USD."""
        usd = (Decimal(input_tokens) * self.usd_per_mtok_in
               + Decimal(output_tokens) * self.usd_per_mtok_out) / Decimal(10 ** 6)
        return int((usd * NANO).to_integral_value(rounding=ROUND_CEILING))

    def worst_case_nano(self, body: dict) -> int:
        """What this request can cost at most: one input token per byte sent, plus what the
        API adds per request and per question, plus the longest answer the API can give."""
        qs = body.get("questions")
        nq = len(qs) if isinstance(qs, (dict, list)) else 1
        return self.cost_nano(request_bytes(body) + REQUEST_OVERHEAD_TOKENS
                              + QUESTION_OVERHEAD_TOKENS * max(nq, 1), MAX_OUTPUT_TOKENS)


def encode_body(body: dict) -> bytes:
    """The one serialisation: what is measured for the reservation is what goes on the wire."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def request_bytes(body: dict) -> int:
    n = len(encode_body(body))
    if n > MAX_REQUEST_BYTES:
        raise ValueError("Jev request is %d bytes; above %d it cannot be bounded and is refused"
                         % (n, MAX_REQUEST_BYTES))
    return n


def usd(nano: int) -> str:
    return format((Decimal(nano) / NANO).normalize(), "f")


class CapReached(RuntimeError):
    """The daily cap blocks this paid call and no free fallback could answer it."""

    def __init__(self, message: str, retry_after_s: int, status: dict):
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.status = status


class Ledger:
    """One SQLite file shared by every local Jev caller. Amounts are integer nano-USD."""

    def __init__(self, path: Path, timezone: str = "", now: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tz = ZoneInfo(timezone) if timezone else None
        self._now = now
        self._local = threading.local()
        with self._tx() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jev_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL, at TEXT NOT NULL, caller TEXT NOT NULL,
                status TEXT NOT NULL,              -- RESERVED | OK | UNKNOWN_BILLING | NOT_BILLED | BLOCKED
                reserved_nano INTEGER NOT NULL,    -- worst case, held until settled
                cost_nano INTEGER,                 -- NULL while RESERVED: the reservation counts
                input_tokens INTEGER, output_tokens INTEGER,
                request_bytes INTEGER NOT NULL, served_by TEXT, detail TEXT,
                ts REAL NOT NULL DEFAULT 0)""")
            if "ts" not in [r[1] for r in db.execute("PRAGMA table_info(jev_calls)")]:
                db.execute("ALTER TABLE jev_calls ADD COLUMN ts REAL NOT NULL DEFAULT 0")
            # A ledger written before `ts` existed: take each row's time from `at`, so an old
            # hold is aged from when it was made, not treated as made in 1970.
            for rid, at in db.execute("SELECT id, at FROM jev_calls WHERE ts = 0").fetchall():
                try:
                    ts = datetime.fromisoformat(at).timestamp()
                except ValueError:
                    ts = self.now().timestamp()                 # unreadable: treat as just made
                db.execute("UPDATE jev_calls SET ts = ? WHERE id = ?", (ts, rid))
            db.execute("CREATE TABLE IF NOT EXISTS jev_meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS jev_calls_day ON jev_calls(day)")
            db.execute("""CREATE TABLE IF NOT EXISTS jev_alerts (
                day TEXT PRIMARY KEY, at TEXT NOT NULL, detail TEXT NOT NULL)""")

    def now(self) -> datetime:
        if self._now:
            return self._now()
        return datetime.now(self._tz) if self._tz else datetime.now().astimezone()

    def day(self) -> str:
        """The LOCAL calendar day: the cap resets at local midnight."""
        return self.now().date().isoformat()

    def seconds_to_reset(self) -> int:
        n = self.now()
        nxt = (n + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int((nxt - n).total_seconds()))

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.execute("PRAGMA busy_timeout = 30000")
            self._local.c = c
        return c

    class _Tx:
        def __init__(self, c):
            self.c = c

        def __enter__(self):
            self.c.execute("BEGIN IMMEDIATE")           # the write lock serialises check + reserve
            return self.c

        def __exit__(self, et, *_):
            self.c.execute("ROLLBACK" if et else "COMMIT")

    def _tx(self) -> "_Tx":
        return Ledger._Tx(self._conn())

    def _committed(self, db, day: str) -> int:
        """Today's settled cost, plus EVERY unsettled hold whatever day it was made: a call
        that straddles midnight does not get a second budget."""
        return db.execute(
            "SELECT COALESCE(SUM(COALESCE(cost_nano, reserved_nano)), 0) FROM jev_calls "
            "WHERE day = ? OR status = 'RESERVED'", (day,)).fetchone()[0]

    def _sweep(self, db) -> None:
        """A hold older than the TTL belongs to a crashed process: charge it its worst case,
        in TODAY's budget (the day the sweep runs). It is converted, never forgotten, and it
        never leaves the current day's count by being converted."""
        db.execute("UPDATE jev_calls SET day = ?, status = 'UNKNOWN_BILLING', cost_nano = reserved_nano, "
                   "served_by = 'jev', detail = 'never settled: charged the worst case' "
                   "WHERE status = 'RESERVED' AND ts < ?",
                   (self.day(), self.now().timestamp() - RESERVATION_TTL_S))

    def bound_factor(self) -> int:
        """Thousandths by which reservations are scaled: 1000 unless the provider was ever
        seen to bill more than a reservation."""
        r = self._conn().execute("SELECT v FROM jev_meta WHERE k = 'bound_factor'").fetchone()
        return max(1000, r[0]) if r else 1000


    def reserve(self, caller: str, worst_nano: int, cap_nano: int, request_bytes: int):
        """-> (call id, None) when it fits under the cap, else (None, committed nano-USD)."""
        day = self.day()
        with self._tx() as db:
            self._sweep(db)
            spent = self._committed(db, day)
            if spent + worst_nano > cap_nano:
                return None, spent
            cur = db.execute(
                "INSERT INTO jev_calls(day, at, caller, status, reserved_nano, request_bytes, ts) "
                "VALUES (?, ?, ?, 'RESERVED', ?, ?, ?)",
                (day, self.now().isoformat(timespec="seconds"), caller, worst_nano, request_bytes,
                 self.now().timestamp()))
            return cur.lastrowid, None

    def settle(self, call_id: int, status: str, cost_nano: int, input_tokens: int | None,
               output_tokens: int | None, detail: str = "") -> None:
        """The cost lands in the day the call SETTLES in: a call that started before midnight
        and finished after it is spend of the new day, not a free ride on the old one.

        -> the new bound factor (thousandths) when this settlement exceeded its reservation,
        else None. In that case, in this same transaction, every hold still in flight is
        widened by the observed ratio and so is every later reservation: nothing is admitted
        on a bound that has just been shown to be wrong."""
        with self._tx() as db:
            row = db.execute("SELECT reserved_nano FROM jev_calls WHERE id = ?", (call_id,)).fetchone()
            db.execute("UPDATE jev_calls SET day = ?, status = ?, cost_nano = ?, input_tokens = ?, "
                       "output_tokens = ?, served_by = 'jev', detail = ? WHERE id = ?",
                       (self.day(), status, cost_nano, input_tokens, output_tokens, detail[:300], call_id))
            reserved = row[0] if row else 0
            if not reserved or cost_nano <= reserved:
                return None
            ratio = -(-cost_nano * 1000 // reserved) + 100          # thousandths, +10% margin
            cur = db.execute("SELECT v FROM jev_meta WHERE k = 'bound_factor'").fetchone()
            factor = -(-max(1000, cur[0] if cur else 1000) * ratio // 1000)
            db.execute("INSERT INTO jev_meta(k, v) VALUES ('bound_factor', ?) "
                       "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (factor,))
            db.execute("UPDATE jev_calls SET reserved_nano = (reserved_nano * ? + 999) / 1000 "
                       "WHERE status = 'RESERVED'", (ratio,))
            return factor

    def blocked(self, caller: str, worst_nano: int, request_bytes: int, served_by: str,
                detail: str) -> bool:
        """Record a call the cap refused. True when it is the first block of the day."""
        day, at = self.day(), self.now().isoformat(timespec="seconds")
        with self._tx() as db:
            db.execute("INSERT INTO jev_calls(day, at, caller, status, reserved_nano, cost_nano, "
                       "request_bytes, served_by, detail) VALUES (?, ?, ?, 'BLOCKED', 0, 0, ?, ?, ?)",
                       (day, at, caller, request_bytes, served_by, detail[:300]))
            first = db.execute("INSERT OR IGNORE INTO jev_alerts(day, at, detail) VALUES (?, ?, ?)",
                               (day, at, detail[:300])).rowcount == 1
        return first

    def status(self, cap_nano: int) -> dict:
        day = self.day()
        db = self._conn()
        committed = self._committed(db, day)
        held = db.execute("SELECT COALESCE(SUM(reserved_nano), 0) FROM jev_calls "
                          "WHERE status = 'RESERVED'").fetchone()[0]
        r = db.execute(
            "SELECT 0, 0, "
            "COALESCE(SUM(status IN ('OK', 'UNKNOWN_BILLING', 'NOT_BILLED', 'RESERVED')), 0), "
            "COALESCE(SUM(status = 'BLOCKED'), 0), COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(status = 'UNKNOWN_BILLING'), 0) FROM jev_calls WHERE day = ?",
            (day,)).fetchone()
        alert = db.execute("SELECT at FROM jev_alerts WHERE day = ?", (day,)).fetchone()
        return {"day": day, "cap_usd": usd(cap_nano), "spent_usd": usd(committed),
                "remaining_usd": usd(max(cap_nano - committed, 0)), "in_flight_usd": usd(held),
                "paid_calls": r[2], "blocked_calls": r[3], "input_tokens": r[4],
                "unknown_billing_calls": r[5], "cap_tripped_at": alert[0] if alert else None,
                "resets_in_s": self.seconds_to_reset(), "ledger": str(self.path)}


class HttpStatus(RuntimeError):
    """The endpoint answered with an HTTP error status. Billed or not depends on the status."""

    def __init__(self, code: int):
        super().__init__("HTTP %d" % code)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):                 # a redirect is an error, never followed
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post(url: str, key: str | None, body: dict, timeout: float) -> dict:
    """POST JSON. Never follows a redirect: the key and the request go to `url` or nowhere."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    r = urllib.request.Request(url, data=encode_body(body), method="POST", headers=headers)
    try:
        with _OPENER.open(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HttpStatus(exc.code) from None


def laya_fallback() -> Callable[[dict, float], dict] | None:
    """Laya (Apache-2.0, Jev-compatible primitives) in-process: zero API cost. None if absent."""
    try:
        import laya                                             # noqa: PLC0415 -- optional
    except ImportError:
        return None
    lock, box = threading.Lock(), {}

    def call(body: dict, timeout: float) -> dict:
        with lock:
            if "agent" not in box:
                box["agent"] = laya.load(os.environ.get("LAYA_MODEL", "convaiinnovations/laya"),
                                         subfolder=os.environ.get("LAYA_SUBFOLDER", "typed-decisions") or None)
            return box["agent"].predict(body.get("state"), body.get("questions") or {})
    return call


def url_fallback(url: str) -> Callable[[dict, float], dict] | None:
    """A Jev-compatible endpoint ON THIS MACHINE. Anything else could be a paid service that
    the cap does not see, so only a LITERAL loopback IP is accepted (a name such as
    `localhost` can be re-pointed by a hosts file or DNS), and `_post` follows no redirect."""
    if not url:
        return None
    u = urllib.parse.urlsplit(url)
    try:
        loopback = ipaddress.ip_address(u.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if u.scheme not in ("http", "https") or not loopback:
        raise ValueError("fallback_url must be http(s) to a literal loopback IP (127.0.0.1 or "
                         "[::1]): a fallback has to be free and local")
    return lambda body, timeout: _post(url, None, body, timeout)


@dataclass
class Served:
    response: dict
    served_by: str                 # "jev" | "fallback:laya" | "fallback:url"
    cost_usd: str                  # exact, "0" for a fallback
    blocked: bool                  # True when the cap refused the paid call
    status: dict

    def public(self) -> dict:
        return {"served_by": self.served_by, "cost_usd": self.cost_usd, "blocked": self.blocked,
                **{k: self.status[k] for k in ("day", "cap_usd", "spent_usd", "remaining_usd")}}


class Guard:
    """The only door to the paid Jev API. `call` either pays within the cap, or does not pay."""

    def __init__(self, cfg: GuardConfig, get_key: Callable[[], str], *,
                 post: Callable[[str, dict, float], dict] | None = None,
                 fallback: Callable[[dict, float], dict] | None | str = "auto",
                 now: Callable[[], datetime] | None = None,
                 alert: Callable[[dict], None] | None = None):
        self.cfg = cfg
        self.get_key = get_key
        self.ledger = Ledger(cfg.ledger, cfg.timezone, now)
        self._post = post or (lambda key, body, timeout: _post(API, key, body, timeout))
        if fallback == "auto":
            fallback = (laya_fallback() if cfg.fallback == "laya"
                        else url_fallback(cfg.fallback_url) if cfg.fallback == "url" else None)
        self.fallback = fallback
        self.fallback_name = "fallback:" + (cfg.fallback if cfg.fallback != "defer" else "custom")
        self._alert = alert or self._default_alert

    def status(self) -> dict:
        s = self.ledger.status(self.cfg.cap_nano)
        s["price"] = "$%s/Mtok in, $%s/Mtok out" % (self.cfg.usd_per_mtok_in, self.cfg.usd_per_mtok_out)
        s["fallback"] = self.fallback_name if self.fallback else "none (blocked calls are deferred)"
        return s

    def _default_alert(self, event: dict) -> None:
        log.error("JEV GUARD ALERT %s: %s", event.get("event"), json.dumps(event))
        try:
            with open(self.cfg.ledger.with_name("jev_guard_alerts.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
        except OSError as exc:
            log.error("could not write the cap alert file: %s", type(exc).__name__)
        if self.cfg.alert_url:
            try:
                _post(self.cfg.alert_url, None, event, 5.0)
            except Exception as exc:                            # an alert never breaks a call
                log.error("cap alert webhook failed: %s", type(exc).__name__)

    def call(self, body: dict, *, caller: str = "unknown", timeout: float = 20.0) -> Served:
        nbytes = request_bytes(body)                            # refuses an unboundable request
        worst = -(-self.cfg.worst_case_nano(body) * self.ledger.bound_factor() // 1000)
        timeout = min(float(timeout), MAX_TIMEOUT_S)            # no call outlives its hold's TTL
        call_id, spent = self.ledger.reserve(caller, worst, self.cfg.cap_nano, nbytes)
        if call_id is None:
            return self._blocked(body, caller, worst, nbytes, spent, timeout)
        # From here every path settles the reservation: nothing sent -> NOT_BILLED; sent and
        # anything but a clean answer with usage -> charged the worst case.
        sent = False
        try:
            key = self.get_key()
            if not key:
                raise RuntimeError("no TypeSafe key readable (by name)")
            sent = True
            resp = self._deadline(lambda: self._post(key, body, timeout), timeout)
        except BaseException as exc:
            free = not sent or (isinstance(exc, HttpStatus) and exc.code in NOT_BILLED_STATUSES)
            self.ledger.settle(call_id, "NOT_BILLED" if free else "UNKNOWN_BILLING",
                               0 if free else worst, None, None,
                               str(exc) if isinstance(exc, HttpStatus) else type(exc).__name__)
            raise
        u = resp.get("usage") if isinstance(resp, dict) else None
        tin, tout = (u.get("input_tokens"), u.get("output_tokens")) if isinstance(u, dict) else (None, None)
        if all(type(t) is int and t >= 0 for t in (tin, tout)):
            cost = self.cfg.cost_nano(tin, tout)
            factor = self.ledger.settle(call_id, "OK", cost, tin, tout)
            if factor:              # the provider billed past the bound: widened for good, loudly
                self._alert({"event": "jev_reservation_exceeded", "caller": caller,
                             "reserved_usd": usd(worst), "cost_usd": usd(cost),
                             "request_bytes": nbytes, "input_tokens": tin, "output_tokens": tout,
                             "reservations_now_scaled_by": factor / 1000})
        else:                                                   # answered, usage absent or malformed
            cost = worst
            self.ledger.settle(call_id, "UNKNOWN_BILLING", worst, None, None, "no usable usage in response")
        return Served(resp, "jev", usd(cost), False, self.ledger.status(self.cfg.cap_nano))

    @staticmethod
    def _deadline(fn: Callable[[], dict], seconds: float) -> dict:
        """Run the transport under a hard wall-clock deadline. A socket timeout is only an
        inactivity timeout (a server can drip bytes for ever) and an injected transport can
        ignore it; this cannot be outlived. Past the deadline the caller raises TimeoutError,
        which charges the worst case; whatever the transport returns later is discarded."""
        box: dict = {}

        def run():
            try:
                box["ok"] = fn()
            except BaseException as exc:                        # noqa: BLE001 -- re-raised below
                box["err"] = exc
        t = threading.Thread(target=run, daemon=True, name="jev-call")
        t.start()
        t.join(seconds)
        if t.is_alive():
            raise TimeoutError("Jev call passed its %.0fs wall-clock deadline" % seconds)
        if "err" in box:
            raise box["err"]
        return box["ok"]

    def _blocked(self, body, caller, worst, nbytes, spent, timeout) -> Served:
        why = ("daily Jev cap $%s reached: $%s committed today, this call could cost up to $%s"
               % (self.cfg.cap_usd, usd(spent), usd(worst)))
        served_by = self.fallback_name if self.fallback else "deferred"
        if self.ledger.blocked(caller, worst, nbytes, served_by, why):
            self._alert({"event": "jev_daily_cap_tripped", "at": self.ledger.now().isoformat(timespec="seconds"),
                         "caller": caller, "detail": why, "action": served_by,
                         **self.ledger.status(self.cfg.cap_nano)})
        st = self.ledger.status(self.cfg.cap_nano)
        if not self.fallback:
            raise CapReached(why + "; no free fallback configured, call deferred",
                             self.ledger.seconds_to_reset(), st)
        try:
            resp = self.fallback(body, timeout)
        except Exception as exc:
            raise CapReached("%s; free fallback failed (%s), call deferred" % (why, type(exc).__name__),
                             self.ledger.seconds_to_reset(), st) from exc
        return Served(resp, self.fallback_name, "0", True, st)
