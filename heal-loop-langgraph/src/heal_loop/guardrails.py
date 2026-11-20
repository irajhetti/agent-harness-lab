"""Path rules for the fixer's edits. Enforced in code before anything is written.

The prompt also tells the model these rules, but the prompt is not the control. This file is.
"""

from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from .config import HealConfig
from .schemas import FileEdit


def path_problem(root: Path, rel: str, cfg: HealConfig) -> str | None:
    """Why `rel` may not be written under `root`, or None if it may."""
    if "\\" in rel or "\x00" in rel:
        return "backslash or NUL in path"
    path = PurePosixPath(rel)
    if path.is_absolute():
        return "absolute path"
    if ".." in path.parts:
        return "path traversal"
    if ".git" in (part.lower() for part in path.parts):
        return "inside .git"
    # Refuse any path that is, or passes through, a symlink. One could point outside the workspace,
    # or at a protected file under a harmless-looking name.
    if (root / path).resolve() != root.resolve() / path:
        return "goes through a symlink"
    if (root / path).is_dir():
        return "is a directory"
    norm = path.as_posix()
    # Deny patterns match the whole path or just the file name, so app/.env is refused too.
    # They ignore case, because some file systems do.
    names = (norm.lower(), path.name.lower())
    if any(fnmatchcase(name, pattern.lower()) for name in names for pattern in cfg.deny):
        return "protected file"
    if not any(fnmatchcase(norm, pattern) for pattern in cfg.allow):
        return "not on the allow-list"
    return None


def violations(root: Path, edits: list[FileEdit], cfg: HealConfig) -> list[str]:
    """One line per refused edit. An empty list means every edit may be applied."""
    found = []
    for edit in edits:
        problem = path_problem(root, edit.path, cfg)
        if problem:
            found.append(f"{edit.path}: {problem}")
    return found
