"""Path rules, tested directly. The graph-level refusals are in test_graph.py."""

from dataclasses import replace
from pathlib import Path

import pytest

from heal_loop import HealConfig
from heal_loop.guardrails import path_problem, violations
from heal_loop.schemas import FileEdit


def test_allowed_path_passes(cfg: HealConfig) -> None:
    assert path_problem(cfg.workspace, "app/report.py", cfg) is None
    assert path_problem(cfg.workspace, "app/new_module.py", cfg) is None


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("../outside.py", "path traversal"),
        ("app/../../outside.py", "path traversal"),
        ("/etc/passwd", "absolute path"),
        ("app\\report.py", "backslash or NUL in path"),
        (".git/hooks/pre-commit", "inside .git"),
        ("job.py", "protected file"),
        ("README.md", "not on the allow-list"),
        ("app", "is a directory"),
    ],
)
def test_refused_paths(cfg: HealConfig, path: str, problem: str) -> None:
    assert path_problem(cfg.workspace, path, cfg) == problem


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "app/.env",
        "secrets.yaml",
        "app/secrets.py",
        "deploy/server.pem",
        "nightly.timer",
        "JOB.PY",
        "app/.ENV",
    ],
)
def test_deny_list_wins_even_when_everything_is_allowed(cfg: HealConfig, path: str) -> None:
    wide_open = replace(cfg, allow=("*",))
    assert path_problem(cfg.workspace, path, wide_open) == "protected file"


def test_symlinks_are_refused(cfg: HealConfig, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (cfg.workspace / "app" / "link").symlink_to(outside)  # a way out of the workspace
    (cfg.workspace / "app" / "alias.py").symlink_to(cfg.workspace / "job.py")  # the protected job, renamed
    wide_open = replace(cfg, allow=("*",))
    assert path_problem(cfg.workspace, "app/link/x.py", wide_open) == "goes through a symlink"
    assert path_problem(cfg.workspace, "app/alias.py", cfg) == "goes through a symlink"


def test_violations_lists_each_refused_edit(cfg: HealConfig) -> None:
    edits = [FileEdit(path="app/report.py", new_content=""), FileEdit(path="job.py", new_content="")]
    assert violations(cfg.workspace, edits, cfg) == ["job.py: protected file"]
