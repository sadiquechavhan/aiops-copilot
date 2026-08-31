"""
verify_detection -- score the detector against ground truth: precision and recall.

Runs with NO stack. It reads the committed metric exports and the committed
labels, so it answers "does detection find the known faults, and does it stay
quiet otherwise" long after the run's data has left Prometheus -- the same
non-perishable check verify_traces is for self-time. It imports the SHIPPING
detector from aiops_mcp.detect rather than reimplementing it; a local copy would
let the check pass while the detector itself was broken.

WHAT IS SCORED
--------------
A detection is TRUE if its time span overlaps a known incident, allowing for
detection lag; otherwise it is a FALSE POSITIVE. An incident is RECALLED if at
least one detection overlaps it.

    recall    = incidents recalled / incidents total
    precision = true detections / detections total

WHY A LAG TOLERANCE, AND WHY IT IS NOT CHEATING
-----------------------------------------------
latency and error rate are computed over a 2-minute window, so a signal ramps in
after a fault starts and decays out after it clears -- the detector empirically
fires ~20s late and stays lit ~1:45 past recovery. Matching an incident to
[start - FRONT_GRACE, end + BACK_TAIL] forgives exactly that instrument lag, no
more. It is bounded by the rate window, not tuned to the answer: a detection
sitting in the genuinely quiet middle of a gap between incidents still counts
against precision. Matching is by TIME ONLY -- whether the RIGHT signal fired is
attribution, which is correlation (S5), not detection.

THE ASSERTIONS
--------------
The point of the run is a number, but a few things must hold or the detector is
wrong, not just weak (mirrors verify_traces' asserted signals):
  - every latency and error-rate fault is recalled by the CORE (RED) signals;
  - every fault, pool exhaustion included, is recalled once SATURATION is added;
  - the CORE detector raises no false positive.
The expected CORE miss on pool exhaustion is REPORTED, not asserted: it is the
finding, and a future change that made the pool visible to RED should not read as
a regression.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiops_mcp.detect import detect_run                     # noqa: E402
from aiops_mcp.window import parse_absolute                 # noqa: E402

# The IsolationForest secondary is optional: import it lazily so a machine without
# scikit-learn still scores the stdlib z-score primary. When it is present the
# scorer measures it alongside the primary; when it is not, that column is absent
# and the primary's assertions are unaffected.
try:
    from aiops_mcp.detect import detect_iforest             # noqa: E402
    HAS_IFOREST = True
except Exception:                                           # pragma: no cover
    HAS_IFOREST = False

GROUND_TRUTH = os.path.join(ROOT, "ground_truth.jsonl")
RUNS = os.path.join(ROOT, "runs")

# Bounded by the 2-minute rate window, not fitted to the result. FRONT_GRACE
# covers ramp-in and any clock skew; BACK_TAIL is the rate window (120s) plus a
# scrape of margin, which is what the latency detections empirically trailed.
FRONT_GRACE = 30
BACK_TAIL = 150

# RED sees these; saturation is what pool exhaustion needs.
RED_FAULTS = {"latency", "error_rate"}


def load_incidents():
    """ground_truth.jsonl -> {run_id: [incident dicts]} using confirmed edges.

    confirmed_start/end are when the fault was verified active/cleared, tighter
    than the requested start/end; fall back to the requested edges if absent.
    """
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
                "fault": row["fault_type"], "service": row["service"],
                "start": parse_absolute(start, "start"),
                "end": parse_absolute(end, "end"),
                "expected": row.get("expected_signal", [])})
    for incidents in runs.values():
        incidents.sort(key=lambda i: i["start"])
    return runs


def _overlaps(det, w0, w1):
    return det["start"] <= w1 and det["end"] >= w0


def score(incidents, detections):
    """Match a detector's detections to incidents by time.

    Pure matching -- detection happens in the caller, so the same scorer grades
    the z-score primary and the IsolationForest secondary without knowing which
    produced the list. Returns per-incident recall + attribution, the
    false-positive list, and the precision/recall counts.
    """
    matched_det = [False] * len(detections)

    per_incident = []
    for inc in incidents:
        w0, w1 = inc["start"] - FRONT_GRACE, inc["end"] + BACK_TAIL
        hits = []
        for idx, det in enumerate(detections):
            if _overlaps(det, w0, w1):
                matched_det[idx] = True
                hits.append(det)
        onset_lag = None
        if hits:
            first = min(h["start"] for h in hits)
            onset_lag = round(first - inc["start"])
        per_incident.append({
            "iid": inc["iid"], "fault": inc["fault"], "service": inc["service"],
            "recalled": bool(hits),
            "signals": sorted({h["signal"] for h in hits}),
            "onset_lag_s": onset_lag})

    false_positives = [detections[i] for i in range(len(detections)) if not matched_det[i]]
    recalled = sum(1 for p in per_incident if p["recalled"])
    true_dets = sum(matched_det)
    return {
        "per_incident": per_incident, "false_positives": false_positives,
        "n_incidents": len(incidents), "n_recalled": recalled,
        "n_detections": len(detections), "n_true": true_dets,
        "recall": recalled / len(incidents) if incidents else 0.0,
        "precision": true_dets / len(detections) if detections else 1.0}


def _pct(x):
    return f"{100 * x:5.1f}%"


def print_run(run_id, incidents, results):
    """Print one run's table. results is an ordered list of (label, score_result),
    one column per detection method -- CORE (RED), + SATURATION, and (if
    scikit-learn is installed) IForest."""
    print(f"\n{'=' * 78}\n{run_id}  ({len(incidents)} incidents)\n{'=' * 78}")

    def families(signals):            # dedup families: 3 latency signals -> "latency"
        return ",".join(sorted({sig.split(":")[0] for sig in signals})) or "-"

    def cell(p):
        return "MISS" if not p["recalled"] else f"hit {p['onset_lag_s']:+d}s {families(p['signals'])}"

    print(f"{'inc':4}{'fault':13}{'service':10}"
          + "".join(f"{label:>22}" for label, _ in results))
    per = [res["per_incident"] for _, res in results]
    for i, inc in enumerate(incidents):
        base = per[0][i]
        print(f"{base['iid']:4}{base['fault']:13}{base['service']:10}"
              + "".join(f"{cell(pi[i]):>22}" for pi in per))

    for label, res in results:
        fps = res["false_positives"]
        print(f"\n  {label:18}  recall {res['n_recalled']}/{res['n_incidents']} "
              f"({_pct(res['recall'])})   precision {res['n_true']}/{res['n_detections']} "
              f"({_pct(res['precision'])})   false positives: {len(fps)}")
        for fp in fps:
            extra = f" driver={fp['driver']}" if "driver" in fp else ""
            print(f"      FP  {fp['signal']:18} peak={fp['peak']} z={fp['peak_z']}"
                  f"{extra} ({fp['points']} pts)")


def check(run_id, incidents, core, sat):
    """Return a list of assertion failures (empty = all held)."""
    failures = []
    core_by_iid = {p["iid"]: p for p in core["per_incident"]}
    sat_by_iid = {p["iid"]: p for p in sat["per_incident"]}
    for inc in incidents:
        iid = inc["iid"]
        if inc["fault"] in RED_FAULTS and not core_by_iid[iid]["recalled"]:
            failures.append(f"{run_id}/{iid}: CORE missed a {inc['fault']} fault "
                            f"(RED signals must catch this)")
        if not sat_by_iid[iid]["recalled"]:
            failures.append(f"{run_id}/{iid}: full golden signals missed the "
                            f"{inc['fault']} fault (nothing caught it)")
    if core["false_positives"]:
        failures.append(f"{run_id}: CORE raised {len(core['false_positives'])} "
                        f"false positive(s) in quiet periods")
    return failures


def _zscore_dets(run_id, include_saturation):
    return detect_run(os.path.join(RUNS, run_id),
                      include_saturation=include_saturation)["detections"]


def _iforest_dets(run_id):
    return detect_iforest(os.path.join(RUNS, run_id),
                          include_saturation=True)["detections"]


def main():
    incidents_by_run = load_incidents()

    scored = []
    if os.path.isdir(RUNS):
        for run_id in sorted(os.listdir(RUNS)):
            if not os.path.exists(os.path.join(RUNS, run_id, "metrics", "index.json")):
                continue
            if run_id not in incidents_by_run:
                continue
            scored.append(run_id)

    if not scored:
        print("no runs with both a metrics export and ground-truth labels.")
        print("record one:  py -3.12 tools/scenario_runner.py")
        print("export it:   py -3.12 tools/export_metrics.py <run_id>")
        return 1

    all_failures, blind, iforest_totals = [], [], [0, 0, 0, 0]  # rec, inc, true, det
    for run_id in scored:
        incidents = incidents_by_run[run_id]
        core = score(incidents, _zscore_dets(run_id, include_saturation=False))
        sat = score(incidents, _zscore_dets(run_id, include_saturation=True))
        results = [("CORE (RED)", core), ("+ SATURATION", sat)]
        if HAS_IFOREST:
            iforest = score(incidents, _iforest_dets(run_id))
            results.append(("IForest (mv)", iforest))
            iforest_totals[0] += iforest["n_recalled"]
            iforest_totals[1] += iforest["n_incidents"]
            iforest_totals[2] += iforest["n_true"]
            iforest_totals[3] += iforest["n_detections"]
        print_run(run_id, incidents, results)
        all_failures += check(run_id, incidents, core, sat)

        # The saturation win, collected as we go: RED-blind, pool-gauge-visible.
        for c, s in zip(core["per_incident"], sat["per_incident"]):
            if not c["recalled"] and s["recalled"]:
                blind.append((run_id, c["iid"], c["fault"]))

    # The findings, stated once each.
    print(f"\n{'-' * 78}")
    if blind:
        print("RED-blind faults recovered only by the saturation signal:")
        for rid, iid, fault in blind:
            print(f"  {rid}/{iid}  {fault}  -- no latency or 5xx signal moved; "
                  f"the pool gauge did")

    # The z-score vs IsolationForest contrast: same faults, opposite philosophy --
    # per-signal thresholds you can read off (attribution built in) vs one
    # multivariate verdict with no per-signal wiring (attribution recovered after
    # the fact from the driving feature). Reported, not asserted: it is a secondary
    # trained on only ~10 baseline vectors, so its number is a finding to observe,
    # not a gate on the build (mirrors the pool-miss being reported, not asserted).
    if HAS_IFOREST:
        rec, inc, true, det = iforest_totals
        print("\nIsolationForest secondary (multivariate, trained on baseline only):")
        print(f"  recall {rec}/{inc} ({_pct(rec / inc if inc else 0)})   "
              f"precision {true}/{det} ({_pct(true / det if det else 1)})")
        print("  one verdict per step, no per-signal thresholds; driver feature "
              "read back for attribution")
    else:
        print("\nIsolationForest secondary not scored -- scikit-learn is not "
              "installed (py -3.12 -m pip install scikit-learn).")

    print(f"\n{'=' * 78}")
    if all_failures:
        print(f"FAILED ({len(all_failures)}):")
        for f in all_failures:
            print(f"  - {f}")
        return 1
    print(f"scored {len(scored)} run(s); all assertions held")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
