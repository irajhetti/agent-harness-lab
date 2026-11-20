"""Database connections. Two roles: the owner seeds, the reader retrieves."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .access import User

# No passwords: the demo database uses trust auth and is published on loopback only.
# See the README for why that is acceptable here and nowhere else.
_BASE = "127.0.0.1:55432/ragdb?connect_timeout=3"
OWNER_DSN = os.environ.get("PRAG_OWNER_DSN", f"postgresql://rag_owner@{_BASE}")
READER_DSN = os.environ.get("PRAG_READER_DSN", f"postgresql://rag_reader@{_BASE}")

Cursor = psycopg.Cursor[dict[str, Any]]


def set_session_user(cur: Cursor, user: User) -> None:
    """Tell row level security who is asking, for this transaction only.

    `set_config(name, value, true)` is `SET LOCAL` in a form that takes bound parameters.
    The values vanish at commit or rollback, so a pooled connection cannot carry one
    caller's clearance into the next caller's request.
    """
    cur.execute(
        "SELECT set_config('app.user_level', %s, true),"
        " set_config('app.user_compartments', %s, true)",
        (user.level.name, ",".join(sorted(user.compartments))),
    )


@contextmanager
def reader_session(user: User) -> Iterator[Cursor]:
    """One read-only transaction as `rag_reader`, with the caller's clearance set."""
    with psycopg.connect(READER_DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        conn.read_only = True
        set_session_user(cur, user)
        yield cur


@contextmanager
def owner_session() -> Iterator[Cursor]:
    """A transaction as `rag_owner`. Used to seed, and by tests that need to see everything."""
    with psycopg.connect(OWNER_DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        yield cur
