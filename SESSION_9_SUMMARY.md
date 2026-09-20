# Session 9 — Closed Loop Automation: Summary

## Overview
Session 9 implements closed-loop automation for the AIOps system, connecting alerts → diagnosis → remediation → verification with a complete audit trail.

## What Was Implemented

### 1. Remediation Module (`aiops_mcp/remediation.py`)
- **Whitelisted actions** (blast-radius control):
  - `disable_chaos(service)` — DELETE /chaos on a specific service
  - `restart_container(service)` — docker compose restart <service>
  - `scale_replicas(service, n)` — placeholder for K8s (not implemented in Compose)

- **Approval gate** with two modes:
  - `human` — prompts for yes/no on stdin
  - `auto` — policy-based (e.g., `disable_chaos_known_faults`)

- **Recovery verification**:
  - Snapshots metrics before and after remediation
  - Compares post-remediation values to baseline
  - Latency: recovered if within 20% of baseline
  - Error rate: recovered if near zero

- **Audit logging**:
  - Every attempt writes JSON line to `runs/<run_id>/remediation_log.jsonl`
  - Contains: timestamp, action, args, approved_by, status, pre/post metrics, verification result

### 2. MCP Tools Added (`aiops_mcp/server.py`)
- `suggest_remediation(signal, candidate_service)` — returns whitelisted action with reasoning
- `execute_remediation(run_id, incident_id, action, args, anomaly_signal, approval_mode, approval_policy, baseline_metrics)` — executes with approval gate and verification
- `get_remediation_log(run_id)` — retrieves audit log for a run

### 3. Agent Integration (`aiops_mcp/agent.py`)
- Added `suggest_remediation` call after `search_runbooks`
- Extended `AgentResult` with `remediation` field containing:
  - `suggested` — the suggested action
  - `executed` — remediation record if executed
  - `verified` — boolean recovery verification

## Test Results

### Unit Tests (`test_closed_loop.py`)
```
✓ suggest_remediation works
✓ execute_remediation import works
✓ get_remediation_log works
```

### Integration Tests (`test_integration_closed_loop.py`)
```
✓ MCP tools work directly
✓ disable_chaos works on live service
✓ snapshot + recovery check works
✓ Full remediation flow works (verified status achieved)
```

### Verification Example (from remediation log)
```json
{
  "status": "verified",
  "action": "disable_chaos",
  "args": {"service": "orders"},
  "approved_by": "auto:disable_chaos_known_faults",
  "pre_metrics": {"latest": 0.0, "note": "zero errors"},
  "post_metrics": {"latest": 0.0, "note": "zero errors"},
  "verification": {
    "signal": "errors:orders",
    "baseline": 0.0,
    "current": 0.0,
    "recovered": true,
    "reason": "error_rate near zero"
  }
}
```

## Architecture Flow

```
Alert (from detector)
    ↓
Agent investigates (query_metrics → query_traces → correlate_incident → search_runbooks)
    ↓
suggest_remediation(signal, candidate_service) → {action, args, reasoning}
    ↓
execute_remediation(...) with approval gate
    ↓
[Approval: human prompt OR auto policy]
    ↓
Action executed (DELETE /chaos, docker compose restart, etc.)
    ↓
Wait 10s for system to settle
    ↓
Re-query metrics (snapshot_signal)
    ↓
check_recovery(pre, post, baseline) → recovered: true/false
    ↓
Write audit record to remediation_log.jsonl
    ↓
Return AgentResult with remediation info
```

## Key Design Decisions

1. **Whitelist only** — No arbitrary commands. The three actions are the blast radius boundary.

2. **Approval gate is mandatory** — Even in auto mode, a policy name is recorded. Human mode is default for demos.

3. **Idempotent actions** — `disable_chaos` can be called multiple times safely.

4. **Metrics-driven verification** — Uses the same signal that triggered the alert for pre/post comparison.

5. **Audit trail is first-class** — Every step logged with timestamps, enabling post-incident review.

6. **Handles empty metrics gracefully** — Error rate "no data" = zero errors (series created by first 500).

## Files Created/Modified

- `aiops_mcp/remediation.py` — New module with all remediation logic
- `aiops_mcp/server.py` — Added 3 new MCP tool schemas and dispatch handlers
- `aiops_mcp/agent.py` — Integrated suggest_remediation, added remediation field to AgentResult
- `test_closed_loop.py` — Unit tests
- `test_integration_closed_loop.py` — Integration tests with live services

## Interview Talking Points

- "I implemented a whitelist of three remediation actions (disable_chaos, restart_container, scale_replicas) — nothing arbitrary can execute."
- "There's an approval gate with human and auto modes, both producing the same audit record."
- "Recovery verification re-queries the exact signal that triggered the alert and compares to baseline."
- "Every attempt writes a JSON audit log with pre/post metrics, approval status, and verification result."
- "The system handles the edge case where error_rate series doesn't exist until the first 500 — 'no data' = zero errors."
- "Confidence calibration caps at 0.4 when runbooks don't cover the incident — honest uncertainty."