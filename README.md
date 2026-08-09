# AIOps Copilot

A small distributed system that observes itself, breaks itself on a schedule, and
writes down exactly what it broke and when.

Three Spring Boot services, a Postgres database, and a full OpenTelemetry pipeline
into Jaeger, Prometheus and Grafana — plus a chaos mechanism that injects labelled
faults and produces machine-readable ground truth for them.

---

## Why build the telemetry generator first

Most anomaly-detection projects load a static dataset. That produces a demo, not a
result — you can say "it works" but you cannot say *how well*, because you never
knew which points were genuinely anomalous.

Running a live system and injecting faults into it buys three things a dataset
cannot:

- **Correlated signals.** Metrics and traces from one system, describing one event.
- **Ground truth.** The fault was injected, so its start, end, service and type are
  known exactly — which means detection accuracy becomes measurable rather than
  assertable.
- **Failure modes worth knowing.** The things that break while building the
  observability stack are the things that break in real ones.

Everything downstream — detection, correlation, retrieval — is scored against
`ground_truth.jsonl`.

---

## Architecture

```
        ┌──────────────── System under observation (Java 21) ────────────────┐
        │   gateway :8080 ──► orders :8081 ──► inventory :8082 ──► Postgres  │
        │   each exposing /chaos: latency · error-rate · pool-exhaust        │
        └───────────────────────────────┬───────────────────────────────────┘
                                        │ OpenTelemetry Java agent
                                        │ (zero code changes — a -javaagent flag)
                            ┌───────────▼────────────┐
                            │    OTel Collector      │
                            └──┬──────────────┬──────┘
                    traces ────┘              └──── metrics
                       ▼                            ▼
                    Jaeger                     Prometheus ──► Grafana
                  (Badger, disk)                              (annotated
                                                            incident bands)
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

## Measured results

One command produces 30 minutes of telemetry containing four labelled incidents.
The runner then queries Prometheus over each window and fails the run if a fault
did not move the metric it should.

| inc | service | fault | metric | baseline | during | verdict |
|---|---|---|---|---|---|---|
| i1 | inventory | latency 400 ms | p95 inventory | 0.025 s | 0.619 s | rose |
| i1 | inventory | latency 400 ms | p95 gateway | 0.044 s | 0.655 s | rose |
| i2 | orders | error-rate 20 % | 5xx orders | 0.000 /s | 1.162 /s | rose |
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

## What is built

| | Status |
|---|---|
| 3 Spring Boot services + Postgres, Docker Compose | done |
| OTel agent → Collector → Jaeger / Prometheus / Grafana | done |
| Chaos endpoints in all three services | done |
| Scenario runner, ground truth, signal verification | done |
| MCP server exposing metrics/traces/logs as tools | planned |
| Anomaly detection scored against ground truth | planned |
| Trace-based correlation and root-cause ranking | planned |
| RAG over runbooks; incident-response agent | planned |

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
- **Eight labelled incidents is not a sample.** Accuracy figures computed over this
  many events would have error bars wide enough to be meaningless, so none are
  quoted.
- **Every fault was designed by the same person who will write the detector.** That
  is the most flattering possible evaluation setup, and it is worth saying out loud.
- **Compose is not Kubernetes.** Service discovery here is Docker's embedded DNS.
  None of the questions that make Kubernetes hard arise at all.

---

## Notes for anyone running this

- **Jaeger stores traces on disk (Badger, 72 h TTL), not in memory.** It originally
  used in-memory storage; a single process restart destroyed the trace evidence for
  a completed 30-minute labelled run. Storage that lives in a process's heap is not
  storage.
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
