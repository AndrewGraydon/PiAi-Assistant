"""
Sentence embedding wrapper for PiAi Assistant.

Uses sentence-transformers (all-MiniLM-L6-v2) to produce 384-dimensional
embeddings on the Pi 5 CPU. Inference takes ~50-100ms per sentence.

The model is loaded EAGERLY at construction time (not lazily) to avoid a
cold-start delay on the first user query.

For offline Pi operation:
  1. Pre-download while connected:
     python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
  2. Set TRANSFORMERS_OFFLINE=1 in .env to prevent download attempts
"""

from __future__ import annotations

import logging
from typing import List

log = logging.getLogger(__name__)


class Embedder:
    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        log.info("Loading embedding model: %s (this may take 5-30s on first run)", model_name)
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name)
            log.info("Embedding model loaded")
        except ImportError as e:
            raise ImportError(
                "sentence-transformers not installed. Run: pip install sentence-transformers"
            ) from e

    def embed(self, text: str) -> List[float]:
        """Return a 384-dimensional embedding vector for a single text."""
        return self._model.encode(text, show_progress_bar=False).tolist()

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Return embeddings for a list of texts."""
        return self._model.encode(texts, show_progress_bar=False).tolist()
