import sys
import subprocess
import os
import json

ROOT = 'c:/Users/sadiq/Downloads/New_Project'
mcp_process = subprocess.Popen(
    ['python', '-m', 'aiops_mcp.server'],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
    cwd=ROOT,
    env={**os.environ, 'PYTHONIOENCODING': 'utf-8',
         'AIOPS_PROMETHEUS_URL': 'http://localhost:9090',
         'AIOPS_JAEGER_URL': 'http://localhost:16686'}
)
print(f'Process started: {mcp_process.pid}')

request = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1.0'}}}
line = json.dumps(request) + '\n'
mcp_process.stdin.write(line)
mcp_process.stdin.flush()
print('Request sent')

response_line = mcp_process.stdout.readline()
print(f'Response: {response_line}')

stderr = mcp_process.stderr.read()
print(f'Stderr: {stderr}')

mcp_process.terminate()
mcp_process.wait(timeout=5)