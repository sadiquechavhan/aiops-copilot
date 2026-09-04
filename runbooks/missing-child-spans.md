# Runbook: Missing Child Spans — Identifying Error Origin

## The Signal
A span with **0 children** that should have children (based on service topology) indicates the service failed BEFORE calling its downstream dependency.

## Our Service Topology
```
gateway (POST /api/orders)
  └── orders (POST /api/orders)
        └── inventory (POST /reserve)
              └── Postgres (DB)
```

Expected children:
- gateway entry span → should have orders child
- orders entry span → should have inventory child
- inventory entry span → should have DB child

## The Pattern

| Scenario | Entry Span Children | Meaning |
|---|---|---|
| Normal request | Has expected children | Healthy |
| Downstream timeout | Has children (timeout span) | Error propagated FROM downstream |
| Downstream 500 | Has children (500 span) | Error propagated FROM downstream |
| **Fast fail (injected 500)** | **0 children** | **ERROR ORIGIN** — failed before calling downstream |
| Real fast fail (circuit breaker, validation) | 0 children | ERROR ORIGIN |

## How to Check (from correlate.py)

```python
# get_missing_children_signal(run_id, incident_id, service)
# Returns True if entry spans for this service have 0 children
```

Algorithm:
1. Load trace export for the incident
2. Find entry spans for the service (no parent in same service)
3. Check if entry span has children
4. 0 children → missing_children = True

## Our Verified Result (i2)
- orders entry spans: **0 children** (chaos filter returned 500 before calling inventory)
- inventory entry spans: N/A (never reached)
- gateway entry spans: Has orders child (orders was called, returned 500)

## Correlation Logic (from correlate_errors)
1. Get 5xx rate per service → orders has highest
2. Check orders for missing children → **True**
3. Conclusion: orders is the error origin (failed fast)

## Why This Isn't a Test Artefact
Entry 56 in ENGINEERING_LOG.md: "A REAL 500 from a downstream timeout also has no children — the timeout happened before the call returned."

So missing children is a GENUINE error-origin signal, not just a chaos-injection artefact. It means "this service did not successfully complete a downstream call."

## Related
- `error-rate-orders.md` (i2)
- `error-propagation.md` (cascade diagnosis)
- `correlate.py` (implementation in `get_missing_children_signal` and `correlate_errors`)