"""Which code produced a record (the brief: persist experiments, configs, strategies and commits).

Recorded on every run and experiment row, immutable afterwards (schema triggers):
- code_version: "<app version>+<git HEAD, 12 chars>" ("+nogit" outside a repository). HEAD is read from
  the .git files directly: no subprocess, no network.
- code_tree_sha256: a hash of the exact bytes that ran: the paper_advisor package, config/, and the
  /paper-* procedures the advisor follows (.claude/commands). HEAD alone cannot show uncommitted edits;
  two runs with equal tree hashes ran identical code and instructions.

code_identity() is computed once per process and cached, so it describes the code this process loaded,
not edits made on disk while it runs (e.g. a long-lived dashboard creating an experiment later).
SYNC WARNING: COMMAND_FILES mirrors the slash commands in ../.claude/commands/.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from . import __version__
from .paths import APP_DIR, PROJECT_ROOT

COMMAND_FILES = ("paper-morning.md", "paper-status.md", "paper-review.md")
CODE_SUFFIXES = (".py", ".sql", ".css", ".js", ".json")


def git_head(root: Path = PROJECT_ROOT) -> str | None:
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return head or None  # detached HEAD holds the sha itself
    ref = head[5:]
    try:
        loose = git / ref
        if loose.is_file():  # a loose ref is newer than any packed copy
            return loose.read_text().strip() or None
        packed = git / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except OSError:
        return None
    return None


def code_tree_sha256(app_dir: Path = APP_DIR, root: Path = PROJECT_ROOT) -> str:
    files = [p for sub in ("paper_advisor", "config") if (app_dir / sub).is_dir()
             for p in (app_dir / sub).rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix in CODE_SUFFIXES]
    files += [root / ".claude" / "commands" / n for n in COMMAND_FILES if (root / ".claude" / "commands" / n).is_file()]
    h = hashlib.sha256()
    for p in sorted(files, key=lambda x: str(x.relative_to(root))):
        h.update(str(p.relative_to(root)).encode())
        h.update(b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


@lru_cache(maxsize=1)
def code_identity() -> dict:
    head = git_head()
    return {"code_version": f"{__version__}+{head[:12] if head else 'nogit'}", "code_tree_sha256": code_tree_sha256()}
