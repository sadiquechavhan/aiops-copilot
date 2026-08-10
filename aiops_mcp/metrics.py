"""
query_metrics -- Prometheus, behind a closed vocabulary.

WHY THE MODEL DOES NOT WRITE PromQL
-----------------------------------
The obvious tool is query_metrics(promql, start, end). It is curl with extra
steps, and it adds a failure mode: a wrong PromQL query and a healthy quiet
system return the same thing -- an empty result that looks like an answer.

The ways to be wrong here are not hypothetical. They are all in the engineering
log, each having cost real time:

  * the histogram is http_server_request_duration_seconds_{count,bucket,sum};
    guessing http_server_requests_seconds (the Micrometer name) returns nothing
  * the label is service_name, not service or job -- and it only exists because
    the collector sets resource_to_telemetry_conversion (entry 35)
  * collector-internal metrics carry NO _total suffix:
    otelcol_exporter_sent_spans, not otelcol_exporter_sent_spans_total (entry 39)
  * omitting http_route!~"/health|/chaos.*" mixes operator traffic into
    application numbers; a single 501 from the chaos control plane put a
    permanent fake 5xx series on the dashboard (entry 64 / what-broke 3)
  * the 5xx series DOES NOT EXIST until the first 500 occurs, so "no errors" and
    "wrong query" are the same empty vector without `or vector(0)`

Every one of those returns a valid, empty, plausible result. So the tool owns
the PromQL and the model owns the question: five metric names, three service
names. The domain knowledge is compiled in where it is right once, instead of
living in a prompt where it costs tokens every turn and is wrong silently.

The cost, stated plainly: a question I did not anticipate cannot be asked. No
raw-PromQL escape hatch this session -- it would re-open the silent-wrongness
hole for a benefit that is currently hypothetical. The discipline is to count
the questions the vocabulary cannot express, and add query_metrics_raw as a
SEPARATE tool when there is a third real one, so its cost is explicit.

The response echoes the PromQL it ran (~40 tokens), which turns the tool from a
black box into something both the model and the operator can check.
"""

import time

from . import (APP_ROUTE_EXCLUSION, FETCH_POINT_CAP, POINT_BUDGET,
               PROMETHEUS_URL, PROM_RETENTION_HOURS, PROM_TIMEOUT_S, SERVICES)
from .http import get_json
from .util import BackendError, BadArgument, percentile, rnd
from .window import SCRAPE_INTERVAL_S, utc_iso

# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------
METRICS = ("latency", "request_rate", "error_rate", "error_ratio", "db_pool")
PERCENTILES = (50, 95, 99)

_APP_ONLY = f'http_route!~"{APP_ROUTE_EXCLUSION}"'


def _svc_selector(service):
    if service == "all":
        return ""
    return f'service_name="{service}", '


# `sum by (le, service_name)` and not `sum by (le)`: grouping by service_name
# keeps the three services as separate series in one query. The scenario runner
# groups by le alone because it asks about one service at a time; doing that
# here would silently merge all three into one number whenever service="all".
_Q_LATENCY = ('histogram_quantile({q}, sum by (le, service_name) (rate('
              'http_server_request_duration_seconds_bucket'
              '{{{svc}{app}}}[{rate}])))')

_Q_RATE = ('sum by (service_name) (rate('
           'http_server_request_duration_seconds_count{{{svc}{app}}}[{rate}]))')

# NOTE ON `or vector(0)`, which the scenario runner uses here and this does not.
#
# The 5xx series does not exist until the first 500 occurs, so a healthy system
# and a typo both return an empty vector. scenario_runner.py fixes that with
# `or vector(0)` -- correct there, because it uses bare sum() and asks about one
# service at a time.
#
# It cannot be used here. vector(0) carries NO LABELS, so under
# `sum by (service_name)` it comes back as a series whose service_name is empty
# -- a phantom fourth service, reported to the model as fact. Trading a silent
# empty for a confident fabrication is the wrong direction.
#
# So the empty case is resolved AFTER the query instead, by asking Prometheus
# whether the 5xx series exists at all (_series_exists). That distinguishes
# "nothing has ever failed" from "the window is quiet" without inventing a row.
_Q_ERRORS = ('sum by (service_name) (rate('
             'http_server_request_duration_seconds_count'
             '{{{svc}http_response_status_code=~"5..", {app}}}[{rate}]))')

# Ratio, not rate: 20% of requests failing is the fault Session 2 injects (i2),
# and a rate in req/s cannot be compared against it without also knowing total
# throughput. The denominator repeats the exclusion so numerator and denominator
# describe the same population -- otherwise health probes dilute the ratio.
_Q_ERROR_RATIO = (
    '100 * (sum by (service_name) (rate('
    'http_server_request_duration_seconds_count'
    '{{{svc}http_response_status_code=~"5..", {app}}}[{rate}]))'
    ' / sum by (service_name) (rate('
    'http_server_request_duration_seconds_count{{{svc}{app}}}[{rate}])))')

# HikariCP, inventory only -- the only service with a datasource. This is
# Session 2's i3 signal: pool saturation with NO latency or error impact, the
# deliberate negative case a golden-signal detector should miss.
_Q_POOL = 'sum by (state) (db_client_connections_usage{{pool_name="inventory-pool"}})'


def _rate_window(duration_s):
    """The [Nm] inside rate(), sized to the question being asked.

    A rate window shorter than ~2 scrape intervals can contain a single sample
    and produce nothing; much longer than the question smears the answer across
    time the caller did not ask about. Session 2 measured both failure modes: a
    5m window evaluated inside a 4-minute incident still contained pre-incident
    samples and understated the fault, and a 2m window looking back further than
    the gap between incidents made incident N's baseline contain incident N-1.
    """
    seconds = max(2 * SCRAPE_INTERVAL_S, min(300, duration_s / 4.0))
    steps = max(2, int(round(seconds / SCRAPE_INTERVAL_S)))
    return f"{steps * SCRAPE_INTERVAL_S}s"


def _query(expr, at=None):
    params = {"query": expr}
    if at is not None:
        params["time"] = f"{at:.3f}"
    payload = get_json(PROMETHEUS_URL, "/api/v1/query", params,
                       PROM_TIMEOUT_S, "prometheus")
    return _unwrap(payload, expr)


def _query_range(expr, window, step):
    params = {"query": expr, "start": f"{window.start:.3f}",
              "end": f"{window.end:.3f}", "step": str(step)}
    payload = get_json(PROMETHEUS_URL, "/api/v1/query_range", params,
                       PROM_TIMEOUT_S, "prometheus")
    return _unwrap(payload, expr)


def _unwrap(payload, expr):
    """Prometheus can answer 200 with status:"error" -- that is a backend_error.

    Worth knowing rather than assuming: a 200 is not automatically success here.
    Treating it as one would report a rejected query as an empty result, which
    is the single failure this session is supposed to prevent.
    """
    if not isinstance(payload, dict):
        raise BackendError("prometheus returned a non-object response",
                           target="prometheus")
    if payload.get("status") != "success":
        raise BackendError(
            f"prometheus rejected the query: {payload.get('error', 'unknown')}",
            target="prometheus", promql=expr[:200])
    return payload.get("data", {})


def _series_exists(metric_name, service, extra_selector=""):
    """Does this series exist AT ALL, anywhere in retention?

    Called only on the empty path, so it costs nothing in the common case. It is
    what separates the two reasons a result can be empty:

        series_absent        -- no such series has ever existed. For 5xx this is
                                the real answer "nothing has ever failed", which
                                is TRUE and useful, not a failure.
        no_samples_in_window -- the series exists but this window is quiet, or
                                the stack was down for it.

    Session 2 has a metric that legitimately does not exist until the first 500
    occurs. Reporting that as an error would make a healthy system look broken;
    reporting it as a plain empty result would hide a real difference.

    `extra_selector` is load-bearing for the error metrics and was missing in
    the first version of this function, which asked whether
    http_server_request_duration_seconds_count existed at all. It always does --
    so a service that had simply never returned a 500 was reported as
    "no traffic, or the stack was not running", which is FALSE. The probe has to
    ask about the same series the query asked about, 5xx filter included.
    """
    parts = []
    if service != "all":
        parts.append(f'service_name="{service}"')
    if extra_selector:
        parts.append(extra_selector)
    selector = "{" + ",".join(parts) + "}" if parts else ""
    # An EXPLICIT retention-wide range. /api/v1/series without start/end does
    # not mean "all of time" -- it applies a default lookback, so a series that
    # went quiet an hour ago reads as absent and the caller is told "this has
    # never existed" about something that plainly has. The claim being made here
    # is "anywhere in retention", so the request has to say that.
    span = {"match[]": f"{metric_name}{selector}",
            "start": f"{time.time() - PROM_RETENTION_HOURS * 3600:.0f}",
            "end": f"{time.time():.0f}"}
    try:
        payload = get_json(PROMETHEUS_URL, "/api/v1/series", span,
                           PROM_TIMEOUT_S, "prometheus")
    except Exception:
        return None                       # unknown beats a guess
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None
    return bool(payload.get("data"))


def _downsample(points, budget):
    """Reduce to `budget` points by MAX over each bucket, not by sampling.

    Max, not mean or stride: the caller is looking for incidents. Averaging a
    4-minute latency spike into a 24-hour window makes it disappear -- the exact
    signal the tool exists to surface. Taking the max preserves "something bad
    happened here" at the cost of overstating the typical value, and the headline
    percentiles alongside are computed from the full-resolution data anyway, so
    nothing authoritative rests on these points.
    """
    if len(points) <= budget:
        return points
    size = len(points) / float(budget)
    out = []
    for i in range(budget):
        chunk = points[int(i * size):max(int(i * size) + 1, int((i + 1) * size))]
        real = [v for v in chunk if v is not None]
        out.append(max(real) if real else None)
    return out


def _collect(data):
    """Prometheus matrix -> {service: [values]} plus the timestamps.

    NaN is dropped rather than propagated: histogram_quantile emits NaN for a
    bucket with no observations, and a NaN in JSON is not valid JSON at all --
    json.dumps writes a bare `NaN` token that strict parsers reject.
    """
    series, stamps = {}, []
    for entry in data.get("result", []):
        name = entry.get("metric", {}).get("service_name") or \
               entry.get("metric", {}).get("state") or "all"
        values, times = [], []
        for pair in entry.get("values", []):
            try:
                value = float(pair[1])
            except (ValueError, IndexError, TypeError):
                continue
            times.append(float(pair[0]))
            values.append(None if value != value else value)
        series[name] = values
        if len(times) > len(stamps):
            stamps = times
    return series, stamps


def query_metrics(metric=None, service="all", window=None, start=None, end=None,
                  percentile_=95):
    """The tool. Returns a dict; the caller serialises it."""
    from .window import resolve                       # local: avoids a cycle

    if metric not in METRICS:
        raise BadArgument(f"unknown metric {metric!r}", field="metric",
                          known=list(METRICS))
    if service not in SERVICES + ("all",):
        raise BadArgument(f"unknown service {service!r}", field="service",
                          known=list(SERVICES) + ["all"])
    if percentile_ not in PERCENTILES:
        raise BadArgument(f"percentile must be one of {list(PERCENTILES)}",
                          field="percentile", known=list(PERCENTILES))
    if metric == "db_pool" and service not in ("inventory", "all"):
        raise BadArgument(
            f"db_pool exists only for inventory -- it is the only service with a "
            f"datasource. Got service={service!r}.", field="service",
            known=["inventory", "all"])

    win = resolve(window=window, start=start, end=end)
    rate = _rate_window(win.duration)
    svc, app = _svc_selector(service), _APP_ONLY

    if metric == "latency":
        expr = _Q_LATENCY.format(q=percentile_ / 100.0, svc=svc, app=app, rate=rate)
        unit, base = "ms", "http_server_request_duration_seconds_bucket"
        scale, probe = 1000.0, ""
    elif metric == "request_rate":
        expr = _Q_RATE.format(svc=svc, app=app, rate=rate)
        unit, base, scale, probe = "req/s", "http_server_request_duration_seconds_count", 1.0, ""
    elif metric == "error_rate":
        expr = _Q_ERRORS.format(svc=svc, app=app, rate=rate)
        unit, base, scale = "req/s", "http_server_request_duration_seconds_count", 1.0
        probe = f'http_response_status_code=~"5..",{_APP_ONLY}'
    elif metric == "error_ratio":
        expr = _Q_ERROR_RATIO.format(svc=svc, app=app, rate=rate)
        unit, base, scale = "%", "http_server_request_duration_seconds_count", 1.0
        probe = f'http_response_status_code=~"5..",{_APP_ONLY}'
    else:
        expr = _Q_POOL.format()
        unit, base, scale, probe = "connections", "db_client_connections_usage", 1.0, ""

    # Fetch at native resolution where affordable, then downsample locally. The
    # alternative -- asking Prometheus for exactly 12 points -- makes Prometheus
    # pick one sample per step and DROP a spike that fell between them. Fetching
    # densely and taking the max per bucket keeps incidents visible. The cap
    # bounds the work: a 24h window at 15s would be 5,760 points per series.
    step = win.step(POINT_BUDGET)
    fetch_step = max(SCRAPE_INTERVAL_S,
                     int(win.duration / FETCH_POINT_CAP / SCRAPE_INTERVAL_S + 1)
                     * SCRAPE_INTERVAL_S)
    series, stamps = _collect(_query_range(expr, win, fetch_step))

    if not series or all(not any(v is not None for v in vs) for vs in series.values()):
        exists = _series_exists(base, service, probe)
        reason = ("series_absent" if exists is False else
                  "no_samples_in_window" if exists else "unknown")
        is_error_metric = metric in ("error_rate", "error_ratio")

        if reason == "series_absent" and is_error_metric:
            # The one empty result that is a real, positive answer rather than
            # an absence of one. Phrased as the finding it is, because a model
            # told "no data" will hedge or go looking again, while a model told
            # "zero errors" can answer the question it was asked.
            note = (f"no 5xx responses have EVER been recorded for "
                    f"service={service} on application routes within retention. "
                    f"The error series is created by the first 500, so this "
                    f"means zero errors -- not missing data.")
        elif reason == "series_absent":
            note = (f"no {base} series exists for service={service} anywhere in "
                    f"retention -- this metric has never been recorded for it.")
        elif reason == "no_samples_in_window":
            if is_error_metric:
                # For an error metric this is the ordinary healthy answer, and
                # it must not read as a data problem. The 5xx series is created
                # by the first 500 and then persists, so "the series exists but
                # this window has none" means ZERO ERRORS in this window. Saying
                # "no traffic, or the stack was not running" here would be a
                # false alarm about the observability stack -- the exact
                # direction of wrongness this tool exists to avoid.
                note = (f"no 5xx responses for service={service} on application "
                        f"routes in this window. The series exists (errors have "
                        f"occurred at other times within retention), so this is "
                        f"a measured zero, not missing data.")
            else:
                note = (f"the {base} series exists but has no samples in this "
                        f"window -- either no traffic, or the stack was not "
                        f"running. Try a wider window, or call telemetry_status.")
        else:
            note = ("could not determine whether the series exists; the "
                    "emptiness of this result is unexplained.")

        result = {"status": "empty", "metric": metric, "service": service,
                  "reason": reason, "window": win.payload(), "promql": expr,
                  "notes": win.notes + [note]}
        if is_error_metric and reason in ("series_absent", "no_samples_in_window"):
            # Stated as a number so the model does not have to infer one from
            # prose. Both empty reasons mean the same thing for an error metric:
            # zero. They differ only in whether errors have EVER been seen, and
            # that difference is in `reason` for anyone who cares.
            result["value"] = 0
        return result

    # For latency, the authoritative numbers are three instant queries whose rate
    # window is the whole duration. Done once here rather than per series.
    heads = headline_percentiles(service, win) if metric == "latency" else {}

    out = []
    for name in sorted(series):
        values = [v for v in series[name] if v is not None]
        if not values:
            continue
        scaled = [v * scale for v in values]
        points = _downsample([None if v is None else round(v * scale, 1)
                              for v in series[name]], POINT_BUDGET)

        row = {"service": name}
        if metric == "latency":
            # Headline percentiles come from a SINGLE INSTANT QUERY whose rate
            # window is the whole duration -- the real p50/p95/p99 over the
            # window. Averaging per-step percentiles is arithmetically wrong
            # (the mean of quantiles is not the quantile of the union) and is a
            # thing people ship. All three are returned because the caller
            # frequently wants the spread, and a second tool call to get it costs
            # far more than the ~12 tokens two extra numbers cost here.
            row.update(heads.get(name, {}))
            row["points_are"] = f"p{percentile_}"
        row.update({"min": rnd(min(scaled)), "max": rnd(max(scaled)),
                    "avg": rnd(sum(scaled) / len(scaled)),
                    "points": points})
        if stamps and series[name]:
            index = max(range(len(series[name])),
                        key=lambda i: (series[name][i] is not None, series[name][i] or 0))
            if index < len(stamps):
                row["peak_at"] = utc_iso(stamps[index])
        out.append(row)

    result = {"status": "ok", "metric": metric, "unit": unit,
              "window": win.payload(), "step_s": step,
              "series": out, "promql": expr, "notes": list(win.notes)}
    if metric == "latency":
        # Said once at the top level rather than repeated per point: `points` is
        # the requested percentile over time, while p50/p95/p99 on each row are
        # the window-wide values. Without this the model has two sets of numbers
        # that disagree and no stated reason why.
        result["reading_this"] = (
            f"p50/p95/p99 per service are computed over the whole window; "
            f"`points` are p{percentile_} per {step}s bucket (max-aggregated).")
    if metric != "db_pool":
        result["excluded_routes"] = ["/health", "/chaos*"]
    return result


def headline_percentiles(service, win):
    """p50/p95/p99 over the whole window, in one instant query per percentile.

    Separate from the range query on purpose. The range query answers "what did
    the shape look like"; this answers "what was the number". Computing the
    second from the first would be wrong -- see the note above about means of
    quantiles.
    """
    rate = f"{int(win.duration)}s"
    svc = _svc_selector(service)
    out = {}
    for p in PERCENTILES:
        expr = _Q_LATENCY.format(q=p / 100.0, svc=svc, app=_APP_ONLY, rate=rate)
        data = _query(expr, at=win.end)
        for entry in data.get("result", []):
            name = entry.get("metric", {}).get("service_name", "all")
            try:
                value = float(entry["value"][1])
            except (ValueError, KeyError, IndexError, TypeError):
                continue
            if value == value:
                out.setdefault(name, {})[f"p{p}"] = rnd(value * 1000.0)
    return out
