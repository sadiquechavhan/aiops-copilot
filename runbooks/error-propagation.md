# Runbook: Error Propagation — 5xx Cascade Diagnosis

## The Problem
A 5xx error in one service cascades upstream. The gateway shows 5xx, but the origin may be downstream.

In our system:
- **i2**: Orders injects 500 (20%) → Gateway shows 5xx cascade
- Inventory never sees the request (orders fails fast)

## The Solution: Missing Child Spans + 5xx Rate

Two signals together identify the error origin:

1. **5xx rate per service** — which service is returning 5xx?
2. **Missing child spans** — does that service's entry span have children?

| Service | 5xx Rate | Entry Span Children | Verdict |
|---|---|---|---|
| orders | HIGH (~1.2/s) | **0 children** | **ERROR ORIGIN** (failed fast) |
| gateway | HIGH (~1.2/s) | Has children (orders span) | Propagated |
| inventory | 0 | N/A | Not involved |

## Algorithm
1. Get 5xx rate per service in the anomaly window (`query_metrics` with `metric=error_rate`)
2. Find the service with the highest 5xx rate
3. Check that service's traces: does its entry span have children?
4. **0 children** = error origin (failed before calling downstream)
5. **Has children** = error propagated from downstream

## Why Missing Children Works
- **Injected 500**: chaos filter returns 500 immediately, chain never runs → 0 children
- **Real downstream timeout**: call happens, times out → HAS children (the timeout span)
- **Real downstream 500**: call happens, downstream returns 500 → HAS children

Missing children means "this service decided to fail without trying downstream" — that's the error origin.

## Remediation
```bash
# Disable the chaos fault at the origin service
curl -X DELETE http://orders:8080/chaos
```

## Verification
- 5xx rate returns to 0 on origin service
- Cascade 5xx returns to 0 on upstream services
- `GET /chaos` on origin shows `requests_failed=0`

## Related
- `error-rate-orders.md` (i2)
- `missing-child-spans.md` (detailed methodology)