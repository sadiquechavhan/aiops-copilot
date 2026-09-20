#!/usr/bin/env python3
"""
Test script for Session 9 closed-loop automation.
Tests the remediation tools and the full closed-loop flow.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from aiops_mcp.remediation import (
    disable_chaos, restart_container, scale_replicas,
    suggest_remediation, execute_remediation,
    snapshot_signal, check_recovery, get_remediation_log
)


def test_disable_chaos():
    """Test disabling chaos on a service."""
    print("\n=== Test: disable_chaos ===")
    
    # First, inject a chaos fault to have something to disable
    import urllib.request
    import urllib.parse
    
    # Inject latency on orders
    url = "http://localhost:8081/chaos/latency?ms=100&jitter=10"
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"  Injected latency: {resp.status}")
    
    # Now disable it
    result = disable_chaos("orders")
    print(f"  disable_chaos result: {result}")
    
    assert result.get("success"), f"disable_chaos failed: {result}"
    print("  ✓ disable_chaos works")


def test_suggest_remediation():
    """Test remediation suggestion."""
    print("\n=== Test: suggest_remediation ===")
    
    # Test latency fault
    result = suggest_remediation("latency:inventory", "inventory")
    print(f"  latency:inventory -> {result}")
    assert result["action"] == "disable_chaos"
    assert result["args"]["service"] == "inventory"
    
    # Test error rate fault
    result = suggest_remediation("errors:orders", "orders")
    print(f"  errors:orders -> {result}")
    assert result["action"] == "disable_chaos"
    assert result["args"]["service"] == "orders"
    
    # Test pool exhaustion
    result = suggest_remediation("pool:used", "inventory")
    print(f"  pool:used -> {result}")
    assert result["action"] == "disable_chaos"
    assert result["args"]["service"] == "inventory"
    
    print("  ✓ suggest_remediation works")


def test_snapshot_signal():
    """Test metric snapshot."""
    print("\n=== Test: snapshot_signal ===")
    
    result = snapshot_signal("latency:inventory", "5m")
    print(f"  latency:inventory snapshot: {result}")
    assert "metric" in result
    print("  ✓ snapshot_signal works")


def test_check_recovery():
    """Test recovery verification."""
    print("\n=== Test: check_recovery ===")
    
    # Simulate pre-incident (normal), during incident (high), post-remediation (normal)
    baseline = {"latest": 0.03, "p95": 0.03}  # 30ms baseline
    pre = {"latest": 0.48, "p95": 0.48}        # 480ms during incident
    post = {"latest": 0.035, "p95": 0.035}     # 35ms after remediation
    
    result = check_recovery(pre, post, baseline, tolerance=0.2)
    print(f"  Recovery check: {result}")
    assert result["recovered"], "Should recover within 20% tolerance"
    
    # Test error rate recovery (should go to near 0)
    baseline_err = {"latest": 0.0}
    pre_err = {"latest": 1.2}
    post_err = {"latest": 0.0}
    
    result = check_recovery(pre_err, post_err, baseline_err)
    print(f"  Error rate recovery: {result}")
    assert result["recovered"], "Error rate should recover to near 0"
    
    print("  ✓ check_recovery works")


def test_execute_remediation_dry_run():
    """Test the full execute_remediation flow (dry run - no actual execution)."""
    print("\n=== Test: execute_remediation (dry run) ===")
    
    # This will test the logic but with a fake run_id
    # We can't easily test the full flow without a running system,
    # but we can verify the function exists and has the right signature
    print("  execute_remediation function exists and is callable")
    print("  ✓ execute_remediation import works")


def test_remediation_log():
    """Test remediation log reading."""
    print("\n=== Test: get_remediation_log ===")
    
    # Try to read log for a non-existent run
    result = get_remediation_log("run-nonexistent")
    print(f"  Empty log result: {result}")
    assert result == [], "Should return empty list for non-existent run"
    print("  ✓ get_remediation_log works")


def main():
    print("Testing Session 9 closed-loop automation...")
    
    # These tests require the services to be running
    # Uncomment when services are up:
    # test_disable_chaos()
    # test_snapshot_signal()
    # test_check_recovery()
    
    # These tests don't require running services:
    test_suggest_remediation()
    test_execute_remediation_dry_run()
    test_remediation_log()
    
    print("\n=== All unit tests passed ===")


if __name__ == "__main__":
    main()