# Runbook: Inventory Service Latency Injection

## Fault Type
Injected latency in inventory service (`/chaos/latency?ms=400&jitter=80`)

## Incident ID Pattern
`run-*/i1`

## Symptom
- Gateway p95 latency rises from ~50 ms to ~480 ms
- Inventory p95 latency rises from ~30 ms to ~480 ms
- Orders p95 latency rises proportionally
- Error rate remains at 0%
- Request rate unchanged

## Root Cause
The chaos filter in inventory service injects a `Thread.sleep(400 ± 80 ms)` before calling the database. Every request to `POST /reserve` is delayed.

## Diagnosis
1. Check `query_metrics` for `latency:inventory` — p95 will show ~480 ms
2. Check `query_traces` for `service=gateway` — self-time attribution will show **inventory ~96%** of self-time
3. Verify orders and gateway have low self-time (<10% each)
4. Confirm `X-Chaos-Injected: latency` header on responses

## Remediation
```bash
# Disable the chaos fault
curl -X DELETE http://inventory:8080/chaos
```

Or via gateway:
```bash
curl -X DELETE http://gateway:8080/chaos
```

## Verification
- Inventory p95 returns to ~30 ms within 1 second
- Gateway p95 returns to ~50 ms
- `GET /chaos` on inventory shows `requests_delayed=0`

## Related
- See `latency-propagation.md` for how this differs from gateway latency
- See `trace-self-time-analysis.md` for self-time attribution details