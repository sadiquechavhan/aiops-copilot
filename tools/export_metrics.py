"""
export_metrics -- save a labelled run's raw golden-signal timeline into the repo.

WHY THIS EXISTS
---------------
Prometheus keeps 24h. A day after a labelled run its metrics are gone, so a
metric detector cannot be scored against known faults after the fact -- the run
that produced the labels is exactly the run whose metrics have expired. Traces
got export_traces and verify_traces; the metrics side had nothing but a live
stack inside its retention window. This is the "Fourth debt" in PROGRESS.

This tool does for metrics what export_traces did for traces: it writes the run's
signals into runs/<run_id>/metrics/ so detection can be scored OFFLINE, with no
stack and no data inside retention -- the property that makes verify_traces
survive the containers being gone.

WHY A CONTINUOUS TIMELINE, NOT PER-INCIDENT SLICES
--------------------------------------------------
export_traces slices per incident because a trace is evidence FOR one incident.
A detector is different: it is scored across the WHOLE run. It has to fire during
the four incidents AND stay quiet in the gaps between them, so its precision
lives in those quiet gaps. Exporting only the incident windows would throw the
quiet periods away and make every detector look perfectly precise. So this writes
one continuous window: T=0 to the end of the run plus a recovery tail.

WHY THE WINDOW STARTS AT T=0 (run_meta.started)
-----------------------------------------------
run_meta.started is the warmup-excluded boundary: the S2 fix settles the JIT
under load before labelled time begins, so "normal" is measured settled, not
during the ~490ms->190ms warmup decay. Starting the export there means the
detector never sees that transient. baseline_window (T+15..T+165) sits inside
the window and is recorded, because "learn normal from the run's own quiet
period" is the one label a detector is allowed to use (PROGRESS section 4).

WHY DERIVED SIGNALS, NOT RAW BUCKETS
------------------------------------
The purest reading of "store the input, derive the rest" (export_traces) is to
store the raw counters and histogram buckets and compute rate() and
histogram_quantile() at read time. Rejected: that means reimplementing
Prometheus's counter-reset-aware rate() and bucket-interpolating quantile in
Python, which can silently disagree with the shipping queries -- for a
re-quantiling benefit a detection scorer never uses. Traces store raw because
folding is cheap and genuinely re-derivable; the metric analogue is neither. So
this stores the DERIVED golden signals -- the detector's actual feature space --
computed by the SAME PromQL the MCP server ships, imported rather than copied so
the export exercises the real query code (the reason verify_traces imports _fold).
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Imported, not reimplemented: the domain knowledge (the service_name label, the
# /health|/chaos exclusion, no `or vector(0)` under sum-by) is right once in
# metrics.py and paid for in the engineering log. Copying the query strings here
# would let this tool and the server drift apart silently.
from aiops_mcp import PROMETHEUS_URL, SERVICES                      # noqa: E402
from aiops_mcp.metrics import (                                     # noqa: E402
    _APP_ONLY, _Q_ERROR_RATIO, _Q_ERRORS, _Q_LATENCY, _Q_POOL, _Q_RATE,
    _query_range, _svc_selector)
from aiops_mcp.window import (                                      # noqa: E402
    SCRAPE_INTERVAL_S, Window, parse_absolute, utc_iso)

GROUND_TRUTH = os.path.join(ROOT, "ground_truth.jsonl")

# Native scrape resolution, no downsampling. The MCP tool downsamples to ~12
# points to bound a chat response; an offline export has no such budget and the
# detector wants every sample.
STEP_S = SCRAPE_INTERVAL_S

# Fixed, not adaptive. metrics.py sizes the rate window to the query duration
# (duration/4, capped at 5m); over a ~35-minute export that would pick 5m and
# smear the 4-minute incidents. 2m is what scenario_runner's Q_P95 and the
# Grafana dashboard use, so the exported shape matches every other view of a run.
RATE_WINDOW = "120s"

# Recovery does not finish the instant a fault is cleared: the 2m rate window
# keeps averaging the fault out for two more minutes. Extend past run_meta.ended
# so the return-to-normal is fully captured rather than clipped mid-descent.
TAIL_PAD_S = 180

# metric -> (unit, query template, is_rate_based). db_pool is a gauge: no rate
# window, no route exclusion (it is not an HTTP metric). The p95 quantile for
# latency is fixed here; p50/p99 are a different question a detector is not asked.
P95 = 0.95
METRIC_SPECS = [
    ("latency", "s", _Q_LATENCY, True),
    ("request_rate", "req/s", _Q_RATE, True),
    ("error_rate", "req/s", _Q_ERRORS, True),
    ("error_ratio", "%", _Q_ERROR_RATIO, True),
    ("db_pool", "connections", _Q_POOL, False),
]


def load_meta(run_id):
    """runs/<run_id>/run_meta.json -> the window boundaries the run recorded.

    run_meta is the run's own account of itself: when labelled time started
    (warmup already excluded), when it ended, and which slice it calls the clean
    baseline. Deriving these from ground-truth offsets instead would rebuild
    knowledge the runner already wrote down, and get it wrong the day the
    scenario timing changes.
    """
    path = os.path.join(ROOT, "runs", run_id, "run_meta.json")
    if not os.path.exists(path):
        raise SystemExit(f"no run_meta.json for {run_id} at {path}")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def has_labels(run_id):
    """True if ground_truth.jsonl carries at least one incident for this run.

    A run with no labels cannot be scored, so exporting its metrics would produce
    evidence for a question nobody can ask. Same guard export_traces makes.
    """
    if not os.path.exists(GROUND_TRUTH):
        return False
    with open(GROUND_TRUTH, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and json.loads(line).get("run_id") == run_id:
                return True
    return False


def series_pairs(data):
    """Prometheus matrix -> {series_name: [[epoch, value], ...]}.

    NOT metrics.py's _collect: that returns {name: [values]} against one shared
    timestamp list (the longest series'), which misaligns a series that starts
    late -- and the 5xx series starts late by construction, the instant the first
    500 is recorded. Keeping each series' own timestamps is the faithful thing to
    store; alignment onto a common grid is the reader's decision, not the
    recorder's.

    NaN -> None: histogram_quantile emits NaN for an empty bucket, and a bare NaN
    token is not valid JSON. Same reason _collect drops it.
    """
    out = {}
    for entry in data.get("result", []):
        metric = entry.get("metric", {})
        name = metric.get("service_name") or metric.get("state") or "all"
        pairs = []
        for pair in entry.get("values", []):
            try:
                value = float(pair[1])
            except (ValueError, IndexError, TypeError):
                continue
            pairs.append([float(pair[0]), None if value != value else value])
        out[name] = pairs
    return out


def build_query(template, is_rate):
    """Fill a shipping template for all services at once.

    svc="" (the "all" selector) keeps the three services as separate series via
    `sum by (service_name)` in one query, instead of three round trips. Gauges
    get neither a rate window nor the app-route exclusion.
    """
    if not is_rate:                       # db_pool: no placeholders to fill
        return template.format()
    return template.format(q=P95, svc=_svc_selector("all"), app=_APP_ONLY,
                           rate=RATE_WINDOW)


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("run_id", help="run_id with a run_meta.json under runs/")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing export. Refused by default: "
                             "these files are committed evidence and a re-run "
                             "after Prometheus's 24h retention replaces them with "
                             "empty windows")
    options = parser.parse_args()

    if not has_labels(options.run_id):
        raise SystemExit(f"no ground-truth rows for {options.run_id} -- "
                         f"nothing to score, so nothing to export")

    meta = load_meta(options.run_id)
    start = parse_absolute(meta["started"], "started")
    end = parse_absolute(meta["ended"], "ended") + TAIL_PAD_S
    window = Window(start, end)
    baseline = meta.get("baseline_window")

    out_dir = os.path.join(ROOT, "runs", options.run_id, "metrics")

    # Refuse to replace good evidence with an empty answer -- export_traces'
    # reasoning exactly. The guard is on the OUTPUT, not the window age: a re-run
    # after retention (or after the Prometheus volume is reset) fetches empty and
    # would overwrite the committed series with nothing, and no error fires
    # because Prometheus answers 200 with an empty matrix. Knowing the window is
    # "too old" would require this tool to track retention, which a volume reset
    # invalidates anyway.
    existing = []
    if os.path.isdir(out_dir):
        for name, *_ in METRIC_SPECS:
            path = os.path.join(out_dir, f"{name}.json")
            if os.path.exists(path):
                existing.append((name, path))
    if existing and not options.force:
        print(f"{len(existing)} export(s) already in {out_dir}:")
        for name, path in existing:
            print(f"  {name}.json  {os.path.getsize(path):,} bytes")
        raise SystemExit(
            "\nRefusing to overwrite. These files are committed evidence and are\n"
            "not regenerable once the window leaves Prometheus's 24h retention --\n"
            "a re-run would replace them with empty windows.\n"
            "To score what is already here, no stack needed:\n"
            "  py -3.12 tools/verify_detection.py\n"
            "To export anyway, pass --force.")

    os.makedirs(out_dir, exist_ok=True)
    print(f"run {options.run_id}")
    print(f"window {utc_iso(start)} .. {utc_iso(end)} "
          f"({int(window.duration)}s, step {STEP_S}s, rate {RATE_WINDOW})\n")

    index, empty, failures = [], 0, 0
    for name, unit, template, is_rate in METRIC_SPECS:
        expr = build_query(template, is_rate)
        try:
            data = _query_range(expr, window, STEP_S)
        except Exception as exc:
            # Reported, not raised: one failed metric should not cost the others,
            # and a partial export is worth more than none.
            print(f"  FAIL  {name:<13} {type(exc).__name__}: {exc}")
            failures += 1
            continue

        pairs = series_pairs(data)
        pairs = {k: v for k, v in pairs.items() if v}     # drop all-empty series
        if not pairs:
            # Not written. An empty matrix is indistinguishable from "the window
            # is past retention", and with --force it would clobber real evidence
            # with that ambiguity. For an error metric it can also mean a true
            # zero -- the detector resolves that by treating an absent 5xx series
            # as measured zero; the recorder does not fabricate the zeros.
            print(f"  empty {name:<13} no series in window, not written")
            empty += 1
            continue

        total_points = sum(len(v) for v in pairs.values())
        payload = {
            "run_id": options.run_id, "metric": name, "unit": unit,
            "step_s": STEP_S,
            "rate_window": RATE_WINDOW if is_rate else None,
            "window": window.payload(), "baseline_window": baseline,
            "promql": expr, "series": pairs,
        }
        path = os.path.join(out_dir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        present = sorted(pairs)
        print(f"  ok    {name:<13} {len(present)} series "
              f"[{', '.join(present)}]  {total_points} points  "
              f"{os.path.getsize(path):,} bytes")
        index.append({"metric": name, "file": f"{name}.json", "unit": unit,
                      "series": present, "points": total_points})

    # Merged index, like export_traces: a partial re-export must not erase the
    # record of metrics still on disk from a previous run of this tool.
    index_path = os.path.join(out_dir, "index.json")
    merged = {}
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as handle:
            for entry in json.load(handle).get("metrics", []):
                if os.path.exists(os.path.join(out_dir, entry.get("file", ""))):
                    merged[entry["metric"]] = entry
    for entry in index:
        merged[entry["metric"]] = entry

    order = [name for name, *_ in METRIC_SPECS]
    with open(index_path, "w", encoding="utf-8") as handle:
        json.dump({"run_id": options.run_id, "prometheus_url": PROMETHEUS_URL,
                   "step_s": STEP_S, "rate_window": RATE_WINDOW,
                   "window": window.payload(), "baseline_window": baseline,
                   "services": list(SERVICES),
                   "metrics": sorted(merged.values(),
                                     key=lambda e: order.index(e["metric"]))},
                  handle, indent=2)

    print(f"\n{len(index)}/{len(METRIC_SPECS)} metric(s) exported"
          + (f"; {empty} empty and skipped" if empty else "")
          + (f"; {failures} failed" if failures else "")
          + f"; index lists {len(merged)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
