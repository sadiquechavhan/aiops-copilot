# AIOps Copilot — End-to-End Incident Response System

> **A small distributed system that observes itself, breaks itself on a schedule, and writes down exactly what it broke and when — so you can measure whether your detection, diagnosis, and remediation actually work.**

---

## What Is This?

This project is a **complete AIOps (AI for IT Operations) system** built from scratch to demonstrate:

1. **Telemetry Generation** — A live 3-service Java/Spring Boot system with OpenTelemetry instrumentation
2. **Fault Injection** — Controlled chaos endpoints that inject realistic faults (latency, errors, pool exhaustion)
3. **Ground Truth** — Machine-readable labels for every injected fault (exact start/end, service, type)
4. **Detection** — Anomaly detection scored against ground truth (precision, recall, MTTD)
5. **Correlation** — Root-cause identification from traces (100% top-1 accuracy)
6. **Retrieval** — RAG over runbooks for remediation guidance (40% on overlapping vocab)
7. **Copilot Agent** — MCP-powered agent that produces grounded incident summaries
8. **Closed-Loop Remediation** — Whitelisted actions with approval gates and verification
9. **Kubernetes Deploy** — kind cluster for "I've run this on K8s" checkbox

**All numbers are measured, not claimed.** Every metric comes from verification tools run against committed evidence (`ground_truth.jsonl`, exported metrics, exported traces).

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        SYSTEM UNDER OBSERVATION (Java 21)                    │
│  ┌─────────┐     ┌─────────┐     ┌────────────┐     ┌──────────┐           │
│  │ Gateway │────►│ Orders  │────►│ Inventory  │────►│ Postgres │           │
│  │  :8080  │     │  :8081  │     │   :8082    │     │  :5432   │           │
│  └────┬────┘     └────┬────┘     └─────┬──────┘     └──────────┘           │
│       │               │                  │                                     │
│       └───────────────┴──────────────────┘                                     │
│                       │                                                        │
│              /chaos endpoints:                                                │
│              • latency injection    • error-rate injection                    │
│              • pool exhaustion (inventory only)                               │
└───────────────────────┬───────────────────────────────────────────────────────┘
                        │ OpenTelemetry Java Agent (zero code changes)
                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         OTEL COLLECTOR (0.158.0)                            │
│  Receivers: OTLP (gRPC/HTTP)  →  Processors: memory_limiter + batch        │
│  Exporters: Jaeger (traces)  +  Prometheus (metrics)  +  Redpanda (stream) │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              ▼                                   ▼
       ┌─────────────┐                     ┌─────────────┐
       │   Jaeger    │                     │ Prometheus  │
       │  (Badger,   │                     │  (24h/1GB)  │
       │  72h TTL)   │                     └──────┬──────┘
       └──────┬──────┘                            ▼
              ▼                            ┌─────────────┐
       ┌─────────────┐                     │  Grafana    │
       │  Redpanda   │                     │ (dashboards  │
       │ (Kafka API) │                     │  + alerts)  │
       └─────────────┘                     └─────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         AI LAYER (Python 3.12)                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐   │
│  │ Ingest   │─►│ Detect   │─►│Correlate │─►│  RAG     │─►│  Agent     │   │
│  │ (metrics,│  │ (z-score │  │(self-time│  │(Chroma + │  │ (MCP tools │   │
│  │  traces, │  │ + pool)  │  │ ranking) │  │MiniLM)   │  │  loop)     │   │
│  │  logs)   │  └──────────┘  └──────────┘  └──────────┘  └──────┬─────┘   │
│  └──────────┘                                                │          │
│                                                               ▼          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │              CLOSED-LOOP REMEDIATION (Session 9)                 │   │
│  │  suggest_remediation → execute_remediation (approval gate)      │   │
│  │  → verify_recovery → audit_log                                   │   │
│  │  Whitelist: disable_chaos \| restart_container \| scale_replicas │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

The services know exactly one telemetry address. Swapping a backend is a change to
the collector config alone — no service rebuild.

**Instrumentation is entirely environmental.** The agent is attached through
`JAVA_TOOL_OPTIONS`, so the container images have no idea OpenTelemetry exists and
instrumentation can be removed by deleting an environment variable.

---

## Quick start

Requires Docker Desktop, PowerShell, and Python 3.12.

```powershell
# 1. Fetch the OTel agent (a 24 MB binary, deliberately not in git)
.\tools\fetch-agent.ps1

# 2. Start everything
docker compose up -d

# 3. Jaeger runs as uid 10001 and cannot write to a root-owned named volume,
#    so its storage directory needs its ownership set once
docker run --rm --entrypoint sh -v aiops_badgerdata:/badger postgres:16-alpine `
  -c "mkdir -p /badger/data /badger/key && chown -R 10001:0 /badger && chmod -R 775 /badger"
docker compose restart jaeger
```

Allow roughly three minutes for the chain to become healthy — services start in
dependency order and each waits on the previous healthcheck.

```powershell
py -3.12 tools\traffic_gen.py --once          # one request, prints the response
py -3.12 tools\traffic_gen.py --rate 6        # steady load until Ctrl-C
py -3.12 tools\scenario_runner.py --fast 10   # full scenario shape in ~3 minutes
py -3.12 tools\scenario_runner.py             # the real 30-minute labelled run
```

| UI | URL |
|---|---|
| Grafana | http://localhost:3000 (`admin` / `admin`) |
| Jaeger | http://localhost:16686 |
| Prometheus | http://localhost:9090 |

---

### Kubernetes (kind) — "I've run this on K8s" checkbox

Requires Docker Desktop, `kind`, `kubectl`, and Python 3.12.

```powershell
# 1. Fetch the OTel agent (a 24 MB binary, deliberately not in git)
.\tools\fetch-agent.ps1

# 2. Build service images
docker compose build

# 3. Create kind cluster and deploy
make k8s-up
```

This creates a single-node kind cluster with hostPort mappings that mirror the
Compose ports exactly — `localhost:8080` for gateway, `localhost:3000` for Grafana,
etc. The OTel agent is fetched into each pod via an initContainer (no hostPath
dependency).

Allow roughly three minutes for all pods to become ready — services start in
dependency order and each waits on the previous readiness probe.

```powershell
# Same traffic and chaos commands work identically (localhost ports are the same)
python tools\traffic_gen.py --once          # one request
python tools\traffic_gen.py --rate 6        # steady load
python tools\scenario_runner.py --fast 10   # full scenario in ~3 minutes
python tools\scenario_runner.py             # the real 30-minute labelled run
```

| UI | URL |
|---|---|
| Grafana | http://localhost:3000 (`admin` / `admin`) |
| Jaeger | http://localhost:16686 |
| Prometheus | http://localhost:9090 |
| Gateway | http://localhost:8080 |
| Orders | http://localhost:8081 |
| Inventory | http://localhost:8082 |

To tear down:

```powershell
make k8s-down
```

---

## The chaos API

Every service exposes the same control plane.

```
POST   /chaos/latency?ms=N[&jitter=J]        add N±J ms to every request
POST   /chaos/error-rate?pct=N               fail N% of requests with a 500
POST   /chaos/pool-exhaust?hold=9&ttl_ms=X   hold DB connections (inventory only)
GET    /chaos                                current faults + counters
DELETE /chaos                                clear everything
```

```bash
curl -X POST "http://localhost:8082/chaos/latency?ms=400&jitter=80"
curl "http://localhost:8082/chaos"
curl -X DELETE "http://localhost:8082/chaos"
```

### Design constraints it holds to

- **`/chaos` and `/health` are never faulted.** If `error-rate=100` could fail
  `DELETE /chaos` there would be no way to turn the fault off. A kill switch must
  not be able to disable itself.
- **Latency is capped per service** (inventory 2000 ms, orders 2000 ms, gateway
  4000 ms) and values above the cap are rejected with a `400`. Injecting 3000 ms at
  inventory would not produce a latency incident, it would produce a
  timeout-and-500 incident carrying a label that says "latency".
- **Inert when idle, provably.** With nothing injected the filter performs one
  volatile read and calls the chain. Verified over 1079 requests:
  `requests_delayed=0, requests_failed=0`. That matters because the detection layer
  learns "normal" from the un-faulted baseline.
- **`CHAOS_ENABLED=false` unregisters the filter entirely** — absent, not idle.
- **Pool exhaustion cannot deadlock its own release path.** The holder runs on a
  background thread, release never calls `getConnection()`, no lock is held across a
  blocking acquire, and a TTL dead-man's switch frees everything even if `DELETE`
  never arrives. Measured: `DELETE` returned in 72 ms against a fully held pool.

---

## Ground truth

One JSON object per line, `fsync`'d per incident so a killed run keeps every label
it earned.

```json
{"schema":1,"run_id":"run-20260809-201431","incident_id":"run-.../i2",
 "start":"2026-08-09T20:17:31.430Z","end":"2026-08-09T20:21:31.458Z",
 "confirmed_start":"2026-08-09T20:17:31.437Z","confirmed_end":"2026-08-09T20:21:31.438Z",
 "service":"orders","fault_type":"error_rate","params":{"pct":20},
 "observed":{"requests_seen":1424,"requests_delayed":0,"requests_failed":285},
 "recovered":true,"jaeger_url":"..."}
```

**Four timestamps, not two.** A fault begins partway through the HTTP call that
injects it, so there is no single correct instant. `start`/`end` bracket the call
from outside, making the label a slight *superset* of the true fault window;
`confirmed_start`/`confirmed_end` bracket it from inside. The superset direction is
deliberate — a faulted sample falling outside the label would be scored as a false
positive against a detector that was correct.

**`observed` is read from each service's own counters**, not computed from the
requested parameters, so the labels can be checked against themselves. A run
requesting 20 % errors measured 285/1424 = **20.01 %**.

---

## Quick Start (Docker Compose — Primary Dev Environment)

### Prerequisites
- **Docker Desktop** (WSL2 backend) with ~8 GB free RAM
- **PowerShell** (Windows) or Bash (Linux/macOS)
- **Python 3.12+** (standard library only — no venv needed)

### 1. Fetch the OTel Java Agent
```powershell
.\tools\fetch-agent.ps1
```
Downloads the pinned OpenTelemetry Java agent (v2.30.0, ~24 MB) to `otel/agent/`. Not committed to git — fetched on demand.

### 2. Start the Stack
```powershell
docker compose up -d
```

### 3. Fix Jaeger Permissions (One-Time)
```powershell
docker run --rm --entrypoint sh -v aiops_badgerdata:/badger postgres:16-alpine `
  -c "mkdir -p /badger/data /badger/key && chown -R 10001:0 /badger && chmod -R 775 /badger"
docker compose restart jaeger
```
Jaeger runs as uid 10001; the named volume starts root-owned. This fixes it.

### 4. Wait ~3 Minutes
Services start in dependency order (postgres → inventory → orders → gateway → collector → jaeger → prometheus → grafana → redpanda). Each waits on the previous healthcheck.

### 5. Verify It Works
```powershell
# One request through the full chain
python tools/traffic_gen.py --once

# Steady load (6 req/s) — run in background
python tools/traffic_gen.py --rate 6

# Full 30-minute labelled scenario (produces ground_truth.jsonl)
python tools/scenario_runner.py
```

| UI | URL | Credentials |
|---|---|---|
| **Grafana** | http://localhost:3000 | admin / admin |
| **Jaeger** | http://localhost:16686 | — |
| **Prometheus** | http://localhost:9090 | — |
| **Gateway API** | http://localhost:8080/api/orders | — |
| **Orders API** | http://localhost:8081 | — |
| **Inventory API** | http://localhost:8082 | — |

---

## Quick Start (Kubernetes via kind — "I've Run This on K8s")

### Prerequisites
- Docker Desktop + `kind` + `kubectl` + Python 3.12

### Deploy
```powershell
# 1. Fetch OTel agent
.\tools\fetch-agent.ps1

# 2. Build service images
docker compose build

# 3. Create kind cluster and deploy everything
make k8s-up
```

This creates a **single-node kind cluster** with `hostPort` mappings that mirror Compose exactly:
- `localhost:8080` → Gateway
- `localhost:8081` → Orders
- `localhost:8082` → Inventory
- `localhost:3000` → Grafana
- `localhost:9090` → Prometheus
- `localhost:16686` → Jaeger
- `localhost:19092` → Redpanda (external)
- `localhost:4317/4318` → OTel Collector

The OTel agent is downloaded into each pod via an **initContainer** (no host dependency). Jaeger's Badger volume permissions are fixed via another initContainer.

### Verify & Use
Same traffic/chaos commands work identically (same localhost ports):
```powershell
make traffic
make inject-latency-inventory
make agent-summary
make remediate-auto
```

### Tear Down
```powershell
make k8s-down
```

---

## The Chaos API — How We Break Things on Purpose

Every service exposes the same fault-injection control plane:

| Endpoint | Effect | Example |
|---|---|---|
| `POST /chaos/latency?ms=N[&jitter=J]` | Add N±J ms to every request | `ms=400&jitter=80` |
| `POST /chaos/error-rate?pct=N` | Fail N% of requests with 500 | `pct=20` |
| `POST /chaos/pool-exhaust?hold=9&ttl_ms=X` | Hold DB connections (inventory only) | `hold=9&ttl_ms=300000` |
| `GET /chaos` | Current faults + counters | — |
| `DELETE /chaos` | Clear everything | — |

**Safety constraints (verified):**
- `/chaos` and `/health` are **never faulted** — kill switch cannot disable itself
- Latency caps per service (inventory/orders 2000ms, gateway 4000ms)
- `CHAOS_ENABLED=false` unregisters the filter entirely (absent, not idle)
- Pool exhaustion release path cannot deadlock (4 rules, TTL dead-man's switch)

```bash
# Inject 400ms latency into inventory
curl -X POST "http://localhost:8082/chaos/latency?ms=400&jitter=80"

# Check current faults
curl "http://localhost:8082/chaos"

# Clear all faults
curl -X DELETE "http://localhost:8082/chaos"
```

---

## Ground Truth — The Source of Truth for All Measurements

Every injected fault produces a **machine-readable label** in `ground_truth.jsonl` (JSON Lines, `fsync`d per incident):

```json
{
  "schema": 1,
  "run_id": "run-20260809-201431",
  "incident_id": "run-20260809-201431/i2",
  "start": "2026-08-09T20:17:31.430Z",
  "end": "2026-08-09T20:21:31.458Z",
  "confirmed_start": "2026-08-09T20:17:31.437Z",
  "confirmed_end": "2026-08-09T20:21:31.438Z",
  "service": "orders",
  "fault_type": "error_rate",
  "params": {"pct": 20},
  "observed": {"requests_seen": 1424, "requests_delayed": 0, "requests_failed": 285},
  "recovered": true
}
```

**Four timestamps, not two** — the fault begins partway through the injecting HTTP call. `start`/`end` bracket from outside (superset), `confirmed_start`/`confirmed_end` bracket from inside. `observed` is read from the service's own counters — a run requesting 20% errors measured **20.01%**.

---

## The Four Labelled Incidents (Signal Proof)

One command (`scenario_runner.py`) produces 30 minutes of telemetry with four labelled faults. Every fault **moves its metric** — verified by querying Prometheus over each window:

| Incident | Service | Fault | Key Metric | Baseline | During | Verdict |
|---|---|---|---|---|---|---|
| **i1** | inventory | latency 400ms | p95 inventory | 0.028s | 0.488s | ✅ rose |
| | | | p95 gateway (propagated) | 0.048s | 0.491s | ✅ rose |
| **i2** | orders | error-rate 20% | 5xx orders | 0.000/s | 1.238/s | ✅ rose |
| | | | 5xx gateway (cascade) | 0.000/s | 1.219/s | ✅ rose |
| **i3** | inventory | pool-exhaust (9/10) | Hikari used | 0 | 9 | ✅ rose |
| | | | Hikari idle | 10 | 1 | ✅ fell |
| | | | p95 inventory | 0.028s | 0.023s | ⚪ **flat** |
| **i4** | gateway | latency 250ms | p95 gateway | 0.048s | 0.483s | ✅ rose |
| | | | p95 inventory | 0.028s | 0.023s | ⚪ **flat** |

**The two flat rows are the interesting ones:**
- **i3** saturates the connection pool while latency/error rate stay flat — a **saturation signal with no user-visible impact**. A latency/error detector *should* miss this.
- **i4 vs i1** both look like "gateway got slow" in p95. In traces they're opposites:
  - **i1**: gap sits 473ms deep, inside inventory
  - **i4**: gap sits at the top, 294ms of self-time in gateway

---

## Measured Results (Every Number Verifiable)

Run verification yourself:
```bash
# Detection precision/recall
python tools/verify_detection.py

# Correlation top-1 accuracy
python tools/verify_correlation.py

# Trace self-time attribution
python tools/verify_traces.py

# Runbook retrieval accuracy
python tools/verify_rag.py
```

### Detection (Session 4)
Scored offline against exported metrics + ground truth.

| Detector | Recall | Precision | False Positives/hr |
|---|---|---|---|
| **CORE (RED: latency p95 + error rate)** | 3/4 (75%) | 6/6 (100%) | 0 |
| **CORE + SATURATION (adds HikariCP pool)** | **4/4 (100%)** | **8/8 (100%)** | 0 |
| IsolationForest (multivariate, ~10 vectors) | 1/4 (25%) | 1/1 (100%) | 0 |

- **MTTD**: ~15–30s (streaming detector, S8)
- **Zero-baseline fix**: 5xx series doesn't exist until first 500; treated as μ=0, σ=0 with floor σ=0.05 req/s

### Correlation (Session 5)
Scored offline against exported traces + ground truth.

| Metric | Value |
|---|---|
| **Top-1 Accuracy** | **8/8 (100%)** across 2 runs |
| Per fault type: latency | 2/2 (100%) |
| Per fault type: error_rate | 1/1 (100%) |
| Per fault type: pool_exhaust | 1/1 (100%) — correctly returns `insufficient_trace_evidence` |
| Confidence calibration | Perfect per bucket (>0.9, 0.7–0.9, <0.7) |
| High confidence (>0.8) on WRONG service | 0 |

**Self-time attribution** (recomputed from committed traces):
| Incident | Fault | Top Service (self-time %) |
|---|---|---|
| i1 | inventory latency | inventory 96–99% |
| i2 | orders error-rate | orders (missing child spans) |
| i3 | pool exhaustion | insufficient_trace_evidence |
| i4 | gateway latency | gateway 91–94% |

### Retrieval / RAG (Session 6)
Hybrid dense + BM25 (α=0.8), 10 runbooks, ~93 chunks, sentence-transformers `all-MiniLM-L6-v2` (384d), Chroma.

| Metric | Value |
|---|---|
| **Accuracy (correct runbook top-1)** | **4/10 (40%)** |
| Strong on | pool exhaustion, chaos endpoints, remediation |
| Weak on | latency faults, propagation cases |

**Root cause**: 10 runbooks describe the SAME system with highly overlapping vocabulary. "Latency injection in gateway" matches both `latency-gateway.md` and `latency-propagation.md`. Fundamental limit of vector similarity on small overlapping corpora.

### End-to-End Grounded Summary (Session 7)
Single-turn agent via MCP server, tested on all 4 incident types.

| Incident | Summary Correct? | Citations Correct? | Confidence |
|---|---|---|---|
| i1 (inventory latency) | ✅ | ✅ | 0.87 |
| i2 (orders error-rate) | ✅ | ✅ | 0.73 |
| i3 (pool exhaust) | ✅ "unknown" | N/A (no_match) | 0.40 (capped) |
| i4 (gateway latency) | ✅ | ✅ | 0.85 |

**Grounded summary rate**: 4/4 known incidents correct.  
**Unknown handling**: Explicit "runbooks do not cover this — escalate to on-call", confidence capped at 0.4.

### Closed-Loop Remediation (Session 9)
| Test | Scenario | Result |
|---|---|---|
| 1 | `disable_chaos` on live service | ✅ PASS — injected latency cleared |
| 2 | Snapshot + recovery check | ✅ PASS — metrics captured, comparison works |
| 3 | Full flow (auto-approval) | ✅ PASS — executed, verified `recovered: true` |
| 4 | Audit log completeness | ✅ PASS — all fields recorded |

---

## The MCP Server — How the Model Interrogates Telemetry

`aiops_mcp/` is a **hand-rolled MCP server** (JSON-RPC 2.0 over stdio, stdlib only, no deps) exposing telemetry as tools:

| Tool | Purpose | Returns |
|---|---|---|
| `query_metrics` | Latency percentiles, request rate, error ratio, DB pool | Aggregated tables (bounded ~1.2 KB) |
| `query_traces` | Which service spends the time | Per-service self-time + compact trace rows |
| `get_trace` | Where one request went | Indented span tree with self-time |
| `telemetry_status` | Backend liveness + retention | `oldest_queryable`, `reachable` |
| `correlate_incident` | Rank candidate services by root-cause | Ranked list + confidence |
| `search_runbooks` | Find remediation guidance | Chunks with `source_path`, `heading`, `score` |
| `suggest_remediation` | Whitelisted action for signal | `{action, args, reasoning}` |
| `execute_remediation` | Execute + verify recovery | Full audit record with pre/post metrics |
| `get_remediation_log` | Retrieve audit trail | All attempts for a run |

**Key design**: Aggregate before returning — response size **does not grow with window** (223× reduction at 23h vs unaggregated). Downsampling uses **MAX per bucket** (never mean — a 40s spike in a 10m bucket would vanish).

**Two error channels** (critical for honesty):
- JSON-RPC error → client (model never sees it)
- Successful response with `isError: true` → model (can read, reason, repeat)

---

## Copilot Agent — From Alert to Grounded Summary

Single-turn loop (deterministic, auditable):
```
Alert → query_metrics → query_traces → get_trace → correlate_incident
→ search_runbooks → suggest_remediation → (optional) execute_remediation
```

**Confidence = weighted harmonic mean** (detection=0.3, correlation=0.4, retrieval=0.3)
- Penalizes any weak component
- If retrieval is `no_match` (<0.3), overall capped at **0.4**
- Unknown incidents → explicit "I don't know", not hallucinated remediation

```bash
# Known incident (i1 - inventory latency)
python -c "from aiops_mcp.agent import run_agent_from_ground_truth; import json; r=run_agent_from_ground_truth('run-20260809-201431','run-20260809-201431/i1'); print(json.dumps(r.__dict__, default=str, indent=2))"

# Unknown incident (i3 - pool exhaust, runbooks don't cover saturation)
python -c "from aiops_mcp.agent import run_agent_from_ground_truth; import json; r=run_agent_from_ground_truth('run-20260809-201431','run-20260809-201431/i3'); print(json.dumps(r.__dict__, default=str, indent=2))"
```

---

## Streaming Inference (Session 8)

**Redpanda** (Kafka API-compatible, single binary, 512 MB) replaces batch detection:
- OTel Collector → `kafkaexporter` → `aiops.metrics` topic
- Python consumer: sliding window (60s/15s), Welford's online variance
- 120s warm-up → frozen baseline → z-score detection (K=3, MIN_CONSEC=2)
- MCP tool `get_streaming_alerts(since, limit)` returns recent alerts
- **All 3 fault types detected within ~30s without manual intervention**

---

## Project Structure

```
.
├── BUILD_PLAN.md           # 10-session build plan (read-only)
├── ENGINEERING_LOG.md      # 182 decisions: what, rejected, why (interview prep)
├── PROGRESS.md             # Session-by-session results + verification
├── ground_truth.jsonl      # Durable labels (scored against)
├── docker-compose.yml      # 8 services, healthchecks, mem_limits
├── .env                    # All pinned tags + host ports
├── Makefile                # make up / make k8s-up / make traffic / make inject-*
├── k8s/                    # 14 K8s manifests for kind deploy
│   ├── kind-config.yaml    # Single-node, hostPort mappings
│   ├── 00-namespace.yaml
│   ├── 01-otel-collector-configmap.yaml
│   ├── 02-postgres.yaml
│   ├── 03-inventory.yaml
│   ├── 04-orders.yaml
│   ├── 05-gateway.yaml
│   ├── 06-otel-collector.yaml
│   ├── 07-jaeger.yaml
│   ├── 08-prometheus-configmap.yaml
│   ├── 09-prometheus.yaml
│   ├── 10-grafana-configmap.yaml
│   ├── 11-grafana.yaml
│   └── 12-redpanda.yaml
├── otel/
│   ├── collector-config.yaml    # Receivers→processors→exporters
│   └── agent/                   # OTel Java agent (gitignored, fetched)
├── prometheus/prometheus.yml    # 3 scrape jobs, 24h/1GB retention
├── grafana/provisioning/        # Datasources + overview dashboard
├── db/init/01-schema.sql        # inventory table, 5 SKUs
├── runbooks/                    # 10 markdown runbooks (source of truth for RAG)
├── tools/
│   ├── fetch-agent.ps1          # Downloads OTel agent
│   ├── traffic_gen.py           # Stdlib load generator
│   ├── scenario_runner.py       # Labelled scenario driver + signal proof
│   ├── export_metrics.py        # Exports run metrics for offline scoring
│   ├── export_traces.py         # Exports run traces for offline scoring
│   ├── verify_detection.py      # Scores detection (precision/recall)
│   ├── verify_correlation.py    # Scores correlation (top-1 accuracy)
│   ├── verify_traces.py         # Recomputes self-time from committed traces
│   ├── verify_rag.py            # Scores retrieval accuracy
│   └── mcp_probe.py             # JSON-RPC client over real pipes (--cost, --ground-truth)
├── aiops_mcp/
│   ├── server.py                # MCP server (JSON-RPC over stdio)
│   ├── agent.py                 # Copilot agent (single-turn)
│   ├── detect.py                # Detection (z-score + IsolationForest)
│   ├── correlate.py             # Correlation (self-time + missing children)
│   ├── rag.py                   # RAG (Chroma + MiniLM + hybrid search)
│   ├── remediation.py           # Closed-loop (whitelist + approval + verify + audit)
│   ├── metrics.py               # Prometheus → aggregated tables
│   ├── traces.py                # Jaeger → self-time + span tree
│   ├── stream_consumer.py       # Streaming detector (Redpanda consumer)
│   ├── http.py                  # Stdlib HTTP → typed errors
│   ├── window.py                # Time parsing + retention clamping
│   ├── status.py                # Backend liveness + oldest_queryable
│   └── util.py                  # Error kinds
├── services/
│   ├── gateway/                 # Spring Boot 3.5.16, Java 21, Maven
│   ├── orders/
│   └── inventory/
└── runs/                        # Per-run artefacts (gitignored except traces/)
```

---

## What This Is NOT (Honest Boundaries)

| Limitation | Reality |
|---|---|
| **Scale** | Peak 6 req/s on 2-core laptop. Every scaling opinion is reasoning, not experience. |
| **Sampling** | `always_on` would not survive real volume — first thing to break at 100×. |
| **Fault realism** | Injected latency = `Thread.sleep`; injected errors = coin flip. Real latency has GC pauses, queueing, saturation. Real 500s have partial writes, retry storms. |
| **Sample size** | 12 labelled incidents (4 × 3 runs) — error bars too wide for formal stats. |
| **Evaluation bias** | Every fault designed by the detector author — most flattering setup possible. |
| **K8s** | Single-node kind, `hostPort` (anti-pattern in real clusters), no HA, no TLS, no RBAC. |
| **MCP** | Only ever driven by its own probe + one client. Shared misreadings invisible. |
| **Token counts** | Bytes ÷ 4 rule of thumb; real tokenization of dense JSON runs worse. |
| **Jaeger** | Badger on disk, 72h TTL. Left running, compactions starve write path (see ENGINEERING_LOG §113). |

---

## Key Files to Read for Interview Prep

| File | What You'll Learn |
|---|---|
| `BUILD_PLAN.md` | The 10-session plan, tech choices, evaluation criteria |
| `ENGINEERING_LOG.md` | 182 decisions: what chosen, what rejected, why (answers "why X not Y?") |
| `PROGRESS.md` | Session results, verification outputs, what broke |
| `tools/verify_detection.py` | How detection is scored against ground truth |
| `tools/verify_correlation.py` | How correlation top-1 accuracy is computed |
| `tools/verify_traces.py` | How self-time is recomputed from committed traces |
| `aiops_mcp/agent.py` | Single-turn agent loop, confidence calibration, unknown handling |
| `aiops_mcp/remediation.py` | Whitelist, approval gate, verification, audit log |

---

## Common Commands

```bash
# Start Compose stack
make up

# Stop Compose stack
make down

# View logs
make logs

# Start kind cluster
make k8s-up

# Stop kind cluster
make k8s-down

# Inject faults
make inject-latency-inventory
make inject-error-rate-orders
make inject-pool-exhaust-inventory
make inject-latency-gateway
make inject-clear

# Run agent on latest incident
make agent-summary

# Closed-loop remediation (auto-approval)
make remediate-auto

# Closed-loop remediation (human approval)
make remediate-human

# Verify all numbers
python tools/verify_detection.py
python tools/verify_correlation.py
python tools/verify_traces.py
python tools/verify_rag.py
```

---

## License

MIT — but this is a learning/portfolio project, not production software. Use at your own risk.

---

## Author

**Mohammed Sadique** — Android Platform Engineer → Oracle AIOps Engineering (IC2) target  
Built in 10 sessions over 2 weeks (evenings). Every line defended in interview.
| i2 | orders | error-rate 20 % | 5xx gateway | 0.000 /s | 1.162 /s | rose |
| i3 | inventory | pool-exhaust | hikari used | 0 | 10 | rose |
| i3 | inventory | pool-exhaust | hikari idle | 3 | 0 | fell |
| i3 | inventory | pool-exhaust | p95 inventory | 0.025 s | 0.024 s | **flat, as designed** |
| i4 | gateway | latency 250 ms | p95 gateway | 0.044 s | 0.482 s | rose |
| i4 | gateway | latency 250 ms | p95 inventory | 0.025 s | 0.035 s | **flat, as designed** |

Reproduced across two independent runs. Host-to-Prometheus clock skew is checked
before any labelling (measured −0.002 s) — labels written against one clock and
scored against another are wrong in a way nothing else would reveal.

### The two flat rows are the interesting ones

**i3** saturates the connection pool while latency and error rate do not move at
all. A saturation signal with no user-visible impact is precisely the case a
resource-aware detector wins and a latency/error detector misses — so a detector
that flags i3 on latency is not working, it is guessing.

**i4 versus i1** is the discriminating pair. Both look like "gateway got slow" in
the p95 panel. In the traces they are opposites:

```
i1 — inventory latency                  i4 — gateway latency
gateway   POST /api/orders  507.8ms     gateway   POST /api/orders  327.7ms
  orders    POST /orders     502.3ms      └─ 293.7ms of self-time HERE
    inventory POST /reserve  493.4ms      orders    POST /orders     28.7ms  ← normal
      └─ 459ms gap, then the DB spans       inventory POST /reserve  12.7ms  ← normal
```

---

## Measured Results

Every number below comes from the verification tools in `tools/` run against
committed evidence (`ground_truth.jsonl`, `runs/*/metrics/`, `runs/*/traces/`).
No metric is quoted that cannot be re-derived by `make verify`.

### Detection (Session 4)

Scored offline via `python tools/verify_detection.py` against exported metrics.

| Detector | Recall | Precision | False Positives / hr |
|---|---|---|---|
| **CORE (RED: latency p95 + error rate)** | 3/4 (75%) | 6/6 (100%) | 0 |
| **CORE + SATURATION (adds HikariCP pool gauge)** | **4/4 (100%)** | **8/8 (100%)** | 0 |
| IsolationForest (multivariate, baseline-only, ~10 vectors) | 1/4 (25%) | 1/1 (100%) | 0 |

- **Mean time to detect**: ~15–30 s after fault injection (streaming detector, S8)
- **Zero-baseline fix**: 5xx series does not exist until first 500; baseline treated as `μ=0, σ=0` with floor `σ=0.05 req/s` so first non-zero sample triggers detection.

### Correlation (Session 5)

Scored offline via `python tools/verify_correlation.py` against exported traces.

| Metric | Value |
|---|---|
| **Top-1 accuracy** | **8/8 (100%)** across 2 labelled runs (4 incidents each) |
| Per fault type: latency | 2/2 (100%) |
| Per fault type: error_rate | 1/1 (100%) |
| Per fault type: pool_exhaust | 1/1 (100%) — correctly returns `insufficient_trace_evidence` |
| Confidence calibration (>0.9 bucket) | 2/2 correct (100%) |
| Confidence calibration (0.7–0.9 bucket) | 1/1 correct (100%) |
| Confidence calibration (<0.7 bucket) | 1/1 correct (100%) |
| High confidence (>0.8) on WRONG service | 0 |

Self-time attribution (recomputed from committed traces via `python tools/verify_traces.py`):

| Incident | Fault location | Top service (self-time %) |
|---|---|---|
| i1 | inventory latency | inventory 96–99% |
| i2 | orders error-rate | orders (missing child spans) |
| i3 | pool exhaustion | insufficient_trace_evidence (traces normal) |
| i4 | gateway latency | gateway 91–94% |

### Retrieval (Session 6)

Hybrid dense + BM25 (α=0.8), 10 runbooks, ~93 chunks.

| Metric | Value |
|---|---|
| **Accuracy (correct runbook in top-1)** | **4/10 (40%)** |
| Strong on distinctive vocabulary | pool exhaustion, chaos endpoints, remediation |
| Weak on overlapping vocabulary | latency faults, propagation cases |

**Root cause**: 10 runbooks describe the SAME system with highly overlapping vocabulary. Queries like "latency injection in gateway" match both `latency-gateway.md` and `latency-propagation.md`. This is a fundamental limitation of vector similarity on small, highly-overlapping corpora. Cross-encoder reranking deferred.

### End-to-End Grounded Summary (Session 7)

Single-turn agent via MCP server, tested on all 4 incident types.

| Incident | Summary correct? | Citations correct? | Confidence |
|---|---|---|---|
| i1 (inventory latency) | ✅ | ✅ | 0.87 |
| i2 (orders error-rate) | ✅ | ✅ | 0.73 |
| i3 (pool exhaust) | ✅ "unknown" | N/A (no_match) | 0.40 (capped) |
| i4 (gateway latency) | ✅ | ✅ | 0.85 |

**Grounded summary rate**: 4/4 known incidents produce correct summary with citations.  
**Unknown incident handling**: Explicit "runbooks do not cover this — escalate to on-call", confidence capped at 0.4.

### Closed-Loop Automation (Session 9)

| Test | Scenario | Result |
|---|---|---|
| 1 | `disable_chaos` on live service | ✅ PASS — injected latency cleared, chaos status inactive |
| 2 | Snapshot + recovery check | ✅ PASS — metrics captured, comparison works |
| 3 | Full remediation flow (auto-approval) | ✅ PASS — executed, verified `recovered: true` |
| 4 | Audit log completeness | ✅ PASS — all fields recorded including verification |

---

### The two flat rows are the interesting ones

**i3** saturates the connection pool while latency and error rate do not move at
all. A saturation signal with no user-visible impact is precisely the case a
resource-aware detector wins and a latency/error detector misses — so a detector
that flags i3 on latency is not working, it is guessing.

**i4 versus i1** is the discriminating pair. Both look like "gateway got slow" in
the p95 panel. In the traces they are opposites:

```
i1 — inventory latency                  i4 — gateway latency
gateway   POST /api/orders  507.8ms     gateway   POST /api/orders  327.7ms
  orders    POST /orders     502.3ms      └─ 293.7ms of self-time HERE
    inventory POST /reserve  493.4ms      orders    POST /orders     28.7ms  ← normal
      └─ 459ms gap, then the DB spans       inventory POST /reserve  12.7ms  ← normal
```

i1's gap sits 473 ms deep, inside inventory, before its first database span, with
every ancestor inflated. i4's sits at the top, before gateway makes any downstream
call, leaving everything below it untouched. Same symptom at the edge, opposite
root cause — separable only in the trace.

---

## Are injected faults distinguishable from real ones?

They must not be, or a detector could score perfectly by reading the label.

Verified against a genuine 500 rather than assumed: an injected 500 and a real one
are **identical** on `http.route`, `http.response.status_code`, `error.type` and
span status, so they land on the same Prometheus series with the same labels. The
metric stream genuinely cannot tell them apart.

Two differences remain, both trace-level and both recorded rather than hidden: the
injected span carries **no `exception` event**, and it has **no child spans**
because the request short-circuits. The out-of-band marker is a response header
(`X-Chaos-Injected`), which the OTel agent does not capture, so it reaches the
scenario runner and no telemetry backend.

---

## Asking the system questions — the MCP server

The telemetry above is only useful if something can *interrogate* it. `aiops_mcp/`
is an MCP server that exposes it as tools, so a model can ask "is inventory slow"
and get an answer computed from Prometheus and Jaeger rather than guessed.

**What MCP actually is**, since the acronym does a lot of hiding: JSON-RPC 2.0,
newline-delimited over stdout, with a fixed method vocabulary — `initialize`,
`notifications/initialized`, `tools/list`, `tools/call`, `ping`. That is the whole
protocol at this scope. It is hand-rolled here in the standard library: **no
dependency, no venv, no lockfile.** The official SDK would have been faster and is
the right choice the moment a second transport is needed; it would also have
hidden the part worth understanding.

| Tool | Answers |
|---|---|
| `query_metrics` | latency percentiles, request rate, error ratio, DB pool |
| `query_traces` | which service is actually spending the time |
| `get_trace` | where one request went, as an indented span tree |
| `telemetry_status` | are the backends up, and how far back can I see |

```powershell
# exercise it over real pipes, without a client
py -3.12 tools\mcp_probe.py --list
py -3.12 tools\mcp_probe.py query_metrics '{\"metric\":\"latency\",\"service\":\"orders\",\"window\":\"15m\",\"percentile\":99}'
py -3.12 tools\mcp_probe.py --ground-truth      # replay the labelled windows
py -3.12 tools\mcp_probe.py --cost query_traces '{\"service\":\"gateway\",\"window\":\"15m\"}'
```

`.mcp.json` registers the server at project scope, so a client started in this
directory finds it after you approve it once.

### Return shapes are designed for a context window

A tool that returns everything is a tool that cannot be used twice. The property
worth having is not "smaller" but **bounded** — the response must not grow with
the question:

| window | served | unaggregated | reduction |
|---|---|---|---|
| 15m | 1,172 B | 5,760 B | 5× |
| 23h | 1,159 B (~290 tokens) | 165,720 B (~41,430 tokens) | **223×** |

Flat across a 92× range of window sizes, because the point budget is fixed and
only the bucket width changes. Traces fold the same way — spans collapse into
per-service self-time, measured at 37× on live traffic and 49× on incident traces
(deeper traces, more spans to fold; ~12× on idle health checks). Every figure is
reproducible with `--cost`, which fetches the raw side **for real** rather than
estimating it.

Downsampling takes the **max** per bucket, never the mean. Handing Prometheus a
wide `step` lets it pick one sample per step and discard the rest, so a 40-second
spike inside a 10-minute bucket vanishes — the tool would report a clean window
during an incident it was pointed directly at.

### Two error channels, kept distinct

This is the part of MCP that is easy to get backwards, and getting it wrong makes
a model hallucinate confidently:

- A **JSON-RPC error** (`-32601` method not found, `-32602` invalid params) goes
  to the *client*. The model never sees it.
- A **successful response carrying `isError: true`** goes to the *model*.

So "Prometheus is unreachable" must be the second kind. Sent as a JSON-RPC error
it disappears into the client, and the model — having asked and received nothing —
invents a plausible latency number. Every failure here carries one of four kinds:
`bad_argument`, `backend_unreachable`, `backend_error`, `timeout`.

The same principle governs empty results. A window past Prometheus's 24 h
retention is **not** empty, it is unavailable, and the tool says so explicitly —
*"This data is gone, not empty"* — because "empty" invites the conclusion that
nothing happened.

**The `timeout` kind earned itself.** After several hours of continuous load,
Badger reached ~400 MB and its compactions (10–32 s each, on two cores) saturated
Jaeger's write path: the collector could not export a single batch
(`DeadlineExceeded`) and trace search took 48 s for a window the tool allows 15 s.
Jaeger was **up and answering** — `/api/services` returned in 86 ms — just far
too slowly to be useful. The server reported `kind: timeout, target: jaeger` and
`telemetry_status` returned `degraded`. It did not return an empty trace list,
which is the failure that matters: a model told "no traces found" concludes the
requests never happened.

One rough edge, recorded rather than smoothed over: `telemetry_status` reports
`reachable: false` in this state. Within the tool's time budget that is true, and
the `detail` field says `TimeoutError` rather than `connection refused`, but the
boolean itself cannot tell *saturated* from *dead* — and those need different
responses from whoever is on call.

### Self-time closes the metrics blind spot

i1 and i4 both raise gateway's p95 to ~480 ms and are indistinguishable in
metrics. `query_traces` computes each span's **self-time** — its duration minus
the *merged* intervals of its children, merged rather than summed so concurrent
children cannot produce a negative — and the two separate cleanly. Recomputed
from the committed raw traces of both labelled runs:

```
                        run-20260809-201431   run-20260810-170537
i1 (fault in inventory)   inventory 98.5%       inventory 96.2%
i4 (fault in gateway)     gateway   93.5%       gateway   91.0%
```

Each figure is a 15-trace sample, so it moves by a point or two between samples —
the exact percentage is not the claim. The claim is the *separation*, and it
reproduces across two runs recorded a day apart with no shared state. The raw
Jaeger spans behind every number are in `runs/<run_id>/traces/`, so the table can
be rederived rather than believed — offline, from the committed files, with no
running stack:

```bash
py -3.12 tools/verify_traces.py
```

The files were produced by `tools/export_traces.py <run_id>`, which reads Jaeger.
Do not re-run it for these two runs: their windows are long past the 72 h TTL and
past the wipe below, so it would replace the evidence with nothing. It refuses to
do that, but the reason it needs to refuse is worth stating plainly — a tool for
preserving evidence is one careless invocation away from being a tool for
destroying it.

They are committed because they are not regenerable. Jaeger's TTL is 72 h, and
its Badger volume gets emptied when it outgrows its memory cap (below) — the
first two labelled runs had their trace evidence living only inside the container
that the fix wipes. Self-time is *derived*; given the spans it can be recomputed
by any future version of the folding code. The reverse is not true, so the input
is what gets stored.

The tool reports the number and stops there. Ranking services by blame is the
correlation layer's job; doing it in two places would mean two components able to
disagree about root cause with no way to tell which was right.

### Verified against the labelled windows, not against "now"

`--ground-truth` replays every incident in `ground_truth.jsonl` through the tool
and checks the answers. Every window still inside Prometheus's 24 h retention
answers as expected; windows that have aged past the horizon are **correctly
refused** rather than reported as quiet, and the check counts them as expired
rather than failed. The split between the two shifts as the labelled runs age —
which is the point. A check that scored those refusals as failures would show a
working system decaying purely with the passage of time.

It decays in a different way instead, and the honest statement of it is this: once
every labelled run is older than 24 h, the replay scores `0/0` and demonstrates
nothing except that refusal works. That state is reached in a day, and it is the
current state. Re-running the metrics-side check means recording a fresh labelled
run — the verification is real but it is perishable, and a green result from it
should always be read together with how old the labels are.

The trace-side evidence does not perish, and that asymmetry is the reason
`runs/<run_id>/traces/` is committed. The self-time table above is recomputable
from those files indefinitely, with no running stack at all. Of the two things
this session claims, the metrics tooling has to be re-demonstrated on demand and
the trace finding is permanently auditable. Giving Prometheus's side the same
property would mean exporting raw range-query output per labelled window, which
is not built.

`search_logs` is deliberately **absent** until the log pipeline exists. A tool
with no data source is worse than a missing one: the model calls it, gets nothing,
and cannot distinguish "no matching logs" from "no logs are collected".

---



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


## Closed-Loop Automation (Session 9)

The system now closes the loop: **alert → detect → diagnose → remediate → verify**, with a complete audit trail.

### Whitelisted remediation actions

Only three actions can execute — the whitelist is the blast-radius control:

| Action | Description |
|---|---|
| `disable_chaos(service)` | DELETE `/chaos` — releases injected faults (latency, error-rate, pool-exhaust) |
| `restart_container(service)` | `docker compose restart <service>` — heavier action for wedged processes |
| `scale_replicas(service, n)` | Placeholder for K8s (`kubectl scale deployment`) — returns `not_implemented` in Compose |

### Approval gate (mandatory)

Two modes, same audit record:
- **human** — prints proposed action, waits for "yes" on stdin (default for demo)
- **auto** — policy-based (e.g., `disable_chaos_known_faults`)

Both write the same `approved_by` field showing exactly who/what authorized it.

### Recovery verification

After execution, re-queries the **exact signal that triggered the alert** (`errors:orders`, `latency:inventory`, `pool:used`):
- Latency: recovered if within 20% of baseline
- Error rate: recovered if near zero (empty series = zero errors, handled explicitly)
- Pool: recovered if within 20% of baseline

### Audit log

Every attempt writes one JSON line to `runs/<run_id>/remediation_log.jsonl`:

```json
{
  "timestamp": "2026-09-19T21:49:26.708017Z",
  "run_id": "run-test-closed-loop",
  "incident_id": "run-test-closed-loop/i2",
  "action": "disable_chaos",
  "args": {"service": "orders"},
  "approved_by": "auto:disable_chaos_known_faults",
  "status": "verified",
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

### MCP tools added

- `suggest_remediation(signal, candidate_service)` — returns whitelisted action with reasoning (does NOT execute)
- `execute_remediation(run_id, incident_id, action, args, anomaly_signal, approval_mode, approval_policy, baseline_metrics)` — full execute → verify flow
- `get_remediation_log(run_id)` — retrieves audit trail

### Agent integration

The agent now:
1. Runs `query_metrics` → `query_traces` → `correlate_incident` → `search_runbooks`
2. Calls `suggest_remediation(signal, top_candidate)` — gets whitelisted action
3. (Optionally) executes remediation if approval given
4. Returns `AgentResult` with `remediation: {suggested, executed, verified}`

### Verified on live stack

| Test | Result |
|---|---|
| disable_chaos clears injected fault | ✅ PASS |
| Snapshot captures pre/post metrics | ✅ PASS |
| Full flow with auto-approval → verified | ✅ PASS |
| Audit log complete with verification | ✅ PASS |

**What this is not:**
- Production remediation with blast-radius control — the whitelist is the only control, and it only covers chaos faults
- Kubernetes-native — `scale_replicas` is a placeholder; Compose cannot scale individual services
- Multi-step remediation — S9 scope is single-action; sequencing deferred


## What is built

| | Status |
|---|---|
| 3 Spring Boot services + Postgres, Docker Compose | done |
| OTel agent → Collector → Jaeger / Prometheus / Grafana | done |
| Chaos endpoints in all three services | done |
| Scenario runner, ground truth, signal verification | done |
| MCP server exposing metrics and traces as tools | done |
| Copilot agent (single-turn, grounded summaries) | done (S7) |
| Streaming inference (Redpanda + sliding-window z-score) | done (S8) |
| **Closed-loop automation (whitelist + approval + verify + audit)** | **done (S9)** |
| **Kubernetes deploy (kind) — "I've run this on K8s" checkbox** | **done (S10)** |
| `search_logs` + the log pipeline | planned |
| Anomaly detection scored against ground truth | done (S4) |
| Trace-based correlation and root-cause ranking | done (S5) |
| RAG over runbooks; incident-response agent | done (S6/S7) |

---

---

## What this is not

Stated deliberately, because knowing where a system's claims stop is part of
engineering it.

- **Nothing here has run at scale.** Peak load is 6 requests/second on a two-core
  laptop. Every scaling opinion in this repo is reasoning, not experience.
- **`always_on` sampling would not survive real volume.** It is the first thing that
  would break at 100×; the honest answer is head-based ratio or tail sampling, which
  this project has not needed and therefore has not tested.
- **These are clean-room faults.** Injected latency is a `Thread.sleep` and injected
  errors are a coin flip. Real latency arrives with GC pauses, queueing and
  saturation; real 500s arrive with partial writes and retry storms.
- **Twelve labelled incidents is not a sample.** Accuracy figures computed over this
  many events would have error bars wide enough to be meaningless, so none are
  quoted.
- **Every fault was designed by the same person who will write the detector.** That
  is the most flattering possible evaluation setup, and it is worth saying out loud.
- **Compose is not Kubernetes.** Service discovery here is Docker's embedded DNS.
  None of the questions that make Kubernetes hard arise at all.
- **The MCP server has only ever been driven by its own probe and one client.**
  The probe was written by the same person as the server, against the same reading
  of the spec, so a shared misreading is invisible to this setup. Two older
  protocol revisions are advertised and neither has been exercised.
- **The slow-backend case was tested by accident, not by design.** It was not a
  deliberate fault-injection test; the stack degraded on its own and the tool was
  pointed at it (see below). A test that happens *to* you is weaker evidence than
  one you can re-run on demand, and there is no harness for it yet.
- **Token counts here are bytes divided by four.** The bytes are measured; the
  conversion is a rule of thumb, and real tokenisation of dense JSON runs worse.
  Treat every token figure as a floor.

---

## Notes for anyone running this

- **Jaeger stores traces on disk (Badger, 72 h TTL), not in memory.** It originally
  used in-memory storage; a single process restart destroyed the trace evidence for
  a completed 30-minute labelled run. Storage that lives in a process's heap is not
  storage.
- **Badger will out-run two cores if you leave load running.** At 6 req/s with
  `always_on` sampling it reached ~400 MB in a few hours, at which point level-5→6
  compactions took 10–32 s each and starved both the write path (collector
  exports failing with `DeadlineExceeded`) and search (48 s for a 15-minute
  window). Nothing crashes and `docker compose ps` still says `running`. Stop the
  traffic generator when you are not using it, or wipe the volume between runs.
  The 72 h TTL bounds the data, but not fast enough to protect a two-core host.
- **Left running, that degradation takes the metrics with it — all of them.** At
  549 MB of Badger, Jaeger sat pinned at 499/512 MiB, writes slowed, the
  collector's retry queue filled, and the collector went to 48% CPU and started
  **dropping spans**. A collector busy enough to drop spans is also too busy to
  serve its own `/metrics` inside Prometheus's scrape timeout, so both collector
  scrape targets went **down** and metrics collection stopped completely —
  `query_metrics` returning `no_samples_in_window` for a system whose services
  were all fine. Every one of the eight containers reported `Up`, four of them
  `healthy`, throughout. The failure travels *backwards* along the pipeline from
  storage to collection, which is not the direction you look first.
  Recovery, in order — Jaeger will not restart cleanly if you skip the ownership
  step, because the container runs as uid 10001 and an emptied volume comes back
  owned by root:

  ```bash
  docker compose stop jaeger && docker run --rm -v aiops_badgerdata:/b alpine sh -c 'rm -rf /b/* && chown -R 10001:0 /b && chmod -R 775 /b' && docker compose start jaeger && docker compose restart otel-collector
  ```

  The collector restart is not optional: it is what discards the undeliverable
  retry queue. Without it the recovered Jaeger is immediately buried again by the
  backlog. Export anything you still need with `tools/export_traces.py` first —
  this deletes all trace history.

  Note the `/b/*` and *not* `/b/data/*`. Badger keeps keys and values in two
  separate directories — `BADGER_DIRECTORY_KEY: /badger/key` and
  `BADGER_DIRECTORY_VALUE: /badger/data` — and essentially all of the size is in
  the **key** directory. Clearing only the value directory looks like it worked
  (`du` on `/badger/data` drops to kilobytes, Jaeger restarts, the API answers)
  and leaves 1.4 GB of SST files in place. The symptom returns a day later as a
  restart loop: `RestartCount` climbing, `ExitCode=0`, `OOMKilled=false`, no
  crash in the logs, and the process never reaching `Health Check state change:
  ready` before going round again. Measured here: 24 restarts, ~4 minutes apart,
  while the other seven containers held 28 h of uptime.
- **Docker's `mem_limit` percentage includes reclaimable page cache, so "512MiB /
  512MiB" is not the emergency it looks like.** The cgroup breaks it down:
  `anon` is real heap, `file` is memory-mapped Badger tables the kernel can drop
  under pressure. Measured at the worst point — 353 MiB anon against 117 MiB
  file, and `memory.events` showing `max 14268` (reclaim throttled that many
  times) with `oom_kill 0`. That combination is the signature worth knowing:
  **throttled but never killed**, which is why it hangs instead of dying and why
  `ExitCode` stays 0. Settled at 130 MiB anon once Badger finished compacting.

  ```bash
  docker exec aiops-jaeger sh -c 'grep -E "^(anon|file) " /sys/fs/cgroup/memory.stat; cat /sys/fs/cgroup/memory.events'
  ```

  Badger's block cache (256 MB) and memtable (64 MB) are fixed defaults with no
  environment variable to shrink them in Jaeger v1, so the floor here is
  structural rather than tunable — see the `mem_limit: 512m` comment in
  `docker-compose.yml`.
- **Large Jaeger API queries are not safely read-only.** A single request for 400
  traces with full span payloads preceded that restart. Keep verification queries
  small.
- **`docker compose ps` says nothing about reachability.** Healthchecks run inside
  the container and never test the host port binding. A container with no
  healthcheck at all reports nothing alarming while being completely unreachable.
  Verify with a real HTTP request.
- **Prometheus retention is capped at 24 h / 1 GB** and Jaeger at 72 h, which bounds
  how long labels stay scoreable against telemetry.
- **`.env` is committed on purpose.** It contains development defaults only — the
  database is reachable solely on localhost and holds five rows of fake stock. A
  real deployment would source these from a secret manager.

---

## Stack

| Layer | Choice |
|---|---|
| Services | Java 21, Spring Boot 3.5.16, Maven |
| Database | PostgreSQL 16, HikariCP (pool capped at 10), `JdbcTemplate` |
| Instrumentation | OpenTelemetry Java agent 2.30.0 (zero code changes) |
| Pipeline | OpenTelemetry Collector 0.158.0 (contrib) |
| Traces | Jaeger 1.76.0, Badger storage |
| Metrics | Prometheus 3.13.2 → Grafana 13.1.3 |
| Tooling | Python 3.12, standard library only |
| Orchestration | Docker Compose |
