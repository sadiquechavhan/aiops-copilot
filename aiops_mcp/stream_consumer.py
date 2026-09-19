"""
Session 8 — Streaming Inference Consumer

Reads metrics from Redpanda (Kafka topic aiops.metrics), maintains sliding window
with frozen baseline, runs z-score detection, emits alerts to ring buffer.

Architecture:
- Single consumer, single thread (volume is ~60 msg/min)
- Sliding window: 60s window, 15s step (matches Prometheus scrape)
- Warm-up: first 120s (8 windows) establish baseline, then FROZEN
- Detection: z-score per (service, metric) using frozen baseline stats
- Alert emission: in-memory ring buffer (last 100 alerts), exposed via MCP tool

Message format (JSON from OTel Collector kafkaexporter):
{
  "resourceMetrics": [{
    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "inventory"}}]},
    "scopeMetrics": [{
      "metrics": [{
        "name": "http.server.request.duration",
        "gauge": {"dataPoints": [{
          "attributes": [{"key": "http.route", "value": {"stringValue": "/reserve"}}],
          "asDouble": 0.487,
          "timeUnixNano": "1725123456789000000"
        }]}
      }]
    }]
  }]
}

We extract: service_name, metric_name, value, timestamp
"""

import json
import math
import signal
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from threading import Thread
from typing import Any, Dict, List, Optional

try:
    from confluent_kafka import Consumer, KafkaError
except ImportError:
    print("confluent-kafka not installed. Run: py -3.12 -m pip install confluent-kafka", file=sys.stderr)
    sys.exit(1)


# =============================================================================
# Configuration
# =============================================================================

KAFKA_BOOTSTRAP = "localhost:19092"  # External port from docker-compose
METRICS_TOPIC = "aiops.metrics"
LOGS_TOPIC = "aiops.logs"
CONSUMER_GROUP = "aiops-stream-detector"

# Windowing
WINDOW_SECONDS = 60       # Sliding window size
STEP_SECONDS = 15         # Step size (matches Prometheus scrape)
WARMUP_SECONDS = 120      # Baseline establishment period (8 windows)

# Detection thresholds (match detect.py)
K_SIGMA = 3.0
MIN_CONSECUTIVE = 2       # >=2 consecutive flagged points = alert

# Floors per metric (from detect.py)
FLOORS = {
    "latency": 0.010,     # seconds (10 ms)
    "error_rate": 0.05,   # req/s
    "error_ratio": 0.5,   # percent
    "db_pool": 0.5,       # connections
}

# Alert ring buffer size
ALERT_BUFFER_SIZE = 100


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class MetricPoint:
    """A single metric data point."""
    timestamp: float      # Unix epoch seconds
    service: str          # gateway, orders, inventory
    metric: str           # latency_p95, error_rate, db_pool_used, etc.
    value: float


@dataclass
class BaselineStats:
    """Welford's online algorithm for mean/variance."""
    mean: float = 0.0
    m2: float = 0.0
    count: int = 0

    def update(self, x: float):
        self.count += 1
        delta = x - self.mean
        self.mean += delta / self.count
        delta2 = x - self.mean
        self.m2 += delta * delta2

    @property
    def variance(self) -> float:
        return self.m2 / (self.count - 1) if self.count > 1 else 0.0

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)

    def sigma_eff(self, floor: float) -> float:
        return max(self.std, floor)


@dataclass
class WindowState:
    """State for one sliding window computation."""
    # Deque of (window_start_timestamp, metric_points_list)
    # Each window aggregates points that fall in [window_start, window_start + WINDOW_SECONDS)
    windows: deque = field(default_factory=lambda: deque(maxlen=WINDOW_SECONDS // STEP_SECONDS + 1))
    
    # Baseline stats per (service, metric) — FROZEN after warmup
    baselines: Dict[tuple, BaselineStats] = field(default_factory=dict)
    baseline_frozen: bool = False
    warmup_start: Optional[float] = None

    # Alert ring buffer
    alerts: deque = field(default_factory=lambda: deque(maxlen=ALERT_BUFFER_SIZE))
    
    # Track consecutive flagged points per signal for persistence rule
    consecutive: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Last alert key to deduplicate
    seen_alert_keys: set = field(default_factory=set)


@dataclass
class Alert:
    """An emitted detection alert."""
    window_start: float
    window_end: float
    service: str
    metric: str
    signal: str           # e.g., "latency:inventory"
    family: str           # latency, errors, saturation
    peak_value: float
    peak_z: float
    baseline_mean: float
    baseline_std: float
    timestamp: float = field(default_factory=time.time)


# =============================================================================
# Metric parsing from OTel Collector kafkaexporter JSON
# =============================================================================

def parse_kafka_metrics_message(message_value: bytes) -> List[MetricPoint]:
    """
    Parse OTel Collector kafkaexporter JSON message into MetricPoints.
    
    The kafkaexporter sends batches of ResourceMetrics. We extract:
    - service.name from resource attributes
    - metric name and value from scopeMetrics
    - timestamp from data point
    """
    try:
        data = json.loads(message_value.decode('utf-8'))
    except json.JSONDecodeError:
        return []
    
    points = []
    
    # Handle both single object and array of resourceMetrics
    resource_metrics = data.get("resourceMetrics", [])
    if isinstance(resource_metrics, dict):
        resource_metrics = [resource_metrics]
    
    for rm in resource_metrics:
        # Extract service.name from resource attributes
        service_name = "unknown"
        resource_attrs = rm.get("resource", {}).get("attributes", [])
        for attr in resource_attrs:
            if attr.get("key") == "service.name":
                service_name = attr.get("value", {}).get("stringValue", "unknown")
                break
        
        scope_metrics = rm.get("scopeMetrics", [])
        for sm in scope_metrics:
            metrics = sm.get("metrics", [])
            for metric in metrics:
                metric_name = metric.get("name", "unknown")
                
                # Handle gauge (single value) and sum (cumulative)
                for dp_key in ["gauge", "sum"]:
                    dp_container = metric.get(dp_key)
                    if not dp_container:
                        continue
                    data_points = dp_container.get("dataPoints", [])
                    for dp in data_points:
                        # Extract value
                        value = None
                        if "asDouble" in dp:
                            value = dp["asDouble"]
                        elif "asInt" in dp:
                            value = float(dp["asInt"])
                        
                        if value is None:
                            continue
                        
                        # Extract timestamp (nanoseconds -> seconds)
                        ts_nano = dp.get("timeUnixNano") or dp.get("startTimeUnixNano")
                        if ts_nano:
                            timestamp = int(ts_nano) / 1e9
                        else:
                            timestamp = time.time()
                        
                        # Normalize metric name for our detection
                        normalized_metric = normalize_metric_name(metric_name)
                        if normalized_metric is None:
                            continue  # Skip internal metrics
                        
                        points.append(MetricPoint(
                            timestamp=timestamp,
                            service=service_name,
                            metric=normalized_metric,
                            value=value
                        ))
    
    return points


def normalize_metric_name(otel_name: str) -> str:
    """Map OTel metric names to our detection metric names.
    
    Only keep golden signals for detection - filter out internal OTel metrics.
    OTel metric names from javaagent:
    """
    # Map of OTel metric names to our internal names
    # Only include actual golden signals we care about
    mapping = {
        # Latency metrics - these are the key ones
        "http.server.request.duration": "latency",
        
        # Error metrics
        "http.server.error.rate": "error_rate",
        
        # DB Pool metrics
        "db.client.connections.used": "db_pool_used",
        "db.client.connections.idle": "db_pool_idle",
        "db.client.connections.max": "db_pool_max",
    }
    
    # Try exact match first
    if otel_name in mapping:
        return mapping[otel_name]
    
    # Filter out ALL internal OTel metrics
    internal_prefixes = [
        "jvm.",
        "process.",
        "otlp.",
        "queueSize",
        "processedSpans",
        "system.cpu",
        "runtime.",
    ]
    for prefix in internal_prefixes:
        if otel_name.startswith(prefix):
            return None  # Skip this metric
    
    return None  # Default: skip unknown metrics


# =============================================================================
# Sliding window aggregation
# =============================================================================

def get_window_start(timestamp: float) -> float:
    """Get the window start timestamp for a given timestamp."""
    return math.floor(timestamp / STEP_SECONDS) * STEP_SECONDS


def aggregate_window(points: List[MetricPoint]) -> Dict[tuple, float]:
    """
    Aggregate points in a window to compute per-(service,metric) values.
    For latency, we'd ideally compute percentiles, but we get pre-aggregated.
    For now, use the mean of points in the window (kafkaexporter sends one per scrape).
    """
    grouped = defaultdict(list)
    for p in points:
        grouped[(p.service, p.metric)].append(p.value)
    
    result = {}
    for key, values in grouped.items():
        # For our volume, there's typically 1 point per (service,metric) per 15s
        # Use mean (or the single value)
        result[key] = sum(values) / len(values)
    
    return result


# =============================================================================
# Detection logic (adapted from detect.py)
# =============================================================================

def detect_zscore(
    value: float,
    baseline: BaselineStats,
    metric_type: str,
    direction: str = "rise"
) -> tuple:
    """
    Compute z-score and whether it exceeds threshold.
    Returns: (z_score, is_anomaly)
    """
    floor = FLOORS.get(metric_type, 0.01)
    sigma_eff = baseline.sigma_eff(floor)
    
    if sigma_eff == 0:
        return 0.0, False
    
    if direction == "rise":
        deviation = value - baseline.mean
    else:  # fall
        deviation = baseline.mean - value
    
    z_score = deviation / sigma_eff
    is_anomaly = z_score > K_SIGMA
    
    return z_score, is_anomaly


def get_metric_family_and_direction(metric: str) -> tuple:
    """Determine metric family and expected anomaly direction."""
    if metric.startswith("latency"):
        return "latency", "rise"
    elif metric.startswith("error_rate") or metric.startswith("error_ratio"):
        return "errors", "rise"
    elif metric.startswith("db_pool_used"):
        return "saturation", "rise"
    elif metric.startswith("db_pool_idle"):
        return "saturation", "fall"
    elif metric.startswith("request_rate"):
        return "traffic", "rise"
    return "unknown", "rise"


# =============================================================================
# Main consumer class
# =============================================================================

class StreamingDetector:
    def __init__(self):
        self.state = WindowState()
        self.consumer = None
        self.running = False
        
    def create_consumer(self):
        """Create Kafka consumer."""
        conf = {
            'bootstrap.servers': KAFKA_BOOTSTRAP,
            'group.id': CONSUMER_GROUP,
            'auto.offset.reset': 'latest',  # Only process new messages
            'enable.auto.commit': True,
            'auto.commit.interval.ms': 5000,
        }
        self.consumer = Consumer(conf)
        # Only subscribe to metrics topic for now (logs topic doesn't exist yet)
        self.consumer.subscribe([METRICS_TOPIC])
        
    def process_message(self, msg):
        """Process a single Kafka message."""
        if msg.error():
            if msg.error().code() != KafkaError._PARTITION_EOF:
                print(f"Kafka error: {msg.error()}", file=sys.stderr)
            return
        
        topic = msg.topic()
        # Only process metrics topic
        if topic != METRICS_TOPIC:
            return
        
        try:
            points = parse_kafka_metrics_message(msg.value())
        except UnicodeDecodeError:
            # Skip non-UTF-8 messages (e.g., protobuf)
            return
        
        if not points:
            return
        
        # Group points by window
        window_points = defaultdict(list)
        for p in points:
            window_start = get_window_start(p.timestamp)
            window_points[window_start].append(p)
        
        for window_start, pts in window_points.items():
            self.process_window(window_start, pts)
    
    def process_window(self, window_start: float, points: List[MetricPoint]):
        """Process all points in a single window."""
        window_end = window_start + WINDOW_SECONDS
        
        # Aggregate points in this window
        aggregated = aggregate_window(points)
        
        # Initialize warmup timer
        if self.state.warmup_start is None:
            self.state.warmup_start = window_start
        
        # Check if we're still in warmup
        in_warmup = (window_start - self.state.warmup_start) < WARMUP_SECONDS
        
        # Update baselines or detect
        for (service, metric), value in aggregated.items():
            baseline_key = (service, metric)
            
            if in_warmup:
                # Warmup: update baseline stats
                if baseline_key not in self.state.baselines:
                    self.state.baselines[baseline_key] = BaselineStats()
                self.state.baselines[baseline_key].update(value)
            else:
                # Freeze baseline on first post-warmup window
                if not self.state.baseline_frozen:
                    self.state.baseline_frozen = True
                    print(f"[Baseline frozen at {time.strftime('%H:%M:%S', time.localtime(window_start))}]", flush=True)
                    for k, bs in self.state.baselines.items():
                        print(f"  {k}: mean={bs.mean:.4f}, std={bs.std:.4f}, n={bs.count}", flush=True)
                
                # Detection
                if baseline_key in self.state.baselines:
                    baseline = self.state.baselines[baseline_key]
                    family, direction = get_metric_family_and_direction(metric)
                    metric_type = metric.split("_")[0]  # latency, error, db_pool
                    
                    z_score, is_anomaly = detect_zscore(value, baseline, metric_type, direction)
                    
                    signal_key = f"{family}:{service}"
                    
                    if is_anomaly:
                        self.state.consecutive[signal_key] += 1
                        
                        if self.state.consecutive[signal_key] >= MIN_CONSECUTIVE:
                            # Check deduplication
                            alert_key = (window_start, signal_key)
                            if alert_key not in self.state.seen_alert_keys:
                                self.state.seen_alert_keys.add(alert_key)
                                self.emit_alert(Alert(
                                    window_start=window_start,
                                    window_end=window_end,
                                    service=service,
                                    metric=metric,
                                    signal=signal_key,
                                    family=family,
                                    peak_value=value,
                                    peak_z=z_score,
                                    baseline_mean=baseline.mean,
                                    baseline_std=baseline.std,
                                ))
                    else:
                        self.state.consecutive[signal_key] = 0
        
        # Store window for reference
        self.state.windows.append((window_start, aggregated))
    
    def emit_alert(self, alert: Alert):
        """Emit an alert to the ring buffer."""
        self.state.alerts.append(alert)
        print(f"[ALERT] {time.strftime('%H:%M:%S', time.localtime(alert.window_start))} "
              f"{alert.signal} peak={alert.peak_value:.4f} z={alert.peak_z:.1f} "
              f"(baseline μ={alert.baseline_mean:.4f} σ={alert.baseline_std:.4f})", flush=True)
    
    def get_recent_alerts(self, since: float = 0, limit: int = 50) -> List[Alert]:
        """Get alerts since a given timestamp (for MCP tool)."""
        alerts = [a for a in self.state.alerts if a.timestamp >= since]
        return alerts[-limit:]
    
    def run(self):
        """Main consumer loop."""
        self.create_consumer()
        self.running = True
        
        print(f"[Streaming Detector] Starting consumer on {METRICS_TOPIC}, {LOGS_TOPIC}")
        print(f"[Streaming Detector] Window={WINDOW_SECONDS}s, Step={STEP_SECONDS}s, Warmup={WARMUP_SECONDS}s")
        print(f"[Streaming Detector] Waiting for messages...", flush=True)
        
        try:
            while self.running:
                msg = self.consumer.poll(1.0)
                if msg is None:
                    continue
                self.process_message(msg)
        except KeyboardInterrupt:
            print("\n[Streaming Detector] Shutting down...")
        finally:
            self.consumer.close()
    
    def stop(self):
        self.running = False


# Singleton for MCP tool access
_detector_instance = None

def get_streaming_detector() -> StreamingDetector:
    """Get or create the singleton streaming detector instance."""
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = StreamingDetector()
    return _detector_instance


# =============================================================================
# CLI entry point
# =============================================================================

def main():
    detector = StreamingDetector()
    
    def signal_handler(sig, frame):
        detector.stop()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    detector.run()


if __name__ == "__main__":
    main()