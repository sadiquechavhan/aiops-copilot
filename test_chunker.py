import sys
sys.path.insert(0, '.')

from aiops_mcp.rag import RunbookChunker
from pathlib import Path

chunker = RunbookChunker()
chunks = chunker.chunk_file("runbooks/pool-exhaust-inventory.md", base_dir=Path("runbooks"))
for i, chunk in enumerate(chunks):
    print(f"=== Chunk {i+1} ===")
    print(f"Heading: {chunk.heading}")
    print(f"Level: {chunk.heading_level}")
    print(f"Parent: {chunk.parent_heading}")
    print(f"Source: {chunk.source_path}")
    print(f"Text:\n{chunk.text[:200]}...")
    print()