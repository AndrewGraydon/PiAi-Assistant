"""
ChromaDB vector memory store for PiAi Assistant.

Provides semantic retrieval of past conversation snippets and memory hints.
ChromaDB uses SQLite internally (no separate server needed).

All operations happen in the single pipeline thread — no concurrent access,
so SQLite locking is not an issue.

ChromaDB cosine distance notes:
  - Distance range: [0, 2] (0 = identical, 2 = opposite)
  - We convert to similarity: 1 - dist/2  → range [0, 1]
  - score_threshold in config is applied after this conversion

memory_hint entries (from CAAL pattern):
  - Stored as plain text chunks: "key: value"
  - Expire based on TTL stored in metadata
  - Retrieved and injected as context like any other chunk
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from src.memory.embedder import Embedder

log = logging.getLogger(__name__)


class MemoryStore:
    def __init__(
        self,
        db_path: str,
        collection_name: str,
        embedder: Embedder,
        top_k: int,
        score_threshold: float,
    ) -> None:
        self.embedder = embedder
        self.top_k = top_k
        self.score_threshold = score_threshold

        try:
            import chromadb
        except ImportError as e:
            raise ImportError(
                "chromadb not installed. Run: pip install chromadb"
            ) from e

        self._client = chromadb.PersistentClient(path=db_path)
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        log.info(
            "ChromaDB loaded: %d documents in collection '%s' at %s",
            self._collection.count(), collection_name, db_path,
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, text: str) -> List[str]:
        """
        Retrieve the top-k most semantically similar chunks.
        Filters out chunks below score_threshold.
        Returns a list of plain-text strings suitable for LLM context injection.
        """
        count = self._collection.count()
        if count == 0:
            return []

        embedding = self.embedder.embed(text)
        n = min(self.top_k, count)

        try:
            results = self._collection.query(
                query_embeddings=[embedding],
                n_results=n,
                include=["documents", "distances", "metadatas"],
            )
        except Exception as e:
            log.warning("ChromaDB query error: %s", e)
            return []

        chunks: List[str] = []
        for doc, dist, meta in zip(
            results["documents"][0],
            results["distances"][0],
            results["metadatas"][0],
        ):
            # Filter expired hints
            expires_at = (meta or {}).get("expires_at")
            if expires_at and time.time() > expires_at:
                log.debug("Skipping expired memory chunk")
                continue

            # Convert cosine distance [0,2] to similarity [0,1]
            similarity = 1.0 - (dist / 2.0)
            if similarity >= self.score_threshold:
                chunks.append(doc)
                log.debug("Memory chunk (sim=%.3f): %r", similarity, doc[:60])

        return chunks

    # ------------------------------------------------------------------
    # Add / store
    # ------------------------------------------------------------------

    def add(self, text: str, metadata: Optional[Dict] = None) -> None:
        """
        Store a text chunk (e.g. conversation summary) in the memory store.
        """
        self._store(text, metadata or {})

    def add_hint(self, key: str, value: str, ttl_s: int = 604800) -> None:
        """
        Store a structured key/value memory hint (from CAAL memory_hint pattern).
        The hint is stored as "{key}: {value}" with a TTL-based expiry.

        ttl_s = 0 means no expiry.
        """
        text = f"{key}: {value}"
        expires_at = (time.time() + ttl_s) if ttl_s > 0 else None
        meta: Dict = {
            "type": "hint",
            "key": key,
            "stored_at": time.time(),
        }
        if expires_at:
            meta["expires_at"] = expires_at

        self._store(text, meta)
        log.debug("Memory hint stored: %s = %r (ttl=%ds)", key, value, ttl_s)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _store(self, text: str, metadata: Dict) -> None:
        embedding = self.embedder.embed(text)
        doc_id = f"doc_{int(time.time() * 1000)}"
        if "timestamp" not in metadata:
            metadata["timestamp"] = time.time()

        try:
            self._collection.add(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[text],
                metadatas=[metadata],
            )
        except Exception as e:
            log.warning("ChromaDB add error: %s", e)

    def count(self) -> int:
        return self._collection.count()
