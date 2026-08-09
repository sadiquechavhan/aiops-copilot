#!/usr/bin/env python3
"""
Steady baseline traffic generator for the AIOps stack.

Standard library only, on purpose -- no `pip install` needed. Sessions 3 and
later bring real Python dependencies (drain3, scikit-learn, chromadb); this
script deliberately has none, so it works the moment Python is installed.

The point of this script is *boring, steady* load. Session 4 learns a normal
baseline from it and then flags departures from that baseline, which only works
if "normal" is genuinely stable.

Examples
--------
    python tools/traffic_gen.py --once
        Send exactly one request and print the response. This is what proves
        the Session 1 done condition in Jaeger.

    python tools/traffic_gen.py
        5 requests/second until Ctrl-C.

    python tools/traffic_gen.py --rate 20 --workers 8
        Heavier, still steady.
"""

import argparse
import json
import random
import signal
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque

SKUS = ["SKU-1", "SKU-2", "SKU-3", "SKU-4", "SKU-5"]


class Stats:
    """Thread-safe counters. A lock is overkill for ints in CPython, but the
    latency deque genuinely needs one."""

    def __init__(self):
        self._lock = threading.Lock()
        self.sent = 0
        self.ok = 0
        self.failed = 0
        self.status_counts = {}
        # Bounded: we only ever report on the recent window, and an unbounded
        # list would grow without limit over a multi-hour run.
        self.latencies_ms = deque(maxlen=2000)

    def record(self, status, elapsed_ms):
        with self._lock:
            self.sent += 1
            if isinstance(status, int) and 200 <= status < 400:
                self.ok += 1
            else:
                self.failed += 1
            key = str(status)
            self.status_counts[key] = self.status_counts.get(key, 0) + 1
            self.latencies_ms.append(elapsed_ms)

    def snapshot(self):
        with self._lock:
            lat = sorted(self.latencies_ms)
            return {
                "sent": self.sent,
                "ok": self.ok,
                "failed": self.failed,
                "statuses": dict(self.status_counts),
                "p50": _pct(lat, 0.50),
                "p95": _pct(lat, 0.95),
                "p99": _pct(lat, 0.99),
            }


def _pct(sorted_values, fraction):
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * fraction))
    return round(sorted_values[idx], 1)


def send_one(url, timeout):
    """POST one order. Returns (status, elapsed_ms, body_or_error)."""
    payload = json.dumps({
        "sku": random.choice(SKUS),
        "qty": random.randint(1, 3),
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, (time.perf_counter() - started) * 1000, body
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx is a real response, not a transport failure. Record the code.
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, (time.perf_counter() - started) * 1000, body
    except Exception as exc:
        # Connection refused, DNS failure, timeout: no status code exists.
        return type(exc).__name__, (time.perf_counter() - started) * 1000, str(exc)


def worker(url, per_worker_interval, timeout, stats, stop_event):
    while not stop_event.is_set():
        cycle_started = time.perf_counter()
        status, elapsed_ms, _ = send_one(url, timeout)
        stats.record(status, elapsed_ms)

        # Subtract the time the request itself took, so the achieved rate stays
        # near the target even as latency moves. Session 2's injected latency
        # will make this matter.
        remaining = per_worker_interval - (time.perf_counter() - cycle_started)
        if remaining > 0:
            stop_event.wait(remaining)


def main():
    parser = argparse.ArgumentParser(description="Baseline load generator for the AIOps stack.")
    parser.add_argument("--url", default="http://localhost:8080/api/orders",
                        help="gateway endpoint (default: %(default)s)")
    parser.add_argument("--rate", type=float, default=5.0,
                        help="target requests per second, aggregate (default: %(default)s)")
    parser.add_argument("--workers", type=int, default=4,
                        help="concurrent sender threads (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="per-request timeout in seconds (default: %(default)s)")
    parser.add_argument("--report-every", type=float, default=10.0,
                        help="seconds between summary lines (default: %(default)s)")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="stop after this many seconds (default: 0 = run until Ctrl-C). "
                             "Session 2's scenario runner needs bounded runs.")
    parser.add_argument("--once", action="store_true",
                        help="send a single request, print the response, exit")
    args = parser.parse_args()

    if args.once:
        status, elapsed_ms, body = send_one(args.url, args.timeout)
        print(f"status  : {status}")
        print(f"latency : {elapsed_ms:.0f} ms")
        print(f"body    : {body}")
        return 0 if isinstance(status, int) and 200 <= status < 400 else 1

    if args.rate <= 0 or args.workers <= 0:
        print("--rate and --workers must both be positive", file=sys.stderr)
        return 2

    per_worker_interval = args.workers / args.rate
    stats = Stats()
    stop_event = threading.Event()

    signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    print(f"target   : {args.url}")
    print(f"rate     : {args.rate} req/s across {args.workers} workers "
          f"({per_worker_interval:.2f}s per worker between requests)")
    print("Ctrl-C to stop.\n")

    threads = [
        threading.Thread(
            target=worker,
            args=(args.url, per_worker_interval, args.timeout, stats, stop_event),
            daemon=True,
        )
        for _ in range(args.workers)
    ]
    for thread in threads:
        thread.start()

    started = time.time()
    deadline = started + args.duration if args.duration > 0 else None
    last_sent = 0
    try:
        while not stop_event.is_set():
            stop_event.wait(args.report_every)
            if stop_event.is_set():
                break
            if deadline is not None and time.time() >= deadline:
                break
            snap = stats.snapshot()
            window_rate = (snap["sent"] - last_sent) / args.report_every
            last_sent = snap["sent"]
            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"sent={snap['sent']:<7} ok={snap['ok']:<7} failed={snap['failed']:<5} "
                f"rate={window_rate:5.1f}/s  "
                f"p50={snap['p50']:>6}ms p95={snap['p95']:>7}ms p99={snap['p99']:>7}ms  "
                f"{snap['statuses']}"
            )
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=2)

        snap = stats.snapshot()
        duration = time.time() - started
        print(f"\nstopped after {duration:.0f}s")
        print(f"  sent   : {snap['sent']}")
        print(f"  ok     : {snap['ok']}")
        print(f"  failed : {snap['failed']}")
        print(f"  status : {snap['statuses']}")
        print(f"  p50/p95/p99 : {snap['p50']} / {snap['p95']} / {snap['p99']} ms")

    return 0


if __name__ == "__main__":
    sys.exit(main())
