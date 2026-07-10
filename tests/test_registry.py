"""pick_adapter ordering: hermes claims by source, everything else by content (TB-13, Task 4)."""

from pathlib import Path
import subprocess

import pytest

from toolbench.adapters import ComposedAdapter, UnknownSchema
from toolbench.hermes import HermesAdapter
from toolbench.registry import pick_adapter
from toolbench.sources import SessionRef


def _ok(stdout: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_hermes_ref_picks_the_hermes_adapter() -> None:
    ref = SessionRef(
        agent="hermes", source="agentsview", project="h", session_id="hermes:1", path=None
    )
    assert isinstance(pick_adapter(ref), HermesAdapter)


def test_hermes_with_a_path_is_not_claimed_by_hermes_adapter(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text('{"sessionId":"s1"}\n', encoding="utf-8")
    ref = SessionRef(agent="hermes", source="raw", project="h", session_id="s1", path=str(p))
    assert isinstance(pick_adapter(ref), ComposedAdapter)


def test_claude_ref_picks_the_composed_adapter() -> None:
    ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="c:1", path=None)
    assert isinstance(pick_adapter(ref), ComposedAdapter)


def test_composed_adapter_parses_a_raw_claude_session(tmp_path: Path) -> None:
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


def test_composed_adapter_parses_codex_end_to_end() -> None:
    """Was `..._raises_unknown_schema_for_codex`. TB-12: the seam now carries codex."""
    ref = SessionRef(
        agent="codex", source="agentsview", project="p", session_id="codex:1", path=None
    )
    body = (
        '{"type":"session_meta","payload":{"session_id":"c1"}}\n'
        '{"type":"response_item","timestamp":"t","payload":'
        '{"type":"function_call","name":"exec_command","arguments":"{}","call_id":"k1"}}\n'
        '{"type":"response_item","timestamp":"t","payload":'
        '{"type":"function_call_output","call_id":"k1","output":"ok"}}\n'
    )
    adapter = ComposedAdapter(runner=lambda argv: _ok(body))
    result = adapter.parse(ref)
    assert len(result.calls) == 1
    assert result.calls[0].name == "exec_command"
    assert result.calls[0].agent == "codex"  # ref fields flow through
    assert result.calls[0].session_id == "c1"  # lifted from session_meta


def test_composed_adapter_still_raises_unknown_schema_for_an_unregistered_schema() -> None:
    """cursor remains unregistered; the terminal fallback must stay loud."""
    ref = SessionRef(
        agent="cursor", source="agentsview", project="p", session_id="cursor:1", path=None
    )
    adapter = ComposedAdapter(runner=lambda argv: _ok('{"role":"user","message":{}}\n'))
    with pytest.raises(UnknownSchema):
        adapter.parse(ref)


def test_unknown_schema_is_a_runtime_error_so_passive_demotes_it() -> None:
    assert issubclass(UnknownSchema, RuntimeError)
