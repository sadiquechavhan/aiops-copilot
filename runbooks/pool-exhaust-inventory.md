# Runbook: Inventory Service Pool Exhaustion

## Fault Type
Pool exhaustion in inventory service (`/chaos/pool-exhaust?hold=9&ttl_ms=240000`)

## Incident ID Pattern
`run-*/i3`

## Symptom
- HikariCP `db_client_connections_usage{state="used"}` rises from 0 to 9 (of 10 max)
- HikariCP `db_client_connections_usage{state="idle"}` falls from 10 to 1
- **Latency p95 stays flat** (~30 ms for all services)
- **Error rate stays at 0%** for all services
- Request rate unchanged
- Traces look completely ordinary (34 ms end-to-end)

## Root Cause
A background daemon thread in inventory service takes 9 of 10 HikariCP connections and holds them. Only 1 connection remains for actual requests. At 6 req/s with ~15 ms DB work per request, the single connection has ~10x the capacity needed, so requests never queue.

## Diagnosis
1. Check `query_metrics` for `db_pool` — `used` = 9, `idle` = 1 (or 0 under momentary contention)
2. Check `query_metrics` for `latency:inventory` — p95 stays at ~30 ms (KEY: latency is FLAT)
3. Check `query_metrics` for `error_rate:inventory` — 0 req/s
4. Check `query_traces` — traces are ordinary, self-time distributed normally
5. Confirm `GET /chaos` on inventory shows `pool_held=9`

## Why This Is Invisible to RED Signals
This is a **saturation** fault, not a latency or error fault. The system has spare capacity (1 connection handles 6 req/s easily), so the golden signals (latency, errors, traffic) don't move. Only the resource gauge (pool usage) shows the problem.

This is exactly the case a resource detector wins and a latency/error detector misses.

## Remediation
```bash
# Release the pool exhaustion fault
curl -X DELETE http://inventory:8080/chaos
```

The pool will immediately return to `used=0`, `idle=10`.

## Verification
- `db_client_connections_usage{state="used"}` returns to 0
- `db_client_connections_usage{state="idle"}` returns to 10
- `GET /chaos` on inventory shows `pool_held=0`

## Related
- See `saturation-diagnosis.md` for diagnosing pool exhaustion when traces are ordinary
- See `trace-self-time-analysis.md` for why traces look normal here