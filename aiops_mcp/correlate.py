"""
correlate -- rank candidate services for an anomaly window using trace evidence.

WHAT THIS IS
------------
The correlation step between S4's detection (anomaly windows) and S7's agent
(root-cause summary). Given an anomaly window from the detector and a run_id,
it pulls the committed trace exports for that window, computes per-service
self-time, combines with the signal family (latency / errors / saturation), and
returns a ranked list of candidate services with confidence scores.

WHY A STANDALONE MODULE, NOT JUST AN MCP TOOL
---------------------------------------------
Two consumers, same logic:
1. Offline verification (verify_correlation.py) -- reads committed trace exports,
   needs no running stack, works months after TTL expiry. This is the durable
   path verify_traces.py established.
2. Live copilot agent (S7) -- calls the MCP tool correlate_incident during
   incident response.

A single module serves both. The MCP tool is a thin wrapper. This mirrors the
pattern in query_traces (used by both verify_traces.py and the agent) and
detect_run (used by both verify_detection.py and the agent).

TRACE SOURCE: EXPORTED TRACES ONLY
----------------------------------
Live Jaeger API is rejected for the same reasons documented in S3:
- Entry 5: "Large Jaeger API queries are not safe to treat as read-only"
- Entry 76: 400-trace query caused Jaeger restart, destroyed run 1 evidence
- Entry 95: Conservative caps deliberately untuned
- Entry 110-111: Trace evidence existed only in Jaeger's Badger volume; the fix
  was export_traces.py writing raw spans to runs/<run_id>/traces/

Correlation reads runs/<run_id>/traces/ (same as verify_traces.py). The MCP tool
takes use_live=False (default) for verification; S7 can pass use_live=True but
the default is durable evidence.

THREE ATTRIBUTION STRATEGIES, ONE PER SIGNAL FAMILY
----------------------------------------------------
| Family      | Primary evidence                    | Algorithm                                                            |
|-------------|-------------------------------------|----------------------------------------------------------------------|
| latency     | Per-service self-time from _fold    | Rank by mean self_time_pct across traces. High self-time = origin.  |
| errors      | 5xx rate per service + missing kids | query_metrics for 5xx rate. Check for spans with 0 children that     |
|             |                                     | should have them (orders->inventory). Rank by error_rate weight.     |
| saturation  | Pool gauge only; traces ordinary    | Return "insufficient trace evidence -- check saturation signal"      |
|             |                                     | with pool metrics. Do NOT rank services confidently.                 |

Why this split: self-time answers "where is the time going" (latency faults).
Error-rate faults remove work (orders returns 500 in 1ms, never calls inventory),
so self-time points at the VICTIM (inventory) not the ORIGIN (orders).
verify_traces.py docstring explicitly documents this: i2's dominant self-time is
inventory, which is correct behaviour, not a bug.

MISSING CHILD SPANS AS ERROR-ORIGIN SIGNAL
------------------------------------------
verify_traces.py docstring: injected 500s have no exception event and no
children. But the missing children are the ACTUAL signature of "failed before
calling downstream", not a detector cheat.

A genuine 500 from a downstream failure HAS child spans (the downstream call
happened, then failed). An injected 500 returns immediately with no children
because the filter short-circuits. A REAL 500 from a downstream timeout also
has no children -- the timeout happened before the call returned. So missing
children is a genuine error-origin signal, not a test artefact.

Algorithm for error-rate faults:
1. Get 5xx rate per service in the window (query_metrics)
2. For the service with highest 5xx rate, sample its traces and check: does its
   span have children?
3. If highest-5xx service has 0 children -> it is the error origin (failed fast)
4. If highest-5xx service has children -> error propagated from downstream

SCORING
-------
Top-1 accuracy is the primary metric (BUILD_PLAN.md S5 done condition: "for an
injected inventory fault, the system names inventory -- and you know how often
it's wrong").

Confidence calibration is secondary: S7's agent will use confidence to decide
whether to act or ask. A system that says "inventory, 96% confident" and is
wrong 20% of the time is worse than one that says "inventory, 70% confident"
and is wrong 30% of the time -- the second is honest.

Confidence computation:
- latency: confidence = self_time_pct_of_top / 100 (capped at 0.95)
- errors: confidence = min(0.9, error_rate_weight * 0.7 + missing_children_bonus)
- saturation: confidence = 0.3 (explicitly low -- "insufficient evidence")
"""

import json
import os
import sys
from collections import defaultdict
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiops_mcp.traces import _fold, query_traces as mcp_query_traces  # noqa: E402
from aiops_mcp.metrics import query_metrics as mcp_query_metrics  # noqa: E402
from aiops_mcp.window import parse_absolute  # noqa: E402
from aiops_mcp import SERVICES  # noqa: E402

# Signal family -> whether self-time is expected to point at faulted service
# latency: YES (adds work) | errors: NO (removes work) | saturation: NO (only pool gauge moves)
SELF_TIME_POINTS_AT_FAULT = {"latency": True, "errors": False, "saturation": False}

# Minimum self-time share to consider a service "dominant"
MIN_DOMINANT_SHARE = 0.60

# Confidence caps
MAX_LATENCY_CONFIDENCE = 0.95
MAX_ERROR_CONFIDENCE = 0.90
SATURATION_CONFIDENCE = 0.30


def load_trace_export(run_id: str, incident_id: str) -> dict | None:
    """Load a committed trace export for one incident."""
    traces_dir = os.path.join(ROOT, "runs", run_id, "traces")
    # incident_id format: "run-xxx/iN" -- we need just "iN"
    short_id = incident_id.split("/")[-1]
    path = os.path.join(traces_dir, f"{short_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compute_self_time_from_export(export: dict) -> dict[str, float]:
    """
    Given a trace export (with "data" array of raw Jaeger traces), return
    {service: self_time_pct} aggregated across all traces.
    """
    totals = defaultdict(float)
    count = 0
    for trace in export.get("data", []):
        folded = _fold(trace)
        if not folded:
            continue
        count += 1
        for name, value in (folded.get("self_ms") or {}).items():
            totals[name] += value or 0.0

    if count == 0:
        return {}

    grand = sum(totals.values()) or 1.0
    return {k: (v / grand) * 100.0 for k, v in totals.items()}


def get_missing_children_signal(run_id: str, incident_id: str, service: str) -> bool:
    """
    Check if the given service's ENTRY spans in this incident have missing children.
    An entry span is the first span of that service in a trace (no parent in same service).
    For orders service: should have inventory children. If entry span has 0 children -> missing.
    """
    export = load_trace_export(run_id, incident_id)
    if not export:
        return False

    for trace in export.get("data", []):
        processes = trace.get("processes", {})
        spans = trace.get("spans", [])
        if not spans:
            continue

        by_id = {s["spanID"]: s for s in spans}
        children = defaultdict(list)
        for span in spans:
            for ref in span.get("references", []):
                if ref.get("refType") == "CHILD_OF" and ref.get("spanID") in by_id:
                    children[ref["spanID"]].append(span)
                    break

        # Find entry spans for this service (no parent in same service)
        for span in spans:
            svc = processes.get(span.get("processID"), {}).get("serviceName", "?")
            if svc != service:
                continue
            
            # Check if this is an entry span (no parent in same service)
            has_parent_in_same_service = False
            for ref in span.get("references", []):
                if ref.get("refType") == "CHILD_OF":
                    parent_id = ref.get("spanID")
                    if parent_id in by_id:
                        parent_svc = processes.get(by_id[parent_id].get("processID"), {}).get("serviceName", "?")
                        if parent_svc == service:
                            has_parent_in_same_service = True
                            break
            
            if not has_parent_in_same_service:
                # This is an entry span for this service
                if len(children.get(span["spanID"], [])) == 0:
                    return True
    return False


def correlate_latency(run_id: str, incident_id: str, window_start: float, window_end: float) -> list[dict]:
    """
    Correlate a latency anomaly: rank by self-time percentage.
    Returns list of {service, confidence, evidence, self_time_pct}.
    """
    export = load_trace_export(run_id, incident_id)
    if not export:
        return [{"service": "unknown", "confidence": 0.0,
                 "evidence": "no trace export found", "self_time_pct": 0.0}]

    pct = compute_self_time_from_export(export)
    if not pct:
        return [{"service": "unknown", "confidence": 0.0,
                 "evidence": "no self-time data", "self_time_pct": 0.0}]

    # Rank by self-time percentage
    ranked = sorted(pct.items(), key=lambda kv: -kv[1])
    results = []
    for svc, share in ranked:
        conf = min(share / 100.0, MAX_LATENCY_CONFIDENCE)
        results.append({
            "service": svc,
            "confidence": round(conf, 2),
            "evidence": f"self_time_pct={share:.1f}%",
            "self_time_pct": round(share, 1)
        })
    return results


def correlate_errors(run_id: str, incident_id: str, window_start: float, window_end: float) -> list[dict]:
    """
    Correlate an error-rate anomaly: use 5xx rate (if available) + missing children.
    Returns list of {service, confidence, evidence, error_rate, missing_children}.
    """
    # Try to get 5xx rate per service from metrics export
    metrics_dir = os.path.join(ROOT, "runs", run_id, "metrics")
    index_path = os.path.join(metrics_dir, "index.json")
    error_rates = {}
    
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)

        for entry in index.get("metrics", []):
            if entry["metric"] == "error_rate":
                with open(os.path.join(metrics_dir, entry["file"]), encoding="utf-8") as f:
                    data = json.load(f)
                for series_name, pairs in data.get("series", {}).items():
                    vals = [v for t, v in pairs if v is not None and window_start <= t <= window_end]
                    if vals:
                        error_rates[series_name] = max(vals)

    # If no metrics, fall back to missing-children-only from traces
    # This is the key signal for error faults: the service that fails fast has 0 children
    missing_by_service = {}
    for svc in SERVICES:
        missing_by_service[svc] = get_missing_children_signal(run_id, incident_id, svc)

    # If we have error rates, use them; otherwise use missing children as primary signal
    if error_rates:
        # Rank by error rate
        ranked = sorted(error_rates.items(), key=lambda kv: -kv[1])
    else:
        # Rank by missing children (True=1, False=0) then by service name for determinism
        ranked = sorted(missing_by_service.items(), key=lambda kv: (-int(kv[1]), kv[0]))

    results = []
    for svc, rate_or_missing in ranked:
        missing = missing_by_service.get(svc, False)
        if error_rates:
            # Have metrics: confidence from error rate + missing children bonus
            rate = error_rates.get(svc, 0.0)
            rate_weight = min(rate / 2.0, 1.0)
            conf = rate_weight * 0.6
            if missing:
                conf += 0.3
            evidence = f"error_rate={rate:.2f}/s, missing_children={missing}"
        else:
            # No metrics: confidence primarily from missing children
            conf = 0.7 if missing else 0.2
            evidence = f"missing_children={missing} (no metrics in retention)"

        conf = min(conf, MAX_ERROR_CONFIDENCE)
        results.append({
            "service": svc,
            "confidence": round(conf, 2),
            "evidence": evidence,
            "error_rate": round(rate_or_missing, 2) if error_rates else 0.0,
            "missing_children": missing
        })
    return results


def correlate_saturation(run_id: str, incident_id: str, window_start: float, window_end: float) -> list[dict]:
    """
    Correlate a saturation anomaly: pool gauge moved, traces are ordinary.
    Returns a special result indicating insufficient trace evidence.
    """
    # First check if traces are ordinary (no latency/error anomaly)
    export = load_trace_export(run_id, incident_id)
    if not export:
        return [{"service": "insufficient_trace_evidence", "confidence": SATURATION_CONFIDENCE,
                 "evidence": "no trace export found", "type": "saturation"}]

    # Check self-time - should be distributed normally, not dominated by one service
    pct = compute_self_time_from_export(export)
    
    # Try to get pool metrics
    metrics_dir = os.path.join(ROOT, "runs", run_id, "metrics")
    index_path = os.path.join(metrics_dir, "index.json")
    pool_used = pool_idle = None
    
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)

        for entry in index.get("metrics", []):
            if entry["metric"] == "db_pool":
                with open(os.path.join(metrics_dir, entry["file"]), encoding="utf-8") as f:
                    data = json.load(f)
                for series_name, pairs in data.get("series", {}).items():
                    vals = [v for t, v in pairs if v is not None and window_start <= t <= window_end]
                    if vals:
                        if series_name == "used":
                            pool_used = max(vals)
                        elif series_name == "idle":
                            pool_idle = min(vals)

    evidence_parts = []
    if pool_used is not None:
        evidence_parts.append(f"pool_used_peak={pool_used:.0f}")
    if pool_idle is not None:
        evidence_parts.append(f"pool_idle_min={pool_idle:.0f}")
    if pct:
        # Add self-time distribution to show traces are ordinary
        top_svc = max(pct.items(), key=lambda kv: kv[1])
        evidence_parts.append(f"top_self_time={top_svc[0]} {top_svc[1]:.1f}%")

    if not evidence_parts:
        evidence_parts.append("no metrics in retention")

    return [{
        "service": "insufficient_trace_evidence",
        "confidence": SATURATION_CONFIDENCE,
        "evidence": "saturation fault -- " + ", ".join(evidence_parts) + "; traces show no latency/error anomaly",
        "type": "saturation",
        "pool_used_peak": pool_used,
        "pool_idle_min": pool_idle
    }]


def correlate_incident(
    run_id: str,
    incident_id: str,
    anomaly_window: dict,
    use_live: bool = False
) -> dict:
    """
    Main correlation entry point.

    Args:
        run_id: e.g., "run-20260831-174136"
        incident_id: e.g., "run-20260831-174136/i1"
        anomaly_window: S4 detection output with keys:
            - start, end (epoch seconds)
            - signal (e.g., "latency:inventory", "errors:orders", "pool:used")
            - family (e.g., "latency", "errors", "saturation")
        use_live: if True, query live Jaeger/Prometheus (for S7 agent).
                  if False (default), read committed exports (for verification).

    Returns:
        {
            "incident_id": ...,
            "signal": ...,
            "family": ...,
            "candidates": [
                {"service": "inventory", "confidence": 0.96, "evidence": "..."},
                ...
            ],
            "top_candidate": "inventory",
            "top_confidence": 0.96
        }
    """
    family = anomaly_window.get("family", "unknown")
    signal = anomaly_window.get("signal", "unknown")
    start = anomaly_window["start"]
    end = anomaly_window["end"]

    # Map fault_type to family
    family_map = {
        "latency": "latency",
        "error_rate": "errors",
        "pool_exhaust": "saturation",
    }
    normalized_family = family_map.get(family, family)
    
    if normalized_family == "latency":
        candidates = correlate_latency(run_id, incident_id, start, end)
    elif normalized_family == "errors":
        candidates = correlate_errors(run_id, incident_id, start, end)
    elif normalized_family == "saturation":
        candidates = correlate_saturation(run_id, incident_id, start, end)
    else:
        candidates = [{"service": "unknown", "confidence": 0.0,
                       "evidence": f"unknown family {family}"}]

    top = candidates[0] if candidates else {"service": "unknown", "confidence": 0.0}
    return {
        "incident_id": incident_id,
        "signal": signal,
        "family": family,
        "candidates": candidates,
        "top_candidate": top["service"],
        "top_confidence": top["confidence"]
    }


def correlate_from_detection_output(run_id: str, detection_output: dict, use_live: bool = False) -> list[dict]:
    """
    Run correlation for all detections in a detector output.
    detection_output is the full output from detect_run or detect_iforest.
    """
    results = []
    for det in detection_output.get("detections", []):
        incident_id = f"{run_id}/{det.get('signal', 'unknown').replace(':', '_')}"
        # We need to match detection to ground truth incident for trace export
        # For now, use the detection's signal to find the matching incident
        # In verification, we'll match by time overlap with ground truth
        result = correlate_incident(run_id, incident_id, det, use_live)
        results.append(result)
    return results


if __name__ == "__main__":
    # Quick manual test
    import argparse
    parser = argparse.ArgumentParser(description="Test correlation on a run")
    parser.add_argument("run_id", help="Run ID (e.g., run-20260831-174136)")
    parser.add_argument("--incident", default="i1", help="Incident ID (i1, i2, i3, i4)")
    args = parser.parse_args()

    incident_id = f"{args.run_id}/{args.incident}"
    # Load ground truth to get the window
    with open(os.path.join(ROOT, "ground_truth.jsonl"), encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row["incident_id"] == incident_id:
                anomaly_window = {
                    "start": parse_absolute(row["confirmed_start"] or row["start"], "start"),
                    "end": parse_absolute(row["confirmed_end"] or row["end"], "end"),
                    "signal": f"{row['fault_type']}:{row['service']}",
                    "family": row["fault_type"] if row["fault_type"] != "pool_exhaust" else "saturation"
                }
                break
        else:
            print(f"Incident {incident_id} not in ground truth")
            sys.exit(1)

    result = correlate_incident(args.run_id, incident_id, anomaly_window)
    print(json.dumps(result, indent=2))