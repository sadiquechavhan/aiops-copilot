---

## Session 7 — The Copilot Agent

### Agent architecture: single-turn vs multi-turn

**146 · Single-turn agent loop for S7 — multi-turn deferred to S8/S9**
Rejected: multi-turn conversation loop where the agent iterates until confident.

S7 scope explicitly says "single-turn vs multi-turn? (S7 can start single-turn, iterate)" — the decision is to start single-turn. The agent makes one pass: detect → correlate → search_runbooks → synthesize. If runbooks don't cover it, it says so explicitly. Multi-turn (where the agent might refine its query based on tool results) is explicitly deferred to S8/S9. The reason: a single-turn agent is auditable (every tool call is a deterministic function of the alert), measurable (we can score it on ground truth), and its failure modes are clearer for the interview. A multi-turn agent introduces path-dependent behavior that's harder to evaluate.

### System prompt design: tool descriptions and confidence calibration

**147 · System prompt embeds tool contracts, not just names — "what the tool returns" not "what the tool does"**
Rejected: minimal tool descriptions letting the model discover behavior.

Each tool description in the system prompt states: (a) when to call it (ordering), (b) what parameters it takes (closed vocabularies), (c) what the response structure looks like, and (d) how to interpret key fields. This is context engineering: the tool schemas live in the model's context every turn, so they must be dense with signal. The model doesn't need to "explore" — it needs to call the right tool with the right arguments. The SYSTEM_PROMPT in agent.py documents this contract explicitly.

### Confidence calibration: combining three signals

**148 · Weighted harmonic mean of detection + correlation + retrieval confidence — penalizes any weak component**
Rejected: arithmetic mean, max, or min.

Three confidence signals with different semantics:
- Detection confidence: z-score peak_z / 10 (capped at 1.0) — measures "how anomalous is this?"
- Correlation confidence: correlate_incident top_confidence (already 0-1, calibrated in S5) — measures "how sure is the trace evidence?"
- Retrieval confidence: search_runbooks top score (0-1, but <0.3 = no_match) — measures "does a runbook cover this?"

Arithmetic mean would let a high detection confidence mask a no_match retrieval. Max would be meaningless. Min is too harsh (one zero kills everything). Weighted harmonic mean (weights: detection=0.3, correlation=0.4, retrieval=0.3) penalizes any weak component proportionally — if retrieval is 0.2, overall drops to ~0.27 regardless of the others. If retrieval is no_match (0.0), overall is capped at 0.4 (explicit "I don't know" confidence).

### Evidence packing: fitting metric + trace + log into context

**149 · Pre-aggregated tool outputs, not raw data — query_metrics returns percentiles + 40-point series, query_traces returns aggregates + 20 traces**
Rejected: returning raw Prometheus vectors (thousands of points) or raw Jaeger spans.

S3's lesson (ENGINEERING_LOG entry 76): "a tool that dumps 5,000 spans destroys the context window and makes the copilot worse than curl." Every MCP tool was designed to aggregate before returning. query_metrics: window-wide percentiles (p50/p95/p99) + downsampled series (≤40 points). query_traces: per-service self-time aggregates + compact per-trace rows (≤20 traces). get_trace: one indented span tree. search_runbooks: ≤3 chunks with source_path + heading. Total fits in ~3k tokens, leaving room for the model's reasoning and response.

### Runbook grounding: citations and no_match handling

**150 · Every runbook claim cites source_path + heading — no_match (<0.3) returns "runbooks do not cover this"**
Rejected: letting the model synthesize remediation from partial matches.

search_runbooks returns status="no_match" when top score < 0.3, with top_candidates for transparency. The agent's SYSTEM_PROMPT explicitly says: "YOU MUST RESPECT no_match — do not hallucinate remediation." If no_match, the agent returns likely_cause="unknown (not in runbooks)", recommended_action="runbooks do not cover this — escalate to on-call", confidence capped at 0.4. This failure mode IS the interview answer: the system knows when it doesn't know.

### Unknown incident handling: explicit "I don't know" path

**151 · Unknown incidents return structured uncertainty, not hallucinated remediation — confidence capped at 0.4**
Rejected: generating a "best guess" remediation from partial runbook matches.

When search_runbooks returns no_match, the agent produces:
- likely_cause: "unknown (runbooks do not cover X in Y)"
- recommended_action: "runbooks do not cover this incident type — escalate to on-call"
- confidence: capped at 0.4 (via harmonic mean formula with retrieval=0)
- citations: top_candidates from search_runbooks (for transparency)

This is the S7 done condition: "Test it on an incident type the runbooks DON'T cover and observe what it does — that failure mode is an interview answer." The agent explicitly says "I don't know" with evidence of what it tried to match, rather than inventing a remediation.

### Confidence threshold: explicit refusal below threshold

**152 · No automatic refusal threshold — agent returns calibrated confidence, human decides whether to act**
Rejected: adding a "confidence threshold" below which agent refuses to act.

The BUILD_PLAN asks "whether to add a 'confidence threshold' below which agent refuses to act" as a decision to make. Decision: no threshold in the agent. The agent returns a calibrated confidence (0.0-1.0) and the human (or S9 closed-loop) decides whether to act. A hard threshold would be arbitrary (why 0.7? why not 0.65?) and would hide the calibration signal. Better to say "confidence 0.42 — escalation recommended" than to silently refuse. The confidence number IS the decision support.

### Audit trail: logging every tool call

**153 · Raw tool calls recorded in AgentResult.raw_tool_calls for audit trail**
Rejected: no logging, or logging only to stdout.

Every MCP tool call (tool name, arguments, result) is appended to raw_tool_calls during investigation and returned in the final AgentResult. This lets a human (or S9 verifier) replay exactly what the agent did and why. The cost is ~2k tokens per investigation — acceptable for a batch/debugging tool. In S9 this becomes the execution log for the closed loop.