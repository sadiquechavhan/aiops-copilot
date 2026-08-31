"""
verify_correlation -- score the correlation against ground truth: top-1 accuracy.

Runs with NO stack. It reads the committed trace exports and the committed
labels, so it answers "does correlation name the right service, and how often
is it wrong" long after the run's data has left Jaeger -- the same
non-perishable check verify_traces is for self-time.

WHAT IS SCORED
--------------
For each labelled incident, correlation produces a ranked list of candidates.
Top-1 accuracy = incidents where top candidate == faulted service / total incidents.

Confidence calibration: for each confidence bucket (>0.9, 0.7-0.9, <0.7), what
fraction of predictions in that bucket are correct?

THE ASSERTIONS
--------------
The point of the run is a number, but a few things must hold:
  - Every latency fault (i1, i4) has top candidate == faulted service
  - Every error-rate fault (i2) has top candidate == faulted service
  - Pool exhaustion (i3) returns "insufficient_trace_evidence" with low confidence
  - No incident has high confidence (>0.8) on the WRONG service
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiops_mcp.correlate import correlate_incident  # noqa: E402
from aiops_mcp.window import parse_absolute         # noqa: E402

GROUND_TRUTH = os.path.join(ROOT, "ground_truth.jsonl")
RUNS = os.path.join(ROOT, "runs")


def load_incidents():
    """ground_truth.jsonl -> {run_id: [incident dicts]} using confirmed edges."""
    runs = {}
    if not os.path.exists(GROUND_TRUTH):
        raise SystemExit(f"no ground truth at {GROUND_TRUTH}")
    with open(GROUND_TRUTH, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            start = row.get("confirmed_start") or row["start"]
            end = row.get("confirmed_end") or row["end"]
            runs.setdefault(row["run_id"], []).append({
                "iid": row["incident_id"].split("/")[-1],
                "fault": row["fault_type"],
                "service": row["service"],
                "start": parse_absolute(start, "start"),
                "end": parse_absolute(end, "end"),
                "expected_signal": row.get("expected_signal", []),
                "params": row.get("params", {}),
            })
    for incidents in runs.values():
        incidents.sort(key=lambda i: i["start"])
    return runs


def _match_incident_to_export(incidents, run_id, incident_id_short):
    """Find the ground truth incident matching an export file."""
    for inc in incidents.get(run_id, []):
        if inc["iid"] == incident_id_short:
            return inc
    return None


def score_correlation(incidents, correlations):
    """
    Match correlation results to incidents and compute top-1 accuracy + confidence calibration.
    """
    per_incident = []
    for run_id, corr_results in correlations.items():
        for corr in corr_results:
            inc_id_short = corr["incident_id"].split("/")[-1]
            inc = _match_incident_to_export(incidents, run_id, inc_id_short)
            if not inc:
                print(f"  WARNING: {corr['incident_id']} not in ground truth")
                continue

            top = corr["top_candidate"]
            top_conf = corr["top_confidence"]
            fault_type = inc["fault"]
            
            # For pool_exhaust, correct prediction is "insufficient_trace_evidence"
            if fault_type == "pool_exhaust":
                correct = (top == "insufficient_trace_evidence")
                expected = "insufficient_trace_evidence"
            else:
                correct = (top == inc["service"])
                expected = inc["service"]

            per_incident.append({
                "incident_id": corr["incident_id"],
                "fault_type": fault_type,
                "expected_service": expected,
                "top_candidate": top,
                "top_confidence": top_conf,
                "correct": correct,
                "all_candidates": corr["candidates"],
            })

    # Top-1 accuracy
    total = len(per_incident)
    correct = sum(1 for p in per_incident if p["correct"])
    top1_accuracy = correct / total if total else 0.0

    # Per-fault-type breakdown
    by_fault = {}
    for p in per_incident:
        ft = p["fault_type"]
        if ft not in by_fault:
            by_fault[ft] = {"total": 0, "correct": 0}
        by_fault[ft]["total"] += 1
        if p["correct"]:
            by_fault[ft]["correct"] += 1

    # Confidence calibration
    buckets = {">0.9": {"total": 0, "correct": 0},
               "0.7-0.9": {"total": 0, "correct": 0},
               "<0.7": {"total": 0, "correct": 0}}
    for p in per_incident:
        conf = p["top_confidence"]
        if conf > 0.9:
            b = ">0.9"
        elif conf >= 0.7:
            b = "0.7-0.9"
        else:
            b = "<0.7"
        buckets[b]["total"] += 1
        if p["correct"]:
            buckets[b]["correct"] += 1

    # High confidence on WRONG service
    high_conf_wrong = [p for p in per_incident if not p["correct"] and p["top_confidence"] > 0.8]

    return {
        "per_incident": per_incident,
        "top1_accuracy": top1_accuracy,
        "total": total,
        "correct": correct,
        "by_fault_type": by_fault,
        "confidence_buckets": buckets,
        "high_confidence_wrong": high_conf_wrong,
    }


def _pct(x):
    return f"{100 * x:5.1f}%"


def print_results(run_id, results):
    """Print one run's results."""
    print(f"\n{'=' * 78}\n{run_id}  ({results['total']} incidents)\n{'=' * 78}")

    print(f"{'inc':4}{'fault':15}{'expected':12}{'predicted':12}{'conf':>6}{'correct':>8}")
    for p in results["per_incident"]:
        status = "YES" if p["correct"] else "NO "
        print(f"{p['incident_id']:4}{p['fault_type']:15}{p['expected_service']:12}"
              f"{p['top_candidate']:12}{p['top_confidence']:6.2f}{status:>8}")

    print(f"\n  Top-1 accuracy: {results['correct']}/{results['total']} ({_pct(results['top1_accuracy'])})")

    print("\n  Per fault type:")
    for ft, data in results["by_fault_type"].items():
        acc = data["correct"] / data["total"] if data["total"] else 0
        print(f"    {ft:15} {data['correct']}/{data['total']} ({_pct(acc)})")

    print("\n  Confidence calibration:")
    for b, data in results["confidence_buckets"].items():
        if data["total"] > 0:
            acc = data["correct"] / data["total"]
            print(f"    {b:>6}: {data['correct']}/{data['total']} correct ({_pct(acc)})")

    if results["high_confidence_wrong"]:
        print("\n  HIGH CONFIDENCE ON WRONG SERVICE (FAIL):")
        for p in results["high_confidence_wrong"]:
            print(f"    {p['incident_id']}: predicted {p['top_candidate']} "
                  f"({p['top_confidence']:.2f}), expected {p['expected_service']}")


def check_assertions(results):
    """Return list of assertion failures (empty = all held)."""
    failures = []

    # 1. Every latency fault has correct top candidate
    for p in results["per_incident"]:
        if p["fault_type"] == "latency" and not p["correct"]:
            failures.append(f"{p['incident_id']}: latency fault -- expected {p['expected_service']}, "
                            f"got {p['top_candidate']} (conf={p['top_confidence']:.2f})")

    # 2. Every error-rate fault has correct top candidate
    for p in results["per_incident"]:
        if p["fault_type"] == "error_rate" and not p["correct"]:
            failures.append(f"{p['incident_id']}: error_rate fault -- expected {p['expected_service']}, "
                            f"got {p['top_candidate']} (conf={p['top_confidence']:.2f})")

    # 3. Pool exhaustion returns insufficient_trace_evidence with low confidence
    for p in results["per_incident"]:
        if p["fault_type"] == "pool_exhaust":
            if p["top_candidate"] != "insufficient_trace_evidence":
                failures.append(f"{p['incident_id']}: pool_exhaust -- expected "
                                f"'insufficient_trace_evidence', got {p['top_candidate']}")
            if p["top_confidence"] > 0.5:
                failures.append(f"{p['incident_id']}: pool_exhaust -- confidence "
                                f"{p['top_confidence']:.2f} too high (should be ~0.3)")

    # 4. No high confidence (>0.8) on wrong service
    for p in results["high_confidence_wrong"]:
        failures.append(f"{p['incident_id']}: high confidence ({p['top_confidence']:.2f}) "
                        f"on wrong service {p['top_candidate']} (expected {p['expected_service']})")

    return failures


def main():
    incidents = load_incidents()

    # Find runs that have trace exports
    runs = sorted(name for name in os.listdir(RUNS)
                  if os.path.isdir(os.path.join(RUNS, name, "traces")))
    if not runs:
        print("no committed trace evidence under runs/*/traces/")
        return 1

    all_results = []
    all_failures = []

    for run in runs:
        print(f"\nProcessing {run}...")
        traces_dir = os.path.join(RUNS, run, "traces")
        index_path = os.path.join(traces_dir, "index.json")
        if not os.path.exists(index_path):
            print(f"  no index.json, skipping")
            continue

        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)

        correlations = []
        for entry in index.get("incidents", []):
            inc_id = entry["incident_id"]
            # Build anomaly_window from the incident's fault type
            # We need to map the incident to a signal/family
            gt_inc = _match_incident_to_export(incidents, run, inc_id.split("/")[-1])
            if not gt_inc:
                print(f"  WARNING: {inc_id} not in ground truth")
                continue

            family = gt_inc["fault"] if gt_inc["fault"] != "pool_exhaust" else "saturation"
            anomaly_window = {
                "start": gt_inc["start"],
                "end": gt_inc["end"],
                "signal": f"{gt_inc['fault']}:{gt_inc['service']}",
                "family": family,
            }

            corr_result = correlate_incident(run, inc_id, anomaly_window, use_live=False)
            correlations.append(corr_result)

        results = score_correlation(incidents, {run: correlations})
        print_results(run, results)

        failures = check_assertions(results)
        if failures:
            all_failures.extend(failures)
        all_results.append(results)

    # Aggregate across all runs
    total_incidents = sum(r["total"] for r in all_results)
    total_correct = sum(r["correct"] for r in all_results)
    overall_accuracy = total_correct / total_incidents if total_incidents else 0

    print(f"\n{'=' * 78}")
    print(f"OVERALL  ({total_incidents} incidents across {len(all_results)} runs)")
    print(f"{'=' * 78}")
    print(f"  Top-1 accuracy: {total_correct}/{total_incidents} ({_pct(overall_accuracy)})")

    if all_failures:
        print(f"\n  ASSERTION FAILURES ({len(all_failures)}):")
        for f in all_failures:
            print(f"    - {f}")
        print("\nFAILED")
        return 1

    print("\nALL ASSERTIONS HELD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())