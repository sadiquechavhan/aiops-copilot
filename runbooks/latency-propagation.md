# Runbook: Latency Propagation — Upstream vs Downstream Discrimination

## The Problem
Two different faults produce **identical gateway p95 latency** (~480 ms):
- **i1**: Inventory latency injection (downstream)
- **i4**: Gateway latency injection (upstream)

The gateway p95 panel alone CANNOT distinguish them.

## The Solution: Trace Self-Time Attribution

Self-time = span duration - merged child intervals. It measures how much time a service spends doing its OWN work (not waiting for downstream).

| Incident | Fault Location | Gateway Self-Time | Inventory Self-Time | Orders Self-Time |
|---|---|---|---|---|
| **i1** | inventory (downstream) | ~4% | **~96%** | ~4% |
| **i4** | gateway (upstream) | **~92%** | ~4% | ~4% |

## How to Use
1. Run `query_traces` for `service=gateway` during the incident window
2. Look at the `self_time_pct` per service in the folded output
3. The service with **high self-time % is the fault origin**

## Why This Works
- **Latency faults ADD work** (sleep, slow query, GC pause) — the faulty service accumulates self-time
- **Propagation is waiting** — upstream services spend time in child spans, not self-time

## Verification Commands
```bash
# Via MCP tools
query_traces(service="gateway", window="15m", limit=15)
# Returns folded self-time per service
```

## Related
- `latency-inventory.md` (i1)
- `latency-gateway.md` (i4)
- `trace-self-time-analysis.md` (detailed self-time methodology)