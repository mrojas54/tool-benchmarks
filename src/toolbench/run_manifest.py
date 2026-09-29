"""The S40 run-manifest: which branches constitute one orchestration run.

JSON, following the `--freeze` manifest precedent (S37, `toolbench/freeze.py`) --
no new format, stdlib only (S20).

The orchestrator emits this **at dispatch**, while the branch data is still live.
`.lattice/orchestration/agents.md` cannot serve: its Active table (the only one
with Branch/Worktree columns) is overwritten each dispatch tick and collapses to
"(none -- dispatch complete)" on finish, while the surviving Archived table has no
branch column at all. By the time a run is measurable, agents.md has discarded the
key we filter on.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class MalformedRunManifest(RuntimeError):
    """The run-manifest is unreadable or cannot define a run's branch set."""


@dataclass(frozen=True)
class RunManifest:
    """One orchestration run: its tickets and the branches its delegators worked on.

    `worktrees` are the delegators' linked-worktree paths, recorded at dispatch like
    `branches`. They claim only DETACHED-HEAD entries whose `cwd` lies inside one (see
    `worktree_for`): a detached checkout stamps no branch, so its directory is the only
    dispatch-time key left. Never the repo root -- that would claim every detached
    session in the clone.
    """

    run: str
    tickets: tuple[str, ...]
    branches: frozenset[str]
    worktrees: tuple[str, ...] = ()

    @property
    def ticket_count(self) -> int:
        return len(self.tickets)

    def worktree_for(self, cwd: str) -> str | None:
        """The manifest worktree containing `cwd`, or None.

        Compared by path COMPONENTS, never string prefix: `/wt/tb-1` must not claim
        `/wt/tb-12`. When worktrees nest, the deepest wins, so each entry is claimed
        exactly once. Pure path math -- the transcript's machine may not be this one.
        """
        if not cwd:
            return None
        parts = _path_parts(cwd)
        best: str | None = None
        best_len = 0
        for worktree in self.worktrees:
            tree = _path_parts(worktree)
            if len(tree) > best_len and parts[: len(tree)] == tree:
                best, best_len = worktree, len(tree)
        return best


def _path_parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(os.path.normpath(os.path.expanduser(path))).parts


def _worktree_tuple(data: dict[str, object], path: str) -> tuple[str, ...]:
    """Normalized absolute worktree paths. A relative path cannot be matched against a
    transcript's absolute `cwd`, and "/" would claim every detached entry anywhere --
    both are refused rather than silently matching nothing or everything."""
    out = []
    for raw in _str_tuple(data, "worktrees"):
        norm = os.path.normpath(os.path.expanduser(raw))
        if not os.path.isabs(norm) or norm == os.sep:
            raise MalformedRunManifest(
                f"{path}: worktree {raw!r} must be an absolute path below the filesystem root"
            )
        out.append(norm)
    return tuple(out)


def _str_tuple(data: dict[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise MalformedRunManifest(f"`{key}` must be a list of strings")
    return tuple(str(v) for v in value)


def read_run_manifest(path: str) -> RunManifest:
    """Read a run-manifest. Raises MalformedRunManifest on anything unusable."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise MalformedRunManifest(f"{path} could not be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        # UnicodeDecodeError subclasses ValueError, not OSError -- it would
        # otherwise escape as an uncaught traceback instead of a hard stop (S23).
        raise MalformedRunManifest(f"{path} is not valid UTF-8: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedRunManifest(
            f"{path} is not valid JSON (the run-manifest is JSON, not markdown): {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise MalformedRunManifest(f"{path} must contain a JSON object")

    branches = _str_tuple(data, "branches")
    if not branches:
        # A run with no branches attributes nothing; every ticket would read as
        # costing zero. Refuse loudly rather than emit a confident wrong number.
        raise MalformedRunManifest(
            f"{path} defines no `branches`; a run with no branch set can attribute nothing"
        )

    run = data.get("run", "")
    if not isinstance(run, (str, int)):
        raise MalformedRunManifest(f"`run` must be a string or int, got {type(run).__name__}")
    return RunManifest(
        run=str(run),
        tickets=_str_tuple(data, "tickets"),
        branches=frozenset(branches),
        worktrees=_worktree_tuple(data, path),
    )
