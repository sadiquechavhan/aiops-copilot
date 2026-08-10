"""
query_traces -- Jaeger, folded into a span table.

WHY NOT JUST RETURN THE TRACES
------------------------------
A single trace here is 7 spans, each with ~20 tags, two timestamps, a process
reference and a references array. That is roughly 6 KB of JSON. Twenty traces is
120 KB -- around 30,000 tokens for one tool call, most of it identical tags
repeated per span, to answer a question like "is inventory slow".

Worse, it is not just expensive, it is unreadable: the model would have to do
per-span arithmetic across twenty documents to find the answer, in-context, with
no way to check itself. So this tool does the arithmetic and returns the result.

SELF-TIME, the only non-obvious number in this file
---------------------------------------------------
A span's `duration` includes everything it waited on. When gateway takes 480ms
and its call to inventory takes 470ms, gateway is not slow -- it is BLOCKED. Its
own contribution is 10ms.

    self_time = duration - (time covered by its direct children)

"Covered by" rather than "sum of", because concurrent children overlap: summing
two 100ms children that ran in parallel claims 200ms of a 120ms span and yields
a negative self-time. So child intervals are MERGED before subtracting.

This is what discriminates Session 2's i1 from i4. Both raise gateway's p95 to
roughly 480ms and are indistinguishable in metrics alone:

    i1 (fault in inventory): gateway self-time small, inventory self-time large
    i4 (fault in gateway):   gateway self-time large, inventory unchanged

The tool computes and reports this. It does NOT rank services or name a cause --
that is Session 5's correlation job, and doing it here would mean two components
disagreeing about root cause with no way to tell which was right.

SAMPLING ACROSS THE WINDOW
--------------------------
Jaeger returns most-recent-first. One request for 30 traces over a 4-minute
window is really "the last 30 traces", which for a fault that ended 3 minutes in
means sampling only the recovery. So the window is split into thirds and each is
fetched separately, giving coverage of the beginning, middle and end.

CAPS
----
MAX_TRACES is 40 and MAX_SPANS_PER_TRACE is 60, both deliberately conservative.
Session 2 asked Jaeger for 400 traces with full payloads and the container
restarted, destroying a completed labelled run's trace evidence. Cause was never
established (OOMKilled=false, ExitCode=0) -- which is exactly why the caps are
low rather than tuned. An unexplained failure cannot be argued away.
"""

from . import (DEFAULT_TRACES, JAEGER_TIMEOUT_S, JAEGER_TTL_HOURS, JAEGER_URL,
               MAX_SPANS_PER_TRACE, MAX_TRACES, SERVICES)
from .http import get_json
from .util import BackendError, BadArgument, percentile, rnd
from .window import jaeger_window, utc_iso

# Tags worth returning, out of the ~20 on every span. Everything else --
# container.id, telemetry.sdk.version, otel.scope.*, thread.id -- is either
# constant across the whole trace or irrelevant to why something was slow, and
# is pure token cost repeated per span.
_KEEP_TAGS = ("http.request.method", "http.response.status_code", "http.route",
              "server.address", "db.system", "db.operation", "db.statement",
              "error", "otel.status_code", "exception.type", "exception.message")

_SUBWINDOWS = 3


def _tags(span):
    out = {}
    for tag in span.get("tags", []):
        key = tag.get("key")
        if key in _KEEP_TAGS:
            out[key] = tag.get("value")
    return out


def _merged_child_time(children):
    """Union of child intervals, in microseconds.

    Merging rather than summing is what keeps self-time from going negative when
    a span fans out concurrently. Standard interval-union: sort by start, extend
    the open interval while the next one overlaps, else close it and start a new
    one.
    """
    if not children:
        return 0
    spans = sorted((c["startTime"], c["startTime"] + c["duration"]) for c in children)
    total, cur_start, cur_end = 0, spans[0][0], spans[0][1]
    for start, end in spans[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    return total + (cur_end - cur_start)


def _fold(trace):
    """One Jaeger trace -> a compact summary with self-time per service."""
    processes = trace.get("processes", {})
    spans = trace.get("spans", [])[:MAX_SPANS_PER_TRACE]
    if not spans:
        return None

    by_id = {s["spanID"]: s for s in spans}
    children = {}
    root = None
    for span in spans:
        parent = None
        for ref in span.get("references", []):
            if ref.get("refType") == "CHILD_OF" and ref.get("spanID") in by_id:
                parent = ref["spanID"]
                break
        if parent is None:
            # The earliest parentless span is the root. Picking by start time
            # rather than by array position because Jaeger does not promise an
            # order, and a truncated trace can have several parentless spans.
            if root is None or span["startTime"] < root["startTime"]:
                root = span
        else:
            children.setdefault(parent, []).append(span)

    if root is None:
        root = min(spans, key=lambda s: s["startTime"])

    per_service, error_count = {}, 0
    for span in spans:
        service = processes.get(span.get("processID"), {}).get("serviceName", "?")
        self_us = span["duration"] - _merged_child_time(children.get(span["spanID"], []))
        entry = per_service.setdefault(service, {"self_us": 0, "spans": 0})
        entry["self_us"] += max(0, self_us)
        entry["spans"] += 1
        tags = _tags(span)
        status = tags.get("http.response.status_code")
        if tags.get("error") or (isinstance(status, int) and status >= 500):
            error_count += 1

    root_tags = _tags(root)
    return {
        "trace_id": trace.get("traceID"),
        "start": utc_iso(root["startTime"] / 1_000_000),
        "duration_ms": rnd(root["duration"] / 1000.0),
        "spans": len(spans),
        "root": root.get("operationName"),
        "route": root_tags.get("http.route"),
        "status": root_tags.get("http.response.status_code"),
        "errors": error_count,
        # Where the time actually went, per service, for THIS trace.
        "self_ms": {name: rnd(v["self_us"] / 1000.0)
                    for name, v in sorted(per_service.items())},
    }


def _fetch(service, start_us, end_us, limit):
    payload = get_json(JAEGER_URL, "/api/traces",
                       {"service": service, "start": start_us, "end": end_us,
                        "limit": limit}, JAEGER_TIMEOUT_S, "jaeger")
    if not isinstance(payload, dict):
        return []
    # Jaeger reports query errors in an `errors` array alongside a null data,
    # rather than with a non-2xx status. Silently treating that as "no traces"
    # would be the same empty-vs-broken confusion this package exists to avoid.
    if payload.get("errors") and not payload.get("data"):
        from .util import BackendError
        raise BackendError(f"jaeger: {str(payload['errors'])[:200]}", target="jaeger")
    return payload.get("data") or []


def get_trace(trace_id=None):
    """One trace as an indented span tree, PRE-RENDERED AS TEXT.

    The only tool here that returns text instead of JSON, and the reason is
    structural rather than aesthetic.

    Everything else returns tables of numbers, where JSON is clearly right: the
    model needs to compare p95 across three services, and prose would force it
    to parse sentences back into numbers it could already have had.

    A span tree is the opposite case. It is hierarchy, and indentation encodes
    hierarchy for free -- two spaces per level. The JSON equivalent spends a
    "children":[...] key and a pair of brackets at every level, and the model
    still has to reconstruct the shape mentally to read it. Measured on an
    8-span trace: 831 bytes of text against 1,791 bytes of equivalent nested
    JSON (46%), and 6% of Jaeger's own 14,363-byte response.

    The rule this follows: JSON for things being compared, text for things being
    read. Not "text is always cheaper" -- it is not, for tables of numbers,
    where prose would force the model to parse sentences back into figures.
    """
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise BadArgument("trace_id is required", field="trace_id")
    tid = trace_id.strip()
    if not all(c in "0123456789abcdefABCDEF" for c in tid):
        # Caught here rather than at Jaeger, which answers a malformed ID with
        # an empty result -- indistinguishable from a trace that has expired.
        raise BadArgument(
            f"trace_id must be hexadecimal, got {tid[:40]!r}", field="trace_id")

    try:
        payload = get_json(JAEGER_URL, f"/api/traces/{tid}", None,
                           JAEGER_TIMEOUT_S, "jaeger")
    except BackendError as exc:
        # Jaeger answers an unknown trace id with HTTP 404, which get_json
        # reports as backend_error -- correct for a generic HTTP helper, wrong
        # for the model, which would read "jaeger returned HTTP 404" as "the
        # backend is broken" and stop. On THIS endpoint a 404 is a statement
        # about the trace, not about Jaeger, so it is translated here rather
        # than in http.py, which has no way to know that.
        if getattr(exc, "extra", {}).get("http_status") != 404:
            raise
        payload = None
    data = (payload or {}).get("data") or []
    if not data:
        return {"status": "empty", "trace_id": tid, "reason": "not_found",
                "notes": [f"no trace {tid} in Jaeger. Either the id is wrong or "
                          f"it aged out of the {JAEGER_TTL_HOURS}h TTL."]}

    trace = data[0]
    processes = trace.get("processes", {})
    spans = trace.get("spans", [])
    truncated = len(spans) > MAX_SPANS_PER_TRACE
    spans = sorted(spans, key=lambda s: s["startTime"])[:MAX_SPANS_PER_TRACE]

    by_id = {s["spanID"]: s for s in spans}
    kids, roots = {}, []
    for span in spans:
        parent = next((r["spanID"] for r in span.get("references", [])
                       if r.get("refType") == "CHILD_OF" and r.get("spanID") in by_id),
                      None)
        (kids.setdefault(parent, []) if parent else roots).append(span)
    for group in kids.values():
        group.sort(key=lambda s: s["startTime"])

    origin = roots[0]["startTime"] if roots else spans[0]["startTime"]
    lines = []

    def walk(span, depth):
        service = processes.get(span.get("processID"), {}).get("serviceName", "?")
        self_us = span["duration"] - _merged_child_time(kids.get(span["spanID"], []))
        tags = _tags(span)
        # Offset from trace start is what makes a gap visible: a child starting
        # 400ms in, under a 410ms parent, is the parent having waited before
        # calling it -- which points at queueing or a pool, not at the child.
        bits = [f"{'  ' * depth}{service}/{span.get('operationName')}",
                f"+{(span['startTime'] - origin) / 1000.0:.0f}ms",
                f"dur={span['duration'] / 1000.0:.1f}ms",
                f"self={max(0, self_us) / 1000.0:.1f}ms"]
        status = tags.get("http.response.status_code")
        if status is not None:
            bits.append(f"status={status}")
        for key in ("db.statement", "exception.type", "exception.message", "error"):
            if tags.get(key):
                bits.append(f"{key}={str(tags[key])[:120]}")
        lines.append(" ".join(bits))
        for child in kids.get(span["spanID"], []):
            walk(child, depth + 1)

    for root in roots or spans[:1]:
        walk(root, 0)

    header = (f"trace {tid}  spans={len(spans)}"
              f"{' (truncated)' if truncated else ''}  "
              f"start={utc_iso(origin / 1_000_000)}\n"
              f"dur=duration including waiting; self=own work excluding children; "
              f"+Nms=offset from trace start\n")
    return {"status": "ok", "trace_id": tid, "spans": len(spans),
            "truncated": truncated, "tree": header + "\n".join(lines)}

def query_traces(service=None, window=None, start=None, end=None,
                 limit=DEFAULT_TRACES, min_duration_ms=None, errors_only=False):
    """Sampled traces over a window, folded to self-time per service."""
    if service not in SERVICES:
        raise BadArgument(
            f"unknown service {service!r} -- traces are queried by the service "
            f"that STARTED the trace", field="service", known=list(SERVICES))
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise BadArgument(f"limit must be an integer, got {limit!r}", field="limit")
    limit = max(1, min(MAX_TRACES, limit))

    win = jaeger_window(window=window, start=start, end=end)

    # Three sub-windows so the sample spans the whole period, not just its tail.
    per_window = max(1, limit // _SUBWINDOWS)
    slice_us = win.duration * 1_000_000 / _SUBWINDOWS
    base_us = win.start * 1_000_000

    seen, traces = set(), []
    for i in range(_SUBWINDOWS):
        chunk_start = int(base_us + i * slice_us)
        chunk_end = int(base_us + (i + 1) * slice_us)
        for trace in _fetch(service, chunk_start, chunk_end, per_window):
            tid = trace.get("traceID")
            if tid in seen:
                continue
            seen.add(tid)
            folded = _fold(trace)
            if folded:
                traces.append(folded)

    if not traces:
        return {"status": "empty", "service": service, "reason": "no_traces",
                "window": win.payload(),
                "notes": win.notes + [
                    f"no traces started by {service} in this window. Jaeger keeps "
                    f"72h; the sampler is always_on, so an empty result means no "
                    f"requests rather than sampling loss."]}

    # Filters applied AFTER folding, so the counts below describe the sample
    # actually taken rather than what survived the filter.
    total_sampled = len(traces)
    if min_duration_ms is not None:
        traces = [t for t in traces if (t["duration_ms"] or 0) >= float(min_duration_ms)]
    if errors_only:
        traces = [t for t in traces if t["errors"]]

    durations = [t["duration_ms"] for t in traces if t["duration_ms"] is not None]

    # Aggregate self-time across the sample. This is the number that answers
    # "which service is actually spending the time", and it is why the tool
    # returns a table instead of traces.
    totals = {}
    for trace in traces:
        for name, value in (trace.get("self_ms") or {}).items():
            totals[name] = totals.get(name, 0.0) + (value or 0.0)
    grand = sum(totals.values()) or 1.0

    traces.sort(key=lambda t: t["duration_ms"] or 0, reverse=True)
    return {
        "status": "ok",
        "service": service,
        "window": win.payload(),
        "sampled": total_sampled,
        "returned": len(traces),
        "sampling": f"{_SUBWINDOWS} sub-windows across the period, "
                    f"most-recent-first within each",
        "duration_ms": {"p50": rnd(percentile(durations, 50)),
                        "p95": rnd(percentile(durations, 95)),
                        "max": rnd(max(durations)) if durations else None},
        "self_time_ms": {name: {"total": rnd(value),
                                "pct": rnd(100.0 * value / grand)}
                         for name, value in sorted(totals.items(),
                                                   key=lambda kv: -kv[1])},
        "self_time_means": (
            "time each service spent in its own work, excluding time it spent "
            "waiting on downstream calls. High self-time is where the latency "
            "originates; high duration with low self-time means the service is "
            "blocked on something else."),
        "traces": traces[:limit],
        "notes": list(win.notes),
    }
