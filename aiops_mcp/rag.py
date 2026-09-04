"""
RAG module for runbook retrieval.

Heading-aware chunking, local embeddings (sentence-transformers all-MiniLM-L6-v2),
Chroma vector store with disk persistence, and BM25 hybrid search.
"""

import os
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict

try:
    from markdown_it import MarkdownIt
    from markdown_it.tree import SyntaxTreeNode
except ImportError:
    MarkdownIt = None
    SyntaxTreeNode = None

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None

try:
    import chromadb
    from chromadb.config import Settings
except ImportError:
    chromadb = None
    Settings = None

try:
    from rank_bm25 import BM25Okapi
except ImportError:
    BM25Okapi = None


@dataclass
class Chunk:
    """A chunk of runbook content with metadata."""
    text: str
    source_path: str
    heading: str
    heading_level: int
    parent_heading: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Chroma doesn't accept None in metadata - convert to empty string
        if d["parent_heading"] is None:
            d["parent_heading"] = ""
        return d


class RunbookChunker:
    """Heading-aware chunking for markdown runbooks."""
    
    def __init__(self):
        if MarkdownIt is None:
            raise ImportError("markdown-it-py not installed. Run: pip install markdown-it-py")
        self.md = MarkdownIt()
    
    def chunk_file(self, file_path: str, base_dir: Optional[Path] = None) -> List[Chunk]:
        """Chunk a markdown file by heading levels (h2 and h3)."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Runbook not found: {file_path}")
        
        # Use base_dir for relative path, or fallback to cwd
        if base_dir:
            try:
                relative_path = path.relative_to(base_dir)
            except ValueError:
                relative_path = path.relative_to(Path.cwd())
        else:
            relative_path = path.relative_to(Path.cwd())
        
        # Always prepend "runbooks/" for consistency
        source_path = f"runbooks/{relative_path}"
        
        content = path.read_text(encoding="utf-8")
        tokens = self.md.parse(content)
        
        chunks = []
        current_heading = None
        current_level = 0
        current_parent = None
        current_content = []
        heading_stack = []  # Stack of (level, heading_text)
        
        i = 0
        while i < len(tokens):
            token = tokens[i]
            
            # Track code blocks to avoid splitting inside them
            if token.type == "fence" and token.tag == "code":
                if current_heading is not None:
                    current_content.append(f"```{token.info}\n{token.content}\n```")
                i += 1
                continue
            
            # Check for heading tokens
            if token.type == "heading_open":
                # Save previous chunk if we have content
                if current_content and current_heading:
                    chunk_text = "\n".join(current_content).strip()
                    if chunk_text:
                        chunks.append(Chunk(
                            text=chunk_text,
                            source_path=source_path,
                            heading=current_heading,
                            heading_level=current_level,
                            parent_heading=current_parent
                        ))
                
                # Start new chunk
                current_level = int(token.tag[1])  # h1, h2, h3 -> 1, 2, 3
                current_heading = None
                current_content = []
                
                # Get the heading text from next inline token
                if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                    current_heading = tokens[i + 1].content
                    
                    # For h1 headings (runbook titles), track as parent but don't create chunk
                    if current_level == 1:
                        # Track h1 as parent for subsequent h2/h3
                        heading_stack.append((1, current_heading))
                        current_content = []
                        current_heading = None
                        i += 2  # Skip heading_open and inline
                        continue
                    
                    # For h2 and h3, add to content and track parent
                    current_content.append(f"{'#' * current_level} {current_heading}")
                    
                    # Update heading stack and find parent
                    # Pop stack until we find a level < current_level
                    while heading_stack and heading_stack[-1][0] >= current_level:
                        heading_stack.pop()
                    current_parent = heading_stack[-1][1] if heading_stack else ""
                    heading_stack.append((current_level, current_heading))
                    
                    i += 2  # Skip heading_open and inline
                    continue
                    
            elif token.type == "heading_close":
                # Heading closed, continue
                pass
            else:
                # Collect content tokens as text
                if current_heading is not None:
                    text = self._token_to_text(token)
                    if text:
                        current_content.append(text)
            
            i += 1
        
        # Don't forget the last chunk
        if current_content and current_heading:
            chunk_text = "\n".join(current_content).strip()
            if chunk_text:
                chunks.append(Chunk(
                    text=chunk_text,
                    source_path=source_path,
                    heading=current_heading,
                    heading_level=current_level,
                    parent_heading=current_parent
                ))
        
        # If no chunks found (no h2/h3 headings), return whole file as one chunk
        if not chunks:
            chunks.append(Chunk(
                text=content.strip(),
                source_path=source_path,
                heading=path.stem,
                heading_level=1,
                parent_heading=""
            ))
        
        return chunks
    
    def _find_parent_heading(self, tokens: List, current_token_idx: int, target_level: int) -> Optional[str]:
        """Find the nearest parent heading of target_level before current position."""
        return None
    
    def _token_to_text(self, token) -> str:
        """Convert a markdown token to readable text."""
        if token.type == "inline":
            return token.content
        elif token.type == "fence" and token.tag == "code":
            return f"```{token.info}\n{token.content}\n```"
        elif token.type in ("paragraph_open", "paragraph_close", "heading_open", "heading_close"):
            return ""
        elif token.type == "bullet_list_open" or token.type == "ordered_list_open":
            return ""
        elif token.type == "list_item_open":
            return "- "
        elif token.type == "list_item_close":
            return "\n"
        elif token.type == "text":
            return token.content
        return ""


class RunbookEmbedder:
    """Local embeddings using sentence-transformers all-MiniLM-L6-v2."""
    
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        if SentenceTransformer is None:
            raise ImportError("sentence-transformers not installed. Run: pip install sentence-transformers")
        self.model = SentenceTransformer(model_name)
        self.dimension = self.model.get_sentence_embedding_dimension()
    
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for a list of texts."""
        embeddings = self.model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return embeddings.tolist()
    
    def embed_single(self, text: str) -> List[float]:
        """Generate embedding for a single text."""
        return self.embed([text])[0]


class RunbookVectorStore:
    """Chroma vector store for runbook chunks with BM25 hybrid search."""
    
    def __init__(self, persist_dir: str = "./chroma_db", collection_name: str = "runbooks"):
        if chromadb is None:
            raise ImportError("chromadb not installed. Run: pip install chromadb")
        
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        
        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False)
        )
        self.collection_name = collection_name
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"}
        )
        
        # BM25 state
        self._bm25 = None
        self._bm25_corpus = None
        self._bm25_doc_ids = None
    
    def add_chunks(self, chunks: List[Chunk], embeddings: List[List[float]]):
        """Add chunks with their embeddings to the store."""
        ids = [f"{c.source_path}::{c.heading}::{i}" for i, c in enumerate(chunks)]
        documents = [c.text for c in chunks]
        metadatas = [c.to_dict() for c in chunks]
        
        self.collection.add(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas
        )
        
        # Build BM25 index
        if BM25Okapi is not None:
            self._build_bm25_index(ids, documents)
    
    def _build_bm25_index(self, ids: List[str], documents: List[str]):
        """Build BM25 index from documents."""
        tokenized_corpus = [self._tokenize(doc) for doc in documents]
        self._bm25 = BM25Okapi(tokenized_corpus)
        self._bm25_corpus = tokenized_corpus
        self._bm25_doc_ids = ids
    
    def _tokenize(self, text: str) -> List[str]:
        """Simple tokenization for BM25."""
        # Lowercase, split on non-alphanumeric, filter short tokens
        tokens = re.findall(r'\b\w+\b', text.lower())
        return [t for t in tokens if len(t) > 2]
    
    def search(self, query_embedding: List[float], k: int = 3) -> List[Dict[str, Any]]:
        """Search for similar chunks using dense retrieval."""
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=k,
            include=["documents", "metadatas", "distances"]
        )
        
        if not results["documents"] or not results["documents"][0]:
            return []
        
        chunks = []
        for i in range(len(results["documents"][0])):
            metadata = results["metadatas"][0][i]
            # Convert distance to similarity score (cosine: 0 = identical, 2 = opposite)
            distance = results["distances"][0][i]
            score = 1.0 - (distance / 2.0)  # Normalize to 0-1 where 1 = identical
            
            chunks.append({
                "text": results["documents"][0][i],
                "source_path": metadata.get("source_path", ""),
                "heading": metadata.get("heading", ""),
                "heading_level": metadata.get("heading_level", 1),
                "parent_heading": metadata.get("parent_heading"),
                "score": round(score, 4)
            })
        
        return chunks
    
    def hybrid_search(self, query: str, query_embedding: List[float], k: int = 3, alpha: float = 0.5) -> List[Dict[str, Any]]:
        """
        Hybrid search combining dense and BM25 sparse retrieval.
        
        Args:
            query: Original query text
            query_embedding: Dense embedding of query
            k: Number of results to return
            alpha: Weight for dense score (1-alpha for BM25). 0.5 = equal weight.
        
        Returns:
            List of result chunks with combined scores
        """
        # Get dense results (fetch more to have candidates for reranking)
        dense_results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(k * 3, self.collection.count()),
            include=["documents", "metadatas", "distances"]
        )
        
        if not dense_results["documents"] or not dense_results["documents"][0]:
            return []
        
        # Get BM25 scores for same documents
        bm25_scores = {}
        if self._bm25 is not None and self._bm25_doc_ids is not None:
            query_tokens = self._tokenize(query)
            bm25_scores_list = self._bm25.get_scores(query_tokens)
            for doc_id, score in zip(self._bm25_doc_ids, bm25_scores_list):
                bm25_scores[doc_id] = score
        
        # Normalize BM25 scores to 0-1 range
        if bm25_scores:
            max_bm25 = max(bm25_scores.values())
            min_bm25 = min(bm25_scores.values())
            if max_bm25 > min_bm25:
                for doc_id in bm25_scores:
                    bm25_scores[doc_id] = (bm25_scores[doc_id] - min_bm25) / (max_bm25 - min_bm25)
            else:
                for doc_id in bm25_scores:
                    bm25_scores[doc_id] = 1.0
        
        # Combine scores
        chunks = []
        for i in range(len(dense_results["documents"][0])):
            metadata = dense_results["metadatas"][0][i]
            doc_id = dense_results["ids"][0][i]
            
            # Dense score (cosine similarity)
            distance = dense_results["distances"][0][i]
            dense_score = 1.0 - (distance / 2.0)
            
            # BM25 score
            bm25_score = bm25_scores.get(doc_id, 0.0)
            
            # Combined score
            combined_score = alpha * dense_score + (1.0 - alpha) * bm25_score
            
            chunks.append({
                "text": dense_results["documents"][0][i],
                "source_path": metadata.get("source_path", ""),
                "heading": metadata.get("heading", ""),
                "heading_level": metadata.get("heading_level", 1),
                "parent_heading": metadata.get("parent_heading"),
                "score": round(combined_score, 4),
                "dense_score": round(dense_score, 4),
                "bm25_score": round(bm25_score, 4)
            })
        
        # Sort by combined score descending
        chunks.sort(key=lambda x: x["score"], reverse=True)
        
        return chunks[:k]
    
    def count(self) -> int:
        """Return number of chunks in the collection."""
        return self.collection.count()
    
    def reset(self):
        """Delete and recreate the collection."""
        self.client.delete_collection(self.collection_name)
        self.collection = self.client.create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"}
        )
        self._bm25 = None
        self._bm25_corpus = None
        self._bm25_doc_ids = None


class RunbookRAG:
    """Main RAG interface for runbook retrieval."""
    
    def __init__(self, runbooks_dir: str = "./runbooks", persist_dir: str = None):
        self.runbooks_dir = Path(runbooks_dir)
        self.chunker = RunbookChunker()
        self.embedder = RunbookEmbedder()
        # Use absolute path for chroma_db to avoid working directory issues
        if persist_dir is None:
            persist_dir = str(self.runbooks_dir.parent / "aiops_mcp" / "chroma_db")
        self.vector_store = RunbookVectorStore(persist_dir)
        self._indexed = False
    
    def build_index(self, force_rebuild: bool = False) -> int:
        """Build the vector index from all runbook files."""
        if self._indexed and not force_rebuild:
            return self.vector_store.count()
        
        # Find all markdown files
        md_files = list(self.runbooks_dir.glob("*.md"))
        if not md_files:
            raise ValueError(f"No markdown files found in {self.runbooks_dir}")
        
        all_chunks = []
        for md_file in md_files:
            chunks = self.chunker.chunk_file(str(md_file), base_dir=self.runbooks_dir)
            all_chunks.extend(chunks)
        
        if not all_chunks:
            raise ValueError("No chunks generated from runbooks")
        
        # Generate embeddings
        texts = [c.text for c in all_chunks]
        embeddings = self.embedder.embed(texts)
        
        # Store in vector DB
        if force_rebuild:
            self.vector_store.reset()
        self.vector_store.add_chunks(all_chunks, embeddings)
        
        self._indexed = True
        return len(all_chunks)
    
    def search(self, query: str, k: int = 3) -> Dict[str, Any]:
        """Search runbooks for relevant chunks using hybrid dense+BM25 search."""
        if not self._indexed:
            self.build_index()
        
        query_embedding = self.embedder.embed_single(query)
        # Use hybrid search favoring dense (semantic) over BM25 (keyword)
        results = self.vector_store.hybrid_search(query, query_embedding, k=k, alpha=0.8)
        
        if not results:
            return {
                "status": "no_match",
                "message": "No relevant runbook found for query.",
                "top_candidates": []
            }
        
        top_score = results[0]["score"]
        if top_score < 0.3:
            return {
                "status": "no_match",
                "message": f"No relevant runbook found for query. Top score: {top_score:.2f}",
                "top_candidates": results
            }
        
        return {
            "status": "success",
            "results": results
        }


def search_runbooks(query: str, k: int = 3) -> Dict[str, Any]:
    """
    MCP tool entry point for searching runbooks.
    
    Returns:
        {
            "status": "success" | "no_match",
            "results": [...] (if success),
            "message": "...",
            "top_candidates": [...] (if no_match)
        }
    """
    rag = RunbookRAG()
    return rag.search(query, k=k)


if __name__ == "__main__":
    # Quick test
    import sys
    query = sys.argv[1] if len(sys.argv) > 1 else "How do I diagnose pool exhaustion?"
    rag = RunbookRAG()
    print(f"Building index...")
    count = rag.build_index()
    print(f"Indexed {count} chunks")
    print(f"\nSearching: {query}")
    result = rag.search(query, k=3)
    print(json.dumps(result, indent=2))