"""The access rule, stated once in plain Python.

The database enforces this rule twice (the SQL filter and row level security). This module
supplies the caller's profile to both, and `permits` is the independent check the leak tests
use to judge what came back.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import IntEnum

_COMPARTMENT = re.compile(r"[a-z0-9_-]+")


class Level(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    RESTRICTED = 2


def parse_compartments(raw: str | Iterable[str]) -> frozenset[str]:
    """Normalise compartment tags. Refuses anything that is not a simple lower-case name.

    The tags are joined with commas into a session setting, so a tag that contained a comma
    could smuggle in a second tag. Refusing odd characters closes that off.
    """
    parts = raw.split(",") if isinstance(raw, str) else list(raw)
    tags = {p.strip().lower() for p in parts if p.strip()}
    for tag in tags:
        if not _COMPARTMENT.fullmatch(tag):
            raise ValueError(f"invalid compartment tag: {tag!r}")
    return frozenset(tags)


@dataclass(frozen=True)
class User:
    """Who is asking. In a real system this comes from the identity provider, never the query."""

    level: Level
    compartments: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, level: str, compartments: str = "") -> User:
        try:
            parsed = Level[level.strip().upper()]
        except KeyError:
            raise ValueError(f"unknown access level: {level!r}") from None
        return cls(parsed, parse_compartments(compartments))


def permits(
    user: User,
    level: Level,
    compartments: Iterable[str],
    valid_from: date,
    valid_to: date | None,
    today: date,
) -> bool:
    """True when `user` may see a chunk with these labels on `today`.

    Three conditions, all required: the level is not above the user's, the user holds every
    compartment on the chunk, and the document is in force today.
    """
    return (
        level <= user.level
        and set(compartments) <= user.compartments
        and valid_from <= today
        and (valid_to is None or today <= valid_to)
    )
