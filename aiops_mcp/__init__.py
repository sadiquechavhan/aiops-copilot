"""
aiops_mcp -- an MCP server exposing the Session 1/2 telemetry stack as tools.

Named aiops_mcp and not mcp on purpose: a local package called `mcp` would
shadow the official SDK's import name, and the resulting failure ("no attribute
Server") points at the wrong file entirely. Costs nothing to avoid.

Everything configurable lives here so no other module hardcodes a URL.
Environment overrides exist so the server can point at a stack that is not on
localhost without editing code -- Session 10 (kind) will need exactly that.
"""

import os

PROMETHEUS_URL = os.environ.get("AIOPS_PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
JAEGER_URL = os.environ.get("AIOPS_JAEGER_URL", "http://localhost:16686").rstrip("/")

SERVER_NAME = "aiops-telemetry"
SERVER_VERSION = "0.1.0"

# The closed vocabulary. The model picks from these; it never writes PromQL.
SERVICES = ("gateway", "orders", "inventory")

# Application routes only. /health is operator liveness traffic and /chaos is the
# fault-injection control plane -- which emits 5xx of ITS OWN (501 where
# pool-exhaust is unsupported, 503 when disabled). Session 2 shipped a permanent
# fake error series onto the dashboard by forgetting this exact exclusion, so it
# is compiled into the tool rather than left to the caller to remember.
#
# Prometheus regex matches are fully anchored, so this means "route is exactly
# /health, or starts with /chaos".
APP_ROUTE_EXCLUSION = "/health|/chaos.*"

# --- Storage bounds, from docker-compose.yml -------------------------------
# A tool that accepts an arbitrary lookback silently returns nothing beyond
# these. Clamping loudly is the whole point of knowing them.
PROM_RETENTION_HOURS = 24        # --storage.tsdb.retention.time=24h
JAEGER_TTL_HOURS = 72            # BADGER_SPAN_STORE_TTL=72h

# --- Response budgets ------------------------------------------------------
# The availability requirement, not a context-window nicety. In Session 2 a
# single Jaeger request for 400 traces with full span payloads preceded a Jaeger
# restart that destroyed a completed labelled run's trace evidence. Cause was
# never established (OOMKilled=false, ExitCode=0), which is precisely why the
# caps are conservative: an unexplained failure cannot be ruled out by argument.
POINT_BUDGET = 12                # points returned per series, whatever the window
FETCH_POINT_CAP = 600            # points fetched per series before downsampling
DEFAULT_TRACES = 20
MAX_TRACES = 40                  # 1/10th of the request that preceded the restart
MAX_SPANS_PER_TRACE = 60

# Prometheus is on the same machine; a slow answer means something is wrong
# rather than far away.
PROM_TIMEOUT_S = 10.0
JAEGER_TIMEOUT_S = 15.0
