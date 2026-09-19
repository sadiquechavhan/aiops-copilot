"""
Session 7 — The Copilot Agent

Given an alert, the agent calls MCP tools in a loop and returns:
  - what happened (summary)
  - evidence (metric + trace + log excerpt)
  - likely cause
  - recommended action from the runbook
  - confidence (calibrated)

DESIGN DECISIONS (recorded in ENGINEERING_LOG.md):
1. Single-turn agent loop (not multi-turn) — S7 scope is single-turn; multi-turn
   deferred to S8/S9. The agent makes one pass: detect → correlate → search_runbooks
   → synthesize. If runbooks don't cover it, it says so explicitly.

2. System prompt with structured tool descriptions — The model receives a system
   prompt that explains each tool's purpose, when to call it, and how to interpret
   its output. Tools are called via the MCP server (stdio JSON-RPC).

3. Confidence calibration — Three components combined:
   - Detection confidence: from z-score peak_z (normalized to 0-1)
   - Correlation confidence: from correlate_incident top_confidence (already 0-1)
   - Retrieval confidence: from search_runbooks top score (0-1, but <0.3 = no_match)
   
   Formula: weighted harmonic mean to penalize any weak component.
   Weights: detection=0.3, correlation=0.4, retrieval=0.3
   
   If retrieval is no_match (<0.3), overall confidence is capped at 0.4.

4. Evidence packing — Context window management:
   - query_metrics: returns pre-aggregated percentiles + downsampled series (≤40 points)
   - query_traces: returns aggregates + compact per-trace rows (≤20 traces)
   - get_trace: returns one indented span tree
   - search_runbooks: returns ≤3 chunks with source_path + heading
   Total fits comfortably in context.

5. Runbook grounding — Every recommendation cites source_path + heading.
   If search_runbooks returns no_match, agent MUST NOT hallucinate remediation.
   Returns "runbooks do not cover this incident type" with top candidates.

6. Unknown incident handling — Explicit "I don't know" path:
   - search_runbooks score < 0.3 → no_match status
   - Agent returns: likely_cause="unknown (not in runbooks)", 
     recommended_action="runbooks do not cover this — escalate to on-call",
     confidence=capped at 0.4
   This failure mode IS the interview answer.
"""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@dataclass
class AgentResult:
    """Structured output from the copilot agent."""
    summary: str
    evidence: dict  # metrics, traces, logs
    likely_cause: str
    recommended_action: str
    confidence: float
    citations: list  # list of {"source_path": ..., "heading": ..., "score": ...}
    raw_tool_calls: list  # audit trail


class CopilotAgent:
    """
    Single-turn copilot agent that uses the MCP server to investigate alerts.
    
    Flow:
    1. Receive alert (anomaly window from detector, or manual alert)
    2. Call query_metrics to get metric evidence
    3. Call query_traces + get_trace for trace evidence
    4. Call correlate_incident for root-cause ranking
    5. Call search_runbooks for remediation guidance
    6. Synthesize grounded incident summary
    """
    
    SYSTEM_PROMPT = """You are an AIOps copilot for a 3-service system (gateway → orders → inventory → Postgres).
Your job: given an alert, investigate using tools and return a grounded incident summary.

TOOLS AVAILABLE (call via MCP server):
1. query_metrics(metric, service, percentile, window/start/end)
   - Use FIRST to understand what golden signal moved
   - metric: latency | request_rate | error_rate | error_ratio | db_pool
   - service: gateway | orders | inventory | all
   - Returns: window-wide percentiles (p50/p95/p99) + downsampled time series

2. query_traces(service, limit, min_duration_ms, errors_only, window/start/end)
   - Use AFTER metrics show WHICH service is affected
   - service: gateway | orders | inventory (entry point of trace)
   - Returns: per-service self-time aggregates + compact per-trace rows
   - self_time = time spent IN that service (excludes downstream waits)

3. get_trace(trace_id)
   - Use AFTER query_traces finds a trace_id worth examining
   - Returns: indented span tree with duration, self-time, offset

4. correlate_incident(run_id, incident_id, anomaly_window, use_live)
   - Use to rank candidate services by root-cause likelihood
   - anomaly_window: {start, end, signal, family} from detector
   - family: latency | errors | saturation | iforest
   - Returns: ranked candidates with confidence scores

5. search_runbooks(query, k)
   - Use LAST to find remediation guidance
   - Returns: up to k chunks with source_path, heading, score
   - If top score < 0.3: returns status="no_match" with top_candidates
   - YOU MUST RESPECT no_match — do not hallucinate remediation

YOUR OUTPUT FORMAT (JSON):
{
  "summary": "One-sentence incident summary",
  "evidence": {
    "metrics": {...},      // key metric readings
    "traces": {...},       // key trace findings
    "logs": []             // log excerpts if any
  },
  "likely_cause": "Service + fault type, or 'unknown (not in runbooks)'",
  "recommended_action": "Specific command from runbook, or 'runbooks do not cover this — escalate to on-call'",
  "confidence": 0.XX,      // 0.0-1.0, calibrated
  "citations": [           // from search_runbooks results
    {"source_path": "...", "heading": "...", "score": 0.XX}
  ]
}

CONFIDENCE CALIBRATION RULES:
- Detection confidence: peak_z / 10 (capped at 1.0) — higher z-score = more confident
- Correlation confidence: from correlate_incident.top_confidence (already calibrated)
- Retrieval confidence: search_runbooks top score (if no_match → 0.0)
- Combined: weighted harmonic mean (detection=0.3, correlation=0.4, retrieval=0.3)
- If retrieval is no_match: overall confidence capped at 0.4
- If any component is 0: overall is 0

CRITICAL RULES:
- NEVER invent a remediation not in runbooks
- If search_runbooks returns no_match → say "runbooks do not cover this"
- Cite every runbook claim with source_path + heading
- Distinguish "runbook says X" from "I don't know"
- If uncertain, lower confidence rather than guess
"""

    def __init__(self, mcp_server_path: str = None):
        if mcp_server_path is None:
            mcp_server_path = os.path.join(ROOT, "aiops_mcp", "server.py")
        self.mcp_server_path = mcp_server_path
        self.mcp_process = None
        self.request_id = 0
        self.raw_tool_calls = []
    
    def _start_mcp_server(self):
        """Start the MCP server as a subprocess."""
        if self.mcp_process is None or self.mcp_process.poll() is not None:
            # Use -m to run as module (like .mcp.json does) to avoid relative import issues
            self.mcp_process = subprocess.Popen(
                ["python", "-m", "aiops_mcp.server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=ROOT,
                env={**os.environ, "PYTHONIOENCODING": "utf-8",
                     "AIOPS_PROMETHEUS_URL": "http://localhost:9090",
                     "AIOPS_JAEGER_URL": "http://localhost:16686"}
            )
            # Initialize handshake
            self._mcp_request("initialize", {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "copilot-agent", "version": "1.0"}
            })
            self._mcp_request("notifications/initialized", {})
    
    def _mcp_request(self, method: str, params: dict = None) -> dict:
        """Send a JSON-RPC request to the MCP server."""
        self.request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method
        }
        if params is not None:
            request["params"] = params
        
        line = json.dumps(request) + "\n"
        self.mcp_process.stdin.write(line)
        self.mcp_process.stdin.flush()
        
        # Read response
        response_line = self.mcp_process.stdout.readline()
        if not response_line:
            stderr = self.mcp_process.stderr.read()
            raise RuntimeError(f"MCP server closed unexpectedly: {stderr}")
        
        response = json.loads(response_line.strip())
        
        if "error" in response:
            raise RuntimeError(f"MCP error: {response['error']}")
        
        return response.get("result", {})
    
    def _call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Call an MCP tool and return the result."""
        self.raw_tool_calls.append({"tool": tool_name, "arguments": arguments})
        result = self._mcp_request("tools/call", {
            "name": tool_name,
            "arguments": arguments
        })
        self.raw_tool_calls[-1]["result"] = result
        return result
    
    def _close(self):
        """Clean up the MCP server process."""
        if self.mcp_process:
            self.mcp_process.terminate()
            self.mcp_process.wait(timeout=5)
            self.mcp_process = None
    
    def _calculate_confidence(self, detection_peak_z: float, 
                              correlation_confidence: float, 
                              retrieval_score: float,
                              retrieval_status: str) -> float:
        """
        Calculate calibrated confidence using weighted harmonic mean.
        
        Weights: detection=0.3, correlation=0.4, retrieval=0.3
        If retrieval is no_match (<0.3), cap overall at 0.4.
        """
        # Normalize detection confidence: peak_z / 10, capped at 1.0
        det_conf = min(detection_peak_z / 10.0, 1.0) if detection_peak_z > 0 else 0.0
        
        # Correlation confidence is already 0-1
        corr_conf = correlation_confidence
        
        # Retrieval confidence
        if retrieval_status == "no_match":
            retr_conf = 0.0
        else:
            retr_conf = retrieval_score
        
        # If any component is 0, overall is 0
        if det_conf == 0 or corr_conf == 0 or retr_conf == 0:
            return 0.0
        
        # Weighted harmonic mean
        weights = [0.3, 0.4, 0.3]
        values = [det_conf, corr_conf, retr_conf]
        
        # Harmonic mean: n / sum(w_i / v_i)
        weighted_sum = sum(w / v for w, v in zip(weights, values))
        combined = sum(weights) / weighted_sum
        
        # Cap if retrieval was no_match
        if retrieval_status == "no_match":
            combined = min(combined, 0.4)
        
        return round(combined, 2)
    
    def investigate(self, alert: dict) -> AgentResult:
        """
        Investigate an alert and return a grounded incident summary.
        
        alert format:
        {
            "run_id": "run-20260831-174136",
            "incident_id": "run-20260831-174136/i1",
            "anomaly_window": {
                "start": 1725123456.0,
                "end": 1725123696.0,
                "signal": "latency:inventory",
                "family": "latency"
            },
            "detection_peak_z": 15.2  // from detector output
        }
        """
        self._start_mcp_server()
        self.raw_tool_calls = []
        
        try:
            run_id = alert.get("run_id")
            incident_id = alert.get("incident_id")
            anomaly_window = alert.get("anomaly_window")
            detection_peak_z = alert.get("detection_peak_z", 0.0)
            
            # Extract window for queries
            window_start = anomaly_window["start"]
            window_end = anomaly_window["end"]
            window_str = f"{(window_end - window_start) / 60:.0f}m"
            
            signal = anomaly_window.get("signal", "unknown")
            family = anomaly_window.get("family", "unknown")
            
            # Parse signal to get service and metric
            # signal format: "latency:inventory" or "errors:orders" or "pool:used"
            signal_parts = signal.split(":")
            metric_name = signal_parts[0] if signal_parts else "unknown"
            service_name = signal_parts[1] if len(signal_parts) > 1 else "all"
            
            # Map signal to query_metrics metric enum
            metric_map = {
                "latency": "latency",
                "errors": "error_rate",
                "error_rate": "error_rate",
                "pool": "db_pool",
                "pool:used": "db_pool",
                "pool:idle": "db_pool"
            }
            query_metric = metric_map.get(metric_name, "latency")
            
            # ============================================================
            # STEP 1: Get metric evidence
            # ============================================================
            metrics_result = self._call_tool("query_metrics", {
                "metric": query_metric,
                "service": service_name if service_name != "all" else "all",
                "window": window_str
            })
            
            # ============================================================
            # STEP 2: Get trace evidence
            # ============================================================
            traces_result = self._call_tool("query_traces", {
                "service": service_name if service_name != "all" else "gateway",
                "limit": 10,
                "min_duration_ms": 100,
                "window": window_str
            })
            
            # Get a detailed trace if available
            trace_detail = None
            if traces_result.get("traces") and len(traces_result["traces"]) > 0:
                trace_id = traces_result["traces"][0].get("trace_id")
                if trace_id:
                    trace_detail = self._call_tool("get_trace", {"trace_id": trace_id})
            
            # ============================================================
            # STEP 3: Correlate to find root cause
            # ============================================================
            correlation_result = self._call_tool("correlate_incident", {
                "run_id": run_id,
                "incident_id": incident_id,
                "anomaly_window": anomaly_window,
                "use_live": False  # Use committed exports for consistency
            })
            
            top_candidate = correlation_result.get("top_candidate", "unknown")
            top_confidence = correlation_result.get("top_confidence", 0.0)
            candidates = correlation_result.get("candidates", [])
            
            # ============================================================
            # STEP 4: Search runbooks for remediation
            # ============================================================
            # Build a query from the signal and top candidate
            runbook_query = f"How to diagnose and remediate {signal} in {top_candidate}"
            runbook_result = self._call_tool("search_runbooks", {
                "query": runbook_query,
                "k": 3
            })
            
            retrieval_status = runbook_result.get("status", "unknown")
            runbook_chunks = runbook_result.get("results", [])
            top_candidates = runbook_result.get("top_candidates", [])
            
            retrieval_score = runbook_chunks[0]["score"] if runbook_chunks else 0.0
            
            # ============================================================
            # STEP 5: Synthesize the incident summary
            # ============================================================
            
            # Build evidence summary
            evidence = {
                "metrics": self._summarize_metrics(metrics_result, signal),
                "traces": self._summarize_traces(traces_result, trace_detail, candidates),
                "logs": []  # Logs not implemented in S7
            }
            
            # Determine likely cause and recommended action
            if retrieval_status == "no_match":
                likely_cause = f"unknown (runbooks do not cover {signal} in {top_candidate})"
                recommended_action = "runbooks do not cover this incident type — escalate to on-call"
                citations = [{"source_path": c.get("source_path", ""), 
                             "heading": c.get("heading", ""), 
                             "score": c.get("score", 0.0)} 
                            for c in top_candidates[:3]]
            else:
                likely_cause = f"{top_candidate} service — {signal} fault"
                # Extract remediation from runbook chunks
                recommended_action = self._extract_remediation(runbook_chunks, signal)
                citations = [{"source_path": c["source_path"], 
                             "heading": c["heading"], 
                             "score": c["score"]} 
                            for c in runbook_chunks]
            
            # Calculate calibrated confidence
            confidence = self._calculate_confidence(
                detection_peak_z, top_confidence, retrieval_score, retrieval_status
            )
            
            # Build one-sentence summary
            summary = self._build_summary(signal, top_candidate, family, 
                                        retrieval_status, confidence)
            
            return AgentResult(
                summary=summary,
                evidence=evidence,
                likely_cause=likely_cause,
                recommended_action=recommended_action,
                confidence=confidence,
                citations=citations,
                raw_tool_calls=self.raw_tool_calls
            )
            
        finally:
            self._close()
    
    def _summarize_metrics(self, metrics_result: dict, signal: str) -> dict:
        """Extract key metric readings for evidence."""
        summary = {"signal_queried": signal}
        
        if "result" in metrics_result and metrics_result["result"]:
            data = metrics_result["result"]
            if isinstance(data, list) and len(data) > 0:
                # Take the first service's data
                svc_data = data[0]
                summary["percentiles"] = svc_data.get("percentiles", {})
                summary["series_preview"] = svc_data.get("series", [])[:5]
                summary["promql"] = metrics_result.get("promql", "")
        
        return summary
    
    def _summarize_traces(self, traces_result: dict, trace_detail: dict, 
                          candidates: list) -> dict:
        """Extract key trace findings for evidence."""
        summary = {"top_candidates": candidates[:3]}
        
        if "result" in traces_result and traces_result["result"]:
            data = traces_result["result"]
            if "aggregates" in data:
                summary["aggregates"] = data["aggregates"]
            if "traces" in data:
                summary["trace_count"] = len(data["traces"])
                summary["sample_traces"] = data["traces"][:3]
        
        if trace_detail and "result" in trace_detail:
            summary["detailed_trace"] = trace_detail["result"]
        
        return summary
    
    def _extract_remediation(self, runbook_chunks: list, signal: str) -> str:
        """Extract specific remediation command from runbook chunks."""
        # Look for code blocks with commands
        for chunk in runbook_chunks:
            text = chunk.get("text", "")
            # Find bash commands in code blocks
            import re
            code_blocks = re.findall(r'```(?:bash|sh)?\n(.*?)\n```', text, re.DOTALL)
            for block in code_blocks:
                block = block.strip()
                if block and ("curl" in block or "DELETE" in block or "restart" in block):
                    return block
        
        # Fallback: return first actionable sentence
        for chunk in runbook_chunks:
            text = chunk.get("text", "")
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("-") or line.startswith("•") or "run" in line.lower():
                    if "curl" in line or "DELETE" in line or "disable" in line.lower():
                        return line.lstrip("-• ").strip()
        
        return "See runbook for remediation steps"
    
    def _build_summary(self, signal: str, top_candidate: str, family: str,
                       retrieval_status: str, confidence: float) -> str:
        """Build a one-sentence incident summary."""
        if retrieval_status == "no_match":
            return (f"Alert: {signal} anomaly detected; correlation points to "
                   f"{top_candidate} ({family} family); "
                   f"runbooks do not cover this incident type; confidence {confidence:.0%}")
        else:
            return (f"Alert: {signal} anomaly detected; correlation identifies "
                   f"{top_candidate} as likely cause ({family} family); "
                   f"runbook remediation available; confidence {confidence:.0%}")


def run_agent_from_detection(run_id: str, detection_output: dict, incident_idx: int = 0) -> AgentResult:
    """
    Convenience function to run the agent on a detector output.
    
    detection_output is the full output from detect_run or detect_iforest.
    incident_idx selects which detection to investigate.
    """
    detections = detection_output.get("detections", [])
    if not detections or incident_idx >= len(detections):
        raise ValueError(f"No detection at index {incident_idx}")
    
    det = detections[incident_idx]
    
    # Extract peak_z from detection
    peak_z = det.get("peak_z", 0.0)
    
    alert = {
        "run_id": run_id,
        "incident_id": f"{run_id}/{det.get('signal', 'unknown').replace(':', '_')}",
        "anomaly_window": {
            "start": det["start"],
            "end": det["end"],
            "signal": det.get("signal", "unknown"),
            "family": det.get("family", "unknown")
        },
        "detection_peak_z": peak_z
    }
    
    agent = CopilotAgent()
    return agent.investigate(alert)


def run_agent_from_ground_truth(run_id: str, incident_id: str) -> AgentResult:
    """
    Run the agent on a ground truth incident.
    
    Loads the anomaly window from ground_truth.jsonl.
    """
    import json
    from aiops_mcp.window import parse_absolute
    
    gt_path = os.path.join(ROOT, "ground_truth.jsonl")
    with open(gt_path, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row["incident_id"] == incident_id:
                anomaly_window = {
                    "start": parse_absolute(row["confirmed_start"] or row["start"], "start"),
                    "end": parse_absolute(row["confirmed_end"] or row["end"], "end"),
                    "signal": f"{row['fault_type']}:{row['service']}",
                    "family": row["fault_type"] if row["fault_type"] != "pool_exhaust" else "saturation"
                }
                break
        else:
            raise ValueError(f"Incident {incident_id} not found in ground_truth.jsonl")
    
    # Estimate detection peak_z from ground truth (not perfect, but usable)
    # In practice, detector would provide this
    peak_z_map = {
        "latency": 15.0,
        "error_rate": 20.0,
        "pool_exhaust": 8.0,
        "saturation": 8.0
    }
    peak_z = peak_z_map.get(row["fault_type"], 10.0)
    
    alert = {
        "run_id": run_id,
        "incident_id": incident_id,
        "anomaly_window": anomaly_window,
        "detection_peak_z": peak_z
    }
    
    agent = CopilotAgent()
    return agent.investigate(alert)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Run the copilot agent on an incident")
    parser.add_argument("--run-id", required=True, help="Run ID (e.g., run-20260831-174136)")
    parser.add_argument("--incident", default="i1", help="Incident ID (i1, i2, i3, i4)")
    parser.add_argument("--detection-file", help="Path to detection JSON output file")
    parser.add_argument("--detection-idx", type=int, default=0, help="Detection index to investigate")
    
    args = parser.parse_args()
    
    incident_id = f"{args.run_id}/{args.incident}"
    
    if args.detection_file:
        with open(args.detection_file, encoding="utf-8") as f:
            detection_output = json.load(f)
        result = run_agent_from_detection(args.run_id, detection_output, args.detection_idx)
    else:
        result = run_agent_from_ground_truth(args.run_id, incident_id)
    
    print(json.dumps(asdict(result), indent=2))