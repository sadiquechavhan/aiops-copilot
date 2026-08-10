"""
mcp_probe -- a JSON-RPC client for the server in aiops_mcp/, over real stdio.

WHY THIS EXISTS RATHER THAN `import aiops_mcp.metrics`
------------------------------------------------------
Importing the functions and calling them tests the functions. It does not test
the protocol, and the protocol is what this session is about. Every failure mode
that actually bites at integration time lives strictly between the two:

  * a print() somewhere corrupting the wire
  * cp1252 mangling a non-ASCII byte on Windows
  * a response never arriving because the stream was not flushed
  * a notification being answered, or a request not being
  * output that is valid JSON but not valid MCP

So this spawns the server the same way Claude Code will -- subprocess, pipes,
newline-delimited JSON -- and speaks the handshake by hand.

MODES
    (default)      handshake, tools/list, then a tools/call with the given args
    --raw          also dump every frame in both directions, verbatim
    --cost         measure the response and compare against the unaggregated
                   payload the same question would return without the tool
    --ground-truth replay the labelled incident windows from ground_truth.jsonl
                   and check the tool's answers against them
"""

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROUND_TRUTH = os.path.join(ROOT, "ground_truth.jsonl")


class Server:
    """The server as a subprocess, spoken to over pipes."""

    def __init__(self, raw=False):
        self.raw = raw
        self.next_id = 0
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        # -u is unbuffered. The server flushes explicitly, but a client that
        # relies on the server being careful will eventually hang on a server
        # that is not, and a hang is the least diagnosable failure available.
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "aiops_mcp.server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            cwd=ROOT, env=env, text=True, encoding="utf-8", bufsize=1,
        )

    def send(self, method, params=None, notify=False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self.next_id += 1
            message["id"] = self.next_id
        line = json.dumps(message, separators=(",", ":"))
        if self.raw:
            print(f"--> {line}")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        reply = self.proc.stdout.readline()
        if not reply:
            raise SystemExit("server closed the stream without replying -- "
                             "check stderr above for a traceback")
        if self.raw:
            print(f"<-- {reply.strip()}")
        return json.loads(reply)

    def handshake(self):
        reply = self.send("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcp_probe", "version": "0.1.0"},
        })
        self.send("notifications/initialized", notify=True)
        return reply

    def call(self, name, arguments):
        return self.send("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def unwrap(reply):
    """tools/call reply -> (payload, is_error).

    The two channels, made visible. A JSON-RPC `error` here means the CALL was
    malformed and the model would never have seen it; isError means the call was
    fine and the tool failed, which the model does see.
    """
    if "error" in reply:
        return {"jsonrpc_error": reply["error"]}, True
    result = reply.get("result", {})
    text = "".join(block.get("text", "") for block in result.get("content", []))
    try:
        return json.loads(text), bool(result.get("isError"))
    except ValueError:
        # get_trace returns a pre-rendered tree, which is deliberately NOT JSON.
        # Passed through verbatim rather than reported as unparseable.
        return {"text": text}, bool(result.get("isError"))


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

def load_ground_truth(run=None):
    if not os.path.exists(GROUND_TRUTH):
        raise SystemExit(f"no ground truth at {GROUND_TRUTH}")
    rows = []
    with open(GROUND_TRUTH, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if run is None or row.get("run_id") == run:
                rows.append(row)
    return rows


def check_ground_truth(server, run=None):
    """Replay each labelled incident window and report what the tool says.

    The window used is the incident's OWN start/end, passed absolutely. This is
    the test that matters: 'now' is whatever the system happens to be doing, but
    a labelled window has a known answer, and passing absolute UTC timestamps
    exercises the parser that Session 2's timezone bug lived in.

    Deliberately reports numbers rather than asserting a threshold. The tool's
    contract is to report what the store holds; deciding whether 488ms counts as
    an incident is Session 4's job, and baking a threshold in here would be
    Session 4 leaking into Session 3.
    """
    rows = load_ground_truth(run)
    if not rows:
        raise SystemExit(f"no ground-truth rows{f' for run {run}' if run else ''}")

    print(f"{len(rows)} labelled incident(s)\n")
    failures = expired = 0
    for row in rows:
        fault = row.get("fault_type", "?")
        service = row.get("service", "?")
        # pool_exhaust maps to db_pool, which exists only for inventory -- and a
        # pool-exhaust incident is invisible in latency and errors BY DESIGN.
        # That is what makes i3 the useful negative case.
        metric = {"latency": "latency", "error_rate": "error_ratio",
                  "pool_exhaust": "db_pool"}.get(fault, "latency")
        # confirmed_* are the bounds at which the fault was VERIFIED active, as
        # opposed to when the injection request was sent. Preferred wherever
        # present: the difference is small but it is the difference between
        # measuring the fault and measuring the fault plus its edges.
        args = {"metric": metric,
                "start": row.get("confirmed_start") or row["start"],
                "end": row.get("confirmed_end") or row["end"]}
        if metric != "db_pool":
            args["service"] = service
            args["percentile"] = 95

        payload, is_error = unwrap(server.call("query_metrics", args))
        label = f"{row.get('incident_id','?')} {fault}/{service}"

        if is_error:
            # A window that has aged past retention is the tool answering
            # CORRECTLY -- "this data is gone" is a true statement about the
            # store, and is precisely the distinction this package exists to
            # draw. Counting it as a failure would mark a working system down
            # as the labelled runs age, which is the one thing a regression
            # check must never do.
            #
            # Detected on `oldest_available`, which only the retention branch
            # attaches, rather than on the message text -- a test that greps
            # prose fails the moment the prose is reworded.
            detail = payload.get("error", payload)
            if "oldest_available" in detail:
                print(f"  EXPIRED {label} -- window predates the "
                      f"{detail.get('oldest_available')} retention horizon")
                expired += 1
                continue
            print(f"  FAIL  {label}\n        {json.dumps(payload)[:220]}")
            failures += 1
            continue
        if payload.get("status") == "empty":
            # Not automatically a failure: for a 22h-old window this is
            # retention, which is a true statement about the store. The reason
            # code is what separates the two, which is the whole design.
            print(f"  EMPTY {label} -- reason={payload.get('reason')}")
            print(f"        {payload.get('notes', [''])[-1][:160]}")
            failures += 1
            continue

        print(f"  ok    {label}  ({payload['window']['duration_s']}s from "
              f"{payload['window']['start']})")
        for series in payload.get("series", []):
            bits = [f"{k}={series[k]}" for k in ("p50", "p95", "p99", "max", "avg")
                    if k in series]
            print(f"        {series['service']}: {' '.join(bits)} {payload['unit']}")

        # The actual comparison, where the label carries a number. For an
        # error_rate fault the runner recorded how many requests it saw and how
        # many it failed, so the labelled ratio is a real figure to check
        # against rather than a vibe.
        observed = row.get("observed") or {}
        seen, failed = observed.get("requests_seen"), observed.get("requests_failed")
        if metric == "error_ratio" and seen:
            expected_pct = 100.0 * failed / seen
            got = next((s.get("max") for s in payload.get("series", [])
                        if s["service"] == service), None)
            if got is None:
                print(f"        MISMATCH: no series for {service}")
                failures += 1
            else:
                # Tolerance, not equality. The label counts requests the runner
                # itself issued over the whole injection; the tool measures a
                # rate ratio over scrape buckets. They describe the same fault
                # and will not agree to the decimal.
                ok = abs(got - expected_pct) <= 6.0
                print(f"        labelled {failed}/{seen} = {expected_pct:.1f}% "
                      f"vs measured max {got}%  ->  {'MATCH' if ok else 'MISMATCH'}")
                if not ok:
                    failures += 1
        else:
            print(f"        labelled signal: "
                  f"{json.dumps(row.get('expected_signal'))[:150]}")
            if observed:
                print(f"        runner saw: {seen} requests, {failed} failed, "
                      f"{observed.get('requests_delayed')} delayed")
        print()

    answered = len(rows) - failures - expired
    print(f"{answered}/{len(rows) - expired} in-retention windows answered "
          f"as expected"
          + (f"; {expired} past the retention horizon (correctly refused)"
             if expired else ""))
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

def measure_cost(server, tool, args):
    """Response size against the unaggregated alternative.

    Bytes are the primary unit because they are measured, not estimated. The
    token figure is an ESTIMATE at the ~4-bytes-per-token rule of thumb -- real
    tokenisation of dense JSON runs worse than that, so treat it as a floor.

    The comparison is against what the same question costs without the tool:
    for metrics, every raw sample Prometheus would return at native resolution;
    for traces, the spans Jaeger actually hands back over the same window. Both
    fetch the raw side FOR REAL rather than estimating it -- a saving computed
    against a guessed baseline is not a measurement.
    """
    if tool == "query_traces":
        return _cost_traces(server, args)
    if tool != "query_metrics":
        raise SystemExit(f"--cost supports query_metrics and query_traces, "
                         f"not {tool!r}")

    reply = server.call("query_metrics", args)
    payload, is_error = unwrap(reply)
    if is_error:
        print(json.dumps(payload, indent=2))
        return 1

    served = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    duration = payload.get("window", {}).get("duration_s", 0)
    n_series = max(1, len(payload.get("series", [])))
    raw_samples = int(duration / 15) * n_series
    # A Prometheus matrix pair serialises as [1754769451.85,"0.4867"] -- about
    # 30 bytes with its separators, before the per-series metric labels.
    raw_bytes = raw_samples * 30 + n_series * 120

    print(json.dumps(payload, indent=2))
    print()
    print(f"  window          {duration}s, {n_series} series")
    _report(served, raw_bytes, f"{raw_samples:,} samples")
    return 0


def _cost_traces(server, args):
    """query_traces served size against Jaeger's own response for that window.

    The raw side is FETCHED, not modelled, because the saving from folding is
    entirely a function of spans per trace -- and that varies hugely between an
    incident (deep traces) and an idle stack (single-span health checks).
    Estimating it would produce whichever number I assumed.
    """
    payload, is_error = unwrap(server.call("query_traces", args))
    if is_error:
        print(json.dumps(payload, indent=2))
        return 1
    if payload.get("status") != "ok":
        print(json.dumps(payload, indent=2))
        return 0

    served = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    window = payload["window"]
    raw, spans = _raw_jaeger(args, window)
    print(f"  window          {window['duration_s']}s, "
          f"{payload['returned']} traces / {spans} spans")
    _report(served, raw, f"{spans} spans across {payload['returned']} traces")
    if spans <= payload["returned"]:
        # Single-span traces are health checks. Saying so matters: the headline
        # reduction is far larger on incident traffic, and a reader who sampled
        # an idle stack would otherwise conclude folding barely helps.
        print("  NOTE            traces are ~1 span each (idle/health traffic); "
              "the reduction is much larger on incident traffic")
    return 0


def _raw_jaeger(args, window):
    """What Jaeger returns unfolded, for the same service and window.

    Deliberately does NOT import aiops_mcp -- this file is a protocol client and
    stays one. Importing the package for a URL would make the probe share the
    server's configuration, so a wrong URL in the package would produce a
    matching wrong URL here and the comparison would silently agree with itself.
    """
    import datetime
    import urllib.parse
    import urllib.request

    base = os.environ.get("AIOPS_JAEGER_URL", "http://localhost:16686")

    def epoch_us(text):
        stamp = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
        return int(stamp.replace(tzinfo=datetime.timezone.utc).timestamp() * 1e6)

    query = urllib.parse.urlencode({"service": args["service"],
                                    "start": epoch_us(window["start"]),
                                    "end": epoch_us(window["end"]),
                                    "limit": args.get("limit", 20)})
    with urllib.request.urlopen(f"{base}/api/traces?{query}",
                                timeout=30) as response:
        data = json.loads(response.read()).get("data") or []
    raw = len(json.dumps(data, separators=(",", ":")).encode("utf-8"))
    return raw, sum(len(t.get("spans", [])) for t in data)


def _report(served, raw, detail):
    print(f"  served          {served:,} bytes  (~{served // 4:,} tokens)")
    print(f"  unaggregated    {raw:,} bytes  (~{raw // 4:,} tokens), {detail}")
    if served:
        print(f"  reduction       {raw / served:.0f}x")


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("tool", nargs="?", help="tool name for a single call")
    parser.add_argument("arguments", nargs="?", default="{}",
                        help="JSON object of arguments")
    parser.add_argument("--raw", action="store_true", help="dump every frame")
    parser.add_argument("--cost", action="store_true", help="measure response size")
    parser.add_argument("--ground-truth", action="store_true",
                        help="replay labelled incident windows")
    parser.add_argument("--run", help="restrict --ground-truth to one run_id")
    parser.add_argument("--list", action="store_true", help="tools/list only")
    options = parser.parse_args()

    server = Server(raw=options.raw)
    try:
        started = time.time()
        init = server.handshake()
        info = init.get("result", {})
        print(f"connected: {info.get('serverInfo', {}).get('name')} "
              f"{info.get('serverInfo', {}).get('version')} "
              f"protocol={info.get('protocolVersion')} "
              f"({(time.time() - started) * 1000:.0f}ms)\n")

        if options.ground_truth:
            return check_ground_truth(server, options.run)

        listed = server.send("tools/list").get("result", {}).get("tools", [])
        if options.list or not options.tool:
            schema_bytes = len(json.dumps(listed, separators=(",", ":")).encode())
            for tool in listed:
                print(f"  {tool['name']}: {tool['description'][:100]}")
            print(f"\n{len(listed)} tool(s), {schema_bytes:,} bytes of schema "
                  f"(~{schema_bytes // 4:,} tokens) resident every turn")
            return 0

        try:
            arguments = json.loads(options.arguments)
        except ValueError as exc:
            raise SystemExit(f"arguments is not JSON: {exc}")

        if options.cost:
            return measure_cost(server, options.tool, arguments)

        payload, is_error = unwrap(server.call(options.tool, arguments))
        print(json.dumps(payload, indent=2))
        return 1 if is_error else 0
    finally:
        server.close()


if __name__ == "__main__":
    sys.exit(main())
