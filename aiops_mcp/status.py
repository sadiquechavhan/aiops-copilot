"""
telemetry_status -- is the observability stack actually answering?

WHY THIS IS A TOOL AND NOT A README LINE
----------------------------------------
Without it, "Prometheus is down" is something the model can only INFER from a
failed query -- and a failed query has several causes, only one of which is an
outage. Making liveness directly askable means the model can check before
concluding, and can tell the operator something true and specific instead of
"the query did not work".

It also answers the question that makes every other answer interpretable: how
far back can I see? A window older than retention is not empty, it is
unavailable, and those are different facts about the world.

REAL HTTP REQUESTS, ON PURPOSE
------------------------------
Not `docker compose ps`. Session 1 lost time to a container that was healthy and
unreachable from the host, and Session 2 to Grafana returning 000 for 25s while
ps said "Up 2 minutes" -- Grafana has no healthcheck defined, so ps had nothing
to report and reported nothing alarming. Absence of alarming health information
reads as fine, and is not. The only question worth asking is whether the API
answers when called.
"""

import time

from . import (JAEGER_TTL_HOURS, JAEGER_URL, PROMETHEUS_URL,
               PROM_RETENTION_HOURS, SERVICES)
from .http import get_json, reachable
from .window import utc_iso


def _prom_oldest():
    """Oldest timestamp Prometheus can actually answer for.

    Computed, not assumed. Retention is a CEILING, not a promise: if the stack
    started an hour ago there is one hour of data regardless of the 24h flag,
    and telling the model it can see 24h back would make every older window look
    like an outage rather than like a young database.
    """
    now = time.time()
    horizon = now - PROM_RETENTION_HOURS * 3600
    try:
        payload = get_json(
            PROMETHEUS_URL, "/api/v1/query_range",
            {"query": "sum(up)", "start": f"{horizon:.0f}",
             "end": f"{now:.0f}", "step": "300"}, 10.0, "prometheus")
    except Exception:
        return None
    results = (payload.get("data") or {}).get("result") or []
    stamps = [v[0] for r in results for v in r.get("values", [])]
    return utc_iso(min(stamps)) if stamps else None


def _reporting_services():
    """Which services have reported a metric in the last 5 minutes.

    A service that is up but not exporting is invisible to every other tool
    here, and looks exactly like a service with no traffic. Worth stating
    explicitly rather than leaving to be discovered by an empty result.
    """
    try:
        payload = get_json(
            PROMETHEUS_URL, "/api/v1/query",
            {"query": 'count by (service_name) '
                      '(up{job!=""} or count by (service_name) '
                      '(http_server_request_duration_seconds_count))'},
            10.0, "prometheus")
    except Exception:
        return None
    names = set()
    for entry in (payload.get("data") or {}).get("result", []):
        name = entry.get("metric", {}).get("service_name")
        if name:
            names.add(name)
    return sorted(names)


def telemetry_status():
    prom_ok, prom_detail = reachable(PROMETHEUS_URL, "/-/healthy")
    jaeger_ok, jaeger_detail = reachable(JAEGER_URL, "/")

    prometheus = {"reachable": prom_ok, "detail": prom_detail,
                  "url": PROMETHEUS_URL,
                  "retention_hours": PROM_RETENTION_HOURS}
    if prom_ok:
        oldest = _prom_oldest()
        prometheus["oldest_queryable"] = oldest
        reporting = _reporting_services()
        if reporting is not None:
            prometheus["reporting_services"] = reporting
            missing = [s for s in SERVICES if s not in reporting]
            if missing:
                # Stated as a finding, because a silently missing service is
                # the failure mode where every other tool returns a truthful
                # empty result about a broken system.
                prometheus["not_reporting"] = missing

    jaeger = {"reachable": jaeger_ok, "detail": jaeger_detail,
              "url": JAEGER_URL, "retention_hours": JAEGER_TTL_HOURS,
              "sampling": "always_on (no sampling loss)"}

    both = prom_ok and jaeger_ok
    return {
        "status": "ok" if both else "degraded",
        "now": utc_iso(time.time()),
        "prometheus": prometheus,
        "jaeger": jaeger,
        "notes": [] if both else [
            "at least one backend is unreachable; metric or trace queries "
            "against it will fail with backend_unreachable rather than return "
            "empty results."],
    }
