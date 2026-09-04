# Runbook: Orders Service Error Rate Injection

## Fault Type
Injected error rate in orders service (`/chaos/error-rate?pct=20`)

## Incident ID Pattern
`run-*/i2`

## Symptom
- Orders 5xx rate rises from 0 to ~1.2 req/s (20% of 6 req/s)
- Gateway 5xx rate rises proportionally (~1.2 req/s cascade)
- Inventory 5xx rate stays at 0
- Latency p95 stays flat (~30 ms) for all services
- Request rate unchanged

## Root Cause
The chaos filter in orders service returns HTTP 500 immediately for 20% of requests (coin flip), **without calling inventory**. The filter short-circuits the chain: no downstream call, no database access.

## Diagnosis
1. Check `query_metrics` for `error_rate:orders` — 5xx rate ~1.2 req/s
2. Check `query_metrics` for `error_rate:gateway` — cascade 5xx ~1.2 req/s
3. Check `query_metrics` for `error_rate:inventory` — 0 req/s (KEY: inventory never sees the request)
4. Check `query_traces` for `service=orders` — spans have **0 children** (missing child spans)
5. Verify `X-Chaos-Injected: error-rate` header on 500 responses

## Missing Child Spans = Error Origin
The orders entry span has 0 children because the chaos filter returned 500 before calling inventory. This is the signature of "failed fast, didn't call downstream".

A genuine downstream failure (e.g., inventory timeout) WOULD have child spans — the call happened, then failed.

## Remediation
```bash
# Disable the chaos fault
curl -X DELETE http://orders:8080/chaos
```

Or via gateway:
```bash
curl -X DELETE http://gateway:8080/chaos
```

## Verification
- Orders 5xx rate returns to 0 within 1 second
- Gateway 5xx rate returns to 0
- `GET /chaos` on orders shows `requests_failed=0`

## Related
- See `error-propagation.md` for how 5xx cascades
- See `missing-child-spans.md` for using missing children to identify error origin