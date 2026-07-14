"""TB-22 / S37: corpus-freeze manifest I/O — round-trips SessionRefs and pins the
discovered fingerprint so a later run can replay identical inputs."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from toolbench.freeze import MANIFEST_VERSION, read_manifest, write_manifest
from toolbench.sources import AgentCensus, SessionRef


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


def test_manifest_legacy_derives_is_subagent_from_path() -> None:
    """Pre-flag manifests omitted is_subagent; replay still excludes nested paths."""
    with TemporaryDirectory() as d:
        path = Path(d) / "corpus.manifest"
        path.write_text(
            json.dumps(
                {
                    "version": MANIFEST_VERSION,
                    "fingerprint": "x",
                    "count": 1,
                    "refs": [
                        {
                            "agent": "claude-code",
                            "source": "raw",
                            "project": "subagents",
                            "session_id": "child",
                            "path": "/tmp/proj-b/subagents/child.jsonl",
                        }
                    ],
                }
            )
        )
        m = read_manifest(str(path))
        assert m.refs[0].is_subagent is True


def test_stale_false_flag_does_not_survive_replay() -> None:
    """TB-29 REGRESSION (caught in review). A manifest frozen BEFORE the discovery fix
    persists an explicit `"is_subagent": false` for a real subagent session -- written
    by the very code that could never set it True. If an explicit flag beat the path,
    that stale `false` would carry the no-op into replay forever: `--freeze old.manifest
    --exclude-subagents` would keep silently including subagents while the report
    claimed otherwise. The path is ground truth; a stale flag is not."""
    with TemporaryDirectory() as d:
        path = Path(d) / "corpus.manifest"
        path.write_text(
            json.dumps(
                {
                    "version": MANIFEST_VERSION,
                    "fingerprint": "x",
                    "count": 1,
                    "refs": [
                        {
                            "agent": "claude-code",
                            "source": "raw",
                            "project": "-Users-me-tool-benchmarks",
                            "session_id": "child",
                            # The real nested layout, stamped False by the old bug.
                            "path": "/tmp/proj/116ef75f/subagents/agent-abc.jsonl",
                            "is_subagent": False,
                        }
                    ],
                }
            )
        )
        m = read_manifest(str(path))
        assert m.refs[0].is_subagent is True


def test_write_manifest_without_census_round_trips_none() -> None:
    """`census` is optional at write time (TB-37): omitting it writes a valid v2
    manifest with no `census` key, and `read_manifest` reports that as `None` --
    the same value it reports for a genuinely pre-TB-37 manifest."""
    with TemporaryDirectory() as d:
        path = str(Path(d) / "corpus.manifest")
        write_manifest(path, _refs(), "abc123")
        m = read_manifest(path)
        assert m.census is None
        assert m.version == MANIFEST_VERSION
        raw = json.loads(Path(path).read_text())
        assert "census" not in raw


def test_write_manifest_persists_census_totals_and_archive_total() -> None:
    census = AgentCensus(totals={"claude": 12, "codex": 3}, archive_total=15)
    with TemporaryDirectory() as d:
        path = str(Path(d) / "corpus.manifest")
        write_manifest(path, _refs(), "abc123", census=census)
        m = read_manifest(path)
        assert m.census is not None
        assert m.census.totals == {"claude": 12, "codex": 3}
        assert m.census.archive_total == 15
        assert m.census.unavailable_reason is None
        # `residual` is derived, not stored, and must reconstruct correctly.
        assert m.census.residual == 0


def test_write_manifest_persists_an_unavailable_census() -> None:
    """A census that itself failed at freeze time round-trips its `unavailable_reason`
    intact, not silently dropped to an empty census or a `None` census -- those are
    three different facts (real, absent, attempted-and-failed) and TB-37 keeps them
    distinguishable through the manifest."""
    census = AgentCensus(totals={}, archive_total=0, unavailable_reason="boom")
    with TemporaryDirectory() as d:
        path = str(Path(d) / "corpus.manifest")
        write_manifest(path, _refs(), "abc123", census=census)
        m = read_manifest(path)
        assert m.census is not None
        assert m.census.totals == {}
        assert m.census.archive_total == 0
        assert m.census.unavailable_reason == "boom"


def test_manifest_version_is_v2() -> None:
    """TB-37 bumps the format so a reader can tell whether a census block is even
    possible on this manifest without inspecting its keys."""
    assert MANIFEST_VERSION == "toolbench-freeze-2"


def test_explicit_false_is_honoured_for_a_genuine_non_subagent() -> None:
    """The self-heal must not overreach: a path with no /subagents/ segment stays False."""
    with TemporaryDirectory() as d:
        path = Path(d) / "corpus.manifest"
        path.write_text(
            json.dumps(
                {
                    "version": MANIFEST_VERSION,
                    "fingerprint": "x",
                    "count": 1,
                    "refs": [
                        {
                            "agent": "claude-code",
                            "source": "raw",
                            "project": "-Users-me-tool-benchmarks",
                            "session_id": "parent",
                            "path": "/tmp/proj/parent.jsonl",
                            "is_subagent": False,
                        }
                    ],
                }
            )
        )
        m = read_manifest(str(path))
        assert m.refs[0].is_subagent is False
