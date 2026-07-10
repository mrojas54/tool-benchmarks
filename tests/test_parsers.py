"""ClaudeParser consumes lines, not a path (TB-13, Task 1)."""

from pathlib import Path

from toolbench.parsers import ClaudeParser, HermesTraceParser, TranscriptParser
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
    lines = (FIXTURES / "schema_hermes_trace.jsonl").read_text(encoding="utf-8").splitlines(
        keepends=True
    )
    result = HermesTraceParser().parse(iter(lines), agent="claude-code", source="raw", project="p")
    assert result.malformed == 0
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.name == "read_file"
    assert call.usage is None
    assert call.usage_provenance is UsageProvenance.ABSENT_BY_EXPORT


def test_hermes_trace_provenance_ignores_a_usage_dict_entirely() -> None:
    """Unconditional: trace never carries usage, so the arm cannot depend on the value."""
    assert HermesTraceParser._provenance({"input_tokens": 5}) is UsageProvenance.ABSENT_BY_EXPORT
    assert HermesTraceParser._provenance(None) is UsageProvenance.ABSENT_BY_EXPORT
