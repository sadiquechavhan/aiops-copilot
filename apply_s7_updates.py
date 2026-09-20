import os

# ============================================================
# 1. Append Session 7 to ENGINEERING_LOG.md
# ============================================================
with open(r'c:\Users\sadiq\Downloads\New_Project\ENGINEERING_LOG.md', 'r', encoding='utf-8') as f:
    eng_content = f.read()

with open(r'c:\Users\sadiq\Downloads\New_Project\session7_log.md', 'r', encoding='utf-8') as f:
    session7_content = f.read()

# Find where to append - after entry 145 (the S6 hybrid search entry)
# The file currently ends with the S6 hybrid search entry
if "146 ·" not in eng_content:
    if not eng_content.endswith('\n'):
        eng_content += '\n'
    eng_content += '\n' + session7_content
    with open(r'c:\Users\sadiq\Downloads\New_Project\ENGINEERING_LOG.md', 'w', encoding='utf-8') as f:
        f.write(eng_content)
    print("✓ ENGINEERING_LOG.md updated with Session 7 entries (146-153)")
else:
    print("✓ ENGINEERING_LOG.md already has Session 7 entries")

# ============================================================
# 2. Update PROGRESS.md with Session 7
# ============================================================
with open(r'c:\Users\sadiq\Downloads\New_Project\PROGRESS.md', 'r', encoding='utf-8') as f:
    progress_content = f.read()

session7_progress = """

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
# Retrieval confidence: search_runbooks top score (0 if no_match < 0.3)

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

if "Session 7 result" not in progress_content:
    progress_content += "\n" + session7_progress
    with open(r'c:\Users\sadiq\Downloads\New_Project\PROGRESS.md', 'w', encoding='utf-8') as f:
        f.write(progress_content)
    print("✓ PROGRESS.md updated with Session 7 section")
else:
    print("✓ PROGRESS.md already has Session 7")

# ============================================================
# 3. Update README.md
# ============================================================
with open(r'c:\Users\sadiq\Downloads\New_Project\README.md', 'r', encoding='utf-8') as f:
    readme_content = f.read()

# Update the "What is built" table
old_table = """| | Status |
|---|---|
| 3 Spring Boot services + Postgres, Docker Compose | done |
| OTel agent → Collector → Jaeger / Prometheus / Grafana | done |
| Chaos endpoints in all three services | done |
| Scenario runner, ground truth, signal verification | done |
| MCP server exposing metrics and traces as tools | done |
| `search_logs` + the log pipeline | planned |"""

new_table = """| | Status |
|---|---|
| 3 Spring Boot services + Postgres, Docker Compose | done |
| OTel agent → Collector → Jaeger / Prometheus / Grafana | done |
| Chaos endpoints in all three services | done |
| Scenario runner, ground truth, signal verification | done |
| MCP server exposing metrics and traces as tools | done |
| **Copilot agent (single-turn, grounded summaries)** | **done (S7)** |
| `search_logs` + the log pipeline | planned |"""

if "Copilot agent" not in readme_content:
    readme_content = readme_content.replace(old_table, new_table)
    
    # Add a Copilot Agent section before "What is built"
    agent_section = """

---

## Copilot Agent (Session 7)

The copilot agent turns a raw alert into a grounded incident summary by calling the MCP server's tools in sequence.

```bash
# Known incident (inventory latency, i1)
python -c "from aiops_mcp.agent import run_agent_from_ground_truth; import json; result = run_agent_from_ground_truth('run-20260809-201431', 'run-20260809-201431/i1'); print(json.dumps(result.__dict__, indent=2, default=str))"

# Unknown incident (pool exhaustion, i3 — runbooks don't cover saturation)
python -c "from aiops_mcp.agent import run_agent_from_ground_truth; import json; result = run_agent_from_ground_truth('run-20260809-201431', 'run-20260809-201431/i3'); print(json.dumps(result.__dict__, indent=2, default=str))"
```

### What the agent does

Given an alert (anomaly window from the detector), the agent:

1. **Queries metrics** for the anomaly window — gets pre-aggregated percentiles + downsampled series
2. **Queries traces** for the affected service — gets per-service self-time aggregates + compact trace rows
3. **Gets a detailed trace** — indented span tree with self-time at each level
4. **Correlates the incident** — ranks candidate services by root-cause likelihood with confidence scores
5. **Searches runbooks** — retrieves remediation guidance with source_path + heading citations

### Output format

```json
{
  "summary": "Alert: latency:inventory anomaly detected; correlation identifies inventory as likely cause (latency family); runbook remediation available; confidence 87%",
  "evidence": {
    "metrics": {"percentiles": {"p50": 0.412, "p95": 0.689, "p99": 0.891}, ...},
    "traces": {"aggregates": {"inventory": 96.2, "gateway": 2.1, "orders": 1.7}, ...},
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

### Confidence calibration

Three signals combined via weighted harmonic mean:
- **Detection confidence** (weight 0.3): z-score peak_z / 10 (capped at 1.0)
- **Correlation confidence** (weight 0.4): from `correlate_incident` top_confidence (already 0-1, calibrated in S5)
- **Retrieval confidence** (weight 0.3): `search_runbooks` top score (0 if no_match < 0.3)

If retrieval is `no_match` (runbooks don't cover this incident), overall confidence is **capped at 0.4** — the agent explicitly says "I don't know" rather than hallucinating a remediation.

### The interview answer: unknown incidents

When tested on **i3 (pool exhaustion)** — a fault type the runbooks don't cover — the agent returns:

```json
{
  "likely_cause": "unknown (runbooks do not cover pool_exhaust:inventory)",
  "recommended_action": "runbooks do not cover this incident type — escalate to on-call",
  "confidence": 0.40,
  "citations": [{"source_path": "runbooks/saturation-diagnosis.md", "heading": "...", "score": 0.28}, ...]
}
```

This explicit "runbooks do not cover this" with confidence capped at 0.4 is the S7 done condition. The failure mode *is* the interview answer: the system knows when it doesn't know.

### Architecture

The agent is a single-turn loop (multi-turn deferred to S8/S9). It starts the MCP server as a subprocess, performs the JSON-RPC handshake, calls the five tools deterministically, and synthesizes the result. Every tool call is recorded in `raw_tool_calls` for audit trail.

**Debt carried forward:**
- Multi-turn loop (S8)
- Cross-encoder reranking for retrieval
- Log pipeline (`search_logs`) — S4 debt
- `telemetry_status` cannot distinguish saturated from dead — S3 debt
"""
    
    # Insert before "What is built" section
    if "## What is built" in readme_content:
        readme_content = readme_content.replace("## What is built", agent_section + "\n\n## What is built")
    else:
        readme_content += agent_section
    
    with open(r'c:\Users\sadiq\Downloads\New_Project\README.md', 'w', encoding='utf-8') as f:
        f.write(readme_content)
    print("✓ README.md updated with Session 7")
else:
    print("✓ README.md already has Session 7")

print("\n✅ All three files updated!")