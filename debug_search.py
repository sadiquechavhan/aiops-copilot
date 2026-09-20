import sys
sys.path.insert(0, '.')

from aiops_mcp.rag import RunbookRAG

rag = RunbookRAG()
print("Building index...")
count = rag.build_index()
print(f"Indexed {count} chunks")

# Check what chunks are being retrieved for a query
result = rag.search("How do I diagnose pool exhaustion in inventory?", k=5)
import json
print(json.dumps(result, indent=2))