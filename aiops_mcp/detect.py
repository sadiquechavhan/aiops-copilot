"""
detect -- flag anomalies in a run's exported golden-signal timeline.

WHAT THIS IS
------------
A fixed-baseline z-score detector. For each signal it learns "normal" (mean and
spread) from the run's recorded baseline_window, then sweeps the timeline and
flags any stretch that deviates far enough, in the direction that matters for
that signal. It reads only the committed export (runs/<run_id>/metrics/), so it
scores offline with no stack -- the property that lets verify_detection survive
the containers being gone, exactly like verify_traces.

WHY A FIXED BASELINE, NOT A ROLLING ONE
---------------------------------------
The obvious "rolling baseline" -- mean/std over a trailing window -- has a fatal
flaw against the faults here: they are sustained for 3-4 minutes. A 5-minute
trailing window spends the first couple of minutes of an incident quietly
absorbing the fault into its own notion of normal, and then STOPS firing while
the incident is still happening. Production systems work around this by freezing
the baseline whenever an anomaly is active; that is real machinery and out of
scope for detection-only. The run already hands us a clean, labelled quiet period
(baseline_window, the one label a detector is allowed to use), so we use it
directly. It cannot be poisoned by a sustained incident because it never updates.

WHY THESE SIGNALS
-----------------
CORE is the classic RED pair -- latency and errors -- the signals almost all
alerting is built on. They catch a latency fault and an error-rate fault, and
they catch propagation up the call chain. They are STRUCTURALLY BLIND to pool
exhaustion: when the inventory connection pool is drained the requests that get a
connection are fast (latency falls, it does not rise) and nothing returns 5xx, so
neither core signal moves. That blindness is the point -- it is why i3 is a
deliberate negative. SATURATION (the HikariCP pool gauge) is the fourth golden
signal; add it and pool exhaustion becomes visible. detect_run takes
include_saturation so the two can be measured against each other.

request_rate is deliberately NOT a detection signal. None of the injected faults
is a traffic anomaly -- request rate stays flat through every one of them -- so
flagging on it could only manufacture false positives. It is context, not signal.

WHY THE FLOORS AND THE PERSISTENCE RULE
---------------------------------------
sigma_eff = max(sigma, floor). The floor is the smallest spread worth reacting to
for a metric, and it also guards the zero-variance case: the error-rate baseline
is exactly zero, so its raw sigma is zero and a bare z-score would divide by it.
K*floor is then the minimum absolute deviation that can trip a flag, so the floor
doubles as "smallest operationally meaningful change." A detection requires
MIN_CONSEC consecutive flagged points; a real incident produces many (the 15s
scrape over a 3-4 minute fault), while sensor noise produces lone spikes, so this
throws the lone spikes away without touching recall.
"""

import argparse
import json
import os
import statistics
import sys
from collections import namedtuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiops_mcp.window import parse_absolute, utc_iso        # noqa: E402

Signal = namedtuple("Signal", "metric series direction family label")
RISE, FALL = "rise", "fall"

# The classic RED signals: latency (per service, so propagation is visible) and
# 5xx error rate. inventory has no error_rate series -- it never throws 5xx -- and
# an absent series is treated as measured zero, not a gap.
CORE_SIGNALS = [
    Signal("latency", "gateway", RISE, "latency", "latency:gateway"),
    Signal("latency", "inventory", RISE, "latency", "latency:inventory"),
    Signal("latency", "orders", RISE, "latency", "latency:orders"),
    Signal("error_rate", "gateway", RISE, "errors", "errors:gateway"),
    Signal("error_rate", "orders", RISE, "errors", "errors:orders"),
]

# The fourth golden signal, saturation. Off by default so the core result is the
# honest RED-only number; turn it on to show it recovers the pool-exhaust miss.
SATURATION_SIGNALS = [
    Signal("db_pool", "used", RISE, "saturation", "pool:used"),
    Signal("db_pool", "idle", FALL, "saturation", "pool:idle"),
]

# Noise floor per metric (see module docstring). Grounded in the measured
# baseline spread: latency sigma runs 0.02-0.03s so a 0.010s floor sits just
# under it; error_rate baseline sigma is 0 so the floor IS the guard; the pool
# gauge idles with a 0-1 connection churn (sigma ~0.3) so 0.5 clears it.
FLOORS = {
    "latency": 0.010,      # seconds (10 ms)
    "error_rate": 0.05,    # req/s -- one stray 500 over a 120s window is 0.008
    "error_ratio": 0.5,    # percent
    "db_pool": 0.5,        # connections
}

K = 3.0            # sigmas -- classic three-sigma
MIN_CONSEC = 2     # >=2 consecutive 15s points (>=30s) before it counts


def load_export(run_dir):
    """runs/<run_id>/metrics/ -> (index, {(metric, series): [[epoch, value], ...]})."""
    mdir = os.path.join(run_dir, "metrics")
    index_path = os.path.join(mdir, "index.json")
    if not os.path.exists(index_path):
        raise SystemExit(f"no metrics export at {mdir}\n"
                         f"  run: py -3.12 tools/export_metrics.py {os.path.basename(run_dir)}")
    with open(index_path, encoding="utf-8") as handle:
        index = json.load(handle)
    series = {}
    for entry in index["metrics"]:
        with open(os.path.join(mdir, entry["file"]), encoding="utf-8") as handle:
            data = json.load(handle)
        for name, pairs in data["series"].items():
            series[(entry["metric"], name)] = pairs
    return index, series


def baseline_stats(pairs, b0, b1):
    """(mean, stdev, n) over the baseline window, or None if it has no samples."""
    vals = [v for t, v in pairs if v is not None and b0 <= t <= b1]
    if not vals:
        return None
    mu = statistics.mean(vals)
    sigma = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return mu, sigma, len(vals)


def sweep(pairs, mu, sigma, direction, floor, k=K, min_consec=MIN_CONSEC, after=None):
    """Contiguous runs of >=min_consec points that deviate past k*sigma_eff.

    One-sided: a RISE signal only reacts to values above the mean, a FALL signal
    only to values below it (idle connections rising during a gateway stall is not
    a pool problem). Points at or before `after` (the end of the baseline window)
    are skipped -- the baseline is the reference, not something to detect on. A
    None value (a scrape gap) ends the current run.
    """
    sigma_eff = max(sigma, floor)
    threshold = k * sigma_eff
    detections, current = [], None

    def close():
        nonlocal current
        if current and current[4] >= min_consec:
            detections.append(current)
        current = None

    for t, v in pairs:
        if v is None:
            close()
            continue
        if after is not None and t <= after:
            continue
        deviation = (v - mu) if direction == RISE else (mu - v)
        if deviation > threshold:
            z = deviation / sigma_eff
            if current is None:
                current = [t, t, v, z, 1]
            else:
                current[1] = t
                current[4] += 1
                more_extreme = v > current[2] if direction == RISE else v < current[2]
                if more_extreme:
                    current[2], current[3] = v, z
        else:
            close()
    close()
    return [{"start": d[0], "end": d[1], "peak": round(d[2], 4),
             "peak_z": round(d[3], 1), "points": d[4]} for d in detections]


def detect_run(run_dir, include_saturation=False, k=K, min_consec=MIN_CONSEC):
    """Detect on one run's export. Returns baseline stats + detections per signal.

    Never reads ground_truth -- detection must not see the labels it will be
    scored against. The only label it uses is baseline_window, which names a quiet
    period, not where the faults are.
    """
    index, series = load_export(run_dir)
    bw = index.get("baseline_window")
    if not bw:
        raise SystemExit("export has no baseline_window -- cannot establish normal")
    b0, b1 = parse_absolute(bw["start"], "baseline start"), parse_absolute(bw["end"], "baseline end")

    signals = list(CORE_SIGNALS)
    if include_saturation:
        signals += SATURATION_SIGNALS

    result = {"run_id": index["run_id"], "baseline_window": bw, "k": k,
              "min_consec": min_consec, "include_saturation": include_saturation,
              "signals": {}, "detections": []}

    for sig in signals:
        pairs = series.get((sig.metric, sig.series))
        if pairs is None:
            # Absent series. For error_rate this means the service never threw a
            # 5xx -- measured zero, nothing to detect -- so it is recorded, not
            # flagged as missing data.
            result["signals"][sig.label] = {"absent": True, "family": sig.family}
            continue
        stats = baseline_stats(pairs, b0, b1)
        if stats is None:
            # No samples in baseline window. For error_rate this happens when no
            # 5xx has occurred yet -- the baseline is genuinely zero. Use floor as
            # sigma_eff so the first non-zero sample can trigger detection.
            mu, sigma, n = 0.0, 0.0, 0
        else:
            mu, sigma, n = stats
        floor = FLOORS[sig.metric]
        dets = sweep(pairs, mu, sigma, sig.direction, floor, k, min_consec, after=b1)
        result["signals"][sig.label] = {
            "mu": round(mu, 4), "sigma": round(sigma, 4), "floor": floor,
            "threshold": round(k * max(sigma, floor), 4), "direction": sig.direction,
            "family": sig.family, "n_baseline": n, "detections": dets}
        for d in dets:
            result["detections"].append({**d, "signal": sig.label, "family": sig.family})

    result["detections"].sort(key=lambda d: d["start"])
    return result


# ---------------------------------------------------------------------------
# IsolationForest secondary -- multivariate, unsupervised (see ENGINEERING_LOG)
# ---------------------------------------------------------------------------
# The z-score detector above is univariate and hand-specified: per signal it is
# told the bad direction and the threshold. IsolationForest is the mirror image
# -- it is handed the WHOLE golden-signal vector at each 15s step and learns the
# shape of normal from the baseline window with no per-signal wiring. It catches
# pool exhaustion without anyone declaring the pool a signal: the pool columns are
# simply part of the vector and they move. The cost is interpretability (it says a
# MOMENT is off, not which signal) and a dependence on how much normal history it
# trains on -- here only the ~10 baseline samples, which is thin and is reported.
#
# It trains on the baseline window ONLY, not the whole run unsupervised: the four
# incidents fill ~45% of the run, and IForest assumes anomalies are rare, so
# fitting on everything would fold the faults into "normal" -- the same poisoning
# that ruled out a rolling z-score baseline. Features are standardized by the
# baseline mean/sigma first (same floors as the z-score) so "far from normal"
# means the same on every axis and the baseline cloud sits at the origin.

IFOREST_FEATURES = [                       # the full golden-signal vector
    ("latency", "gateway"), ("latency", "inventory"), ("latency", "orders"),
    ("error_rate", "gateway"), ("error_rate", "orders"),
]
IFOREST_SATURATION = [("db_pool", "used"), ("db_pool", "idle")]

IFOREST_TREES = 200        # forest size; more trees = steadier score, cheap here
IFOREST_SEED = 0           # deterministic detections across runs of the scorer


def _standardizer(series, features, b0, b1):
    """Per-feature (mu, sigma_eff) over the baseline, for standardizing vectors.

    sigma_eff = max(sigma, floor) -- the same floors the z-score uses, so a
    zero-variance baseline (error_rate) does not blow up the division and a
    sub-floor wobble does not read as many sigmas. An absent series (inventory
    never throws 5xx) has normal = zero and no spread, so it contributes nothing.
    """
    stats = {}
    for metric, name in features:
        pairs = series.get((metric, name))
        base = baseline_stats(pairs, b0, b1) if pairs else None
        if base is None:
            stats[(metric, name)] = (0.0, FLOORS[metric])
        else:
            mu, sigma, _ = base
            stats[(metric, name)] = (mu, max(sigma, FLOORS[metric]))
    return stats


def _feature_grid(series, features, stats):
    """Standardized feature matrix on the gateway-latency time grid.

    Returns (times, rows) where rows[i] is the standardized vector at times[i].
    Every exported rate/gauge series shares one 15s grid (same window+step), so
    lookups align by timestamp; a feature with no point at a grid time (the 5xx
    series before the first 500, or a scrape gap) contributes 0 -- exactly
    baseline, i.e. "no evidence of abnormal on this axis", never a fake spike.
    """
    grid_pairs = series.get(("latency", "gateway")) or []
    times = [t for t, _ in grid_pairs]
    lookup = {key: {t: v for t, v in (series.get(key) or [])} for key in features}
    rows = []
    for t in times:
        row = []
        for key in features:
            mu, sig = stats[key]
            v = lookup[key].get(t)
            row.append(0.0 if v is None else (v - mu) / sig)
        rows.append(row)
    return times, rows


def detect_iforest(run_dir, include_saturation=True, trees=IFOREST_TREES,
                   min_consec=MIN_CONSEC):
    """Multivariate anomaly detection with IsolationForest.

    Trains on the baseline-window vectors (known normal), scores the whole
    timeline, and flags contiguous stretches the forest isolates as anomalous.
    Saturation is ON by default: the point of this detector is that it needs no
    per-signal wiring, so it gets the full vector including the pool. Returns the
    same detection shape as detect_run so the scorer treats both alike.

    sklearn is imported lazily so the z-score path and the scorer never depend on
    it -- a machine without scikit-learn can still run the stdlib primary.
    """
    try:
        from sklearn.ensemble import IsolationForest
    except ImportError as exc:                                    # pragma: no cover
        raise SystemExit(
            "IsolationForest needs scikit-learn (approved, ~100-150MB):\n"
            "  py -3.12 -m pip install scikit-learn\n"
            f"({exc})")

    index, series = load_export(run_dir)
    bw = index.get("baseline_window")
    if not bw:
        raise SystemExit("export has no baseline_window -- cannot establish normal")
    b0 = parse_absolute(bw["start"], "baseline start")
    b1 = parse_absolute(bw["end"], "baseline end")

    features = list(IFOREST_FEATURES) + (IFOREST_SATURATION if include_saturation else [])
    stats = _standardizer(series, features, b0, b1)
    times, rows = _feature_grid(series, features, stats)

    train = [r for t, r in zip(times, rows) if b0 <= t <= b1]
    if len(train) < 2:
        raise SystemExit(f"only {len(train)} baseline vector(s) -- too few to train")

    forest = IsolationForest(n_estimators=trees, contamination="auto",
                             random_state=IFOREST_SEED)
    forest.fit(train)

    # Threshold from the TRAINING scores, not sklearn's contamination heuristic.
    # score_samples returns higher = more normal. contamination="auto" assumes the
    # fitted data already holds the anomaly fraction and fixes the cutoff at a
    # level that, against a clean and tight 11-point baseline, flags essentially
    # the whole post-baseline run as one giant blob (measured: 115/115 points).
    # Calibrate on the known normal instead: a point is anomalous only if the
    # forest isolates it MORE easily than the most-isolated baseline sample. That
    # is the honest novelty rule -- "stranger than anything in the quiet window" --
    # and it needs no magic number. min(), not a percentile, because 11 points is
    # too few to estimate a tail from; the most-isolated normal sample is the
    # concrete, defensible bar.
    train_scores = forest.score_samples(train)
    threshold = min(train_scores)
    scores = forest.score_samples(rows)      # lower => isolated easily => anomalous

    # Contiguous anomalous stretches after the baseline, same persistence rule and
    # shape as the z-score sweep so the scorer stays detector-agnostic. The driver
    # is the feature furthest from normal at the most-anomalous point -- the
    # attribution the forest itself does not give, read back off the vector.
    labels = [f"{m}:{n}" for m, n in features]
    detections, current = [], None

    def close():
        nonlocal current
        if current and current["points"] >= min_consec:
            row = rows[current.pop("_peak_row")]
            top, topz = max(zip(labels, row), key=lambda kv: abs(kv[1]))
            current["signal"] = "iforest"
            current["family"] = "iforest"
            current["driver"] = top
            current["peak"] = round(current.pop("_peak_score"), 3)
            current["peak_z"] = round(topz, 1)
            detections.append(current)
        current = None

    for i, (t, s) in enumerate(zip(times, scores)):
        if t <= b1:                    # baseline is the reference, not detectable
            continue
        if s < threshold:
            if current is None:
                current = {"start": t, "end": t, "points": 1,
                           "_peak_score": s, "_peak_row": i}
            else:
                current["end"] = t
                current["points"] += 1
                if s < current["_peak_score"]:
                    current["_peak_score"], current["_peak_row"] = s, i
        else:
            close()
    close()

    return {"run_id": index["run_id"], "baseline_window": bw,
            "method": "iforest", "trees": trees, "min_consec": min_consec,
            "include_saturation": include_saturation, "features": labels,
            "n_baseline": len(train), "detections": detections}


def _offset(t, t0):
    d = int(round(t - t0))
    return f"T+{d // 60}:{d % 60:02d}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("run_id", help="a run with a metrics/ export under runs/")
    parser.add_argument("--saturation", action="store_true",
                        help="add the pool-saturation signals -- catches pool "
                             "exhaustion, which latency and errors cannot see")
    parser.add_argument("--iforest", action="store_true",
                        help="run the IsolationForest secondary instead: one "
                             "multivariate verdict per step, no per-signal wiring")
    parser.add_argument("--json", action="store_true", help="emit the raw result")
    options = parser.parse_args()

    run_dir = os.path.join(ROOT, "runs", options.run_id)

    if options.iforest:
        result = detect_iforest(run_dir, include_saturation=True)
        if options.json:
            print(json.dumps(result, indent=2))
            return 0
        index, _ = load_export(run_dir)
        t0 = parse_absolute(index["window"]["start"], "window start")
        print(f"run {result['run_id']}   IsolationForest  trees={result['trees']}"
              f"  min_consec={result['min_consec']}")
        print(f"features ({len(result['features'])}): {', '.join(result['features'])}")
        print(f"trained on {result['n_baseline']} baseline vector(s)\n")
        print(f"{len(result['detections'])} detection(s), earliest first:")
        if not result["detections"]:
            print("  (none)")
        for d in result["detections"]:
            print(f"  {_offset(d['start'], t0):>8} .. {_offset(d['end'], t0):<8} "
                  f"score={d['peak']} driver={d['driver']} (z={d['peak_z']}, "
                  f"{d['points']} pts)")
        return 0

    result = detect_run(run_dir, include_saturation=options.saturation)
    if options.json:
        print(json.dumps(result, indent=2))
        return 0

    index, _ = load_export(run_dir)
    t0 = parse_absolute(index["window"]["start"], "window start")

    print(f"run {result['run_id']}   K={result['k']}  min_consec={result['min_consec']}"
          f"  signals={'core+saturation' if options.saturation else 'core (RED)'}")
    print(f"baseline {result['baseline_window']['start']} .. {result['baseline_window']['end']}\n")

    print(f"{'signal':20}{'dir':>5}{'mu':>9}{'sigma':>9}{'thresh':>9}  detections")
    for label, info in result["signals"].items():
        if info.get("absent"):
            print(f"{label:20}{'':>5}{'-- series absent (measured zero) --':>38}")
            continue
        note = f"{len(info['detections'])}" if info["detections"] else "-"
        print(f"{label:20}{info['direction']:>5}{info['mu']:9.4f}{info['sigma']:9.4f}"
              f"{info['threshold']:9.4f}  {note}")

    print(f"\n{len(result['detections'])} detection(s), earliest first:")
    if not result["detections"]:
        print("  (none)")
    for d in result["detections"]:
        print(f"  {_offset(d['start'], t0):>8} .. {_offset(d['end'], t0):<8} "
              f"{d['signal']:20} peak={d['peak']} (z={d['peak_z']}, {d['points']} pts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
