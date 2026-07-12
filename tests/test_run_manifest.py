"""Evals for the S40 run-manifest reader (toolbench.run_manifest).

JSON, following the S37 freeze-manifest precedent -- no new format, stdlib only.
The orchestrator emits this at DISPATCH, while branch data is still live: agents.md
discards its Branch column on run completion, which is why it cannot serve.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from toolbench.run_manifest import MalformedRunManifest, read_run_manifest


def _write(tmp_path: Path, payload: object) -> str:
    path = tmp_path / "run.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_reads_run_tickets_and_branches(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "run": "2",
            "tickets": ["TB-18", "TB-19", "TB-20"],
            "branches": ["feat/tb-18", "tb-19-pytest-gate", "tb-20-cache-read"],
            "worktrees": ["~/wt/tb-19"],
        },
    )
    manifest = read_run_manifest(path)

    assert manifest.run == "2"
    assert manifest.tickets == ("TB-18", "TB-19", "TB-20")
    assert manifest.branches == frozenset(
        {"feat/tb-18", "tb-19-pytest-gate", "tb-20-cache-read"}
    )
    assert manifest.ticket_count == 3


def test_worktrees_optional(tmp_path: Path) -> None:
    path = _write(tmp_path, {"run": "1", "tickets": ["TB-1"], "branches": ["b"]})
    assert read_run_manifest(path).worktrees == ()


def test_empty_branches_is_malformed(tmp_path: Path) -> None:
    """A manifest with no branches can attribute nothing -- refuse it loudly rather
    than emit a confident zero for every ticket."""
    path = _write(tmp_path, {"run": "1", "tickets": ["TB-1"], "branches": []})
    with pytest.raises(MalformedRunManifest, match="branches"):
        read_run_manifest(path)


def test_missing_branches_key_is_malformed(tmp_path: Path) -> None:
    path = _write(tmp_path, {"run": "1", "tickets": ["TB-1"]})
    with pytest.raises(MalformedRunManifest, match="branches"):
        read_run_manifest(path)


def test_non_json_is_malformed(tmp_path: Path) -> None:
    """Named because the ticket originally pointed --run-manifest at agents.md, a
    markdown file. Feeding one in must fail with a clear message, not a stack trace."""
    path = tmp_path / "agents.md"
    path.write_text("# Agents\n\n| Role | Ticket |\n", encoding="utf-8")
    with pytest.raises(MalformedRunManifest, match="not valid JSON"):
        read_run_manifest(str(path))


def test_branch_list_must_be_strings(tmp_path: Path) -> None:
    path = _write(tmp_path, {"run": "1", "tickets": ["TB-1"], "branches": [17]})
    with pytest.raises(MalformedRunManifest, match="branches"):
        read_run_manifest(path)


def test_null_run_is_malformed(tmp_path: Path) -> None:
    path = _write(tmp_path, {"run": None, "tickets": ["TB-1"], "branches": ["b"]})
    with pytest.raises(MalformedRunManifest, match="run"):
        read_run_manifest(path)


def test_list_run_is_malformed(tmp_path: Path) -> None:
    path = _write(tmp_path, {"run": [1, 2], "tickets": ["TB-1"], "branches": ["b"]})
    with pytest.raises(MalformedRunManifest, match="run"):
        read_run_manifest(path)


def test_missing_run_key_defaults_to_empty_string(tmp_path: Path) -> None:
    path = _write(tmp_path, {"tickets": ["TB-1"], "branches": ["b"]})
    assert read_run_manifest(path).run == ""


def test_nonexistent_path_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.json"
    with pytest.raises(MalformedRunManifest):
        read_run_manifest(str(path))
