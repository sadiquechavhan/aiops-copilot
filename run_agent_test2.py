#!/usr/bin/env python3
"""Test script for the copilot agent - writes to file."""
import sys
sys.path.insert(0, 'c:/Users/sadiq/Downloads/New_Project')

from aiops_mcp.agent import run_agent_from_ground_truth
import json

with open('agent_test_output.txt', 'w') as f:
    print("Testing agent on run-20260809-201431/i1 (inventory latency)...")
    f.write("Testing agent on run-20260809-201431/i1 (inventory latency)...\n")
    result = run_agent_from_ground_truth('run-20260809-201431', 'run-20260809-201431/i1')
    output = json.dumps(result.__dict__, indent=2, default=str)
    print(output)
    f.write(output + "\n\n")

    print("\n\nTesting agent on run-20260809-201431/i3 (pool exhaustion - unknown to runbooks)...")
    f.write("\n\nTesting agent on run-20260809-201431/i3 (pool exhaustion - unknown to runbooks)...\n")
    result2 = run_agent_from_ground_truth('run-20260809-201431', 'run-20260809-201431/i3')
    output2 = json.dumps(result2.__dict__, indent=2, default=str)
    print(output2)
    f.write(output2 + "\n")

print("Test complete, output written to agent_test_output.txt")