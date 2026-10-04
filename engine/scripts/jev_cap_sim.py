"""Run: python scripts/jev_cap_sim.py   (spends nothing)

Real-layer check: the router served by uvicorn on a loopback socket, real HTTP in and out.

  client --HTTP--> router /v1/systemone --guard--> stub "TypeSafe" (HTTP, bills like the real one)
                                              \\--> stub local System-1 (HTTP, free) once the cap blocks

No real money: the paid upstream is a local stub that counts every request it receives and
returns `usage` the way the live API does (measured today: ~0.3-0.5 input tokens per byte).
Price and cap are the real defaults: $0.042/Mtok input, $1.00/day.
"""
import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modelrouter import config, jevguard, server  # noqa: E402

ANSWER = {"q": {"type": "choice", "choice": "a", "confidence": 0.9, "probabilities": {"a": 0.9, "b": 0.1}}}
hits = {"paid": 0, "free": 0, "paid_tokens": 0}


def stub(kind):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            hits[kind] += 1
            out = {"model": "jev-1.13.0" if kind == "paid" else "local-system1", "answers": ANSWER}
            if kind == "paid":
                text = json.dumps(json.loads(raw), ensure_ascii=False).encode("utf-8")
                tin = len(text) // 2                      # the live API billed ~0.3-0.5 tokens per byte
                hits["paid_tokens"] += tin
                out["usage"] = {"input_tokens": tin, "output_tokens": 18}
            b = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d/v1/systemone" % srv.server_address[1]


def main(fallback_mode):
    tmp = Path(tempfile.mkdtemp(prefix="jevsim-"))
    paid_url, free_url = stub("paid"), stub("free")
    (tmp / "c.toml").write_text(
        '[secrets]\nsource="env"\n[providers.openrouter]\nsecret="NOPE"\n[server]\nno_auth=true\n'
        '[policy]\ndashboard_url=""\n[local]\nollama_url="http://127.0.0.1:9"\n'
        '[jev]\nenabled=true\ndaily_cap_usd=1.00\n'
        + ('fallback="url"\nfallback_url="%s"\n' % free_url if fallback_mode == "url" else 'fallback="defer"\n'),
        encoding="utf-8")
    cfg = config.load(tmp / "c.toml")
    cfg.state_dir = tmp
    app = server.build(cfg)
    g = app.state.jev_guard                                # built from the config: real price, real cap
    # Only the paid upstream's address is swapped for the stub; the guard, ledger, cap, fallback
    # and alert are the production code paths.
    app.state.jev_guard = jevguard.Guard(g.cfg, lambda: "stub-key",
                                         post=lambda key, body, t: jevguard._post(paid_url, key, body, t))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    us = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    threading.Thread(target=lambda: us.run(sockets=[sock]), daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    # A kalo-sized request: 8 rows with a context window each, ~50 KB.
    body = {"model": "jev-latest", "state": {"rows": [{"claim": "c%d" % i, "context": "Текст на главата. " * 200}
                                                      for i in range(8)]},
            "questions": {"q": {"type": "choice", "instructions": "pick", "criteria": {"a": "A", "b": "B"}}}}
    raw = json.dumps(body).encode()

    def call():
        r = urllib.request.Request(base + "/v1/systemone", data=raw, method="POST",
                                   headers={"Content-Type": "application/json", "X-Jev-Caller": "kalo-canon-sim"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read()), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), dict(e.headers)

    print("== fallback = %s | cap $%s | price $%s/Mtok in | request %d bytes" % (
        fallback_mode, g.cfg.cap_usd, g.cfg.usd_per_mtok_in, len(raw)))
    n, first_block, last = 0, None, None
    while n < 5000:
        n += 1
        code, j, h = call()
        blocked = code == 429 or (j.get("guard") or {}).get("blocked")
        if not blocked:
            last = (n, code, j["guard"])
        if blocked and first_block is None:
            first_block = n
            print("last PAID call   #%d: HTTP %d served_by=%s cost=$%s spent=$%s remaining=$%s" % (
                last[0], last[1], last[2]["served_by"], last[2]["cost_usd"], last[2]["spent_usd"], last[2]["remaining_usd"]))
            if code == 429:
                print("first BLOCKED    #%d: HTTP 429 code=%s Retry-After=%ss\n   message: %s" % (
                    n, j["error"]["code"], h.get("retry-after") or h.get("Retry-After"), j["error"]["message"]))
            else:
                print("first BLOCKED    #%d: HTTP %d served_by=%s model=%s cost=$%s blocked=%s spent=$%s" % (
                    n, code, j["guard"]["served_by"], j["model"], j["guard"]["cost_usd"], j["guard"]["blocked"],
                    j["guard"]["spent_usd"]))
        if first_block and n >= first_block + 200:
            break
    paid_at_block = hits["paid"]
    spend = json.loads(urllib.request.urlopen(base + "/v1/systemone/spend").read())
    print("after 200 more calls past the cap: paid upstream received %d requests in total (%d before the block, "
          "%d after); free fallback served %d" % (hits["paid"], first_block - 1, paid_at_block - (first_block - 1), hits["free"]))
    print("upstream-billed: %d tokens x $0.042/Mtok = $%.6f   ledger says spent=$%s cap=$%s blocked_calls=%d" % (
        hits["paid_tokens"], hits["paid_tokens"] * 0.042 / 1e6, spend["spent_usd"], spend["cap_usd"], spend["blocked_calls"]))
    alerts = (tmp / "jev_guard_alerts.jsonl").read_text("utf-8").splitlines()
    a = json.loads(alerts[0])
    print("alerts written: %d -> %s at %s, caller=%s, action=%s" % (len(alerts), a["event"], a["at"], a["caller"], a["action"]))
    assert float(spend["spent_usd"]) <= 1.0 and hits["paid"] == first_block - 1 and len(alerts) == 1
    assert abs(hits["paid_tokens"] * 0.042 / 1e6 - float(spend["spent_usd"])) < 1e-6
    us.should_exit = True
    for k in hits:
        hits[k] = 0


main("url")
main("defer")
print("OK: the cap held at $1.00 in both modes; no paid request left the router after the block")
