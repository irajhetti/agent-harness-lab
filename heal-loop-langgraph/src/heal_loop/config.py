"""Everything the operator decides. The model can change none of it."""

from dataclasses import dataclass
from pathlib import Path

# Always refused, even when an allow pattern matches. Add the file that defines the job's success.
DEFAULT_DENY = (".env", ".env.*", "secrets*", "*.pem", "*.key", ".github/*", "*.service", "*.timer")


@dataclass(frozen=True)
class HealConfig:
    job: str  # name of the scheduled job, used in branch names and the ledger
    workspace: Path  # the git repository the job runs from
    rerun_command: tuple[str, ...]  # how to run the job again; argument list, no shell
    error_log: Path  # the job's append-only error log
    worktrees_dir: Path  # where throwaway worktrees are made (outside the workspace)
    ledger: Path  # JSONL history of past runs, read by the escalation rules
    allow: tuple[str, ...]  # fnmatch patterns the fixer may edit
    deny: tuple[str, ...] = DEFAULT_DENY
    max_rounds: int = 2  # fixer + reviewer passes in total, as in heal.ts
    daily_call_cap: int = 40  # model calls allowed per 24 hours across all jobs
    rerun_timeout_s: int = 120
