"""Caller keys: what lets someone who is not the operator make real calls safely.

The operator's router token can do everything -- probes that spend money, every caller's
decision records, uncapped spend on the operator's provider keys. A caller key can do
three things: chat completions, list models, and read its own usage. Each has a hard USD
spend cap enforced BEFORE a call is made, and is charged the call's actual cost after.

  - The key value is shown to no one by this module. It is generated, handed straight to
    the operator's secret store (GCP Secret Manager or a file the operator names), and
    only its SHA-256 is kept here.
  - A key looks like  mr_<id>_<secret>. The id is not secret; it selects the row. The
    whole key is compared by hash in constant time.
  - Spend is Decimal, persisted as exact strings. A call whose cost cannot be established
    is charged its reservation (the worst case), never zero.
  - Before a call, the worst case is reserved: two attempts x (prompt + max_tokens + the
    most a budget raise may add) x the key's price ceiling. A call that could overrun
    the cap is refused (HTTP 402) instead of being allowed to.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS client_keys (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT NOT NULL,
  cap_usd TEXT NOT NULL, spent_usd TEXT NOT NULL DEFAULT '0', calls INTEGER NOT NULL DEFAULT 0,
  max_usd_per_mtok TEXT NOT NULL, rpm INTEGER NOT NULL, default_max_tokens INTEGER NOT NULL,
  created REAL NOT NULL, revoked REAL, note TEXT);
CREATE TABLE IF NOT EXISTS client_charges (
  id INTEGER PRIMARY KEY, key_id TEXT NOT NULL, t REAL NOT NULL, decision_id TEXT,
  usd TEXT NOT NULL, basis TEXT NOT NULL);
"""


@dataclass(frozen=True)
class ClientKey:
    id: str
    name: str
    cap_usd: Decimal
    spent_usd: Decimal
    calls: int
    max_usd_per_mtok: Decimal
    rpm: int
    default_max_tokens: int
    created: float
    revoked: float | None

    @property
    def remaining(self) -> Decimal:
        return max(Decimal(0), self.cap_usd - self.spent_usd)

    def public(self) -> dict:
        """Everything but the hash -- safe to show the operator or the key's own holder."""
        return {"id": self.id, "name": self.name, "cap_usd": str(self.cap_usd),
                "spent_usd": str(self.spent_usd), "remaining_usd": str(self.remaining),
                "calls": self.calls, "max_usd_per_mtok": str(self.max_usd_per_mtok),
                "rpm": self.rpm, "default_max_tokens": self.default_max_tokens,
                "created": self.created, "revoked": self.revoked}


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Refusal(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ClientKeys:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._reserved: dict[str, Decimal] = {}          # key id -> in-flight worst cases
        self._recent: dict[str, deque] = {}              # key id -> request times (rpm)

    # --- operator side --------------------------------------------------------------------
    def create(self, name: str, cap_usd: Decimal, *, max_usd_per_mtok: Decimal = Decimal(5),
               rpm: int = 60, default_max_tokens: int = 1024, note: str = "") -> tuple[str, ClientKey]:
        """Returns (key value, record). The caller must hand the value to a secret store
        and drop it; this module never persists or prints it."""
        if cap_usd <= 0:
            raise ValueError("cap_usd must be positive")
        kid = secrets.token_hex(4)
        value = "mr_%s_%s" % (kid, secrets.token_urlsafe(32))
        with self._lock:
            self._db.execute(
                "INSERT INTO client_keys (id,name,key_hash,cap_usd,spent_usd,calls,"
                "max_usd_per_mtok,rpm,default_max_tokens,created,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (kid, name, _hash(value), str(cap_usd), "0", 0, str(max_usd_per_mtok), rpm,
                 default_max_tokens, time.time(), note))
            self._db.commit()
        return value, self.get(kid)

    def get(self, kid: str) -> ClientKey | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM client_keys WHERE id=?", (kid,)).fetchone()
        if not r:
            return None
        return ClientKey(r["id"], r["name"], Decimal(r["cap_usd"]), Decimal(r["spent_usd"]),
                         r["calls"], Decimal(r["max_usd_per_mtok"]), r["rpm"],
                         r["default_max_tokens"], r["created"], r["revoked"])

    def list(self) -> list[ClientKey]:
        with self._lock:
            ids = [r["id"] for r in self._db.execute("SELECT id FROM client_keys ORDER BY created")]
        return [self.get(i) for i in ids]

    def revoke(self, kid: str) -> bool:
        with self._lock:
            n = self._db.execute("UPDATE client_keys SET revoked=? WHERE id=? AND revoked IS NULL",
                                 (time.time(), kid)).rowcount
            self._db.commit()
        return n == 1

    def set_cap(self, kid: str, cap_usd: Decimal) -> bool:
        with self._lock:
            n = self._db.execute("UPDATE client_keys SET cap_usd=? WHERE id=?",
                                 (str(cap_usd), kid)).rowcount
            self._db.commit()
        return n == 1

    def charges(self, kid: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT t,decision_id,usd,basis FROM client_charges "
                                    "WHERE key_id=? ORDER BY t DESC LIMIT ?", (kid, limit)).fetchall()
        return [dict(r) for r in rows]

    # --- request side -------------------------------------------------------------------
    def authenticate(self, presented: str) -> ClientKey | None:
        if not presented.startswith("mr_") or presented.count("_") < 2:
            return None
        kid = presented.split("_", 2)[1]
        with self._lock:
            r = self._db.execute("SELECT key_hash FROM client_keys WHERE id=?", (kid,)).fetchone()
        if not r or not secrets.compare_digest(r["key_hash"], _hash(presented)):
            return None
        return self.get(kid)

    def reserve(self, key: ClientKey, worst_case: Decimal) -> Decimal:
        """Admit one call or refuse it. Returns the amount reserved."""
        if key.revoked:
            raise Refusal(401, "key_revoked", "this key has been revoked")
        now = time.time()
        with self._lock:
            q = self._recent.setdefault(key.id, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= key.rpm:
                raise Refusal(429, "key_rate_limited",
                              "this key allows %d requests per minute" % key.rpm)
            r = self._db.execute("SELECT spent_usd, cap_usd FROM client_keys WHERE id=?",
                                 (key.id,)).fetchone()
            spent, cap = Decimal(r["spent_usd"]), Decimal(r["cap_usd"])
            held = self._reserved.get(key.id, Decimal(0))
            if spent + held + worst_case > cap:
                raise Refusal(402, "spend_cap_reached",
                              "this call could cost up to $%s; the key has $%s left of its $%s cap"
                              " ($%s held by calls in flight)" % (
                                  _fmt(worst_case), _fmt(max(Decimal(0), cap - spent - held)),
                                  _fmt(cap), _fmt(held)))
            self._reserved[key.id] = held + worst_case
            q.append(now)
        return worst_case

    def settle(self, key: ClientKey, reserved: Decimal, cost: Decimal | None,
               decision_id: str | None, basis: str) -> Decimal:
        """Release the reservation and charge what the call cost. Unknown cost is charged
        at the reservation -- the worst case -- never at zero."""
        charge = cost if cost is not None else reserved
        if cost is None:
            basis = "reserved worst case (cost unknown)"
        with self._lock:
            self._reserved[key.id] = max(Decimal(0), self._reserved.get(key.id, Decimal(0)) - reserved)
            r = self._db.execute("SELECT spent_usd FROM client_keys WHERE id=?", (key.id,)).fetchone()
            spent = Decimal(r["spent_usd"]) + charge
            self._db.execute("UPDATE client_keys SET spent_usd=?, calls=calls+1 WHERE id=?",
                             (str(spent), key.id))
            self._db.execute("INSERT INTO client_charges (key_id,t,decision_id,usd,basis) "
                             "VALUES (?,?,?,?,?)", (key.id, time.time(), decision_id, str(charge),
                                                    basis))
            self._db.commit()
        return charge

    def release(self, key: ClientKey, reserved: Decimal) -> None:
        """A call that was never made (refused before any provider was called) costs nothing."""
        with self._lock:
            self._reserved[key.id] = max(Decimal(0), self._reserved.get(key.id, Decimal(0)) - reserved)


def worst_case(prompt_tokens: int, max_tokens: int, clamp_extra: int, attempts: int,
               ceiling_usd_per_mtok: Decimal) -> Decimal:
    """Upper bound on what one routed call can cost: every attempt at the key's price
    ceiling, with the budget raised as far as the router is allowed to raise it."""
    tokens = attempts * (prompt_tokens + max_tokens + clamp_extra)
    return Decimal(tokens) * ceiling_usd_per_mtok / Decimal(1_000_000)


def _fmt(d: Decimal) -> str:
    return format(d.quantize(Decimal("0.000001")), "f")
