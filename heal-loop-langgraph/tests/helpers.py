"""Shared test helpers: run the loop with scripted replies, and look at the toy repository."""

import subprocess
from pathlib import Path
from typing import Any

from heal_loop import HealConfig, Hooks, ScriptedModel, build_graph, start
from heal_loop.fixture import fenced
from heal_loop.graph import Graph, thread

RUN = "run1"
THREAD = thread(RUN)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def snapshot(repo: Path) -> dict[str, bytes]:
    """Every file in the working tree, byte for byte. `.git` is left out."""
    return {
        str(path.relative_to(repo)): path.read_bytes()
        for path in sorted(repo.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }


def run_loop(
    cfg: HealConfig,
    fixer: list[dict[str, Any] | str],
    reviewer: list[dict[str, Any] | str],
    hooks: Hooks | None = None,
    run_id: str = RUN,
) -> tuple[Graph, ScriptedModel, dict[str, Any]]:
    """Start a run. A dict reply is sent as a fenced JSON block, a str reply is sent as it is."""

    def texts(replies: list[dict[str, Any] | str]) -> list[str]:
        return [reply if isinstance(reply, str) else fenced(reply) for reply in replies]

    model = ScriptedModel({"fixer": texts(fixer), "reviewer": texts(reviewer)})
    graph = build_graph(cfg, model, hooks)
    return graph, model, start(graph, run_id)


def roles(model: ScriptedModel) -> list[str]:
    return [role for role, _ in model.calls]
