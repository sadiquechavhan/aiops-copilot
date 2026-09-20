#!/usr/bin/env python3
"""
Integration test for Session 9 closed-loop automation.
Tests the MCP tools and the full agent flow with remediation.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from aiops_mcp.remediation import (
    disable_chaos, suggest_remediation, execute_remediation,
    snapshot_signal, check_recovery
)


def test_disable_chaos_live():
    """Test disabling chaos on a live service."""
    print("\n=== Integration Test: disable_chaos (live) ===")
    
    # First, inject a chaos fault to have something to disable
    import urllib.request
    
    # Inject latency on inventory
    url = "http://localhost:8082/chaos/latency?ms=200&jitter=20"
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"  Injected latency on inventory: {resp.status}")
    
    # Verify it's active
    status_url = "http://localhost:8082/chaos"
    with urllib.request.urlopen(status_url, timeout=10) as resp:
        status = json.loads(resp.read().decode())
        print(f"  Chaos status after inject: active={status.get('active')}, latency={status.get('latency_ms')}ms")
        assert status.get('active') == True
        assert status.get('latency_ms') == 200
    
    # Now disable it via our tool
    result = disable_chaos("inventory")
    print(f"  disable_chaos result: {result}")
    
    assert result.get("success"), f"disable_chaos failed: {result}"
    
    # Verify it's cleared
    with urllib.request.urlopen(status_url, timeout=10) as resp:
        status = json.loads(resp.read().decode())
        print(f"  Chaos status after disable: active={status.get('active')}, latency={status.get('latency_ms')}ms")
        assert status.get('active') == False
        assert status.get('latency_ms') == 0
    
    print("  ✓ disable_chaos works on live service")


def test_snapshot_and_recovery():
    """Test snapshot and recovery verification with live metrics."""
    print("\n=== Integration Test: snapshot + recovery check ===")
    
    # Inject latency on gateway
    import urllib.request
    url = "http://localhost:8080/chaos/latency?ms=300&jitter=30"
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"  Injected latency on gateway: {resp.status}")
    
    # Wait a moment for metrics to update
    import time
    time.sleep(5)
    
    # Snapshot during incident
    during = snapshot_signal("latency:gateway", "2m")
    print(f"  During incident: {during}")
    
    # Disable chaos
    result = disable_chaos("gateway")
    print(f"  disable_chaos: {result.get('success')}")
    
    # Wait for recovery
    time.sleep(10)
    
    # Snapshot after remediation
    after = snapshot_signal("latency:gateway", "2m")
    print(f"  After remediation: {after}")
    
    # Baseline (approximate normal)
    baseline = {"latest": 0.05, "p95": 0.05}  # ~50ms normal
    
    # Check recovery
    recovery = check_recovery(during, after, baseline, tolerance=0.3)
    print(f"  Recovery check: {recovery}")
    
    print("  ✓ Snapshot and recovery check works")


def test_mcp_tools_direct():
    """Test calling the MCP tools directly."""
    print("\n=== Integration Test: MCP tools directly ===")
    
    # Test suggest_remediation
    result = suggest_remediation("latency:inventory", "inventory")
    print(f"  suggest_remediation: {result}")
    assert result["action"] == "disable_chaos"
    
    print("  ✓ MCP tools work directly")


def test_full_remediation_flow():
    """Test the full execute_remediation flow (with auto-approval for testing)."""
    print("\n=== Integration Test: Full remediation flow ===")
    
    import urllib.request
    
    # Inject error rate on orders
    url = "http://localhost:8081/chaos/error-rate?pct=15"
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"  Injected error rate on orders: {resp.status}")
    
    # Wait for metrics
    import time
    time.sleep(5)
    
    # Execute remediation with auto-approval (for testing)
    # Use a fake run_id for this test
    run_id = "run-test-closed-loop"
    incident_id = "run-test-closed-loop/i2"
    
    record = execute_remediation(
        run_id=run_id,
        incident_id=incident_id,
        action="disable_chaos",
        args={"service": "orders"},
        anomaly_signal="errors:orders",
        approval_mode="auto",
        approval_policy="disable_chaos_known_faults",
        baseline_metrics={"latest": 0.0, "p95": 0.0}
    )
    
    print(f"  Remediation record status: {record.status}")
    print(f"  Approved by: {record.approved_by}")
    print(f"  Verification: {record.verification}")
    
    assert record.status in ("verified", "recovery_not_verified"), f"Unexpected status: {record.status}"
    assert record.approved_by == "auto:disable_chaos_known_faults"
    
    print("  ✓ Full remediation flow works")


def main():
    print("Running Session 9 integration tests...")
    print("Requires all services to be running (docker compose up)")
    
    try:
        test_mcp_tools_direct()
        test_disable_chaos_live()
        test_snapshot_and_recovery()
        test_full_remediation_flow()
        
        print("\n=== All integration tests passed ===")
        
    except Exception as e:
        print(f"\n=== Test failed: {e} ===")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()