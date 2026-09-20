#!/usr/bin/env python3
"""Test script for the copilot agent."""
import sys
sys.path.insert(0, 'c:/Users/sadiq/Downloads/New_Project')

from aiops_mcp.agent import run_agent_from_ground_truth
import json

print("Testing agent on run-20260809-201431/i1 (inventory latency)...")
result = run_agent_from_ground_truth('run-20260809-201431', 'run-20260809-201431/i1')
print(json.dumps(result.__dict__, indent=2, default=str))

print("\n\nTesting agent on run-20260809-201431/i3 (pool exhaustion - unknown to runbooks)...")
result2 = run_agent_from_ground_truth('run-20260809-201431', 'run-20260809-201431/i3')
print(json.dumps(result2.__dict__, indent=2, default=str))