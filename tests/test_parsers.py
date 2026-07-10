"""ClaudeParser consumes lines, not a path (TB-13, Task 1)."""

from pathlib import Path

from toolbench.parsers import (
    ClaudeParser,
    CodexParser,
    HermesTraceParser,
    TranscriptParser,
)
from toolbench.transcript import UsageProvenance

FIXTURES = Path(__file__).parent / "fixtures"


def test_claude_parser_claims_a_line_carrying_session_id():
    assert ClaudeParser.claims_line({"type": "last-prompt", "sessionId": "s1"}) is True


def test_claude_parser_does_not_claim_a_codex_line():
    assert ClaudeParser.claims_line({"type": "session_meta", "payload": {}}) is False


def test_claude_parser_joins_tool_use_to_tool_result_from_lines():
    lines = [
        '{"sessionId":"s1","timestamp":"t0","message":{"model":"m","content":'
        '[{"type":"tool_use","id":"u1","name":"Bash","input":{"command":"ls"}}]}}\n',
        '{"sessionId":"s1","timestamp":"t1","message":{"content":'
        '[{"type":"tool_result","tool_use_id":"u1","content":"hello"}]}}\n',
    ]
    result = ClaudeParser().parse(lines, agent="claude", source="raw", project="p")
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.name == "Bash"
    assert call.output_chars == 5  # len("hello")
    assert call.no_result is False
    assert call.result_source == "block_local"
    assert result.malformed == 0


def test_claude_parser_drains_unmatched_call_at_eof():
    lines = [
        '{"sessionId":"s1","timestamp":"t0","message":{"content":'
        '[{"type":"tool_use","id":"u1","name":"Read","input":{}}]}}\n',
    ]
    result = ClaudeParser().parse(lines, agent="claude", source="raw", project="p")
    assert len(result.calls) == 1
    assert result.calls[0].no_result is True
    assert result.calls[0].output_chars == 0


def test_claude_parser_counts_malformed_lines_without_raising():
    lines = ['{"sessionId":"s1"}\n', "not json\n", "\n"]
    result = ClaudeParser().parse(lines, agent="claude", source="raw", project="p")
    assert result.malformed == 1
    assert result.calls == []


def test_claude_parser_is_a_transcript_parser():
    assert issubclass(ClaudeParser, TranscriptParser)
    assert ClaudeParser.schema_tag == "claude"


def test_hermes_trace_parses_cleanly_and_stamps_absent_by_export() -> None:
    """The hazard: it parses, raises nothing, reports 0 malformed -- and has no usage."""
    lines = (
        (FIXTURES / "schema_hermes_trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines(keepends=True)
    )
    result = HermesTraceParser().parse(
        iter(lines), agent="claude-code", source="raw", project="p"
    )
    assert result.malformed == 0
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.name == "read_file"
    assert call.usage is None
    assert call.usage_provenance is UsageProvenance.ABSENT_BY_EXPORT


def test_hermes_trace_provenance_ignores_a_usage_dict_entirely() -> None:
    """Unconditional: trace never carries usage, so the arm cannot depend on the value."""
    assert (
        HermesTraceParser._provenance({"input_tokens": 5})
        is UsageProvenance.ABSENT_BY_EXPORT
    )
    assert HermesTraceParser._provenance(None) is UsageProvenance.ABSENT_BY_EXPORT


# --- CodexParser (TB-12) ---------------------------------------------------


def _codex_calls():
    lines = (
        (FIXTURES / "schema_codex.jsonl")
        .read_text(encoding="utf-8")
        .splitlines(keepends=True)
    )
    return CodexParser().parse(
        iter(lines), agent="codex", source="agentsview", project="p"
    )


def test_codex_parser_is_a_transcript_parser():
    assert issubclass(CodexParser, TranscriptParser)
    assert CodexParser.schema_tag == "codex"


def test_codex_parser_claims_a_session_meta_line():
    assert CodexParser.claims_line({"type": "session_meta", "payload": {}}) is True


def test_codex_parser_does_not_claim_a_claude_line():
    """Detection asserts exactly one parser claims a line; overlap raises AmbiguousSchema."""
    assert CodexParser.claims_line({"type": "last-prompt", "sessionId": "s1"}) is False


def test_codex_parser_joins_function_call_to_output_on_call_id():
    """The defect: join key is `payload.call_id`, never `tool_use_id`."""
    call = _codex_calls().calls[0]
    assert call.name == "exec_command"
    assert call.input_chars == 13  # len('{"cmd":"pwd"}') -- from `arguments`
    assert call.output_chars == 2  # len("ok")
    assert call.no_result is False
    # claude has two possible result locations and must say which; codex has one.
    assert call.result_source == "payload"


def test_codex_parser_joins_custom_tool_call_reading_input_not_arguments():
    """`custom_tool_call` carries `input`; `function_call` carries `arguments`."""
    call = _codex_calls().calls[1]
    assert call.name == "apply_patch"
    assert call.input_chars == 15  # len("*** Begin Patch")
    assert call.output_chars == 8  # len("Success.")


def test_codex_parser_drains_unmatched_call_at_eof():
    """S6: an unmatched call at EOF is kept with no_result, never dropped."""
    call = _codex_calls().calls[2]
    assert call.name == "write_stdin"
    assert call.no_result is True
    assert call.output_chars == 0


def test_codex_parser_yields_exactly_the_three_paired_calls():
    result = _codex_calls()
    assert len(result.calls) == 3
    assert result.malformed == 0


def test_codex_parser_lifts_session_id_from_session_meta_onto_every_call():
    """Unlike claude, codex stamps `session_id` once on `session_meta`, not per line."""
    assert [c.session_id for c in _codex_calls().calls] == ["c1", "c1", "c1"]


def test_codex_parser_takes_model_from_the_governing_turn_context():
    """`model` lives on `turn_context`, not on the call record."""
    assert [c.model for c in _codex_calls().calls] == ["gpt-5.5"] * 3


def test_codex_parser_stamps_absent_by_schema_for_usage():
    """codex emits per-TURN `token_count` events; it has no per-call usage channel (S29)."""
    calls = _codex_calls().calls
    assert len(calls) == 3  # guard: a `for` over an empty list asserts nothing
    for call in calls:
        assert call.usage is None
        assert call.usage_provenance is UsageProvenance.ABSENT_BY_SCHEMA


def test_codex_parser_reports_no_per_call_error_channel():
    """codex encodes exit status inside the output TEXT; there is no `is_error` flag."""
    assert [c.error for c in _codex_calls().calls] == [None, None, None]


def test_codex_parser_counts_malformed_lines_without_raising():
    lines = [
        '{"type":"session_meta","payload":{"session_id":"c1"}}\n',
        "not json\n",
        "\n",
    ]
    result = CodexParser().parse(iter(lines), agent="codex", source="raw", project="p")
    assert result.malformed == 1
    assert result.calls == []
