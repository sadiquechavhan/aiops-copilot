# Runbook: Trace Self-Time Analysis Methodology

## What Is Self-Time?
Self-time = span duration - merged child intervals.

It measures how much time a service spends doing its OWN work, excluding time spent waiting for downstream calls.

```
span duration = 500 ms
child spans: [100-200 ms], [150-250 ms], [300-400 ms]
merged intervals: [100-250 ms], [300-400 ms] = 150 ms + 100 ms = 250 ms
self-time = 500 - 250 = 250 ms (50%)
```

## Why Self-Time, Not Duration?
- **Duration** includes waiting for downstream — pollutes attribution
- **Self-time** isolates the service's own contribution
- High self-time = this service is slow (added work)
- Low self-time = this service is waiting (downstream is slow)

## The Algorithm (from `_fold` in traces.py)

For each trace:
1. Build span tree from references (CHILD_OF)
2. For each service, collect its spans
3. For each span, compute self-time = duration - merged child durations
4. Sum self-time per service across all spans in trace
5. Aggregate across traces: mean self_time_pct per service

## Interpreting Self-Time %

| Self-Time % | Interpretation |
|---|---|
| >60% | This service is the latency origin |
| 20-60% | Contributing, but not dominant |
| <20% | Mostly waiting on downstream |

## Our Verified Results

| Incident | Fault | Dominant Self-Time |
|---|---|---|
| i1 | Inventory latency | **inventory 96%** |
| i4 | Gateway latency | **gateway 92%** |
| i2 | Orders error-rate | inventory 85% (victim, not origin!) |
| i3 | Pool exhaustion | distributed normally |

**Key insight for i2**: Error-rate faults REMOVE work (orders fails fast). Self-time points at the VICTIM (inventory never called), not the ORIGIN (orders). Use missing children for error origin instead.

## Usage in Correlation
```python
# From correlate.py
pct = compute_self_time_from_export(export)
# Returns: {"gateway": 4.2, "orders": 3.1, "inventory": 92.7}
# Rank by pct descending
```

## Related
- `latency-propagation.md` (i1 vs i4 discrimination)
- `correlate.py` (implementation in `correlate_latency`)