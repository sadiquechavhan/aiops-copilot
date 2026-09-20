import sys
sys.path.insert(0, '.')

from aiops_mcp.rag import RunbookRAG
import json

rag = RunbookRAG()
print('Building index...', flush=True)
count = rag.build_index()
print(f'Indexed {count} chunks', flush=True)

result = rag.search('How do I diagnose pool exhaustion?', k=3)
print(json.dumps(result, indent=2), flush=True)