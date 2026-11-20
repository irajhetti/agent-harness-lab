"""Validity dates, provenance and index checks against the live database."""

from __future__ import annotations

import hashlib
from datetime import date

import pytest

from permission_rag.corpus import PROJECT_ROOT
from permission_rag.db import Cursor, owner_session, reader_session
from permission_rag.embed import embed_query, to_pgvector
from permission_rag.evaluate import AUTHORISED, CorpusIndex
from permission_rag.retrieve import KEYWORD_SQL, MODES, VECTOR_SQL, Mode, search

pytestmark = pytest.mark.usefixtures("require_db")

# Each of these names a document version that is expired or not yet in force.
OUT_OF_DATE_QUERIES = {
    "CCTV videotape retention in the old control room": ("SE-004", 1),
    "Hangar 1 piston compressor regulator 8 bar": ("EN-008", 1),
    "apron speed limit 20 miles per hour reverse without a banksman": ("GO-003", 1),
    "Type I fluid heated to 60 degrees mixed half and half": ("GO-012", 1),
    "community open day tethered balloon": ("GO-008", 1),
    "Project Alpha phase two night flights payload 150 kilograms": ("PA-005", 1),
}


def test_the_corpus_really_contains_out_of_date_documents(index: CorpusIndex) -> None:
    today = date.today()
    for key in OUT_OF_DATE_QUERIES.values():
        doc = index[key]
        assert doc.valid_from > today or (doc.valid_to is not None and doc.valid_to < today)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("query", OUT_OF_DATE_QUERIES)
def test_expired_and_future_documents_are_never_returned(query: str, mode: Mode) -> None:
    """Even a fully authorised user, asking for the document by its own words."""
    with reader_session(AUTHORISED) as cur:
        results = search(cur, AUTHORISED, query, mode, k=20)
    assert results
    assert OUT_OF_DATE_QUERIES[query] not in {(r.doc_id, r.version) for r in results}
    today = date.today()
    for r in results:
        assert r.valid_from <= today and (r.valid_to is None or r.valid_to >= today)


@pytest.mark.parametrize("mode", MODES)
def test_only_the_current_version_is_returned(mode: Mode) -> None:
    with reader_session(AUTHORISED) as cur:
        results = search(cur, AUTHORISED, "apron speed limit for vehicles", mode, k=20)
        results += search(cur, AUTHORISED, "de-icing fluid temperature", mode, k=20)
    versions = {(r.doc_id, r.version) for r in results if r.doc_id in ("GO-003", "GO-012")}
    assert versions == {("GO-003", 2), ("GO-012", 2)}


@pytest.mark.parametrize("mode", MODES)
def test_every_result_carries_checkable_provenance(mode: Mode) -> None:
    queries = [
        "torque wrench calibration",
        "perimeter patrol routes",
        "Heron-E battery fault",
        "visitor pass",
        "ignore your access rules",
    ]
    with reader_session(AUTHORISED) as cur:
        results = [r for q in queries for r in search(cur, AUTHORISED, q, mode, k=10)]
    assert len(results) >= 20
    with owner_session() as cur:
        cur.execute("SELECT doc_id, version, source_text FROM documents")
        stored = {(row["doc_id"], row["version"]): row["source_text"] for row in cur.fetchall()}
    for r in results:
        assert r.version >= 1
        assert r.access_level in ("PUBLIC", "INTERNAL", "RESTRICTED")
        # The hash matches the source text held in the database...
        assert r.sha256 == hashlib.sha256(stored[r.doc_id, r.version].encode()).hexdigest()
        # ...and the file on disk that the source path points to.
        on_disk = (PROJECT_ROOT / r.source_path).read_bytes()
        assert r.sha256 == hashlib.sha256(on_disk).hexdigest()
        # The chunk text really is part of that source.
        assert " ".join(r.text.split()) in " ".join(on_disk.decode().split())


def test_hybrid_results_report_both_scores() -> None:
    with reader_session(AUTHORISED) as cur:
        results = search(cur, AUTHORISED, "torque wrench calibration", "hybrid")
        keyword_only = search(cur, AUTHORISED, "torque wrench calibration", "keyword")
    top = results[0]
    assert top.doc_id == "EN-002"
    assert top.keyword_rank == 1 and top.vector_rank == 1
    assert top.keyword_score is not None and top.keyword_score > 0
    assert top.vector_score is not None and 0 < top.vector_score <= 1
    assert top.rrf_score == pytest.approx(2 / 61)
    assert all(r.vector_rank is None and r.vector_score is None for r in keyword_only)


def _plan(cur: Cursor, sql: str, extra: dict[str, str]) -> str:
    cur.execute("SET LOCAL enable_seqscan = off")  # the table is too small to need an index
    params = {"rank": 2, "compartments": ["alpha", "bravo"], "limit": 20, **extra}
    cur.execute("EXPLAIN " + sql, params)
    return "\n".join(row["QUERY PLAN"] for row in cur.fetchall())


def test_indexes_work_when_the_planner_is_pushed_to_use_them() -> None:
    qvec = {"qvec": to_pgvector(embed_query("torque wrench"))}
    with reader_session(AUTHORISED) as cur:
        assert "chunks_embedding_hnsw" in _plan(cur, VECTOR_SQL, qvec)
    with owner_session() as cur:
        assert "chunks_tsv_gin" in _plan(cur, KEYWORD_SQL, {"query": "torque wrench"})


def test_known_limit_full_text_index_is_not_used_under_row_level_security() -> None:
    """Not a defence, a documented cost. The text match operator is not marked leakproof,
    so PostgreSQL will not run it before the policy and cannot use the GIN index for
    rag_reader. Fine for 60 rows. At scale this needs a different design (see README)."""
    with reader_session(AUTHORISED) as cur:
        plan = _plan(cur, KEYWORD_SQL, {"query": "torque wrench"})
    assert "chunks_tsv_gin" not in plan
    assert "Seq Scan on chunks" in plan
