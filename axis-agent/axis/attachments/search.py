"""Local embeddings and rank fusion. No network database or retrieval agent."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import threading

MODEL = "BAAI/bge-small-en-v1.5"


def fuse(*rankings, limit=8):
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, section_id in enumerate(dict.fromkeys(ranking), 1):
            scores[section_id] += 1 / (60 + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))[:limit]


class Embeddings:
    def __init__(self, cache: Path):
        self.cache, self.model, self.error = cache, None, None
        self.lock = threading.Lock()

    def encode(self, texts, *, query=False):
        import numpy as np
        with self.lock:
            if self.error:
                raise RuntimeError(self.error)
            try:
                if self.model is None:
                    from fastembed import TextEmbedding
                    self.model = TextEmbedding(model_name=MODEL, cache_dir=str(self.cache), threads=2)
                method = self.model.query_embed if query else self.model.passage_embed
                # Preserve the end of unusually token-dense sections instead of
                # allowing the embedding model's token limit to silently cut it.
                tokenizer = self.model.model.tokenizer
                chunks, spans = [], []
                for text in texts:
                    pending = [text]
                    start = len(chunks)
                    while pending:
                        part = pending.pop(0)
                        encoded = tokenizer.encode(part)
                        if (encoded.overflowing or len(encoded.ids) > 480) and len(part) > 1:
                            middle = len(part) // 2
                            pending[0:0] = [part[:middle], part[middle:]]
                        else:
                            chunks.append(part)
                    spans.append((start, len(chunks)))
                encoded = np.asarray(list(method(chunks, batch_size=16)), dtype=np.float32)
                vectors = np.asarray([encoded[start:end].mean(axis=0) for start, end in spans], dtype=np.float32)
                vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
                return vectors
            except Exception as exc:
                self.error = f"Semantic search unavailable ({type(exc).__name__}); keyword search remains available."
                raise RuntimeError(self.error) from exc
