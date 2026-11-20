"""The CLI, run as real separate processes: start pauses, a second process resumes from SQLite."""

import subprocess
import sys
from pathlib import Path

from helpers import snapshot

from heal_loop.fixture import BROKEN, FIXED


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "heal_loop", *args], capture_output=True, text=True)


def test_start_pauses_then_a_second_process_approves(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    started = cli("start", "--dir", str(demo))
    assert started.returncode == 0, started.stderr
    assert "PAUSED" in started.stdout and "Nothing has been merged" in started.stdout
    assert (demo / "repo" / "app" / "report.py").read_text() == BROKEN

    resumed = cli("resume", "--dir", str(demo), "--approve")
    assert resumed.returncode == 0, resumed.stderr
    assert resumed.stdout.startswith("FIXED:")
    assert (demo / "repo" / "app" / "report.py").read_text() == FIXED

    again = cli("resume", "--dir", str(demo), "--approve")
    assert again.returncode == 1 and "not waiting for approval" in again.stderr


def test_reject_from_the_cli_changes_nothing(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    cli("start", "--dir", str(demo))
    before = snapshot(demo / "repo")
    resumed = cli("resume", "--dir", str(demo), "--reject")
    assert resumed.stdout.startswith("REJECTED:")
    assert snapshot(demo / "repo") == before


def test_bad_fix_from_the_cli_is_reverted(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    cli("start", "--dir", str(demo), "--bad-fix")
    resumed = cli("resume", "--dir", str(demo), "--approve")
    assert resumed.stdout.startswith("REVERTED:")
    assert (demo / "repo" / "app" / "report.py").read_text() == BROKEN


def test_start_refuses_a_directory_that_is_not_empty(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("mine")
    assert cli("start", "--dir", str(tmp_path)).returncode == 2
    assert (tmp_path / "keep.txt").read_text() == "mine"
