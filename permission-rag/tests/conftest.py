"""Shared fixtures. Integration tests ask for `require_db` and skip cleanly when it is down."""

from __future__ import annotations

import psycopg
import pytest

from permission_rag.db import READER_DSN
from permission_rag.evaluate import CorpusIndex, corpus_index


@pytest.fixture(scope="session")
def require_db() -> None:
    try:
        with psycopg.connect(READER_DSN) as conn:
            conn.execute("SELECT 1 FROM chunks LIMIT 1")
    except psycopg.Error as exc:
        pytest.skip(
            "integration test skipped: the database is not ready on 127.0.0.1:55432."
            f" Run `make up` then `make seed`. ({str(exc).strip()})"
        )


@pytest.fixture(scope="session")
def index() -> CorpusIndex:
    """The corpus as read from disk: the reference for what each profile may see."""
    return corpus_index()
