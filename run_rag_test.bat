@echo off
cd /d "c:\Users\sadiq\Downloads\New_Project"
py -3.12 -c "import sys; sys.path.insert(0, '.'); from aiops_mcp.rag import RunbookRAG; rag = RunbookRAG(); print('Building index...'); count = rag.build_index(); print('Indexed', count, 'chunks'); result = rag.search('How do I diagnose pool exhaustion?', k=3); import json; print(json.dumps(result, indent=2))"