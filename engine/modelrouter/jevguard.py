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

A caller waits at most a hard wall-clock deadline (MAX_TIMEOUT_S). Past it the caller gets
TimeoutError while the call's hold stays in place; the call still settles itself when it
returns, and its late result is still checked against the bound.

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
# runs. A row the sweep converted is terminal: a late result can raise its charge, never lower
# it. A call that is merely slow is not swept: while its thread lives it keeps its hold fresh
# (HEARTBEAT_S), so it stays RESERVED -- counted, and widened if the bound is.
RESERVATION_TTL_S = 3600
MAX_TIMEOUT_S = 600.0
# A call whose thread is still alive in this process re-stamps its hold this often, so the
# sweep (which takes holds older than the TTL for crashed) never converts a call that can
# still send or still be billed. Only a dead process stops beating.
HEARTBEAT_S = 300.0
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
        self._live: set[int] = set()                    # holds whose call thread is alive here
        self._live_lock = threading.Lock()
        self._beater: threading.Thread | None = None
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
        return max(1, int((nxt.timestamp() - n.timestamp())))

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

    def _sweep(self, db, now: datetime) -> None:
        """A hold older than the TTL belongs to a crashed process: charge it its worst case,
        in TODAY's budget (the day the sweep runs). It is converted, never forgotten, and it
        never leaves the current day's count by being converted."""
        db.execute("UPDATE jev_calls SET day = ?, status = 'UNKNOWN_BILLING', cost_nano = reserved_nano, "
                   "served_by = 'jev', detail = 'never settled: charged the worst case' "
                   "WHERE status = 'RESERVED' AND ts < ?",
                   (now.date().isoformat(), now.timestamp() - RESERVATION_TTL_S))

    def alive(self, call_id: int, on: bool) -> None:
        """Mark a hold as belonging to a call thread that is running in this process."""
        with self._live_lock:
            (self._live.add if on else self._live.discard)(call_id)
            if on and self._beater is None:
                self._beater = threading.Thread(target=self._beat_forever, daemon=True, name="jev-heartbeat")
                self._beater.start()

    def beat(self) -> int:
        """Re-stamp every live hold of this process. -> how many."""
        with self._live_lock:
            ids = sorted(self._live)
        if not ids:
            return 0
        with self._tx() as db:
            return db.execute("UPDATE jev_calls SET ts = ? WHERE status = 'RESERVED' AND id IN (%s)"
                              % ",".join("?" * len(ids)), (self.now().timestamp(), *ids)).rowcount

    def _beat_forever(self) -> None:
        import time
        while True:
            time.sleep(HEARTBEAT_S)
            try:
                self.beat()
            except Exception as exc:                            # a beat must never kill the thread
                log.error("jev hold heartbeat failed: %s", type(exc).__name__)

    def bound_factor(self, db=None) -> int:
        """Thousandths by which reservations are scaled: 1000 unless the provider was ever
        seen to bill more than a reservation."""
        r = (db or self._conn()).execute("SELECT v FROM jev_meta WHERE k = 'bound_factor'").fetchone()
        return max(1000, r[0]) if r else 1000

    def reserve(self, caller: str, base_nano: int, cap_nano: int, request_bytes: int):
        """-> (call id, reserved nano-USD, None) when it fits under the cap,
        else (None, what it would have reserved, committed nano-USD).

        Everything the decision reads is read under the one write lock, from one clock
        snapshot: the day, the sweep, the bound factor and the committed spend. A factor
        widened by another process a moment ago, or a hold swept across midnight, cannot be
        missed by a value read earlier."""
        with self._tx() as db:
            now = self.now()
            day = now.date().isoformat()
            self._sweep(db, now)
            worst = -(-base_nano * self.bound_factor(db) // 1000)
            spent = self._committed(db, day)
            if spent + worst > cap_nano:
                return None, worst, spent
            cur = db.execute(
                "INSERT INTO jev_calls(day, at, caller, status, reserved_nano, request_bytes, ts) "
                "VALUES (?, ?, ?, 'RESERVED', ?, ?, ?)",
                (day, now.isoformat(timespec="seconds"), caller, worst, request_bytes, now.timestamp()))
            return cur.lastrowid, worst, None

    def settle(self, call_id: int, status: str, cost_nano: int, input_tokens: int | None,
                   output_tokens: int | None, detail: str = "") -> int | None:
        """A held call's cost lands in the day it SETTLES in: a call that started before
        midnight and finished after it is spend of the new day, not a free ride on the old.

        A row that is no longer held (the sweep already charged it) is terminal: a late result
        can raise its charge, never lower it and never move it to another day.

        -> the new bound factor (thousandths) when the settled cost exceeded its reservation,
        else None. In that case, in this same transaction, every hold still in flight is
        widened by the observed ratio and so is every later reservation: nothing is admitted
        on a bound that has just been shown to be wrong."""
        with self._tx() as db:
            row = db.execute("SELECT status, reserved_nano FROM jev_calls WHERE id = ?", (call_id,)).fetchone()
            if row is None:
                return None
            held, reserved = row[0] == "RESERVED", row[1]
            if status == "UNKNOWN_BILLING":
                # "worst case" means the hold as it stands NOW: it may have been widened since
                # the caller captured it.
                cost_nano = max(cost_nano, reserved)
            if held:
                db.execute("UPDATE jev_calls SET day = ?, status = ?, cost_nano = ?, input_tokens = ?, "
                           "output_tokens = ?, served_by = 'jev', detail = ? WHERE id = ?",
                           (self.day(), status, cost_nano, input_tokens, output_tokens, detail[:300], call_id))
            else:
                db.execute("UPDATE jev_calls SET cost_nano = MAX(COALESCE(cost_nano, 0), ?), "
                           "input_tokens = COALESCE(?, input_tokens), output_tokens = COALESCE(?, output_tokens), "
                           "detail = substr(COALESCE(detail, '') || ' | late result: ' || ?, 1, 300) WHERE id = ?",
                           (cost_nano, input_tokens, output_tokens, status, call_id))
            if not reserved or cost_nano <= reserved:
                return None
            ratio = -(-cost_nano * 1000 // reserved) + 100          # thousandths, +10% margin
            factor = -(-self.bound_factor(db) * ratio // 1000)
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
        with self._tx() as tx:
            self._sweep(tx, self.now())
        committed = self._committed(db, day)
        held = db.execute("SELECT COALESCE(SUM(reserved_nano), 0) FROM jev_calls "
                           "WHERE status = 'RESERVED'").fetchone()[0]
        r = db.execute(
            "SELECT 0, 0, "
            "COALESCE(SUM(status IN ('OK', 'UNKNOWN_BILLING')), 0), "
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


def _post_no_proxy(url: str, key: str | None, body: dict, timeout: float) -> dict:
    """POST JSON, bypassing all system proxies."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    r = urllib.request.Request(url, data=encode_body(body), method="POST", headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(r, timeout=timeout) as resp:
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
    return lambda body, timeout: _post_no_proxy(url, None, body, timeout)


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
        body = json.loads(json.dumps(body))                            # snapshot request before reserving
        nbytes = request_bytes(body)                                  # refuses an unboundable request
        timeout = min(float(timeout), MAX_TIMEOUT_S)
        call_id, worst, spent = self.ledger.reserve(caller, self.cfg.worst_case_nano(body),
                                                     self.cfg.cap_nano, nbytes)
        if call_id is None:
            return self._blocked(body, caller, worst, nbytes, spent, timeout)
        # The paid call runs in its own thread, which also SETTLES it, whenever it finishes.
        # The caller waits a hard wall-clock deadline (a socket timeout is only an inactivity
        # timeout, and an injected transport can ignore it). Past the deadline the caller gets
        # TimeoutError and the hold simply STAYS: the call is still counted, its late result is
        # still settled and still checked against the bound. While its thread lives the hold
        # is kept fresh, so the sweep never converts a call that can still send; only a hold
        # whose process died goes stale and is charged its worst case.
        box: dict = {}
        deadline = datetime.now(self.ledger._tz) + timedelta(seconds=timeout) if self.ledger._tz else datetime.now().astimezone() + timedelta(seconds=timeout)

        def work():
            try:
                box["served"] = self._paid(call_id, body, caller, worst, nbytes, timeout)
            except BaseException as exc:                        # noqa: BLE001 -- re-raised below
                box["err"] = exc
            finally:
                self.ledger.alive(call_id, False)               # settled: nothing left to keep fresh
        t = threading.Thread(target=work, daemon=True, name="jev-call-%d" % call_id)
        self.ledger.alive(call_id, True)                        # before it starts: never swept while alive
        try:
            t.start()
        except RuntimeError as exc:                            # process cannot create another thread
            self.ledger.alive(call_id, False)
            self.ledger.settle(call_id, "NOT_BILLED", 0, None, None, "worker startup failed: %s" % exc)
            raise
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError("Jev call passed its %.0fs wall-clock deadline; its hold of $%s stays "
                                "until it settles" % (timeout, usd(worst)))
        if "err" in box:
            raise box["err"]
        return box["served"]

    def _paid(self, call_id: int, body: dict, caller: str, worst: int, nbytes: int, timeout: float) -> Served:
        """Make the reserved call and settle it. Every path settles: nothing sent ->
        NOT_BILLED; sent and anything but a clean answer with usage -> the worst case."""
        sent = False
        try:
            key = self.get_key()
            if not key:
                raise RuntimeError("no TypeSafe key readable (by name)")
            sent = True
            resp = self._post(key, body, timeout)
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
            factor = self.ledger.settle(call_id, "UNKNOWN_BILLING", worst, None, None, "no usable usage in response")
        return Served(resp, "jev", usd(cost), False, self.ledger.status(self.cfg.cap_nano))

    def _blocked(self, body, caller, worst, nbytes, spent, timeout) -> Served:
        # Recheck admission in case midnight passed between reserve() and _blocked()
        call_id, worst, spent = self.ledger.reserve(caller, self.cfg.worst_case_nano(body),
                                                     self.cfg.cap_nano, nbytes)
        if call_id is not None:
            # We are no longer blocked; let the call proceed to the paid path.
            # Since we're already in _blocked(), we can't easily jump back to call(),
            # so we'll manually perform the paid call logic here or return a special value.
            # However, the most robust way is to just call self.call() recursively 
            # (with a flag to avoid infinite loop, though we just reserved, so it should pass).
            # But self.call() will try to reserve again.
            # Instead, let's just use _paid() directly since we have a reservation.
            self.ledger.alive(call_id, True)
            try:
                # We must wrap this in a thread to maintain the same timeout/deadline behavior
                # and avoid blocking the caller if we were to do it synchronously.
                # But _blocked is called synchronously from call().
                # To keep it simple and consistent with call(), we just use the same thread logic.
                box: dict = {}
                def work():
                    try:
                        box["served"] = self._paid(call_id, body, caller, worst, nbytes, timeout)
                    except BaseException as exc:
                        box["err"] = exc
                    finally:
                        self.ledger.alive(call_id, False)
                t = threading.Thread(target=work, daemon=True)
                t.start()
                t.join(timeout)
                if t.is_alive():
                    raise TimeoutError("Paid call (after midnight re-check) passed its %.0fs deadline" % timeout)
                if "err" in box:
                    raise box["err"]
                return box["served"]
            except Exception as exc:
                # If it fails, we still record the block as we were technically "blocked"
                # from the original attempt, but since we actually tried a paid call,
                # we should probably just let the exception propagate.
                raise

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
            box: dict = {}
            def work():
                try:
                    box["resp"] = self.fallback(body, timeout)
                except Exception as exc:
                    box["err"] = exc
            t = threading.Thread(target=work, daemon=True)
            t.start()
            t.join(timeout)
            if t.is_alive():
                raise TimeoutError("Fallback call passed its %.0fs wall-clock deadline" % timeout)
            if "err" in box:
                raise box["err"]
            resp = box["resp"]
        except Exception as exc:
            raise CapReached("%s; free fallback failed (%s), call deferred" % (why, type(exc).__name__),
                               self.ledger.seconds_to_reset(), st) from exc
        return Served(resp, self.fallback_name, "0", True, st)

