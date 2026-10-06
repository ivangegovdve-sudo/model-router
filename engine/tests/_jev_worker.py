"""A separate PROCESS hammering the shared Jev ledger (used by test_jevguard).

argv: ledger path, cap, price, calls. Prints how many paid calls this process made.
The fake upstream bills the full reservation bound, the most a real request can cost.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modelrouter.jevguard import (QUESTION_OVERHEAD_TOKENS, REQUEST_OVERHEAD_TOKENS,  # noqa: E402
                                  CapReached, Guard, GuardConfig, request_bytes)

ledger, cap, price, calls = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
BODY = {"model": "jev-latest", "state": {"t": "x" * 400},
        "questions": {"q": {"type": "choice", "instructions": "pick", "criteria": {"a": "A", "b": "B"}}}}
paid = 0


def post(key, body, timeout):
    global paid
    paid += 1
    full = request_bytes(body) + REQUEST_OVERHEAD_TOKENS + QUESTION_OVERHEAD_TOKENS * len(body["questions"])
    return {"answers": {}, "usage": {"input_tokens": full, "output_tokens": 18}}


g = Guard(GuardConfig(cap_usd=cap, usd_per_mtok_in=price, ledger=Path(ledger), fallback="defer"),
          lambda: "k", post=post, fallback=None, alert=lambda e: None)
for _ in range(calls):
    try:
        g.call(BODY, caller="worker")
    except CapReached:
        pass
print(json.dumps({"paid": paid}))
