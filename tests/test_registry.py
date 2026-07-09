"""pick_adapter ordering: hermes claims by source, everything else by content (TB-13, Task 4)."""

import subprocess

import pytest

from toolbench.adapters import ComposedAdapter, UnknownSchema
from toolbench.hermes import HermesAdapter
from toolbench.registry import pick_adapter
from toolbench.sources import SessionRef


def _ok(stdout: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_hermes_ref_picks_the_hermes_adapter():
    ref = SessionRef(
        agent="hermes", source="agentsview", project="h", session_id="hermes:1", path=None
    )
    assert isinstance(pick_adapter(ref), HermesAdapter)


def test_hermes_with_a_path_is_not_claimed_by_hermes_adapter(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"sessionId":"s1"}\n', encoding="utf-8")
    ref = SessionRef(agent="hermes", source="raw", project="h", session_id="s1", path=str(p))
    assert isinstance(pick_adapter(ref), ComposedAdapter)


def test_claude_ref_picks_the_composed_adapter():
    ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="c:1", path=None)
    assert isinstance(pick_adapter(ref), ComposedAdapter)


def test_composed_adapter_parses_a_raw_claude_session(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        '{"sessionId":"s1","timestamp":"t0","message":{"content":'
        '[{"type":"tool_use","id":"u1","name":"Bash","input":{}}]}}\n'
        '{"sessionId":"s1","timestamp":"t1","message":{"content":'
        '[{"type":"tool_result","tool_use_id":"u1","content":"ok"}]}}\n',
        encoding="utf-8",
    )
    ref = SessionRef(agent="claude", source="raw", project="p", session_id="s1", path=str(p))
    result = pick_adapter(ref).parse(ref)
    assert len(result.calls) == 1
    assert result.calls[0].name == "Bash"
    assert result.calls[0].agent == "claude"  # ref fields flow through
    assert result.calls[0].project == "p"


def test_composed_adapter_raises_unknown_schema_for_codex():
    ref = SessionRef(
        agent="codex", source="agentsview", project="p", session_id="codex:1", path=None
    )
    adapter = ComposedAdapter(runner=lambda argv: _ok('{"type":"session_meta","payload":{}}\n'))
    with pytest.raises(UnknownSchema):
        adapter.parse(ref)


def test_unknown_schema_is_a_runtime_error_so_passive_demotes_it():
    assert issubclass(UnknownSchema, RuntimeError)
