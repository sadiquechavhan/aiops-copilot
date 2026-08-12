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

## What is built

| | Status |
|---|---|
| 3 Spring Boot services + Postgres, Docker Compose | done |
| OTel agent → Collector → Jaeger / Prometheus / Grafana | done |
| Chaos endpoints in all three services | done |
| Scenario runner, ground truth, signal verification | done |
| MCP server exposing metrics and traces as tools | done |
| `search_logs` + the log pipeline | planned |
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
