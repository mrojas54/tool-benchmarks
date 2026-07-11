"""TB-22 / S37: corpus-freeze manifest I/O — round-trips SessionRefs and pins the
discovered fingerprint so a later run can replay identical inputs."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from toolbench.freeze import MANIFEST_VERSION, read_manifest, write_manifest
from toolbench.sources import SessionRef


def _refs() -> list[SessionRef]:
    return [
        SessionRef("claude", "agentsview", "proj-a", "good-1", None),
        SessionRef("claude-code", "raw", "proj-b", "good-2", "/tmp/proj-b/good-2.jsonl"),
        SessionRef(
            "claude-code",
            "raw",
            "proj-b",
            "sub-3",
            "/tmp/proj-b/subagents/sub-3.jsonl",
            True,
        ),
    ]


def test_manifest_round_trips_refs() -> None:
    with TemporaryDirectory() as d:
        path = str(Path(d) / "corpus.manifest")
        write_manifest(path, _refs(), "abc123")
        m = read_manifest(path)
        assert m.refs == _refs()


def test_manifest_round_trips_is_subagent() -> None:
    with TemporaryDirectory() as d:
        path = str(Path(d) / "corpus.manifest")
        write_manifest(path, _refs(), "abc123")
        m = read_manifest(path)
        by_id = {r.session_id: r for r in m.refs}
        assert by_id["good-1"].is_subagent is False
        assert by_id["good-2"].is_subagent is False
        assert by_id["sub-3"].is_subagent is True


def test_manifest_stores_fingerprint_and_count() -> None:
    with TemporaryDirectory() as d:
        path = str(Path(d) / "corpus.manifest")
        write_manifest(path, _refs(), "deadbeef")
        m = read_manifest(path)
        assert m.fingerprint == "deadbeef"
        assert m.count == 3
        assert m.version == MANIFEST_VERSION


def test_manifest_preserves_agentsview_none_path_and_raw_path() -> None:
    with TemporaryDirectory() as d:
        path = str(Path(d) / "corpus.manifest")
        write_manifest(path, _refs(), "x")
        m = read_manifest(path)
        by_id = {r.session_id: r for r in m.refs}
        assert by_id["good-1"].path is None  # agentsview ref exports by id
        assert by_id["good-2"].path == "/tmp/proj-b/good-2.jsonl"
