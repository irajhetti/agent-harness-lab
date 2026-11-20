"""The pre-flight escalation rules. Ported from test/heal.test.ts with generic log lines."""

from datetime import UTC, datetime, timedelta
from typing import Any

from heal_loop.preflight import escalate_reason, fingerprint, last_error

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
JOB = "nightly-report"


def at(hours_ago: float, **extra: Any) -> dict[str, Any]:
    ts = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {"ts": ts, "job": JOB, "fingerprint": "abc", "outcome": "fixed", "model_calls": 2} | extra


def reason(err: str, fp: str, ledger: list[dict[str, Any]], job: str = JOB) -> str | None:
    return escalate_reason(job, err, fp, ledger, NOW, daily_call_cap=20)


def test_fingerprint_ignores_numbers_and_whitespace() -> None:
    first = fingerprint("2026-10-18T06:32:21Z upload still pending 60s after submit")
    second = fingerprint("2026-10-15T10:11:12Z upload   still pending 90s after submit")
    assert first == second
    assert fingerprint("upload still pending") != fingerprint("submit button not found")


LOG = (
    "2026-10-15T06:31:16.155Z job failed\nsubmit button not found (logged out?)\n\n"
    "2026-10-18T06:32:21.025Z job failed\nupload still pending 60s after submit\n"
)


def test_only_the_latest_failure_counts() -> None:
    assert last_error(LOG) == "2026-10-18T06:32:21.025Z job failed\nupload still pending 60s after submit"
    assert reason(LOG, "x", []) is None


def test_login_problems_go_to_a_person() -> None:
    assert "login" in (reason("LOGGED_OUT: ...", "x", []) or "")
    assert reason("upload still pending", "x", []) is None


def test_the_same_error_is_not_worked_on_twice_in_a_day_but_a_skipped_one_may_be() -> None:
    assert "already worked on" in (reason("e", "abc", [at(2, outcome="escalated")]) or "")
    assert reason("e", "abc", [at(2, outcome="skipped")]) is None
    assert reason("e", "abc", [at(30)]) is None  # yesterday does not count
    assert reason("e", "abc", [at(2)], job="backup") is None  # another job does not count


def test_three_heals_in_a_week_and_the_daily_cap_both_escalate() -> None:
    assert "3 times" in (reason("e", "new", [at(30), at(60), at(100)]) or "")
    assert "budget" in (reason("e", "new", [at(1, job="other", model_calls=20)]) or "")
    assert reason("e", "new", [at(1, job="other", model_calls=19)]) is None
