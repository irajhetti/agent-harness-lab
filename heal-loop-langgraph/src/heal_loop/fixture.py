"""A tiny broken scheduled job, used by the tests and the CLI demo.

`job.py` is the job and defines success. `app/report.py` has a one-line bug. The fixer may
edit `app/*.py` only, so it cannot make the job pass by rewriting the job.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DEFAULT_DENY, HealConfig

JOB = '''"""The scheduled job. It defines success, so the fixer may not edit it."""
import sys

from app.report import total

got = total([3, 4, 5])
if got != 12:
    sys.exit(f"nightly-report: expected total 12, got {got}")
print("nightly-report ok")
'''
BROKEN = "def total(rows):\n    return sum(rows[1:])\n"
FIXED = "def total(rows):\n    return sum(rows)\n"
STILL_BROKEN = "def total(rows):\n    return sum(rows[2:])\n"


def fixture_config(root: Path) -> HealConfig:
    return HealConfig(
        job="nightly-report",
        workspace=root / "repo",
        rerun_command=(sys.executable, "-B", "job.py"),  # -B: leave no __pycache__ in the workspace
        error_log=root / "job.err.log",
        worktrees_dir=root / "worktrees",
        ledger=root / "heal.jsonl",
        allow=("app/*.py",),
        deny=(*DEFAULT_DENY, "job.py"),
    )


def make_fixture(root: Path) -> HealConfig:
    """Create the toy repository under `root`, run the job once so it fails, and keep its error log."""
    cfg = fixture_config(root)
    repo = cfg.workspace
    (repo / "app").mkdir(parents=True)
    (repo / "job.py").write_text(JOB)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "report.py").write_text(BROKEN)
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-q", "-m", "initial"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    failed = subprocess.run(list(cfg.rerun_command), cwd=repo, capture_output=True, text=True)
    cfg.error_log.write_text(f"{datetime.now(UTC).isoformat()} nightly-report failed\n{failed.stderr}")
    return cfg


def fenced(payload: dict[str, Any]) -> str:
    """A model reply the way the prompts ask for it: prose, then one fenced JSON block."""
    return f"Here is my answer.\n```json\n{json.dumps(payload)}\n```"


def proposal(content: str = FIXED, path: str = "app/report.py") -> dict[str, Any]:
    return {
        "fixable": True,
        "cause": "total() skips the first row",
        "rationale": "Sum every row instead of rows[1:].",
        "edits": [{"path": path, "new_content": content}],
    }


def review(verdict: str = "APPROVE", notes: str = "Minimal, and it addresses the cause.") -> dict[str, Any]:
    return {"verdict": verdict, "notes": notes}
