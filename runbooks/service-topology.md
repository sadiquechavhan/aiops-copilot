# Service Topology Reference

## System Overview
```
┌─────────────┐     ┌─────────────┐     ┌────────────────┐     ┌─────────┐
│   Client    │────▶│  Gateway    │────▶│    Orders      │────▶│Inventory│
│             │     │  :8080      │     │    :8080       │     │  :8080  │
└─────────────┘     └─────────────┘     └────────────────┘     └────┬────┘
                                                                      │
                                                                      ▼
                                                               ┌─────────────┐
                                                               │  Postgres   │
                                                               │   :5432     │
                                                               └─────────────┘
```

## Service Details

### Gateway (`gateway`)
- **Port**: 8080
- **Role**: Entry point, routes `POST /api/orders` → orders service
- **Health**: `GET /health` → 200 OK
- **Chaos endpoints**:
  - `POST /chaos/latency?ms=N&jitter=J` — inject latency
  - `POST /chaos/error-rate?pct=N` — inject error rate
  - `GET /chaos` — status
  - `DELETE /chaos` — clear all
- **Downstream**: orders service at `http://orders:8080/api/orders`
- **Timeouts**: connect 2000 ms, read 5000 ms
- **OTel service.name**: `gateway`

### Orders (`orders`)
- **Port**: 8080
- **Role**: Business logic, calls inventory to reserve stock
- **Health**: `GET /health` → 200 OK
- **Chaos endpoints**: same as gateway
- **Downstream**: inventory service at `http://inventory:8080/reserve`
- **Timeouts**: connect 2000 ms, read 3000 ms
- **OTel service.name**: `orders`

### Inventory (`inventory`)
- **Port**: 8080
- **Role**: Stock management, writes to Postgres
- **Health**: `GET /health` → 200 OK (does NOT check DB)
- **Chaos endpoints**: same as gateway + pool exhaustion
  - `POST /chaos/pool-exhaust?hold=9&ttl_ms=X` — hold HikariCP connections
- **Downstream**: Postgres at `jdbc:postgresql://postgres:5432/inventory`
- **Connection pool**: HikariCP, max 10 connections
- **OTel service.name**: `inventory`

### Postgres (`postgres`)
- **Port**: 5432
- **Database**: `inventory`
- **User**: `app` / `app`
- **Schema**: `stock(sku, available)`
- **Seeded**: 1,000,000,000 per SKU (effectively infinite)

## Telemetry Stack

### OpenTelemetry Collector (`otel-collector`)
- **Receives**: OTLP from Java agents (port 4317)
- **Exports**: Jaeger (traces), Prometheus (metrics)
- **Processors**: batch, memory_limiter

### Jaeger (`jaeger`)
- **UI**: http://localhost:16686
- **Storage**: Badger (disk, 72h TTL)
- **OTLP ingest**: 4317 (internal only)

### Prometheus (`prometheus`)
- **UI**: http://localhost:9090
- **Retention**: 24h, 1 GB
- **Scrape interval**: 15s
- **Targets**: gateway, orders, inventory, otel-collector, postgres-exporter

### Grafana (`grafana`)
- **UI**: http://localhost:3000
- **Datasources**: Prometheus, Jaeger (provisioned)
- **Dashboards**: aiops-overview (provisioned)

## Request Flow (Happy Path)
```
1. Client POST /api/orders {sku, qty}
2. Gateway: creates span, calls orders
3. Orders: creates span, calls inventory
4. Inventory: creates span, DB UPDATE stock
5. Inventory: returns 200 {sku, available}
6. Orders: returns 200 {orderId, sku, qty}
7. Gateway: returns 200 {orderId, sku, qty}
```

## Trace Structure (Happy Path)
```
gateway    POST /api/orders          45 ms
  └── orders POST /api/orders        30 ms
        └── inventory POST /reserve  15 ms
              └── DB   UPDATE         2 ms
```

## Metric Names (Prometheus)

### Latency
- `http_server_request_duration_seconds` (histogram)
  - Labels: `service_name`, `http_route`, `http_response_status_code`
  - Excludes: `/health`, `/chaos/**`

### Error Rate
- `http_server_requests_total` (counter)
  - Labels: `service_name`, `http_route`, `http_response_status_code`
  - 5xx rate = `rate(http_server_requests_total{http_response_status_code=~"5.."}[2m])`

### Request Rate
- `http_server_requests_total` (same counter)
  - Request rate = `rate(http_server_requests_total[2m])`

### DB Pool (Inventory only)
- `hikaricp_connections` (gauge)
  - Labels: `service_name`, `state` (used/idle/pending)
  - `db_client_connections_usage{state="used"}` = active connections
  - `db_client_connections_usage{state="idle"}` = available connections

## Chaos Endpoint Contract
```
POST   /chaos/latency?ms=N[&jitter=J]     200 {latency_ms, jitter_ms}
POST   /chaos/error-rate?pct=N             200 {error_pct}
POST   /chaos/pool-exhaust?hold=9&ttl_ms=X 202 {held, ttl_ms}  (inventory only)
GET    /chaos                              200 {requests_seen, requests_delayed, requests_failed, pool_held}
DELETE /chaos                              200 {cleared: true}
```

## Fault Injection Ceilings
| Service | Max Latency | Max Error Rate |
|---|---|---|
| gateway | 4000 ms | 100% |
| orders | 2000 ms | 100% |
| inventory | 2000 ms | 100% |

## Ground Truth Format
```json
{
  "schema": 1,
  "run_id": "run-20260831-174136",
  "incident_id": "run-20260831-174136/i1",
  "start": "2026-08-31T17:42:00.000Z",
  "end": "2026-08-31T17:46:00.000Z",
  "confirmed_start": "2026-08-31T17:42:00.123Z",
  "confirmed_end": "2026-08-31T17:46:00.456Z",
  "service": "inventory",
  "fault_type": "latency",
  "params": {"ms": 400, "jitter": 80},
  "expected_signal": ["latency:inventory", "latency:gateway (propagated)"],
  "observed": {"requests_seen": 240, "requests_delayed": 240, "requests_failed": 0},
  "recovered": true,
  "recovery_s": 0.3
}
```

## Incident Catalog (4 per run)
| ID | Service | Fault Type | Params | Duration | Key Signal |
|---|---|---|---|---|---|
| i1 | inventory | latency | 400±80 ms | 240 s | inventory p95↑, self-time inventory 96% |
| i2 | orders | error_rate | 20% | 240 s | orders 5xx↑, missing children |
| i3 | inventory | pool_exhaust | hold 9/10 | 240 s | pool used=9, latency FLAT |
| i4 | gateway | latency | 250±50 ms | 180 s | gateway p95↑, self-time gateway 92% |