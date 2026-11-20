"""Hybrid retrieval: full-text search and vector search, merged with reciprocal rank fusion.

Both searches carry the same access filter in their WHERE clause (layer 1). The filter is
built only from the caller's profile. Nothing in the query text or in any retrieved
document can change it, because both are only ever passed as bound parameters or returned
as data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, get_args

from .access import User
from .db import Cursor
from .embed import embed_query, to_pgvector

Mode = Literal["keyword", "vector", "hybrid"]
MODES: tuple[Mode, ...] = get_args(Mode)

# Layer 1. WHERE is applied before ORDER BY and LIMIT, so a forbidden row is never ranked
# and can never take a place in the top k.
ACCESS_FILTER = """
        access_rank <= %(rank)s
    AND compartments <@ %(compartments)s::text[]
    AND valid_from <= current_date
    AND (valid_to IS NULL OR valid_to >= current_date)
"""

COLUMNS = (
    "chunk_id, doc_id, version, chunk_index, title, source_path, sha256,"
    " access_level, compartments, valid_from, valid_to, text"
)

# plainto_tsquery joins the words with AND, which finds nothing for a whole question.
# Swapping & for | turns it into "any of these words"; ts_rank then favours chunks that
# match more of them.
KEYWORD_SQL = f"""
    WITH q AS (
        SELECT replace(plainto_tsquery('english', %(query)s)::text, '&', '|')::tsquery AS tsq
    )
    SELECT {COLUMNS}, ts_rank(tsv, q.tsq) AS score
    FROM chunks, q
    WHERE tsv @@ q.tsq AND {ACCESS_FILTER}
    ORDER BY score DESC, chunk_id
    LIMIT %(limit)s
"""

# <=> is cosine distance, so similarity is one minus it.
VECTOR_SQL = f"""
    SELECT {COLUMNS}, 1 - (embedding <=> %(qvec)s::vector) AS score
    FROM chunks
    WHERE {ACCESS_FILTER}
    ORDER BY embedding <=> %(qvec)s::vector
    LIMIT %(limit)s
"""


@dataclass
class Result:
    """One chunk with its provenance and the score from each method."""

    chunk_id: int
    doc_id: str
    version: int
    chunk_index: int
    title: str
    source_path: str
    sha256: str
    access_level: str
    compartments: list[str]
    valid_from: date
    valid_to: date | None
    text: str
    keyword_rank: int | None = None
    keyword_score: float | None = None
    vector_rank: int | None = None
    vector_score: float | None = None
    rrf_score: float = 0.0


def rrf_merge(rankings: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal rank fusion: each list gives an item 1 / (k + rank), and the scores add up.

    Only ranks are used, so the two methods' scores never need to be on the same scale.
    Ties are broken by id so the order is repeatable.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def _fetch(cur: Cursor, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    cur.execute(sql, params)
    return cur.fetchall()


def search(
    cur: Cursor, user: User, query: str, mode: Mode = "hybrid", k: int = 5, candidates: int = 20
) -> list[Result]:
    """Top `k` chunks `user` may see. Each method fetches `candidates` rows, then they are fused.

    With one method, fusion over a single list just keeps that list's order.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode!r}")
    params = {
        "rank": int(user.level),
        "compartments": sorted(user.compartments),
        "limit": candidates,
    }
    keyword_rows: list[dict[str, Any]] = []
    vector_rows: list[dict[str, Any]] = []
    if mode != "vector":
        keyword_rows = _fetch(cur, KEYWORD_SQL, {**params, "query": query})
    if mode != "keyword":
        qvec = to_pgvector(embed_query(query))
        vector_rows = _fetch(cur, VECTOR_SQL, {**params, "qvec": qvec})

    results: dict[int, Result] = {}
    for rank, row in enumerate(keyword_rows, start=1):
        score = float(row.pop("score"))
        result = results.setdefault(row["chunk_id"], Result(**row))
        result.keyword_rank, result.keyword_score = rank, score
    for rank, row in enumerate(vector_rows, start=1):
        score = float(row.pop("score"))
        result = results.setdefault(row["chunk_id"], Result(**row))
        result.vector_rank, result.vector_score = rank, score

    fused = rrf_merge([[r["chunk_id"] for r in keyword_rows], [r["chunk_id"] for r in vector_rows]])
    top: list[Result] = []
    for chunk_id, score in fused[:k]:
        results[chunk_id].rrf_score = score
        top.append(results[chunk_id])
    return top
