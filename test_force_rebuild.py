import sys
sys.path.insert(0, 'c:/Users/sadiq/Downloads/New_Project')
from aiops_mcp.rag import RunbookRAG
import json

rag = RunbookRAG()
count = rag.build_index(force_rebuild=True)
print('Force rebuilt index with', count, 'chunks')
result = rag.search('How do I diagnose pool exhaustion in inventory?', k=3)
print(json.dumps(result, indent=2))