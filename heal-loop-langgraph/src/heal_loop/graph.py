"""The heal loop as a LangGraph StateGraph. Read this file top to bottom: state, nodes, edges.

    gather_evidence -> fixer -> apply_in_worktree -> reviewer -+-> human_approval -> merge_and_rerun -> finish
                         ^                                     |         |                |
                         +------------- CHANGES ---------------+       reject        rerun failed -> revert
                                                               |
    any node that needs a person ------------------------------+-> escalate -> finish

The model only ever returns data (schemas.py). Every action is taken by code in this file.
"""

import json
import re
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from .config import HealConfig
from .guardrails import path_problem, violations
from .hooks import Hooks
from .models import Model, ask
from .preflight import escalate_reason, fingerprint, read_ledger, record
from .prompts import fixer_prompt, reviewer_prompt
from .schemas import FixProposal, Review, Verdict

SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")  # job names and run ids end up in branch names and paths
BOT = ("-c", "user.name=heal-loop", "-c", "user.email=heal-loop@localhost")


class HealState(TypedDict, total=False):
    run_id: str
    evidence: str  # tail of the job's error log
    fingerprint: str
    base: str  # commit the worktree was cut from
    branch: str
    worktree: str
    round: int  # fixer/reviewer passes started so far
    model_calls: int
    proposal: dict[str, Any]  # a validated FixProposal, stored as plain data for the checkpointer
    reviewed_commit: str  # the exact commit the reviewer and the person saw
    diff: str
    review: dict[str, Any]  # a validated Review
    escalation: str  # set by any node that needs a person; routes to `escalate`
    refused_edits: int  # how many edits the path rules refused (a count only, for the trace)
    decision: str  # the person's answer at the approval gate
    merge_commit: str
    outcome: str  # fixed | escalated | skipped | rejected | reverted
    summary: str


class Nodes:
    def __init__(self, cfg: HealConfig, model: Model, hooks: Hooks) -> None:
        self.cfg, self.model, self.hooks = cfg, model, hooks

    # ---- helpers -------------------------------------------------------------------------

    @contextmanager
    def _tool(self, name: str) -> Iterator[None]:
        """Tell the tracing seam that a tool call starts, and how it ends."""
        self.hooks.on_tool_start(name)
        try:
            yield
        except BaseException as error:
            self.hooks.on_error(error)
            raise
        self.hooks.on_tool_end(name)

    def _sh(
        self, args: list[str], cwd: Path, check: bool = True, timeout: int = 60, name: str = ""
    ) -> subprocess.CompletedProcess[str]:
        """Run one command as an argument list. Never a shell string."""
        with self._tool(name or args[0]):
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
            self.hooks.on_tool_call(args, result.returncode)
        if check and result.returncode != 0:
            raise RuntimeError(f"{' '.join(args[:4])} failed: {(result.stderr or result.stdout)[-600:]}")
        return result

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._sh(["git", *BOT, *args], cwd or self.cfg.workspace, check, name=f"git.{args[0]}")

    def _editable_files(self, root: Path) -> str:
        # shortcut: every allow-listed file is pasted into the prompt, capped at 20k characters.
        # A real agent would have read tools; upgrade when the repository is bigger than a toy.
        parts = []
        for rel in self._git("ls-files", cwd=root).stdout.splitlines():
            if path_problem(root, rel, self.cfg) is None:
                parts.append(f"### {rel}\n{(root / rel).read_text(errors='replace')}")
        return "\n".join(parts)[:20_000]

    # ---- nodes ---------------------------------------------------------------------------

    def gather_evidence(self, state: HealState) -> dict[str, Any]:
        """Read the failure, apply the pre-flight rules, then cut a throwaway worktree."""
        cfg = self.cfg
        log = cfg.error_log.read_text(errors="replace") if cfg.error_log.exists() else ""
        evidence = "\n".join(log.split("\n")[-60:])
        fp = fingerprint(evidence)
        update: dict[str, Any] = {"evidence": evidence, "fingerprint": fp, "round": 0, "model_calls": 0}
        why = escalate_reason(cfg.job, evidence, fp, read_ledger(cfg.ledger), datetime.now(UTC), cfg.daily_call_cap)
        if why:
            return update | {"outcome": "skipped", "summary": why}
        base = self._git("rev-parse", "HEAD").stdout.strip()
        branch = f"heal/{cfg.job}-{state['run_id']}"
        worktree = cfg.worktrees_dir / f"{cfg.job}-{state['run_id']}"
        cfg.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "-q", "-b", branch, str(worktree), base)
        return update | {"base": base, "branch": branch, "worktree": str(worktree)}

    def fixer(self, state: HealState) -> dict[str, Any]:
        """Ask the fixer model for a proposal. It returns data; it writes nothing."""
        round_no = state["round"] + 1
        previous = json.dumps(state["proposal"]) if round_no > 1 else ""
        notes = state["review"]["notes"] if round_no > 1 else ""
        files = self._editable_files(Path(state["worktree"]))
        prompt = fixer_prompt(self.cfg, state["evidence"], files, previous, notes)
        proposal, error, calls = ask(self.model, "fixer", prompt, FixProposal, self.hooks)
        update: dict[str, Any] = {"round": round_no, "model_calls": state["model_calls"] + calls}
        if proposal is None:
            return update | {"escalation": f"the fixer gave no valid proposal: {error[-600:]}"}
        if not proposal.fixable:
            return update | {"escalation": f"not fixable in code: {proposal.cause}"}
        return update | {"proposal": proposal.model_dump()}

    def apply_in_worktree(self, state: HealState) -> dict[str, Any]:
        """Check every path, then write the edits into the worktree and commit them there."""
        worktree = Path(state["worktree"])
        proposal = FixProposal.model_validate(state["proposal"])
        refused = violations(worktree, proposal.edits, self.cfg)
        if refused:
            return {
                "escalation": "the fixer tried to edit files it may not touch: " + "; ".join(refused),
                "refused_edits": len(refused),
            }
        with self._tool("apply_edits"):
            for edit in proposal.edits:
                target = worktree / edit.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(edit.new_content)
        self._git("add", "--", *[edit.path for edit in proposal.edits], cwd=worktree)
        message = f"heal({self.cfg.job}): {proposal.cause[:100]}\n\n{proposal.rationale}"
        self._git("commit", "-q", "--allow-empty", "-m", message, cwd=worktree)
        diff = self._git("diff", f"{state['base']}..HEAD", cwd=worktree).stdout
        if not diff.strip():
            return {"escalation": f'the fixer said "{proposal.cause}" but changed nothing'}
        head = self._git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        return {"diff": diff, "reviewed_commit": head}

    def reviewer(self, state: HealState) -> dict[str, Any]:
        """Ask the reviewer model for a verdict on the diff. It has no way to change anything."""
        report = json.dumps({k: state["proposal"][k] for k in ("cause", "rationale")}, indent=2)
        prompt = reviewer_prompt(self.cfg, state["evidence"], report, state["diff"][:60_000])
        review, error, calls = ask(self.model, "reviewer", prompt, Review, self.hooks)
        if review is None:  # an unreadable verdict is never read as approval
            review = Review(verdict=Verdict.ESCALATE, notes=f"reviewer gave no readable verdict: {error[-400:]}")
        return {"review": review.model_dump(mode="json"), "model_calls": state["model_calls"] + calls}

    def human_approval(self, state: HealState) -> dict[str, Any]:
        """Pause the run. Nothing below this node happens until a person resumes it."""
        decision = interrupt(
            {
                "question": f"Merge this fix into the workspace and rerun {self.cfg.job}?",
                "cause": state["proposal"]["cause"],
                "rationale": state["proposal"]["rationale"],
                "reviewer_notes": state["review"]["notes"],
                "diff": state["diff"],
            }
        )
        if decision != "approve":  # anything but an exact "approve" is a no
            return {"decision": "reject", "outcome": "rejected", "summary": "a person rejected the reviewed fix"}
        return {"decision": "approve"}

    def merge_and_rerun(self, state: HealState) -> dict[str, Any]:
        """Merge the exact commit that was reviewed, then run the job with the configured command."""
        cfg = self.cfg
        cause = state["proposal"]["cause"]
        message = f"heal({cfg.job}): {cause[:100]}\n\nReviewer: {state['review']['notes']}"
        merged = self._git("merge", "--no-ff", "-q", "-m", message, state["reviewed_commit"], check=False)
        if merged.returncode != 0:
            self._git("merge", "--abort", check=False)
            detail = (merged.stderr or merged.stdout)[-300:]
            return {"escalation": f"the approved fix could not be merged into the workspace: {detail}"}
        merge_commit = self._git("rev-parse", "HEAD").stdout.strip()
        try:
            rerun = self._sh(
                list(cfg.rerun_command), cfg.workspace, check=False, timeout=cfg.rerun_timeout_s, name="rerun"
            )
            passed = rerun.returncode == 0
        except subprocess.TimeoutExpired:
            passed = False
        if passed:
            return {"merge_commit": merge_commit, "outcome": "fixed", "summary": cause}
        return {"merge_commit": merge_commit}

    def revert(self, state: HealState) -> dict[str, Any]:
        """The rerun still failed, so take the merge back out."""
        self._git("revert", "--no-edit", "-m", "1", state["merge_commit"])
        return {
            "outcome": "reverted",
            "summary": "the approved fix was merged but the rerun still failed, so it was reverted",
        }

    def escalate(self, state: HealState) -> dict[str, Any]:
        """Stop and hand over to a person, with the reason."""
        review = state.get("review", {})
        fallback = f"the reviewer said {review.get('verdict')} on the fixer's change: {review.get('notes')}"
        return {"outcome": "escalated", "summary": state.get("escalation") or fallback}

    def finish(self, state: HealState) -> dict[str, Any]:
        """Every run ends here: write the ledger line and remove the worktree."""
        cfg = self.cfg
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "job": cfg.job,
            "fingerprint": state["fingerprint"],
            "outcome": state["outcome"],
            "model_calls": state["model_calls"],
            "summary": state["summary"],
            "branch": state.get("branch", ""),
        }
        record(cfg.ledger, entry)
        if "worktree" in state:
            self._git("worktree", "remove", "--force", state["worktree"], check=False)
            if state["outcome"] == "fixed":  # otherwise the branch is kept for a person to read
                self._git("branch", "-D", state["branch"], check=False)
        return {}


Graph = CompiledStateGraph[HealState, None, HealState, HealState]


def build_graph(
    cfg: HealConfig, model: Model, hooks: Hooks | None = None, checkpointer: BaseCheckpointSaver[str] | None = None
) -> Graph:
    if not SAFE_NAME.fullmatch(cfg.job):
        raise ValueError(f"job name must match [A-Za-z0-9_-]+ (got {cfg.job!r})")
    nodes = Nodes(cfg, model, hooks or Hooks())

    def unless_escalated(next_node: str) -> Callable[[HealState], str]:
        return lambda state: "escalate" if state.get("escalation") else next_node

    def after_review(state: HealState) -> str:
        verdict = state["review"]["verdict"]
        if verdict == Verdict.APPROVE:
            return "human_approval"
        if verdict == Verdict.CHANGES and state["round"] < cfg.max_rounds:
            return "fixer"
        return "escalate"

    def after_merge(state: HealState) -> str:
        if state.get("escalation"):
            return "escalate"
        return "finish" if state.get("outcome") == "fixed" else "revert"

    graph = StateGraph(HealState)

    def add_traced(name: str) -> None:
        """Add a node, wrapped so the tracing seam in hooks.py sees it start and end."""
        node: Callable[[HealState], dict[str, Any]] = getattr(nodes, name)

        def run(state: HealState) -> dict[str, Any]:
            nodes.hooks.on_node_start(name, state)
            try:
                update = node(state)
            except GraphInterrupt:  # the approval gate: a pause, not a failure
                nodes.hooks.on_node_pause(name)
                raise
            except BaseException as error:
                nodes.hooks.on_error(error)
                raise
            nodes.hooks.on_node_end(name, update)
            return update

        graph.add_node(name, run)

    for name in (
        "gather_evidence",
        "fixer",
        "apply_in_worktree",
        "reviewer",
        "human_approval",
        "merge_and_rerun",
        "revert",
        "escalate",
        "finish",
    ):
        add_traced(name)
    graph.add_edge(START, "gather_evidence")
    graph.add_conditional_edges(
        "gather_evidence", lambda s: "finish" if s.get("outcome") else "fixer", ["finish", "fixer"]
    )
    graph.add_conditional_edges("fixer", unless_escalated("apply_in_worktree"), ["escalate", "apply_in_worktree"])
    graph.add_conditional_edges("apply_in_worktree", unless_escalated("reviewer"), ["escalate", "reviewer"])
    graph.add_conditional_edges("reviewer", after_review, ["human_approval", "fixer", "escalate"])
    graph.add_conditional_edges(
        "human_approval",
        lambda s: "merge_and_rerun" if s.get("decision") == "approve" else "finish",
        ["merge_and_rerun", "finish"],
    )
    graph.add_conditional_edges("merge_and_rerun", after_merge, ["escalate", "revert", "finish"])
    graph.add_edge("revert", "finish")
    graph.add_edge("escalate", "finish")
    graph.add_edge("finish", END)
    # interrupt() needs a checkpointer: the paused state has to live somewhere until the resume.
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def thread(run_id: str) -> RunnableConfig:
    """One run is one LangGraph thread: the checkpointer files the paused state under this id."""
    return {"configurable": {"thread_id": run_id}}


def start(graph: Graph, run_id: str) -> dict[str, Any]:
    """Run until the loop finishes or pauses for approval. The result has "__interrupt__" when paused."""
    if not SAFE_NAME.fullmatch(run_id):
        raise ValueError(f"run id must match [A-Za-z0-9_-]+ (got {run_id!r})")
    result: dict[str, Any] = graph.invoke({"run_id": run_id}, thread(run_id))
    return result


def resume(graph: Graph, run_id: str, approve: bool) -> dict[str, Any]:
    """Give the person's decision to a paused run."""
    if graph.get_state(thread(run_id)).next != ("human_approval",):
        raise RuntimeError(f"run {run_id!r} is not waiting for approval")
    result: dict[str, Any] = graph.invoke(Command(resume="approve" if approve else "reject"), thread(run_id))
    return result
