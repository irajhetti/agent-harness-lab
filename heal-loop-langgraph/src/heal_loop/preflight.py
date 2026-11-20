"""Rules that send a failure to a person before any model is called, and the ledger they read.

Ports lastError, fingerprint, escalateReason and the ledger helpers from heal.ts.
The original budget is in dollars. Local models cost nothing, so here it is a count of model calls.
"""

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

NEEDS_PERSON = re.compile(r"LOGGED_OUT|logged out|not logged in|password|2FA|verification code|two-factor", re.I)
ISO_LINE = re.compile(r"^\d{4}-\d\d-\d\dT")


def last_error(log_tail: str) -> str:
    """The latest failure only. The log is append-only and each run starts with an ISO timestamp."""
    lines = log_tail.split("\n")
    start = 0
    for i, line in enumerate(lines):
        if ISO_LINE.match(line):
            start = i
    return "\n".join(line for line in lines[start:] if line.strip())


def fingerprint(log_tail: str) -> str:
    """Same error with different numbers (timestamps, ids, seconds) gives the same fingerprint."""
    last = "\n".join(last_error(log_tail).split("\n")[-3:])
    normal = re.sub(r"\s+", " ", re.sub(r"[0-9]+", "N", last))
    return hashlib.sha1(normal.encode(), usedforsecurity=False).hexdigest()[:12]


def escalate_reason(
    job: str, err: str, fp: str, ledger: list[dict[str, Any]], now: datetime, daily_call_cap: int
) -> str | None:
    """Why this failure goes to a person instead of the agents, or None to proceed."""
    if NEEDS_PERSON.search(last_error(err)):
        return "it needs a login or a credential, which only a person can do"
    day, week = now - timedelta(days=1), now - timedelta(days=7)

    def at(entry: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(entry["ts"])

    mine = [e for e in ledger if e["job"] == job and at(e) > week]
    if any(e["fingerprint"] == fp and e["outcome"] != "skipped" and at(e) > day for e in mine):
        return "the same error was already worked on in the last 24h"
    if sum(e["outcome"] == "fixed" for e in mine) >= 3:
        return "this job has been healed 3 times this week, so something deeper is wrong"
    if sum(e.get("model_calls", 0) for e in ledger if at(e) > day) >= daily_call_cap:
        return f"the daily budget of {daily_call_cap} model calls is spent"
    return None


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()[-2000:] if line]


def record(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
