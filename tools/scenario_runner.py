#!/usr/bin/env python3
"""
Drives a labelled chaos scenario and writes the ground truth for it.

Standard library only, like traffic_gen.py, so it runs the moment Python is
installed. Session 4 brings real dependencies; this does not need them.

What one run does
-----------------
1. Preflight: every service reachable *from the host* (a container reporting
   "healthy" says nothing about the host port binding -- learned the hard way in
   Session 1), chaos present and inert everywhere, and the host clock agreeing with
   Prometheus's.
2. Warm up the chain. A cold chain's first request took 5.4s against gateway's 5s
   read timeout, so without this the ground truth would open with a failure nobody
   injected.
3. Start steady load, then inject four labelled faults on a fixed schedule, clearing
   and verifying recovery after each.
4. Append one line per incident to ground_truth.jsonl.
5. Query Prometheus over each incident window and prove the metric that should have
   moved actually moved. A chaos endpoint that returns 200 and changes no metric is
   worse than none.

Examples
--------
    py -3.12 tools/scenario_runner.py --fast 10
        The whole shape in ~3 minutes. Use this to debug the runner.

    py -3.12 tools/scenario_runner.py
        The real thing: 30 minutes, four labelled incidents.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

SERVICES = {
    "gateway":   "http://localhost:8080",
    "orders":    "http://localhost:8081",
    "inventory": "http://localhost:8082",
}
GATEWAY_ORDERS_URL = "http://localhost:8080/api/orders"
PROMETHEUS = "http://localhost:9090"
JAEGER_UI = "http://localhost:16686"
GRAFANA = "http://localhost:3000"
GRAFANA_AUTH = ("admin", "admin")

# ---------------------------------------------------------------------------
# PromQL used for the proof table.
#
# The rate window is 2m, not the 5m the dashboard uses for p95. A 5m window
# evaluated inside a 4-minute incident would still contain pre-incident samples and
# would understate the fault. 2m fits entirely inside the shortest incident.
#
# /chaos is excluded for the same reason the dashboard excludes it: the control
# plane emits 5xx of its own (501 where pool-exhaust is unsupported), and those are
# operator responses, not application errors.
# ---------------------------------------------------------------------------
Q_P95 = ('histogram_quantile(0.95, sum by (le) (rate('
         'http_server_request_duration_seconds_bucket'
         '{{service_name="{svc}", http_route!~"/health|/chaos.*"}}[2m])))')

# `or vector(0)` matters: before the first 500 ever occurs there is no 5xx time
# series at all, and an absent series is not the same as a query failure. This makes
# "no errors" return 0 instead of nothing, so the two cases stay distinguishable.
Q_5XX = ('sum(rate(http_server_request_duration_seconds_count'
         '{{service_name="{svc}", http_response_status_code=~"5..",'
         ' http_route!~"/chaos.*"}}[2m])) or vector(0)')

Q_HIKARI_USED = 'sum(db_client_connections_usage{pool_name="inventory-pool", state="used"})'
Q_HIKARI_IDLE = 'sum(db_client_connections_usage{pool_name="inventory-pool", state="idle"})'

# ---------------------------------------------------------------------------
# The scenario. Offsets are seconds from T=0, which is when labelled time starts
# (after warm-up). --fast scales every offset and duration, never the assertions.
#
# Why these four, in this order:
#   i1  deepest service, so the delay propagates up all three. Session 5 has to
#       attribute it to the leaf rather than the edge.
#   i2  middle service, PARTIAL failure. 100% would be trivially detectable and is
#       not what a bad deploy looks like; 20% forces work on rates.
#   i3  resource saturation with no golden-signal impact -- the deliberate negative
#       case, which a latency/error detector should MISS and a resource detector
#       should catch.
#   i4  edge service. Looks like i1 in the gateway p95 panel and is only separable
#       by where the time sits inside the trace. Designed to be the hard one.
#
# Incidents are 3-4 minutes and gaps are 2-4 minutes because OTel exports every 15s,
# Prometheus scrapes every 15s, and a 2m rate window needs to fill and then drain.
# ---------------------------------------------------------------------------
SCENARIO = [
    {
        "id": "i1", "at": 180, "duration": 240,
        "service": "inventory", "fault_type": "latency",
        "params": {"ms": 400, "jitter": 80},
        "checks": [
            {"name": "p95 inventory", "query": Q_P95.format(svc="inventory"),
             "expect": "rise", "min_delta": 0.15, "unit": "s"},
            {"name": "p95 gateway (propagated)", "query": Q_P95.format(svc="gateway"),
             "expect": "rise", "min_delta": 0.15, "unit": "s"},
        ],
    },
    {
        "id": "i2", "at": 660, "duration": 240,
        "service": "orders", "fault_type": "error_rate",
        "params": {"pct": 20},
        "checks": [
            {"name": "5xx rate orders", "query": Q_5XX.format(svc="orders"),
             "expect": "rise", "min_delta": 0.2, "unit": "req/s"},
            {"name": "5xx rate gateway (cascade)", "query": Q_5XX.format(svc="gateway"),
             "expect": "rise", "min_delta": 0.2, "unit": "req/s"},
        ],
    },
    {
        "id": "i3", "at": 1080, "duration": 240,
        "service": "inventory", "fault_type": "pool_exhaust",
        "params": {"hold": 9},
        "checks": [
            {"name": "hikari used", "query": Q_HIKARI_USED,
             "expect": "rise", "min_delta": 5.0, "unit": "conns"},
            {"name": "hikari idle", "query": Q_HIKARI_IDLE,
             "expect": "fall", "min_delta": 1.0, "unit": "conns"},
            # The point of this incident: saturation with no user-visible impact.
            {"name": "p95 inventory (expected FLAT)", "query": Q_P95.format(svc="inventory"),
             "expect": "flat", "tolerance": 0.10, "unit": "s"},
        ],
    },
    {
        "id": "i4", "at": 1500, "duration": 180,
        "service": "gateway", "fault_type": "latency",
        "params": {"ms": 250, "jitter": 50},
        "checks": [
            {"name": "p95 gateway", "query": Q_P95.format(svc="gateway"),
             "expect": "rise", "min_delta": 0.10, "unit": "s"},
            # The discriminator against i1: an edge fault must NOT move the leaf.
            {"name": "p95 inventory (expected FLAT)", "query": Q_P95.format(svc="inventory"),
             "expect": "flat", "tolerance": 0.10, "unit": "s"},
        ],
    },
]
TOTAL_SECONDS = 1800


# ---------------------------------------------------------------------------
# Small HTTP helpers. No requests library: stdlib only.
# ---------------------------------------------------------------------------

def http(method, url, timeout=15, body=None, headers=None):
    """Returns (status, headers_dict, text). A 4xx/5xx is a result, not an exception."""
    data = body
    if data is None and method in ("POST", "PUT"):
        data = b""                      # forces Content-Length: 0 rather than no body
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (response.status, dict(response.headers),
                    response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return (exc.code, dict(exc.headers),
                exc.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return (type(exc).__name__, {}, str(exc))


def http_json(method, url, **kwargs):
    status, headers, text = http(method, url, **kwargs)
    try:
        return status, headers, json.loads(text)
    except Exception:
        return status, headers, None


def post_order(timeout=15):
    """One order through the gateway. Returns (status, elapsed_ms)."""
    payload = json.dumps({"sku": "SKU-1", "qty": 1}).encode()
    started = time.perf_counter()
    status, _, _ = http("POST", GATEWAY_ORDERS_URL, timeout=timeout, body=payload,
                        headers={"Content-Type": "application/json"})
    return status, (time.perf_counter() - started) * 1000


def prom_query(expr, at=None):
    """Instant query. Returns a float, or None when the series does not exist.

    An absent series is not the same as zero -- before the first 500 ever occurs
    there is no 5xx time series at all -- but for the proof table treating it as
    zero is right, and saying so here is cheaper than a special case at each call.
    """
    params = {"query": expr}
    if at is not None:
        params["time"] = f"{at:.3f}"
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode(params)
    status, _, payload = http_json("GET", url, timeout=20)
    if status != 200 or not payload or payload.get("status") != "success":
        return None
    data = payload["data"]
    result = data["result"]
    if not result:
        return 0.0
    try:
        if data.get("resultType") == "scalar":
            # A scalar comes back as [timestamp, "value"] directly, not as a list of
            # series. time() is a scalar; everything else here is a vector. Getting
            # this wrong is a TypeError, not a wrong number, which is the good case.
            value = float(result[1])
        else:
            value = float(result[0]["value"][1])
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return None if value != value else value      # NaN guard: histogram_quantile emits it


def prom_query_range(expr, start, end, step=15):
    """Every sample of `expr` between two epochs. Empty list when the series does not
    exist in that window -- which for a 5xx rate is a real answer, not a failure."""
    if end <= start:
        return []
    params = {"query": expr, "start": f"{start:.3f}", "end": f"{end:.3f}",
              "step": str(step)}
    url = f"{PROMETHEUS}/api/v1/query_range?" + urllib.parse.urlencode(params)
    status, _, payload = http_json("GET", url, timeout=30)
    if status != 200 or not payload or payload.get("status") != "success":
        return []
    result = payload["data"]["result"]
    if not result:
        return []
    values = []
    for pair in result[0].get("values", []):
        try:
            value = float(pair[1])
        except (ValueError, IndexError, TypeError):
            continue
        if value == value:                        # drop NaN rather than propagate it
            values.append(value)
    return values


# ---------------------------------------------------------------------------
# Time. Everything labelled is UTC. The runner's own clock is Windows local time,
# and Prometheus and Jaeger are UTC; mixing them is the classic way to silently
# destroy a ground-truth file.
# ---------------------------------------------------------------------------

def utc_iso(epoch=None):
    moment = datetime.fromtimestamp(epoch if epoch is not None else time.time(),
                                    tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Chaos control plane
# ---------------------------------------------------------------------------

def chaos_status(service):
    status, _, payload = http_json("GET", f"{SERVICES[service]}/chaos", timeout=10)
    return payload if status == 200 else None


def chaos_clear(service):
    status, _, _ = http_json("DELETE", f"{SERVICES[service]}/chaos", timeout=20)
    return status == 200


def clear_all(quiet=False):
    for service in SERVICES:
        ok = chaos_clear(service)
        if not quiet and not ok:
            print(f"  WARNING: could not clear chaos on {service}")


def inject_url(service, fault_type, params, duration_s):
    base = SERVICES[service]
    if fault_type == "latency":
        return (f"{base}/chaos/latency?ms={params['ms']}"
                f"&jitter={params.get('jitter', 0)}")
    if fault_type == "error_rate":
        return f"{base}/chaos/error-rate?pct={params['pct']}"
    if fault_type == "pool_exhaust":
        # TTL is the dead-man's switch. Sized to outlive the incident but expire on
        # its own if this runner is killed, so a lost DELETE cannot leave the pool
        # held forever.
        ttl = max(1000, min(1_800_000, int(duration_s * 1000) + 60_000))
        return (f"{base}/chaos/pool-exhaust?hold={params['hold']}&ttl_ms={ttl}")
    raise ValueError(f"unknown fault_type {fault_type}")


# ---------------------------------------------------------------------------
# Preflight and warm-up
# ---------------------------------------------------------------------------

def preflight():
    print("preflight")
    problems = []

    for service, base in SERVICES.items():
        status, _, _ = http("GET", f"{base}/health", timeout=10)
        print(f"  {service:<10} /health -> {status}")
        if status != 200:
            problems.append(f"{service} unreachable from the host ({status})")

    for service in SERVICES:
        state = chaos_status(service)
        if state is None:
            problems.append(f"{service} has no /chaos endpoint")
            continue
        counters = state["counters"]
        inert = (not state["active"] and state["latency_ms"] == 0
                 and state["error_rate_pct"] == 0
                 and not state["pool_exhaust"]["active"])
        print(f"  {service:<10} /chaos  enabled={state['enabled']} inert={inert} "
              f"seen={counters['requests_seen']} delayed={counters['requests_delayed']} "
              f"failed={counters['requests_failed']}")
        if not state["enabled"]:
            problems.append(f"{service} has CHAOS_ENABLED=false")
        if not inert:
            problems.append(f"{service} already has a fault active")

    # Clock skew. ground_truth.jsonl is written against this machine's clock and
    # scored against Prometheus's; if they disagree the labels are wrong and nothing
    # downstream would ever tell us.
    host_before = time.time()
    prom_time = prom_query("time()")
    host_after = time.time()
    if prom_time is None:
        problems.append("Prometheus did not answer time()")
    else:
        skew = prom_time - (host_before + host_after) / 2
        print(f"  clock skew host vs Prometheus: {skew:+.3f}s")
        if abs(skew) > 2.0:
            problems.append(f"clock skew {skew:+.1f}s exceeds 2s -- labels would be wrong")

    if problems:
        print("\npreflight FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return False
    print("  preflight OK")
    return True


def warm_up(count=12):
    """Pay the cold-chain cost before labelled time starts.

    Session 1 measured 5.4s for the first request through a fully cold chain against
    gateway's 5s read timeout. Without this the first line of ground_truth.jsonl
    would describe a fault nobody injected.
    """
    print(f"\nwarm-up: {count} sequential requests")
    latencies = []
    failures = 0
    for _ in range(count):
        status, elapsed = post_order(timeout=20)
        latencies.append(elapsed)
        if status != 200:
            failures += 1
    latencies_sorted = sorted(latencies)
    p95 = latencies_sorted[min(len(latencies_sorted) - 1, int(len(latencies_sorted) * 0.95))]
    print(f"  first={latencies[0]:.0f}ms  last={latencies[-1]:.0f}ms  "
          f"p95={p95:.0f}ms  failures={failures}")
    if failures:
        print("  warm-up had failures -- refusing to start, the labels would be dirty")
        return None
    return {"p95_ms": round(p95, 1), "first_ms": round(latencies[0], 1),
            "requests": count}


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def verify_recovery(baseline_p95_ms, probes=10):
    """Recovery is asserted, not assumed.

    Two independent checks: the control plane says every service is inert, and real
    traffic through the front door succeeds at roughly the baseline latency.
    """
    started = time.perf_counter()

    inert_deadline = time.perf_counter() + 30
    inert = False
    while time.perf_counter() < inert_deadline:
        states = [chaos_status(service) for service in SERVICES]
        if all(s is not None and not s["active"] for s in states):
            inert = True
            break
        time.sleep(1)

    latencies, failures = [], 0
    for _ in range(probes):
        status, elapsed = post_order(timeout=20)
        latencies.append(elapsed)
        if status != 200:
            failures += 1
    latencies_sorted = sorted(latencies)
    p95 = latencies_sorted[min(len(latencies_sorted) - 1, int(len(latencies_sorted) * 0.95))]

    # Generous but not meaningless: 4x the warm-up p95, floored at 400ms so a very
    # fast baseline does not make this a hair trigger.
    ceiling = max(400.0, 4 * baseline_p95_ms)
    recovered = inert and failures == 0 and p95 <= ceiling
    return {
        "recovered": recovered,
        "recovery_s": round(time.perf_counter() - started, 1),
        "inert": inert,
        "probe_failures": failures,
        "probe_p95_ms": round(p95, 1),
        "probe_ceiling_ms": round(ceiling, 1),
    }


# ---------------------------------------------------------------------------
# Grafana annotations
# ---------------------------------------------------------------------------

def annotate(incident, enabled=True):
    """One region annotation per incident, so the done condition is a shaded band on
    every panel rather than a manual hunt through the time picker."""
    if not enabled:
        return False
    token = base64.b64encode(f"{GRAFANA_AUTH[0]}:{GRAFANA_AUTH[1]}".encode()).decode()
    body = json.dumps({
        "time": int(incident["_start_epoch"] * 1000),
        "timeEnd": int(incident["_end_epoch"] * 1000),
        "tags": ["chaos", incident["service"], incident["fault_type"]],
        "text": (f"{incident['incident_id']} — {incident['fault_type']} on "
                 f"{incident['service']} {json.dumps(incident['params'])}"),
    }).encode()
    status, _, _ = http("POST", f"{GRAFANA}/api/annotations", timeout=15, body=body,
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Basic {token}"})
    return status in (200, 201)


# ---------------------------------------------------------------------------
# One incident
# ---------------------------------------------------------------------------

def run_incident(phase, run_id, baseline_p95_ms, scale):
    duration = phase["duration"] * scale
    service = phase["service"]
    fault_type = phase["fault_type"]
    incident_id = f"{run_id}/{phase['id']}"

    before = chaos_status(service)
    counters_before = before["counters"] if before else {}

    # start is taken BEFORE the call and end AFTER the clear, making the label a
    # slight superset of the true fault window. That direction is deliberate: a
    # faulted sample sitting outside the label would be scored as a false positive
    # against a detector that was right, which is the worst scoring error there is.
    start_epoch = time.time()
    url = inject_url(service, fault_type, phase["params"], duration)
    status, _, _ = http_json("POST", url, timeout=20)
    confirmed_start_epoch = time.time()

    if status not in (200, 202):
        print(f"  !! injection failed: {status} {url}")
        return None

    print(f"  [{time.strftime('%H:%M:%S')}] INJECTED {phase['id']} {fault_type} on "
          f"{service} {phase['params']} for {duration:.0f}s")

    time.sleep(max(0.0, duration - (time.time() - confirmed_start_epoch)))

    confirmed_end_epoch = time.time()
    chaos_clear(service)
    end_epoch = time.time()

    after = chaos_status(service)
    counters_after = after["counters"] if after else {}

    def delta(key):
        try:
            return counters_after[key] - counters_before[key]
        except (KeyError, TypeError):
            return None

    recovery = verify_recovery(baseline_p95_ms)
    print(f"  [{time.strftime('%H:%M:%S')}] CLEARED  {phase['id']} "
          f"recovered={recovery['recovered']} "
          f"probe_p95={recovery['probe_p95_ms']}ms failures={recovery['probe_failures']}")

    return {
        "schema": 1,
        "run_id": run_id,
        "incident_id": incident_id,
        # Superset window -- use this for scoring.
        "start": utc_iso(start_epoch),
        "end": utc_iso(end_epoch),
        # Inner bound: the fault was definitely active across this.
        "confirmed_start": utc_iso(confirmed_start_epoch),
        "confirmed_end": utc_iso(confirmed_end_epoch),
        "service": service,
        "fault_type": fault_type,
        "params": phase["params"],
        "expected_signal": [c["name"] for c in phase["checks"] if c["expect"] != "flat"],
        # Counter deltas read from the service itself, so the injected-failure count
        # is measured rather than inferred from the requested percentage.
        "observed": {
            "requests_seen": delta("requests_seen"),
            "requests_delayed": delta("requests_delayed"),
            "requests_failed": delta("requests_failed"),
        },
        "recovered": recovery["recovered"],
        "recovery_s": recovery["recovery_s"],
        "recovery_detail": recovery,
        "jaeger_url": (f"{JAEGER_UI}/search?service={service}"
                       f"&start={int(start_epoch * 1_000_000)}"
                       f"&end={int(end_epoch * 1_000_000)}&limit=200"),
        "_start_epoch": start_epoch,
        "_end_epoch": end_epoch,
        "_checks": phase["checks"],
    }


# ---------------------------------------------------------------------------
# Proof that the telemetry actually moved
# ---------------------------------------------------------------------------

def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if not ordered:
        return None
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def verify_signals(incidents, baseline_window):
    """For each incident, compare the metric during it to the run's quiet baseline.

    Sampled over a *range*, not at a single instant. A single instant is fragile:
    OTel exports every 15s and Prometheus scrapes every 15s, so any one timestamp can
    still be carrying a pre-fault value, and the check would then fail on a fault that
    genuinely happened. Asking for the whole window and taking the peak makes the
    verdict depend on the fault rather than on export timing.

    <b>The baseline is one window per run, not one per incident.</b> The obvious design
    -- compare each incident to the two minutes immediately before it -- is wrong here,
    and measurably so: a 2m rate window looks back further than the gap between
    incidents, so incident N's "before" still contains incident N-1's fault. Measured
    at --fast 5, i3's supposedly-quiet baseline p95 read 0.476s because it had swallowed
    i1's injected latency. Using the run's own initial quiet period instead gives a
    baseline that is clean by construction, and it is also what "normal" means to the
    detector Session 4 will build.

    The 'during' window skips the first 45s of the incident so the 2m rate windows have
    had time to fill with faulted samples.
    """
    rows = []
    baseline_cache = {}
    baseline_start, baseline_end = baseline_window

    for incident in incidents:
        start, end = incident["_start_epoch"], incident["_end_epoch"]
        duration = end - start
        settle = min(45.0, max(0.0, duration - 30.0))

        for check in incident["_checks"]:
            query = check["query"]
            if query not in baseline_cache:
                baseline_cache[query] = _median(
                    prom_query_range(query, baseline_start, baseline_end))
            during_samples = prom_query_range(query, start + settle, end)

            before = baseline_cache[query]
            during = None
            if during_samples:
                if check["expect"] == "fall":
                    during = min(during_samples)
                elif check["expect"] == "rise":
                    during = max(during_samples)
                else:                                    # flat: the worst excursion
                    during = max(during_samples,
                                 key=lambda v: abs(v - before) if before is not None else v)

            verdict = "NO DATA"
            if before is not None and during is not None:
                if check["expect"] == "rise":
                    verdict = "YES" if during - before >= check["min_delta"] else "NO"
                elif check["expect"] == "fall":
                    verdict = "YES" if before - during >= check["min_delta"] else "NO"
                else:
                    verdict = ("FLAT" if abs(during - before) <= check["tolerance"]
                               else "MOVED")
            rows.append({
                "incident": incident["incident_id"].split("/")[-1],
                "service": incident["service"],
                "fault": incident["fault_type"],
                "metric": check["name"],
                "expect": check["expect"],
                "before": before,
                "during": during,
                "samples": len(during_samples),
                "unit": check["unit"],
                "verdict": verdict,
            })
    return rows


def format_signals(rows):
    def fmt(value):
        return "  n/a" if value is None else f"{value:.3f}"
    lines = ["| inc | service | fault | metric | expect | baseline | during | unit | n | verdict |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['incident']} | {row['service']} | {row['fault']} | "
                     f"{row['metric']} | {row['expect']} | {fmt(row['before'])} | "
                     f"{fmt(row['during'])} | {row['unit']} | {row.get('samples', 0)} | "
                     f"**{row['verdict']}** |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run a labelled chaos scenario and write ground truth.")
    parser.add_argument("--fast", type=float, default=1.0,
                        help="divide every duration by this. --fast 10 gives the whole "
                             "shape in ~3 minutes, for debugging the runner itself.")
    parser.add_argument("--rate", type=float, default=6.0, help="target req/s")
    parser.add_argument("--workers", type=int, default=4, help="load generator threads")
    parser.add_argument("--skip-annotations", action="store_true",
                        help="do not POST Grafana annotations")
    parser.add_argument("--skip-verify", action="store_true",
                        help="do not query Prometheus for the proof table")
    args = parser.parse_args()

    scale = 1.0 / max(args.fast, 0.01)
    run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # Only a full-fidelity run contributes labels. A --fast run compresses a
    # 4-minute incident into seconds, which is long enough to prove the mechanics and
    # far too short to be a real incident -- its rows would be noise in the file
    # Session 4 scores against, and noise there is indistinguishable from a bad
    # detector. Fast runs keep their ground truth inside their own run directory.
    full_fidelity = abs(scale - 1.0) < 1e-9
    ground_truth_path = (ROOT / "ground_truth.jsonl" if full_fidelity
                         else run_dir / "ground_truth.jsonl")

    total = TOTAL_SECONDS * scale
    print(f"run_id   : {run_id}")
    print(f"duration : {total:.0f}s ({total / 60:.1f} min), scale x{scale:.3f}")
    # ASCII only in console output: the Windows console runs cp1252 and an em-dash
    # comes out as a replacement character.
    print(f"labels   : {ground_truth_path.relative_to(ROOT)}"
          f"{'' if full_fidelity else '   (fast run - kept out of the real label file)'}")
    print(f"artefacts: runs/{run_id}/\n")

    if not preflight():
        return 2

    baseline = warm_up()
    if baseline is None:
        return 3

    traffic_log = open(run_dir / "traffic.log", "w", encoding="utf-8")
    traffic = subprocess.Popen(
        [sys.executable, str(TOOLS / "traffic_gen.py"),
         "--rate", str(args.rate), "--workers", str(args.workers),
         "--duration", str(int(total + 30)), "--report-every", "15"],
        stdout=traffic_log, stderr=subprocess.STDOUT, cwd=str(ROOT))

    incidents = []
    run_started_epoch = time.time()
    t_zero = time.perf_counter()
    print(f"\nT=0 at {utc_iso(run_started_epoch)} — labelled time starts\n")

    try:
        for phase in SCENARIO:
            target = phase["at"] * scale
            while True:
                remaining = target - (time.perf_counter() - t_zero)
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 5))
                if traffic.poll() is not None:
                    print("  !! traffic generator exited early")
                    break

            incident = run_incident(phase, run_id, baseline["p95_ms"], scale)
            if incident is None:
                continue
            incidents.append(incident)

            # fsync per line: a killed run keeps every label it earned. This is why
            # the file is JSON Lines and not a single JSON array -- a crash mid-write
            # leaves a valid prefix rather than an unparseable file.
            record = {k: v for k, v in incident.items() if not k.startswith("_")}
            with open(ground_truth_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

            if annotate(incident, enabled=not args.skip_annotations):
                print(f"  [{time.strftime('%H:%M:%S')}] annotated {incident['incident_id']}")

        # Quiet tail: recovery has to be visible in the telemetry, not just asserted.
        remaining = total - (time.perf_counter() - t_zero)
        if remaining > 0:
            print(f"\nquiet tail: {remaining:.0f}s")
            time.sleep(remaining)

    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        # An aborted run must never leave the system broken.
        clear_all(quiet=True)
        if traffic.poll() is None:
            traffic.terminate()
            try:
                traffic.wait(timeout=20)
            except subprocess.TimeoutExpired:
                traffic.kill()
        traffic_log.close()

    run_ended_epoch = time.time()

    # The run's own quiet period, between warm-up ending and the first injection.
    # Clean by construction: nothing has been injected yet and the chain is warm.
    baseline_window = (run_started_epoch + 15,
                       run_started_epoch + SCENARIO[0]["at"] * scale - 15)

    rows = []
    if incidents and not args.skip_verify:
        print("\nverifying signals (waiting 45s so the last window is fully scraped)")
        print(f"  baseline window: {utc_iso(baseline_window[0])} .. "
              f"{utc_iso(baseline_window[1])} "
              f"({baseline_window[1] - baseline_window[0]:.0f}s of quiet)")
        time.sleep(45)
        rows = verify_signals(incidents, baseline_window)
        table = format_signals(rows)
        print("\n" + table + "\n")
        (run_dir / "signals.md").write_text(
            f"# Signal proof — {run_id}\n\n{table}\n\n"
            + "\n".join(f"- **{i['incident_id'].split('/')[-1]}** "
                        f"({i['fault_type']} on {i['service']}): {i['jaeger_url']}"
                        for i in incidents) + "\n",
            encoding="utf-8")

    meta = {
        "run_id": run_id,
        "started": utc_iso(run_started_epoch),
        "ended": utc_iso(run_ended_epoch),
        "scale": scale,
        "rate": args.rate,
        "workers": args.workers,
        "warmup": baseline,
        "warmup_excluded_before": utc_iso(run_started_epoch),
        "baseline_window": {"start": utc_iso(baseline_window[0]),
                            "end": utc_iso(baseline_window[1])},
        "incidents": [i["incident_id"] for i in incidents],
        "signals": rows,
    }
    (run_dir / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"incidents written : {len(incidents)} -> {ground_truth_path.relative_to(ROOT)}")
    print(f"artefacts         : runs/{run_id}/")
    for incident in incidents:
        print(f"  {incident['incident_id'].split('/')[-1]}  {incident['fault_type']:<13} "
              f"{incident['service']:<10} recovered={incident['recovered']}")

    failed = [r for r in rows if r["verdict"] in ("NO", "MOVED", "NO DATA")]
    if failed:
        print("\nSIGNAL CHECKS FAILED:")
        for row in failed:
            print(f"  {row['incident']} {row['metric']}: expected {row['expect']}, "
                  f"got {row['before']} -> {row['during']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
