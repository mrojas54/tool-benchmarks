"""ClaudeParser consumes lines, not a path (TB-13, Task 1)."""

from toolbench.parsers import ClaudeParser, TranscriptParser


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
