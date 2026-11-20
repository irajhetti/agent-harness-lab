"""Local CPU embeddings through fastembed. No API and no key."""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import TYPE_CHECKING

from .corpus import PROJECT_ROOT

if TYPE_CHECKING:
    from fastembed import TextEmbedding

# fastembed runs Qdrant's quantised ONNX export of this model. Vectors are normalised.
MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384
CACHE_DIR = PROJECT_ROOT / ".model-cache"  # git-ignored; downloaded on first use


@lru_cache(maxsize=1)
def _model() -> TextEmbedding:
    from fastembed import TextEmbedding  # slow import, so only when an embedding is needed

    # Two threads: this machine also runs a live service.
    return TextEmbedding(MODEL_NAME, cache_dir=str(CACHE_DIR), threads=2)


def embed_passages(texts: Sequence[str]) -> list[list[float]]:
    return [vector.tolist() for vector in _model().embed(list(texts), batch_size=16)]


@lru_cache(maxsize=4096)
def embed_query(text: str) -> tuple[float, ...]:
    """Queries and passages are embedded the same way; no query prefix is added."""
    return tuple(embed_passages([text])[0])


def to_pgvector(vector: Sequence[float]) -> str:
    """pgvector's text form, e.g. '[0.1,0.2]'. Sent as a bound parameter and cast in SQL."""
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"
