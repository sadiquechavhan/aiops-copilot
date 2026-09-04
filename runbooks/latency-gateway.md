# Runbook: Gateway Service Latency Injection

## Fault Type
Injected latency in gateway service (`/chaos/latency?ms=250&jitter=50`)

## Incident ID Pattern
`run-*/i4`

## Symptom
- Gateway p95 latency rises from ~50 ms to ~480 ms
- Inventory p95 latency stays flat at ~30 ms
- Orders p95 latency stays flat at ~30 ms
- Error rate remains at 0%
- Request rate unchanged

## Root Cause
The chaos filter in gateway service injects a `Thread.sleep(250 ± 50 ms)` before calling orders service. Every request to `POST /api/orders` is delayed at the entry point.

## Diagnosis
1. Check `query_metrics` for `latency:gateway` — p95 will show ~480 ms
2. Check `query_metrics` for `latency:inventory` — p95 stays at ~30 ms (KEY DIFFERENTIATOR from i1)
3. Check `query_traces` for `service=gateway` — self-time attribution will show **gateway ~92%** of self-time
4. Verify inventory and orders have low self-time (<10% each)
5. Confirm `X-Chaos-Injected: latency` header on responses

## Remediation
```bash
# Disable the chaos fault
curl -X DELETE http://gateway:8080/chaos
```

## Verification
- Gateway p95 returns to ~50 ms within 1 second
- `GET /chaos` on gateway shows `requests_delayed=0`

## Key Differentiator from i1 (Inventory Latency)
| Signal | i1 (Inventory) | i4 (Gateway) |
|---|---|---|
| Gateway p95 | ~480 ms | ~480 ms |
| Inventory p95 | ~480 ms | **~30 ms (flat)** |
| Self-time attribution | inventory 96% | gateway 92% |

The gateway p95 panel alone CANNOT distinguish i1 from i4. Only trace self-time or inventory p95 can separate them.

## Related
- See `latency-propagation.md` for how this differs from inventory latency
- See `trace-self-time-analysis.md` for self-time attribution details