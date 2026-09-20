# =============================================================================
# AIOps Copilot — Makefile
#
# Primary dev environment: Docker Compose (`make up`)
# K8s checkbox: kind (`make k8s-up`)
# =============================================================================

.PHONY: help up down logs ps k8s-up k8s-down k8s-logs k8s-ps \
        fetch-agent build-images load-images \
        traffic inject-latency-inventory inject-error-rate-orders \
        inject-pool-exhaust-inventory inject-latency-gateway \
        remediate-auto remediate-human \
        agent-summary clean

# Default target
help:
	@echo "AIOps Copilot — Session 10"
	@echo ""
	@echo "Docker Compose (primary dev environment):"
	@echo "  make up              - Start the full stack (Compose)"
	@echo "  make down            - Stop the stack"
	@echo "  make logs            - Follow logs"
	@echo "  make ps              - Show container status"
	@echo ""
	@echo "Kubernetes (kind) — 'I've run this on K8s' checkbox:"
	@echo "  make k8s-up          - Create kind cluster and deploy"
	@echo "  make k8s-down        - Delete kind cluster"
	@echo "  make k8s-logs        - Follow K8s pod logs"
	@echo "  make k8s-ps          - Show pod status"
	@echo ""
	@echo "Build & bootstrap:"
	@echo "  make fetch-agent     - Download OTel Java agent (run once)"
	@echo "  make build-images    - Build all 3 service images"
	@echo "  make load-images     - Load images into kind"
	@echo ""
	@echo "Traffic & chaos (works against both Compose and K8s):"
	@echo "  make traffic         - Run traffic generator (steady load)"
	@echo "  make inject-latency-inventory   - Inject 400ms latency into inventory"
	@echo "  make inject-error-rate-orders   - Inject 20% error rate into orders"
	@echo "  make inject-pool-exhaust-inventory - Exhaust inventory DB pool (hold 9/10)"
	@echo "  make inject-latency-gateway     - Inject 250ms latency into gateway"
	@echo "  make inject-clear   - Clear all chaos (DELETE /chaos on all services)"
	@echo ""
	@echo "Agent & remediation:"
	@echo "  make agent-summary   - Run copilot agent on latest incident"
	@echo "  make remediate-auto  - Auto-approve remediation for latest incident"
	@echo "  make remediate-human - Human-approve remediation for latest incident"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean           - Remove all generated artefacts"

# =============================================================================
# Docker Compose targets (primary)
# =============================================================================

up:
	@echo "Starting AIOps stack via Docker Compose..."
	docker compose up -d
	@echo "Waiting for services to be healthy..."
	@timeout /t 180 >nul 2>&1 || sleep 180
	@make ps

down:
	docker compose down -v

logs:
	docker compose logs -f

ps:
	docker compose ps

# =============================================================================
# Bootstrap
# =============================================================================

fetch-agent:
	@echo "Fetching OTel Java agent..."
	@powershell -ExecutionPolicy Bypass -File tools/fetch-agent.ps1

build-images:
	@echo "Building service images..."
	docker compose build

# =============================================================================
# Kind (Kubernetes) targets
# =============================================================================

KIND_CLUSTER_NAME := aiops

k8s-up: fetch-agent build-images load-images k8s-create-cluster k8s-deploy
	@echo ""
	@echo "Kind cluster '$(KIND_CLUSTER_NAME)' is up!"
	@echo "Access points (via hostPort on kind node):"
	@echo "  Gateway:      http://localhost:8080"
	@echo "  Orders:       http://localhost:8081"
	@echo "  Inventory:    http://localhost:8082"
	@echo "  Jaeger UI:    http://localhost:16686"
	@echo "  Prometheus:   http://localhost:9090"
	@echo "  Grafana:      http://localhost:3000 (admin/admin)"
	@echo "  Redpanda:     localhost:19092"
	@echo "  OTel Collector: localhost:4317 (gRPC), localhost:4318 (HTTP)"

k8s-create-cluster:
	@echo "Creating kind cluster..."
	kind create cluster --config k8s/kind-config.yaml --name $(KIND_CLUSTER_NAME)

k8s-deploy:
	@echo "Deploying to kind..."
	kubectl apply -f k8s/00-namespace.yaml
	kubectl apply -f k8s/01-otel-collector-configmap.yaml
	kubectl apply -f k8s/02-postgres.yaml
	kubectl apply -f k8s/03-inventory.yaml
	kubectl apply -f k8s/04-orders.yaml
	kubectl apply -f k8s/05-gateway.yaml
	kubectl apply -f k8s/06-otel-collector.yaml
	kubectl apply -f k8s/07-jaeger.yaml
	kubectl apply -f k8s/08-prometheus-configmap.yaml
	kubectl apply -f k8s/09-prometheus.yaml
	kubectl apply -f k8s/10-grafana-configmap.yaml
	kubectl apply -f k8s/11-grafana.yaml
	kubectl apply -f k8s/12-redpanda.yaml
	@echo "Waiting for pods to be ready..."
	kubectl wait --for=condition=ready pod -l app=postgres -n aiops --timeout=300s
	kubectl wait --for=condition=ready pod -l app=inventory -n aiops --timeout=300s
	kubectl wait --for=condition=ready pod -l app=orders -n aiops --timeout=300s
	kubectl wait --for=condition=ready pod -l app=gateway -n aiops --timeout=300s
	kubectl wait --for=condition=ready pod -l app=otel-collector -n aiops --timeout=180s
	kubectl wait --for=condition=ready pod -l app=jaeger -n aiops --timeout=300s
	kubectl wait --for=condition=ready pod -l app=prometheus -n aiops --timeout=180s
	kubectl wait --for=condition=ready pod -l app=grafana -n aiops --timeout=180s
	kubectl wait --for=condition=ready pod -l app=redpanda -n aiops --timeout=180s

load-images:
	@echo "Loading images into kind..."
	kind load docker-image aiops/inventory:1.0.0 --name $(KIND_CLUSTER_NAME)
	kind load docker-image aiops/orders:1.0.0 --name $(KIND_CLUSTER_NAME)
	kind load docker-image aiops/gateway:1.0.0 --name $(KIND_CLUSTER_NAME)

k8s-down:
	kind delete cluster --name $(KIND_CLUSTER_NAME)

k8s-logs:
	kubectl logs -f -l app=gateway -n aiops --tail=100

k8s-ps:
	kubectl get pods -n aiops -o wide

# =============================================================================
# Traffic & Chaos (work against both Compose and K8s via localhost ports)
# =============================================================================

GATEWAY_URL := http://localhost:8080
ORDERS_URL := http://localhost:8081
INVENTORY_URL := http://localhost:8082

traffic:
	@echo "Starting traffic generator (6 req/s)..."
	python tools/traffic_gen.py --rate 6 --duration 0 --url $(GATEWAY_URL)/api/orders

inject-latency-inventory:
	@echo "Injecting 400ms latency into inventory..."
	curl -X POST "$(INVENTORY_URL)/chaos/latency?ms=400&jitter=80"

inject-error-rate-orders:
	@echo "Injecting 20% error rate into orders..."
	curl -X POST "$(ORDERS_URL)/chaos/error-rate?pct=20"

inject-pool-exhaust-inventory:
	@echo "Exhausting inventory DB pool (hold 9 of 10)..."
	curl -X POST "$(INVENTORY_URL)/chaos/pool-exhaust?hold=9&ttl_ms=300000"

inject-latency-gateway:
	@echo "Injecting 250ms latency into gateway..."
	curl -X POST "$(GATEWAY_URL)/chaos/latency?ms=250&jitter=50"

inject-clear:
	@echo "Clearing all chaos..."
	curl -X DELETE "$(GATEWAY_URL)/chaos"
	curl -X DELETE "$(ORDERS_URL)/chaos"
	curl -X DELETE "$(INVENTORY_URL)/chaos"

# =============================================================================
# Agent & Remediation
# =============================================================================

# Run agent on the most recent incident from the latest ground truth run
agent-summary:
	@echo "Running copilot agent on latest incident..."
	python -c "
import json, glob, os
runs = sorted(glob.glob('runs/*/run_meta.json'))
if not runs:
    print('No runs found')
    exit(1)
latest = max(runs, key=os.path.getmtime)
run_id = os.path.basename(os.path.dirname(latest))
print(f'Run: {run_id}')
# Find incidents in this run
import json
with open('ground_truth.jsonl') as f:
    for line in f:
        row = json.loads(line)
        if row['run_id'] == run_id:
            print(f'  Incident: {row[\"incident_id\"]} fault={row[\"fault_type\"]} service={row[\"service\"]}')
"

remediate-auto:
	@echo "Running closed-loop remediation (auto-approval)..."
	python -c "
from aiops_mcp.agent import run_agent_from_ground_truth
import json, glob, os
runs = sorted(glob.glob('runs/*/run_meta.json'))
if not runs:
    print('No runs found')
    exit(1)
latest = max(runs, key=os.path.getmtime)
run_id = os.path.basename(os.path.dirname(latest))
# Get first incident from this run
with open('ground_truth.jsonl') as f:
    for line in f:
        row = json.loads(line)
        if row['run_id'] == run_id:
            inc_id = row['incident_id']
            print(f'Remediating {inc_id}...')
            result = run_agent_from_ground_truth(run_id, inc_id)
            print(f'Summary: {result.summary}')
            print(f'Cause: {result.likely_cause}')
            print(f'Action: {result.recommended_action}')
            print(f'Confidence: {result.confidence}')
            if result.remediation:
                print(f'Remediation: {result.remediation}')
            break
"

remediate-human:
	@echo "Running closed-loop remediation (human-approval)..."
	python -c "
from aiops_mcp.agent import run_agent_from_ground_truth
import json, glob, os
runs = sorted(glob.glob('runs/*/run_meta.json'))
if not runs:
    print('No runs found')
    exit(1)
latest = max(runs, key=os.path.getmtime)
run_id = os.path.basename(os.path.dirname(latest))
with open('ground_truth.jsonl') as f:
    for line in f:
        row = json.loads(line)
        if row['run_id'] == run_id:
            inc_id = row['incident_id']
            print(f'Remediating {inc_id} (human approval)...')
            # The agent will prompt for approval
            result = run_agent_from_ground_truth(run_id, inc_id)
            print(f'Summary: {result.summary}')
            break
"

# =============================================================================
# Cleanup
# =============================================================================

clean:
	docker compose down -v --remove-orphans 2>nul || true
	kind delete cluster --name $(KIND_CLUSTER_NAME) 2>nul || true
	rm -rf runs/*/metrics runs/*/traces 2>nul || true
	@echo "Cleaned up"