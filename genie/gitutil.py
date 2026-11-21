"""Thin git helpers. All paths returned are repo-root-relative, POSIX style."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def toplevel(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").strip()


def current_branch(root: Path) -> str:
    return git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()


def tracked_changes(root: Path) -> list[str]:
    out = git(root, "status", "--porcelain", "--untracked-files=no")
    return [line[3:] for line in out.splitlines() if line.strip()]


def untracked(root: Path) -> set[str]:
    out = git(root, "ls-files", "-z", "--others", "--exclude-standard")
    return {p for p in out.split("\0") if p}


def changed_since(root: Path, base: str) -> list[str]:
    out = git(root, "diff", "--name-only", base)
    return [p for p in out.splitlines() if p]


def show(root: Path, rev: str, path: str) -> str | None:
    p = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return p.stdout if p.returncode == 0 else None


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines(root: Path, base: str, path: str) -> list[tuple[int, str]]:
    """Lines added in the working tree relative to ``base`` for one tracked file."""
    out = git(root, "diff", "-U0", base, "--", path)
    lines: list[tuple[int, str]] = []
    n = 0
    for raw in out.splitlines():
        m = _HUNK.match(raw)
        if m:
            n = int(m.group(1))
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            lines.append((n, raw[1:]))
            n += 1
    return lines


def diff(root: Path, base: str, ref: str | None = None) -> str:
    args = ["diff", base] + ([ref] if ref else [])
    return git(root, *args)


def diff_stat(root: Path, base: str, ref: str | None = None) -> dict:
    args = ["diff", "--numstat", base] + ([ref] if ref else [])
    added = removed = files = 0
    for line in git(root, *args).splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            files += 1
            added += int(parts[0]) if parts[0].isdigit() else 0
            removed += int(parts[1]) if parts[1].isdigit() else 0
    return {"files": files, "added": added, "removed": removed}


def add_local_exclude(root: Path, pattern: str) -> bool:
    """Ignore ``pattern`` via .git/info/exclude (local only, never committed)."""
    exclude = Path(git(root, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = root / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if pattern in text.splitlines():
        return False
    with open(exclude, "a", encoding="utf-8") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(pattern + "\n")
    return True
