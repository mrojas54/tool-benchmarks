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
from dataclasses import dataclass
from pathlib import Path


class MalformedRunManifest(RuntimeError):
    """The run-manifest is unreadable or cannot define a run's branch set."""


@dataclass(frozen=True)
class RunManifest:
    """One orchestration run: its tickets and the branches its delegators worked on."""

    run: str
    tickets: tuple[str, ...]
    branches: frozenset[str]
    worktrees: tuple[str, ...] = ()

    @property
    def ticket_count(self) -> int:
        return len(self.tickets)


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
        worktrees=_str_tuple(data, "worktrees"),
    )
