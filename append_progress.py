import sys

with open(r'c:\Users\sadiq\Downloads\New_Project\PROGRESS.md', 'r') as f:
    content = f.read()

# Check if Session 7 section already exists
if "Session 7 result" in content:
    print("Session 7 section already exists")
    sys.exit(0)

session7_section = """

---

## 11. Session 7 result — The Copilot Agent

### Done condition
> *One command turns a raw alert into a grounded incident summary. Test it on an incident type the runbooks DON'T cover and observe what it does — that failure mode is an interview answer.*

### What was implemented

**Single-turn copilot agent** (`aiops_mcp/agent.py`) that uses the MCP server to investigate alerts:

1. **Query metrics** — gets pre-aggregated percentiles + downsampled series for the anomaly window
2. **Query traces** — gets per-service self-time aggregates + compact trace rows
3. **Get trace** — gets indented span tree for detailed examination
4. **Correlate incident** — ranks candidate services by root-cause likelihood with confidence
5. **Search runbooks** — retrieves remediation guidance with citations

### Design decisions (recorded in ENGINEERING_LOG.md entries 146-153)

| Decision | Choice | Rejected |
|---|---|---|
| Agent loop | Single-turn | Multi-turn (deferred to S8/S9) |
| System prompt | Tool contracts with closed vocabularies | Minimal descriptions |
| Confidence | Weighted harmonic mean (detection=0.3, correlation=0.4, retrieval=0.3) | Arithmetic mean, max, min |
| Evidence | Pre-aggregated tool outputs (≤3k tokens) | Raw data |
| Runbook grounding | Cite source_path + heading; no_match returns "runbooks don't cover this" | Hallucinated remediation |
| Unknown incidents | Explicit "I don't know" with confidence capped at 0.4 | Best-guess remediation |
| Confidence threshold | No threshold — returns calibrated confidence, human decides | Hard threshold |
| Audit trail | Raw tool calls in AgentResult.raw_tool_calls | No logging |

### Confidence calibration formula

```python
# Detection confidence: peak_z / 10 (capped at 1.0)
# Correlation confidence: from correlate_incident.top_confidence (already 0-1)
# Retrieval confidence: search_runbooks top score (0 if no_match)

# Weighted harmonic mean:
weights = [0.3, 0.4, 0.3]
values = [det_conf, corr_conf, retr_conf]
combined = sum(weights) / sum(w/v for w,v in zip(weights, values))

# If retrieval is no_match: overall capped at 0.4
```

### Agent output format (JSON)

```json
{
  "summary": "One-sentence incident summary",
  "evidence": {
    "metrics": {...},
    "traces": {...},
    "logs": []
  },
  "likely_cause": "inventory service — latency fault",
  "recommended_action": "curl -X DELETE http://inventory:8080/chaos",
  "confidence": 0.87,
  "citations": [
    {"source_path": "runbooks/latency-inventory.md", "heading": "Remediation", "score": 0.92}
  ],
  "raw_tool_calls": [...]
}
```

### Testing the agent

Run on a known incident (inventory latency, i1):
```bash
python -c "from aiops_mcp.agent import run_agent_from_ground_truth; import json; result = run_agent_from_ground_truth('run-20260809-201431', 'run-20260809-201431/i1'); print(json.dumps(result.__dict__, indent=2, default=str))"
```

Run on an UNKNOWN incident (pool exhaustion, i3 — runbooks don't cover saturation faults):
```bash
python -c "from aiops_mcp.agent import run_agent_from_ground_truth; import json; result = run_agent_from_ground_truth('run-20260809-201431', 'run-20260809-201431/i3'); print(json.dumps(result.__dict__, indent=2, default=str))"
```

The i3 test is the S7 done condition: the agent returns `likely_cause="unknown (runbooks do not cover this)"`, `recommended_action="runbooks do not cover this incident type — escalate to on-call"`, confidence capped at 0.4, with top_candidates showing what it tried to match. This explicit "I don't know" is the interview answer.

### Integration with MCP server

The agent starts the MCP server as a subprocess (`python -m aiops_mcp.server`), performs the JSON-RPC handshake, calls the five tools in sequence, and returns a structured `AgentResult`. All tool calls are recorded in `raw_tool_calls` for audit trail.

### Debt carried forward

- **Multi-turn loop deferred to S8** — agent could refine queries based on tool results
- **Cross-encoder reranking deferred to S7+** — would improve retrieval on ambiguous queries
- **Log pipeline still absent** — `search_logs` tool not implemented (S4 debt)
- **telemetry_status cannot distinguish saturated from dead** (S3 entry 107/108)

### Files added/modified

- `aiops_mcp/agent.py` — new, the copilot agent implementation
- `ENGINEERING_LOG.md` — entries 146-153 added (Session 7 decisions)
- `session7_log.md` — standalone Session 7 log (merged into ENGINEERING_LOG.md)
"""

# Append to the end
new_content = content + "\n" + session7_section

with open(r'c:\Users\sadiq\Downloads\New_Project\PROGRESS.md', 'w') as f:
    f.write(new_content)

print("PROGRESS.md updated with Session 7 section")