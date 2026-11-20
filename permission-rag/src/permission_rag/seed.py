"""Rebuild the schema and load the corpus, as `rag_owner`."""

from __future__ import annotations

from .chunking import chunk_text
from .corpus import PROJECT_ROOT, load_corpus
from .db import owner_session
from .embed import embed_passages, to_pgvector

SCHEMA_SQL = PROJECT_ROOT / "sql" / "schema.sql"


def seed() -> tuple[int, int]:
    """Returns (documents, chunks) loaded. Everything happens in one transaction."""
    docs = load_corpus()
    pieces = [(doc, i, text) for doc in docs for i, text in enumerate(chunk_text(doc.body))]
    # The title is embedded with each chunk so a chunk from the middle of a document
    # still says what it is about.
    vectors = embed_passages([f"{doc.title}\n{text}" for doc, _, text in pieces])

    with owner_session() as cur:
        cur.execute(SCHEMA_SQL.read_text())
        for doc in docs:
            cur.execute(
                "INSERT INTO documents (doc_id, version, title, source_path, sha256, source_text)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (doc.doc_id, doc.version, doc.title, doc.source_path, doc.sha256, doc.source_text),
            )
        for (doc, index, text), vector in zip(pieces, vectors, strict=True):
            cur.execute(
                "INSERT INTO chunks (doc_id, version, chunk_index, title, source_path, sha256,"
                " access_level, compartments, valid_from, valid_to, text, embedding)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)",
                (
                    doc.doc_id,
                    doc.version,
                    index,
                    doc.title,
                    doc.source_path,
                    doc.sha256,
                    doc.level.name,
                    sorted(doc.compartments),
                    doc.valid_from,
                    doc.valid_to,
                    text,
                    to_pgvector(vector),
                ),
            )
        cur.execute("ANALYZE chunks")
    return len(docs), len(pieces)
