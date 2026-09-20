import sys
sys.path.insert(0, 'c:/Users/sadiq/Downloads/New_Project')
from aiops_mcp.rag import RunbookRAG
import json

rag = RunbookRAG()
count = rag.build_index(force_rebuild=True)
print('Indexed', count, 'chunks')

# Test with k=5 to see more results
result = rag.search('How do I diagnose pool exhaustion in inventory?', k=5)
print(json.dumps(result, indent=2))