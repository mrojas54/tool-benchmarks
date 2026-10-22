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
from pathlib import Path, PurePath


class MalformedRunManifest(RuntimeError):
    """The run-manifest is unreadable or cannot define a run's branch set."""


def normalize_path(path: str) -> PurePath:
    """`expanduser` + lexical normalization (collapse `//`, `.`, `..`, trailing `/`).

    Deliberately lexical, never `resolve()`: a run is measured after its worktrees
    are reclaimed, so the paths usually no longer exist, and a filesystem read here
    would make attribution depend on what happens to be on disk today.
    """
    return PurePath(os.path.normpath(os.path.expanduser(path)))


def is_within(cwd: PurePath, root: PurePath) -> bool:
    """Is `cwd` equal to `root` or nested under it? Both are already normalized.

    Compared by path COMPONENT, not string prefix: `/wt/tb-1` must not claim
    `/wt/tb-12`, which a `startswith` check would.
    """
    # `parents` excludes the path itself, so the worktree root needs its own case.
    return cwd == root or root in cwd.parents


@dataclass(frozen=True)
class RunManifest:
    """One orchestration run: its tickets and the branches its delegators worked on.

    `worktrees` (TB-44) are the paths the orchestrator dispatched delegators into. A
    path recorded at dispatch is a correlation key of the same standing as a branch
    name: a detached (`HEAD`) entry whose `cwd` sits inside one belongs to the run.
    """

    run: str
    tickets: tuple[str, ...]
    branches: frozenset[str]
    worktrees: tuple[str, ...] = ()

    @property
    def ticket_count(self) -> int:
        return len(self.tickets)

    def worktree_for(self, cwd: str) -> str | None:
        """The manifest worktree containing `cwd`, as written in the manifest, or None.

        When worktrees nest, the deepest one wins, so usage is credited to the most
        specific key. An empty cwd (the entry recorded none) never matches.
        """
        if not cwd:
            return None
        target = normalize_path(cwd)
        matches = [w for w in self.worktrees if is_within(target, normalize_path(w))]
        return max(matches, key=lambda w: len(normalize_path(w).parts), default=None)


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
    worktrees = _str_tuple(data, "worktrees")
    relative = [w for w in worktrees if not normalize_path(w).is_absolute()]
    if relative:
        # A relative path would match against whatever cwd the ANALYZER runs from --
        # a correlation key that changes meaning with the caller is no key at all.
        raise MalformedRunManifest(
            f"`worktrees` must be absolute (or `~`-rooted) paths; got {', '.join(relative)}"
        )
    return RunManifest(
        run=str(run),
        tickets=_str_tuple(data, "tickets"),
        branches=frozenset(branches),
        worktrees=worktrees,
    )
