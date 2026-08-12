"""
verify_traces -- recompute the README's self-time claim from committed evidence.

WHY THIS IS SEPARATE FROM export_traces
---------------------------------------
export_traces reads Jaeger. This reads the repo. That difference is the whole
point: this check needs no running stack, no Docker, and no data inside its
retention window, so it still works when every labelled run is months old and the
containers are gone. It is the only verification in the project with that
property.

The metrics-side check (mcp_probe --ground-truth) cannot have it. Prometheus keeps
24h, so a day after a labelled run it scores 0/0 and proves only that refusal
works. Both checks are real; one of them is perishable and this one is not.

WHAT IT ASSERTS, AND WHAT IT DELIBERATELY DOES NOT
--------------------------------------------------
Self-time locates *latency*. A span that spent 470ms doing its own work is
visible; a span that returned HTTP 500 in 4ms is not, because failing fast costs
no time. So the assertion covers the two latency incidents only:

    i1  latency injected in inventory  -> inventory dominates self-time
    i4  latency injected in gateway    -> gateway dominates self-time

i2 (error_rate in orders) and i3 (pool_exhaust in inventory) are REPORTED but not
asserted. i2 is the instructive one: its dominant self-time service is inventory,
which is not the faulted service, and that is correct behaviour rather than a bug.
Orders returns its injected 500 immediately, so the fault removes work instead of
adding it, and the normal cost of the chain still sits in inventory. Asserting
"dominant self-time == faulted service" across all four would fail here, and
loosening the assertion until i2 passed would destroy the thing being checked.

That is the honest boundary of the technique: self-time answers "where is the time
going", not "where is the fault". They coincide for latency faults and diverge for
error faults. Session 5's correlation has to combine self-time with error rates
for exactly this reason.

THE THRESHOLD
-------------
Dominance must be >= MIN_SHARE (85%). Measured values are 91.0-98.5% across both
runs, so the margin is wide enough to survive 15-trace sampling noise and narrow
enough to catch a real regression -- specifically a broken _merged_child_time,
which is the subtle failure this guards. Summing child intervals instead of
merging them collapses the separation, and nothing else in the project would
notice.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Imported, not reimplemented. A private name, deliberately: this check exists to
# verify THE SHIPPING FOLDING CODE, so a local copy of the interval-merging logic
# would make it pass while the server was broken -- the exact failure it guards.
from aiops_mcp.traces import _fold  # noqa: E402

GROUND_TRUTH = os.path.join(ROOT, "ground_truth.jsonl")
RUNS = os.path.join(ROOT, "runs")
MIN_SHARE = 85.0

# fault_type -> whether self-time is expected to point at the faulted service.
# See the docstring: latency adds work, error_rate removes it, pool_exhaust adds
# waiting that lands partly on the waiter rather than the resource.
ASSERTED = {"latency": True, "error_rate": False, "pool_exhaust": False}


def labels():
    """incident_id -> ground-truth row, for every labelled incident."""
    out = {}
    with open(GROUND_TRUTH, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                row = json.loads(line)
                out[row["incident_id"]] = row
    return out


def shares(path):
    """Committed Jaeger payload -> (traces folded, {service: pct of self-time})."""
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    totals, folded = {}, 0
    for trace in payload.get("data") or []:
        summary = _fold(trace)
        if not summary:
            continue
        folded += 1
        for name, value in (summary.get("self_ms") or {}).items():
            totals[name] = totals.get(name, 0.0) + (value or 0.0)
    grand = sum(totals.values()) or 1.0
    return folded, {k: 100.0 * v / grand for k, v in totals.items()}


def main():
    by_id = labels()
    runs = sorted(name for name in os.listdir(RUNS)
                  if os.path.isdir(os.path.join(RUNS, name, "traces")))
    if not runs:
        print("no committed trace evidence under runs/*/traces/")
        return 1

    checked = failed = reported = 0
    for run in runs:
        traces_dir = os.path.join(RUNS, run, "traces")
        index_path = os.path.join(traces_dir, "index.json")
        if not os.path.exists(index_path):
            print(f"{run}: no index.json, skipping")
            continue
        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)

        print(f"\n{run}")
        for entry in index.get("incidents", []):
            row = by_id.get(entry["incident_id"])
            if row is None:
                # The evidence claims to be about an incident the labels do not
                # contain. Reported loudly: an orphaned export is a file whose
                # provenance cannot be established, which is worse than absent.
                print(f"  ORPHAN {entry['incident_id']} -- not in ground_truth.jsonl")
                failed += 1
                continue

            path = os.path.join(traces_dir, entry["file"])
            folded, pct = shares(path)
            ranked = sorted(pct.items(), key=lambda kv: -kv[1])
            top, top_pct = ranked[0]
            detail = "  ".join(f"{k} {v:.1f}%" for k, v in ranked)
            fault, service = row.get("fault_type"), row.get("service")
            tag = f"{entry['incident_id'].rsplit('/', 1)[-1]} {fault}/{service}"

            if not ASSERTED.get(fault):
                # Printed with its expectation stated, so the output cannot be
                # mistaken for a check that passed.
                reported += 1
                print(f"  info   {tag:28s} {folded:2d} traces  {detail}"
                      f"   (not asserted: {fault} does not move self-time)")
                continue

            checked += 1
            ok = top == service and top_pct >= MIN_SHARE
            if not ok:
                failed += 1
            print(f"  {'ok    ' if ok else 'FAIL  '}{tag:28s} {folded:2d} traces  "
                  f"{detail}")
            if not ok:
                print(f"         expected {service} dominant at >= {MIN_SHARE}%, "
                      f"got {top} at {top_pct:.1f}%")

    print(f"\n{checked - failed}/{checked} asserted incident(s) reproduce the "
          f"self-time separation; {reported} reported without assertion")
    if failed:
        print("FAILED -- the folding code no longer reproduces the committed "
              "evidence, or the evidence no longer matches the labels")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
