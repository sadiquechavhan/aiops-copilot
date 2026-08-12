"""
export_traces -- save the raw Jaeger traces for a labelled run into the repo.

WHY THIS EXISTS
---------------
The trace evidence for every labelled run lived in exactly one place: Jaeger's
Badger volume. The run directories held run_meta.json, signals.md and
traffic.log, and the "trace evidence" for run-20260809-201431 was prose in a
commit message. So the README's i1-versus-i4 self-time figures were backed by
data that only existed inside a container -- and the fix for a saturated Badger
is to empty that container's volume.

Two independent clocks were running out on it. Badger's TTL is 72h, so the
evidence expires on its own. And Badger outgrows Jaeger's 512MB memory cap after
a few hours of load, which starves the collector and eventually forces exactly
the wipe that destroys it.

WHY RAW JSON AND NOT THE FOLDED VIEW
------------------------------------
query_traces' per-service self-time is DERIVED. Given the raw spans it can be
recomputed at any time, including by a future version of the folding code with a
different opinion about merged intervals. The reverse is not true: no amount of
work reconstructs the spans from a percentage. Store the input, derive the rest.

WHY THE TIMEOUT IS SO LONG
--------------------------
Because the situation this tool exists for is a Jaeger that is barely answering.
Measured at 43s for a single 4-minute window while Badger was at 548MB against a
512MB cap. A tool for preserving evidence during degradation cannot use a budget
that only works when the stack is healthy.
"""

import argparse
import datetime
import json
import os
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROUND_TRUTH = os.path.join(ROOT, "ground_truth.jsonl")
JAEGER = os.environ.get("AIOPS_JAEGER_URL", "http://localhost:16686")

# Generous, deliberately. See the module docstring.
TIMEOUT_S = 180.0


def epoch_us(text):
    """ISO-8601 -> microseconds, which is the only unit Jaeger's API accepts.

    Handles both the plain and fractional-second forms, because ground truth
    carries confirmed_* with milliseconds and start/end without.
    """
    text = text.replace("Z", "+00:00")
    stamp = datetime.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return int(stamp.timestamp() * 1e6)


def fetch(service, start_iso, end_iso, limit):
    query = urllib.parse.urlencode({"service": service,
                                    "start": epoch_us(start_iso),
                                    "end": epoch_us(end_iso),
                                    "limit": limit})
    url = f"{JAEGER}/api/traces?{query}"
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
        return json.loads(response.read())


def load_rows(run_id):
    rows = []
    with open(GROUND_TRUTH, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and json.loads(line).get("run_id") == run_id:
                rows.append(json.loads(line))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("run_id", help="run_id from ground_truth.jsonl")
    parser.add_argument("--service", default="gateway",
                        help="trace root service; gateway is the entry point, "
                             "so its traces contain the whole chain")
    parser.add_argument("--limit", type=int, default=15,
                        help="traces per incident window")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing export. Refused by default: "
                             "these files are committed evidence and a re-run "
                             "after the window leaves Jaeger's TTL replaces them "
                             "with 0 traces")
    options = parser.parse_args()

    rows = load_rows(options.run_id)
    if not rows:
        raise SystemExit(f"no ground-truth rows for {options.run_id}")

    out_dir = os.path.join(ROOT, "runs", options.run_id, "traces")

    # Refuse to replace good evidence with an empty answer.
    #
    # These files are the only copy of the trace evidence and they are committed.
    # Re-running this tool on a run whose windows have aged past Jaeger's TTL --
    # or after the Badger volume has been emptied, which is the operational fix
    # for saturation -- fetches zero traces and writes them straight over the
    # export. Nothing errors: Jaeger answers 200 with an empty data array, which
    # is a truthful statement about a window it no longer holds.
    #
    # So the guard is on the OUTPUT, not on the input. Refusing based on the
    # window age would need this tool to know the TTL, and a wipe invalidates
    # windows that are still nominally inside it.
    existing = {}
    if os.path.isdir(out_dir):
        for row in rows:
            incident = row["incident_id"].rsplit("/", 1)[-1]
            path = os.path.join(out_dir, f"{incident}.json")
            if os.path.exists(path):
                existing[incident] = path
    if existing and not options.force:
        print(f"{len(existing)} export(s) already in {out_dir}:")
        for incident, path in sorted(existing.items()):
            with open(path, encoding="utf-8") as handle:
                have = len(json.load(handle).get("data") or [])
            print(f"  {incident}.json  {have} traces  {os.path.getsize(path):,} bytes")
        raise SystemExit(
            "\nRefusing to overwrite. These files are committed evidence and are\n"
            "not regenerable once the window leaves Jaeger's TTL or the Badger\n"
            "volume is emptied -- a re-run would replace them with 0 traces.\n"
            "To recompute the self-time claim from what is already here, no stack\n"
            "needed:  py -3.12 tools/verify_traces.py\n"
            "To export anyway, pass --force.")

    os.makedirs(out_dir, exist_ok=True)
    print(f"{len(rows)} incident(s) -> {out_dir}\n")

    index, failures, empty = [], 0, 0
    for row in rows:
        incident = row["incident_id"].rsplit("/", 1)[-1]
        # confirmed_* is when the fault was VERIFIED active. Same preference the
        # ground-truth probe uses, so the exported evidence covers the same
        # window the checks are scored over.
        start = row.get("confirmed_start") or row["start"]
        end = row.get("confirmed_end") or row["end"]
        label = f"{incident} {row.get('fault_type')}/{row.get('service')}"

        try:
            payload = fetch(options.service, start, end, options.limit)
        except Exception as exc:
            # Reported, not raised: one unreachable window should not cost the
            # other three, and a partial export is worth strictly more than none.
            print(f"  FAIL  {label} -- {type(exc).__name__}: {exc}")
            failures += 1
            continue

        traces = payload.get("data") or []
        spans = sum(len(t.get("spans", [])) for t in traces)

        if not traces:
            # Not written. An empty export is indistinguishable from a window
            # that had no traffic, and with --force it would overwrite real
            # evidence with that ambiguity. Skipping keeps whatever is on disk.
            print(f"  EMPTY {label} -- 0 traces returned, not written "
                  f"(window past the TTL, or the volume was emptied)")
            empty += 1
            continue

        name = f"{incident}.json"
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        size = os.path.getsize(path)
        print(f"  ok    {label}  {len(traces)} traces / {spans} spans, "
              f"{size:,} bytes")
        index.append({"incident_id": row["incident_id"], "file": name,
                      "root_service": options.service,
                      "window": {"start": start, "end": end},
                      "traces": len(traces), "spans": spans})

    # An index, so the exported files can be tied back to the labels without
    # re-deriving which window each one came from.
    #
    # MERGED with any existing index rather than replacing it. Writing only this
    # run's successes would delete the entries for files that were skipped or
    # failed, orphaning evidence that is still sitting on disk -- and
    # verify_traces.py drives off this index, so an erased entry silently drops
    # an incident from the check. A partial re-export must not narrow the record
    # of what is here.
    index_path = os.path.join(out_dir, "index.json")
    merged = {}
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as handle:
            for entry in json.load(handle).get("incidents", []):
                if os.path.exists(os.path.join(out_dir, entry.get("file", ""))):
                    merged[entry["incident_id"]] = entry
    for entry in index:
        merged[entry["incident_id"]] = entry

    order = [row["incident_id"] for row in rows]
    with open(index_path, "w", encoding="utf-8") as handle:
        json.dump({"run_id": options.run_id, "jaeger_url": JAEGER,
                   "limit": options.limit,
                   "incidents": sorted(merged.values(),
                                       key=lambda e: order.index(e["incident_id"])
                                       if e["incident_id"] in order else 99)},
                  handle, indent=2)

    print(f"\n{len(index)}/{len(rows)} window(s) exported this run"
          + (f"; {empty} empty and left untouched" if empty else "")
          + (f"; {failures} failed" if failures else "")
          + f"; index lists {len(merged)}")
    if index:
        print("verify with:  py -3.12 tools/verify_traces.py")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
