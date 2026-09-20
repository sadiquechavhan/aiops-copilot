import sys
print('Python working', flush=True)
sys.path.insert(0, 'c:/Users/sadiq/Downloads/New_Project')
print('Path inserted', flush=True)
from aiops_mcp.rag import RunbookRAG
print('Imported', flush=True)
rag = RunbookRAG()
print('RAG created', flush=True)
count = rag.build_index()
print(f'Indexed {count} chunks', flush=True)
result = rag.search('How do I diagnose pool exhaustion in inventory?', k=3)
import json
print(json.dumps(result, indent=2), flush=True)