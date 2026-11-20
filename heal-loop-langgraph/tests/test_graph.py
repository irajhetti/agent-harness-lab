"""The loop end to end: real git worktrees, a real rerun, a scripted model. Offline and deterministic."""

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from helpers import RUN, THREAD, git, roles, run_loop, snapshot
from langgraph.types import Command

from heal_loop import HealConfig, Hooks, resume
from heal_loop.fixture import BROKEN, FIXED, STILL_BROKEN, proposal, review

RETRY_MARK = "Your last reply was not valid"


def ledger(cfg: HealConfig) -> list[dict[str, Any]]:
    return [json.loads(line) for line in cfg.ledger.read_text().splitlines()]


def job_passes(cfg: HealConfig) -> bool:
    return subprocess.run(list(cfg.rerun_command), cwd=cfg.workspace, capture_output=True).returncode == 0


# ---- the approval gate ---------------------------------------------------------------------------


def test_approve_pauses_at_the_human_gate_and_nothing_is_merged_before_approval(cfg: HealConfig) -> None:
    before, head = snapshot(cfg.workspace), git(cfg.workspace, "rev-parse", "HEAD")
    graph, model, result = run_loop(cfg, [proposal()], [review()])

    assert graph.get_state(THREAD).next == ("human_approval",)
    shown = result["__interrupt__"][0].value
    assert "+    return sum(rows)" in shown["diff"] and shown["cause"] == "total() skips the first row"
    assert "outcome" not in result
    assert snapshot(cfg.workspace) == before
    assert git(cfg.workspace, "rev-parse", "HEAD") == head
    assert not job_passes(cfg)  # still broken: the fix exists only on the heal branch
    assert not cfg.ledger.exists()


def test_reject_leaves_the_main_workspace_byte_for_byte_unchanged(cfg: HealConfig) -> None:
    before, head = snapshot(cfg.workspace), git(cfg.workspace, "rev-parse", "HEAD")
    graph, _, _ = run_loop(cfg, [proposal()], [review()])
    result = resume(graph, RUN, approve=False)

    assert result["outcome"] == "rejected"
    assert snapshot(cfg.workspace) == before
    assert git(cfg.workspace, "rev-parse", "HEAD") == head
    assert git(cfg.workspace, "status", "--porcelain") == ""
    assert not Path(result["worktree"]).exists()
    assert git(cfg.workspace, "branch", "--list", "heal/*") != ""  # kept for a person to read
    assert ledger(cfg)[-1]["outcome"] == "rejected"


def test_approve_merges_and_a_passing_rerun_finishes_fixed(cfg: HealConfig) -> None:
    graph, model, _ = run_loop(cfg, [proposal()], [review()])
    result = resume(graph, RUN, approve=True)

    assert result["outcome"] == "fixed"
    assert (cfg.workspace / "app" / "report.py").read_text() == FIXED
    assert job_passes(cfg)
    assert git(cfg.workspace, "log", "-1", "--format=%s").startswith("heal(nightly-report):")
    assert not Path(result["worktree"]).exists()
    assert git(cfg.workspace, "branch", "--list", "heal/*") == ""
    assert ledger(cfg)[-1] | {"ts": ""} == {
        "ts": "",
        "job": "nightly-report",
        "fingerprint": result["fingerprint"],
        "outcome": "fixed",
        "model_calls": 2,
        "summary": "total() skips the first row",
        "branch": "heal/nightly-report-run1",
    }
    assert roles(model) == ["fixer", "reviewer"]  # no model is called after the gate


def test_a_failing_rerun_reverts_the_merge(cfg: HealConfig) -> None:
    before, base = snapshot(cfg.workspace), git(cfg.workspace, "rev-parse", "HEAD")
    graph, _, _ = run_loop(cfg, [proposal(STILL_BROKEN)], [review()])  # the reviewer is wrong to approve
    result = resume(graph, RUN, approve=True)

    assert result["outcome"] == "reverted"
    assert snapshot(cfg.workspace) == before
    assert git(cfg.workspace, "diff", base, "HEAD") == ""
    subjects = git(cfg.workspace, "log", "--format=%s", f"{base}..HEAD").splitlines()
    assert subjects[0].startswith("Revert") and any(s.startswith("heal(") for s in subjects[1:])
    assert git(cfg.workspace, "status", "--porcelain") == ""
    assert git(cfg.workspace, "branch", "--list", "heal/*") != ""  # kept for a person to read
    assert ledger(cfg)[-1]["outcome"] == "reverted"


def test_a_rerun_that_times_out_counts_as_a_failure(cfg: HealConfig) -> None:
    slow = replace(cfg, rerun_command=(sys.executable, "-c", "import time; time.sleep(30)"), rerun_timeout_s=1)
    graph, _, _ = run_loop(slow, [proposal()], [review()])
    assert resume(graph, RUN, approve=True)["outcome"] == "reverted"
    assert (cfg.workspace / "app" / "report.py").read_text() == BROKEN


def test_only_the_exact_word_approve_merges(cfg: HealConfig) -> None:
    before = snapshot(cfg.workspace)
    graph, _, _ = run_loop(cfg, [proposal()], [review()])
    result = graph.invoke(Command(resume="yes, go ahead"), THREAD)
    assert result["outcome"] == "rejected"
    assert snapshot(cfg.workspace) == before


def test_only_the_reviewed_commit_is_merged(cfg: HealConfig) -> None:
    graph, _, paused = run_loop(cfg, [proposal()], [review()])
    worktree = Path(paused["worktree"])
    (worktree / "app" / "extra.py").write_text("print('added after the review')\n")
    git(worktree, "add", "-A")
    git(worktree, "-c", "user.name=x", "-c", "user.email=x@localhost", "commit", "-q", "-m", "sneaked in")
    result = resume(graph, RUN, approve=True)

    assert result["outcome"] == "fixed"
    assert not (cfg.workspace / "app" / "extra.py").exists()


def test_a_merge_conflict_escalates_and_leaves_no_half_merge(cfg: HealConfig) -> None:
    graph, _, _ = run_loop(cfg, [proposal()], [review()])
    theirs = "def total(rows):\n    return 12\n"
    (cfg.workspace / "app" / "report.py").write_text(theirs)
    git(
        cfg.workspace, "-c", "user.name=x", "-c", "user.email=x@localhost", "commit", "-qam", "a person got there first"
    )
    result = resume(graph, RUN, approve=True)

    assert result["outcome"] == "escalated" and "could not be merged" in result["summary"]
    assert (cfg.workspace / "app" / "report.py").read_text() == theirs
    assert git(cfg.workspace, "status", "--porcelain") == ""


def test_resume_needs_a_run_that_is_waiting_for_approval(cfg: HealConfig) -> None:
    graph, _, _ = run_loop(cfg, [proposal()], [review()])
    resume(graph, RUN, approve=False)
    with pytest.raises(RuntimeError, match="not waiting for approval"):
        resume(graph, RUN, approve=True)


# ---- model output that cannot be trusted ---------------------------------------------------------


def test_an_unreadable_reviewer_verdict_escalates(cfg: HealConfig) -> None:
    before = snapshot(cfg.workspace)
    _, model, result = run_loop(cfg, [proposal()], ["Looks fine to me, ship it.", "Yes. Approved."])

    assert result["outcome"] == "escalated" and "no readable verdict" in result["summary"]
    assert "__interrupt__" not in result
    assert roles(model) == ["fixer", "reviewer", "reviewer"]
    assert snapshot(cfg.workspace) == before


def test_a_verdict_outside_the_enum_escalates(cfg: HealConfig) -> None:
    _, _, result = run_loop(cfg, [proposal()], [review("LGTM"), review("approve")])
    assert result["outcome"] == "escalated" and "no readable verdict" in result["summary"]


def test_an_invalid_reviewer_reply_is_retried_once_with_the_error_fed_back(cfg: HealConfig) -> None:
    graph, model, _ = run_loop(cfg, [proposal()], ["Looks fine to me.", review()])
    assert graph.get_state(THREAD).next == ("human_approval",)
    assert RETRY_MARK not in model.calls[1][1] and RETRY_MARK in model.calls[2][1]


def test_invalid_fixer_output_is_retried_once_then_escalates(cfg: HealConfig) -> None:
    before = snapshot(cfg.workspace)
    bad = {"cause": "something", "edits": "all of them"}
    _, model, result = run_loop(cfg, [bad, bad, proposal()], [review()])  # the third reply must never be asked for

    assert result["outcome"] == "escalated" and "no valid proposal" in result["summary"]
    assert roles(model) == ["fixer", "fixer"]
    assert RETRY_MARK not in model.calls[0][1]
    assert RETRY_MARK in model.calls[1][1] and "rationale" in model.calls[1][1]
    assert snapshot(cfg.workspace) == before
    assert ledger(cfg)[-1]["model_calls"] == 2


def test_invalid_fixer_output_then_a_valid_retry_carries_on(cfg: HealConfig) -> None:
    graph, model, _ = run_loop(cfg, ["I would fix the sum.", proposal()], [review()])
    assert graph.get_state(THREAD).next == ("human_approval",)
    assert roles(model) == ["fixer", "fixer", "reviewer"]


def test_the_model_cannot_supply_the_rerun_command(cfg: HealConfig) -> None:
    sneaky = proposal() | {"rerun_command": ["true"]}
    _, _, result = run_loop(cfg, [sneaky, sneaky], [])
    assert result["outcome"] == "escalated" and "rerun_command" in result["summary"]


# ---- the fixer / reviewer loop -------------------------------------------------------------------


def test_changes_loops_at_most_twice_then_escalates(cfg: HealConfig) -> None:
    before = snapshot(cfg.workspace)
    _, model, result = run_loop(
        cfg,
        [proposal(STILL_BROKEN), proposal("def total(rows):\n    return sum(rows[:-1])\n"), proposal()],
        [review("CHANGES", "still skips rows"), review("CHANGES", "now it skips the last row"), review()],
    )

    assert result["outcome"] == "escalated" and "CHANGES" in result["summary"]
    assert roles(model) == ["fixer", "reviewer", "fixer", "reviewer"]  # two rounds, never a third
    assert result["round"] == 2
    assert "__interrupt__" not in result
    assert snapshot(cfg.workspace) == before


def test_changes_then_approve_reaches_the_gate_with_the_notes_fed_back(cfg: HealConfig) -> None:
    graph, model, result = run_loop(
        cfg, [proposal(STILL_BROKEN), proposal()], [review("CHANGES", "sum every row, not rows[2:]"), review()]
    )

    assert graph.get_state(THREAD).next == ("human_approval",)
    assert "sum every row, not rows[2:]" in model.calls[2][1]
    diff = result["__interrupt__"][0].value["diff"]
    assert "+    return sum(rows)" in diff and "rows[2:]" not in diff
    assert resume(graph, RUN, approve=True)["outcome"] == "fixed"


def test_a_model_error_stops_the_run_loudly_and_keeps_the_worktree(cfg: HealConfig) -> None:
    before = snapshot(cfg.workspace)
    with pytest.raises(RuntimeError, match="no reply left"):
        run_loop(cfg, [], [])  # stands in for a model server that is down
    assert (cfg.worktrees_dir / "nightly-report-run1").is_dir()
    assert snapshot(cfg.workspace) == before


def test_reviewer_escalate_goes_to_a_person(cfg: HealConfig) -> None:
    _, _, result = run_loop(cfg, [proposal()], [review("ESCALATE", "this changes what the report means")])
    assert result["outcome"] == "escalated" and "this changes what the report means" in result["summary"]
    assert "__interrupt__" not in result


def test_not_fixable_in_code_escalates_without_a_review(cfg: HealConfig) -> None:
    report = {"fixable": False, "cause": "the upstream feed is down", "rationale": "", "edits": []}
    _, model, result = run_loop(cfg, [report], [])
    assert result["outcome"] == "escalated" and "the upstream feed is down" in result["summary"]
    assert roles(model) == ["fixer"]


def test_a_fix_that_changes_nothing_escalates(cfg: HealConfig) -> None:
    _, model, result = run_loop(cfg, [proposal(BROKEN)], [])
    assert result["outcome"] == "escalated" and "changed nothing" in result["summary"]
    assert roles(model) == ["fixer"]


# ---- guardrails ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["job.py", "../outside.py", "app/../../outside.py", "{tmp}/absolute.py", ".env", "app/secrets.py", "README.md"],
)
def test_forbidden_edits_escalate_and_write_nothing(cfg: HealConfig, tmp_path: Path, path: str) -> None:
    before = snapshot(cfg.workspace)
    path = path.replace("{tmp}", str(tmp_path))
    _, model, result = run_loop(cfg, [proposal("print('job passes now')\n", path)], [])

    assert result["outcome"] == "escalated" and "may not touch" in result["summary"]
    assert roles(model) == ["fixer"]  # refused before the reviewer is asked
    assert snapshot(cfg.workspace) == before
    assert not (tmp_path / "absolute.py").exists()
    assert not list(tmp_path.rglob("outside.py"))
    assert not Path(result["worktree"]).exists()


def test_one_bad_edit_refuses_the_whole_proposal(cfg: HealConfig) -> None:
    mixed = proposal()
    mixed["edits"].append({"path": "job.py", "new_content": "print('ok')\n"})
    _, _, result = run_loop(cfg, [mixed], [])
    assert result["outcome"] == "escalated" and "job.py: protected file" in result["summary"]
    assert git(cfg.workspace, "diff", "main", "heal/nightly-report-run1") == ""  # nothing was committed


# ---- pre-flight rules ----------------------------------------------------------------------------


def test_a_login_failure_is_skipped_without_calling_a_model(cfg: HealConfig) -> None:
    cfg.error_log.write_text("2026-01-01T00:00:00Z job failed\nLOGGED_OUT: the session expired\n")
    _, model, result = run_loop(cfg, [proposal()], [review()])

    assert result["outcome"] == "skipped" and "login" in result["summary"]
    assert model.calls == []
    assert len(git(cfg.workspace, "worktree", "list").splitlines()) == 1  # no worktree was made
    assert ledger(cfg)[-1]["outcome"] == "skipped"


def test_the_same_error_is_not_worked_on_twice_in_a_day(cfg: HealConfig) -> None:
    _, _, first = run_loop(cfg, [proposal()], [review("ESCALATE", "not sure")])
    _, model, second = run_loop(cfg, [proposal()], [review()], run_id="run2")

    assert first["outcome"] == "escalated"
    assert second["outcome"] == "skipped" and "already worked on" in second["summary"]
    assert model.calls == []


# ---- the tracing seam ----------------------------------------------------------------------------


def test_hooks_see_every_node_model_call_and_command(cfg: HealConfig) -> None:
    class Recorder(Hooks):
        def __init__(self) -> None:
            self.started: list[str] = []
            self.ended: list[str] = []
            self.model_roles: list[str] = []
            self.commands: list[list[str]] = []

        def on_node_start(self, node: str, state: Mapping[str, Any]) -> None:
            self.started.append(node)

        def on_node_end(self, node: str, update: Mapping[str, Any]) -> None:
            self.ended.append(node)

        def on_model_call(self, role: str, prompt: str, reply: str) -> None:
            self.model_roles.append(role)

        def on_tool_call(self, command: Sequence[str], returncode: int) -> None:
            self.commands.append(list(command))

    seen = Recorder()
    graph, _, _ = run_loop(cfg, [proposal()], [review()], hooks=seen)
    resume(graph, RUN, approve=True)

    assert seen.ended == [
        "gather_evidence",
        "fixer",
        "apply_in_worktree",
        "reviewer",
        "human_approval",
        "merge_and_rerun",
        "finish",
    ]
    assert seen.started.count("human_approval") == 2  # once to pause, once more on resume
    assert seen.model_roles == ["fixer", "reviewer"]
    assert list(cfg.rerun_command) in seen.commands
    assert all(cmd[0] == "git" for cmd in seen.commands if cmd != list(cfg.rerun_command))
