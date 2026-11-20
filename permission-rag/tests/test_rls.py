"""Row level security: the second layer, tested with raw SQL that carries no filter at all."""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import dict_row

from permission_rag.access import permits
from permission_rag.db import OWNER_DSN, READER_DSN
from permission_rag.evaluate import PROFILES, CorpusIndex

pytestmark = pytest.mark.usefixtures("require_db")

UNFILTERED = "SELECT doc_id, version, access_level, compartments FROM chunks"


def reader() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(READER_DSN, row_factory=dict_row)


def unfiltered_rows(level: str | None, compartments: str | None = None) -> list[dict[str, Any]]:
    """Everything rag_reader can see with a bare SELECT and the given session settings."""
    with reader() as conn:
        if level is not None:
            conn.execute("SELECT set_config('app.user_level', %s, true)", (level,))
        if compartments is not None:
            conn.execute("SELECT set_config('app.user_compartments', %s, true)", (compartments,))
        return conn.execute(UNFILTERED).fetchall()


def test_owner_sees_every_row(index: CorpusIndex) -> None:
    """The control: forbidden rows do exist, and the owner role is not held back by RLS."""
    with psycopg.connect(OWNER_DSN, row_factory=dict_row) as conn:
        rows = conn.execute(UNFILTERED).fetchall()
    assert {(r["doc_id"], r["version"]) for r in rows} == set(index)
    assert {r["access_level"] for r in rows} == {"PUBLIC", "INTERNAL", "RESTRICTED"}


@pytest.mark.parametrize("profile", PROFILES)
def test_unfiltered_select_returns_exactly_what_the_profile_may_see(
    profile: str, index: CorpusIndex
) -> None:
    user = PROFILES[profile]
    rows = unfiltered_rows(user.level.name, ",".join(sorted(user.compartments)))
    seen = {(r["doc_id"], r["version"]) for r in rows}
    allowed = {
        key
        for key, doc in index.items()
        if permits(user, doc.level, doc.compartments, doc.valid_from, doc.valid_to, date.today())
    }
    assert seen - allowed == set(), "row level security let a forbidden row through"
    assert seen == allowed  # and it hides nothing the profile is entitled to
    assert allowed != set(index), "every profile should have something hidden from it"


@pytest.mark.parametrize("level", [None, "", "ADMIN", "restricted", "2", "RESTRICTED "])
def test_unset_or_unknown_level_sees_only_public(level: str | None) -> None:
    """Fail closed: anything that is not exactly INTERNAL or RESTRICTED is treated as PUBLIC."""
    rows = unfiltered_rows(level)
    assert rows
    assert {r["access_level"] for r in rows} == {"PUBLIC"}
    assert all(r["compartments"] == [] for r in rows)


def test_settings_do_not_outlive_the_transaction() -> None:
    """A pooled connection must not carry one caller's clearance to the next."""
    with reader() as conn:
        conn.execute("SELECT set_config('app.user_level', 'RESTRICTED', true)")
        assert "RESTRICTED" in {r["access_level"] for r in conn.execute(UNFILTERED)}
        conn.commit()
        assert {r["access_level"] for r in conn.execute(UNFILTERED)} == {"PUBLIC"}


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO chunks (doc_id, version, chunk_index, title, source_path, sha256,"
        " access_level, valid_from, text, embedding) SELECT doc_id, version, 99, title,"
        " source_path, sha256, 'PUBLIC', valid_from, text, embedding FROM chunks LIMIT 1",
        "UPDATE chunks SET access_level = 'PUBLIC'",
        "UPDATE chunks SET compartments = '{}'",
        "DELETE FROM chunks",
        "TRUNCATE chunks",
        "SELECT * FROM documents",
        "CREATE TABLE scratch (x int)",
    ],
)
def test_reader_cannot_write_or_read_the_source_table(statement: str) -> None:
    with reader() as conn:
        conn.execute("SELECT set_config('app.user_level', 'RESTRICTED', true)")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE chunks DISABLE ROW LEVEL SECURITY",
        "DROP POLICY chunks_reader_select ON chunks",
        "ALTER POLICY chunks_reader_select ON chunks USING (true)",
        "CREATE POLICY open_door ON chunks FOR SELECT TO rag_reader USING (true)",
        "CREATE OR REPLACE FUNCTION app_user_rank() RETURNS int LANGUAGE sql AS 'SELECT 2'",
        "DROP TABLE chunks",
        "SET ROLE rag_owner",
        "SET SESSION AUTHORIZATION rag_owner",
        "ALTER ROLE rag_reader BYPASSRLS",
        # Turning row security off does not skip the policy. PostgreSQL refuses the query.
        "SET row_security = off; SELECT count(*) FROM chunks",
    ],
)
def test_reader_cannot_change_or_bypass_the_policy(statement: str) -> None:
    with reader() as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(statement)


def test_reader_role_has_no_special_powers() -> None:
    with reader() as conn:
        row = conn.execute(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles"
            " WHERE rolname = current_user"
        ).fetchone()
        owns = conn.execute(
            "SELECT count(*) AS n FROM pg_class WHERE relowner = current_user::regrole"
        ).fetchone()
    assert row == {
        "rolsuper": False,
        "rolbypassrls": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
    }
    assert owns == {"n": 0}


def test_planner_statistics_are_hidden_from_the_reader() -> None:
    """pg_stats holds sample values from every row. PostgreSQL hides it where RLS applies."""
    query = "SELECT count(*) AS n FROM pg_stats WHERE tablename = 'chunks'"
    with reader() as conn:
        assert conn.execute(query).fetchone() == {"n": 0}
    with psycopg.connect(OWNER_DSN, row_factory=dict_row) as conn:
        row = conn.execute(query).fetchone()
        assert row is not None and row["n"] > 0


def test_known_limit_the_session_setting_is_trusted() -> None:
    """Not a defence, a documented limit. rag_reader can set its own level, so row level
    security protects against a query that forgets the filter. It does not protect against
    application code that lies about who the user is."""
    rows = unfiltered_rows("RESTRICTED", "alpha,bravo")
    assert "RESTRICTED" in {r["access_level"] for r in rows}
