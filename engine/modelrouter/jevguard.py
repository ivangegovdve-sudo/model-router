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
upper bound (one input token per UTF-8 byte of the request: a token is never shorter than a
byte), so settled + in-flight spend can never exceed the cap -- across threads and across
processes sharing the ledger file. A crash between reserve and settle leaves the reservation
standing: a crash cannot buy calls. A call whose billing is unknown (timeout, no `usage`)
is charged its reservation, never $0.

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

import json
import logging
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("modelrouter.jevguard")

API = "https://api.typesafe.ai/v1/systemone"
PAID_HOST = "api.typesafe.ai"
NANO = Decimal(10) ** 9
# Upper bound on output tokens per question, used only if an output price is ever configured.
# Measured 2026-10-01: 47,345 output tokens over 2,664 questions = 18 per question.
OUT_TOKENS_PER_QUESTION = 64


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
        """What this request can cost at most: a token is at least one byte of the request."""
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        nq = len(body.get("questions") or {}) if isinstance(body.get("questions"), dict) else 1
        return self.cost_nano(len(raw), OUT_TOKENS_PER_QUESTION * max(nq, 1))


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
                request_bytes INTEGER NOT NULL, served_by TEXT, detail TEXT)""")
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

    @staticmethod
    def _committed(db, day: str) -> int:
        return db.execute("SELECT COALESCE(SUM(COALESCE(cost_nano, reserved_nano)), 0) "
                          "FROM jev_calls WHERE day = ?", (day,)).fetchone()[0]

    def reserve(self, caller: str, worst_nano: int, cap_nano: int, request_bytes: int):
        """-> (call id, None) when it fits under the cap, else (None, committed nano-USD)."""
        day = self.day()
        with self._tx() as db:
            spent = self._committed(db, day)
            if spent + worst_nano > cap_nano:
                return None, spent
            cur = db.execute(
                "INSERT INTO jev_calls(day, at, caller, status, reserved_nano, request_bytes) "
                "VALUES (?, ?, ?, 'RESERVED', ?, ?)",
                (day, self.now().isoformat(timespec="seconds"), caller, worst_nano, request_bytes))
            return cur.lastrowid, None

    def settle(self, call_id: int, status: str, cost_nano: int, input_tokens: int | None,
               output_tokens: int | None, detail: str = "") -> None:
        with self._tx() as db:
            db.execute("UPDATE jev_calls SET status = ?, cost_nano = ?, input_tokens = ?, "
                       "output_tokens = ?, served_by = 'jev', detail = ? WHERE id = ?",
                       (status, cost_nano, input_tokens, output_tokens, detail[:300], call_id))

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
        r = db.execute(
            "SELECT COALESCE(SUM(COALESCE(cost_nano, reserved_nano)), 0), "
            "COALESCE(SUM(CASE WHEN status = 'RESERVED' THEN reserved_nano END), 0), "
            "COALESCE(SUM(status IN ('OK', 'UNKNOWN_BILLING', 'NOT_BILLED', 'RESERVED')), 0), "
            "COALESCE(SUM(status = 'BLOCKED'), 0), COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(status = 'UNKNOWN_BILLING'), 0) FROM jev_calls WHERE day = ?",
            (day,)).fetchone()
        alert = db.execute("SELECT at FROM jev_alerts WHERE day = ?", (day,)).fetchone()
        return {"day": day, "cap_usd": usd(cap_nano), "spent_usd": usd(r[0]),
                "remaining_usd": usd(max(cap_nano - r[0], 0)), "in_flight_usd": usd(r[1]),
                "paid_calls": r[2], "blocked_calls": r[3], "input_tokens": r[4],
                "unknown_billing_calls": r[5], "cap_tripped_at": alert[0] if alert else None,
                "resets_in_s": self.seconds_to_reset(), "ledger": str(self.path)}


class HttpStatus(RuntimeError):
    """The paid endpoint answered with an HTTP error status: no inference, nothing billed."""

    def __init__(self, code: int):
        super().__init__("HTTP %d" % code)
        self.code = code


def _post(url: str, key: str | None, body: dict, timeout: float) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    r = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
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
    if not url:
        return None
    if PAID_HOST in url.lower():
        raise ValueError("fallback_url points at the paid TypeSafe API; a fallback must be free")
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
        log.error("JEV DAILY CAP TRIPPED: %s", json.dumps(event))
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
        worst = self.cfg.worst_case_nano(body)
        nbytes = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        call_id, spent = self.ledger.reserve(caller, worst, self.cfg.cap_nano, nbytes)
        if call_id is None:
            return self._blocked(body, caller, worst, nbytes, spent, timeout)
        key = self.get_key()
        if not key:
            self.ledger.settle(call_id, "NOT_BILLED", 0, None, None, "no TypeSafe key readable")
            raise RuntimeError("no TypeSafe key readable (by name)")
        try:
            resp = self._post(key, body, timeout)
        except HttpStatus as exc:                               # refused before inference
            self.ledger.settle(call_id, "NOT_BILLED", 0, None, None, str(exc))
            raise
        except BaseException as exc:                            # timeout / reset: may be billed
            self.ledger.settle(call_id, "UNKNOWN_BILLING", worst, None, None, type(exc).__name__)
            raise
        u = resp.get("usage") if isinstance(resp, dict) else None
        tin, tout = (u or {}).get("input_tokens"), (u or {}).get("output_tokens")
        if isinstance(tin, int) and isinstance(tout, int) and tin >= 0 and tout >= 0:
            cost = self.cfg.cost_nano(tin, tout)
            self.ledger.settle(call_id, "OK", cost, tin, tout)
        else:                                                   # answered, usage absent: worst case
            cost = worst
            self.ledger.settle(call_id, "UNKNOWN_BILLING", worst, None, None, "no usage in response")
        return Served(resp, "jev", usd(cost), False, self.ledger.status(self.cfg.cap_nano))

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
