# Runbook: Saturation Diagnosis — Pool Exhaustion When Traces Are Ordinary

## The Problem
A resource saturation fault (pool exhaustion) that produces **zero impact on latency or error rate**. The RED signals (Rate, Errors, Duration) are completely flat. Only the resource gauge moves.

## The Scenario (i3)
- HikariCP pool: 10 connections max
- Chaos holds 9 connections
- 1 connection remains for traffic
- At 6 req/s × 15 ms DB work = 90 ms/s utilization → **1 connection has 10x spare capacity**
- Result: latency flat, errors flat, traces ordinary

## Diagnosis Checklist
1. **Check pool metrics first** — `query_metrics` with `metric=db_pool`
   - `used` near max (9/10 or 10/10)
   - `idle` near 0
2. **Confirm RED signals are flat** — `query_metrics` for `latency` and `error_rate`
   - All p95 ~30 ms
   - All 5xx ~0 req/s
3. **Check traces are ordinary** — `query_traces`
   - Self-time distributed normally (no service >60%)
   - No missing children
4. **Correlate** — saturation signal + flat RED + ordinary traces = pool exhaustion

## Why This Matters
- A latency/error detector WILL MISS this fault
- A resource detector (pool gauge) WILL CATCH it
- This is the "leading indicator" case: pool fills BEFORE latency/errors rise

## Remediation
```bash
# Release the pool exhaustion
curl -X DELETE http://inventory:8080/chaos
```

## Prevention (Production)
- Alert on `db_client_connections_usage / max > 0.8`
- Set `maximum-pool-size` based on peak concurrent requests × safety factor
- Monitor pool wait time (`HikariCP` exposes `pool_wait_time`)

## Verification
- `used` returns to 0, `idle` returns to 10
- RED signals remain flat (they never moved)

## Related
- `pool-exhaust-inventory.md` (i3)
- `trace-self-time-analysis.md` (why traces look normal)