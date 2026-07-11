"""ClaudeParser consumes lines, not a path (TB-13, Task 1)."""

from pathlib import Path

from toolbench.parsers import (
    ClaudeParser,
    CodexParser,
    HermesTraceParser,
    TranscriptParser,
    _PendingCall,
)
from toolbench.transcript import ParseResult, ToolCall, UsageProvenance

FIXTURES = Path(__file__).parent / "fixtures"


def _finish(name: str) -> ToolCall:
    """Emit via the shared join path so parse-time tags are exercised."""
    return _PendingCall(
        name=name,
        input_chars=10,
        session_id="s1",
        ts="2026-07-08T00:00:00Z",
        usage=None,
        usage_provenance=UsageProvenance.ABSENT_UNEXPECTED,
        model=None,
    ).finish(agent="claude-code", source="raw", project="p", output_chars=40)


def test_finish_tags_tool_search_as_deferral() -> None:
    """CQ 3.1: deferral policy is stamped at emit, not in Reducer.absorb."""
    call = _finish("ToolSearch")
    assert call.is_deferral is True
    assert call.is_subagent_fanout is False


def test_finish_tags_subagent_fanout_tools() -> None:
    """CQ 3.1: Agent/Task/spawn_agent are fan-out; wait_agent is not."""
    for name in ("Agent", "Task", "spawn_agent"):
        call = _finish(name)
        assert call.is_subagent_fanout is True, name
        assert call.is_deferral is False, name
    wait = _finish("wait_agent")
    assert wait.is_subagent_fanout is False
    read = _finish("Read")
    assert read.is_subagent_fanout is False
    assert read.is_deferral is False


def test_claude_parser_claims_a_line_carrying_session_id() -> None:
    assert ClaudeParser.claims_line({"type": "last-prompt", "sessionId": "s1"}) is True


def test_claude_parser_does_not_claim_a_codex_line() -> None:
    assert ClaudeParser.claims_line({"type": "session_meta", "payload": {}}) is False


def test_claude_parser_joins_tool_use_to_tool_result_from_lines() -> None:
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


def test_claude_parser_drains_unmatched_call_at_eof() -> None:
    lines = [
        '{"sessionId":"s1","timestamp":"t0","message":{"content":'
        '[{"type":"tool_use","id":"u1","name":"Read","input":{}}]}}\n',
    ]
    result = ClaudeParser().parse(lines, agent="claude", source="raw", project="p")
    assert len(result.calls) == 1
    assert result.calls[0].no_result is True
    assert result.calls[0].output_chars == 0


def test_claude_parser_counts_malformed_lines_without_raising() -> None:
    lines = ['{"sessionId":"s1"}\n', "not json\n", "\n"]
    result = ClaudeParser().parse(lines, agent="claude", source="raw", project="p")
    assert result.malformed == 1
    assert result.calls == []


def test_claude_parser_is_a_transcript_parser() -> None:
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


def _codex_calls() -> ParseResult:
    lines = (
        (FIXTURES / "schema_codex.jsonl")
        .read_text(encoding="utf-8")
        .splitlines(keepends=True)
    )
    return CodexParser().parse(
        iter(lines), agent="codex", source="agentsview", project="p"
    )


def test_codex_parser_is_a_transcript_parser() -> None:
    assert issubclass(CodexParser, TranscriptParser)
    assert CodexParser.schema_tag == "codex"


def test_codex_parser_claims_a_session_meta_line() -> None:
    assert CodexParser.claims_line({"type": "session_meta", "payload": {}}) is True


def test_codex_parser_does_not_claim_a_claude_line() -> None:
    """Detection asserts exactly one parser claims a line; overlap raises AmbiguousSchema."""
    assert CodexParser.claims_line({"type": "last-prompt", "sessionId": "s1"}) is False


def test_codex_parser_joins_function_call_to_output_on_call_id() -> None:
    """The defect: join key is `payload.call_id`, never `tool_use_id`."""
    call = _codex_calls().calls[0]
    assert call.name == "exec_command"
    assert call.input_chars == 13  # len('{"cmd":"pwd"}') -- from `arguments`
    assert call.output_chars == 2  # len("ok")
    assert call.no_result is False
    # claude has two possible result locations and must say which; codex has one.
    assert call.result_source == "payload"


def test_codex_parser_joins_custom_tool_call_reading_input_not_arguments() -> None:
    """`custom_tool_call` carries `input`; `function_call` carries `arguments`."""
    call = _codex_calls().calls[1]
    assert call.name == "apply_patch"
    assert call.input_chars == 15  # len("*** Begin Patch")
    assert call.output_chars == 8  # len("Success.")


def test_codex_parser_joins_tool_search_call_to_its_output() -> None:
    """A third paired call shape. Ignoring it recreates the silent zero this ticket
    exists to kill -- 19 pairs sit in the live archive."""
    call = _codex_calls().calls[2]
    assert call.no_result is False
    assert call.output_chars == 52  # the `tools` list, not an `output` string


def test_codex_parser_names_tool_search_so_the_deferral_metric_sees_it() -> None:
    """codex's tool_search_call has no name field; the parser stamps `ToolSearch`
    and `is_deferral` so the reducer can count without knowing the schema."""
    call = _codex_calls().calls[2]
    assert call.name == "ToolSearch"
    assert call.is_deferral is True


def test_codex_parser_reads_tool_search_arguments_as_a_dict_not_a_json_string() -> None:
    """`function_call.arguments` is a serialized string; `tool_search_call.arguments`
    is a live object. `result_len` normalizes both, but only if we pass the object."""
    assert _codex_calls().calls[2].input_chars == 34


def test_codex_parser_drains_unmatched_call_at_eof() -> None:
    """S6: an unmatched call at EOF is kept with no_result, never dropped."""
    call = _codex_calls().calls[3]
    assert call.name == "write_stdin"
    assert call.no_result is True
    assert call.output_chars == 0


def test_codex_parser_yields_exactly_the_four_paired_calls() -> None:
    result = _codex_calls()
    assert len(result.calls) == 4
    assert result.malformed == 0


def test_codex_parser_identifies_a_session_by_rollout_id_not_session_id() -> None:
    """`session_meta.payload.id` is the unique rollout ID. `session_id` is absent from
    older records and names the PARENT in a subagent rollout, which would collapse a
    subagent into its parent. The fixture's session_meta has both, and they differ."""
    assert [c.session_id for c in _codex_calls().calls] == ["c1"] * 4


def test_codex_parser_takes_model_from_the_governing_turn_context() -> None:
    """`model` lives on `turn_context`, not on the call record."""
    assert [c.model for c in _codex_calls().calls] == ["gpt-5.5"] * 4


def test_codex_parser_stamps_absent_by_schema_for_usage() -> None:
    """codex emits per-TURN `token_count` events; it has no per-call usage channel (S29)."""
    calls = _codex_calls().calls
    assert len(calls) == 4  # guard: a `for` over an empty list asserts nothing
    for call in calls:
        assert call.usage is None
        assert call.usage_provenance is UsageProvenance.ABSENT_BY_SCHEMA


def test_codex_parser_reports_no_per_call_error_channel() -> None:
    """codex encodes exit status inside the output TEXT; there is no `is_error` flag."""
    assert [c.error for c in _codex_calls().calls] == [None] * 4


def test_codex_parser_counts_malformed_lines_without_raising() -> None:
    lines = [
        '{"type":"session_meta","payload":{"id":"c1"}}\n',
        "not json\n",
        "\n",
    ]
    result = CodexParser().parse(iter(lines), agent="codex", source="raw", project="p")
    assert result.malformed == 1
    assert result.calls == []


def test_codex_parser_identifies_older_rollouts_that_carry_no_session_id() -> None:
    """118 of 183 session_meta records in the live archive have no `session_id` at all.
    Keying on it stamps 2086 calls with an empty session identifier."""
    lines = [
        '{"type":"session_meta","payload":{"id":"c9"}}\n',
        '{"type":"response_item","timestamp":"t","payload":'
        '{"type":"function_call","name":"exec_command","arguments":"{}","call_id":"k"}}\n',
        '{"type":"response_item","timestamp":"t","payload":'
        '{"type":"function_call_output","call_id":"k","output":"ok"}}\n',
    ]
    result = CodexParser().parse(iter(lines), agent="codex", source="raw", project="p")
    assert [c.session_id for c in result.calls] == ["c9"]


def test_codex_parser_joins_a_minimal_tool_search_pair() -> None:
    """The reviewer's repro: a bare pair must not parse to a healthy zero."""
    lines = [
        '{"type":"response_item","timestamp":"t","payload":'
        '{"type":"tool_search_call","call_id":"k","arguments":{"query":"x"}}}\n',
        '{"type":"response_item","timestamp":"t","payload":'
        '{"type":"tool_search_output","call_id":"k","tools":[]}}\n',
    ]
    result = CodexParser().parse(iter(lines), agent="codex", source="raw", project="p")
    assert len(result.calls) == 1
    assert result.calls[0].name == "ToolSearch"
    assert result.malformed == 0


# The real live shape: `web_search_call` carries an `id` (a `ws_` handle) but never
# a `call_id`, and there is no `web_search_output` record to pair with (TB-24).
_WEB_SEARCH_LINE = (
    '{"type":"response_item","timestamp":"t","payload":'
    '{"type":"web_search_call","id":"ws_abc","status":"completed",'
    '"action":{"type":"search","query":"x"}}}\n'
)


def test_codex_parser_counts_web_search_call_as_unjoinable() -> None:
    """TB-24 (b): a recognized-but-unjoinable tool record is counted by kind, not
    dropped -- so codex's ~4% web-search undercount is named, not silently absent."""
    lines = [
        '{"type":"session_meta","payload":{"id":"c1"}}\n',
        _WEB_SEARCH_LINE,
        _WEB_SEARCH_LINE,
    ]
    result = CodexParser().parse(iter(lines), agent="codex", source="raw", project="p")
    assert result.unjoinable == {"web_search_call": 2}


def test_codex_parser_does_not_emit_web_search_call_as_a_joined_call() -> None:
    """Decision (b): corpus call counts stay unchanged. web_search_call is counted
    apart from `.calls`, never as a `no_result` orphan (option (a), rejected)."""
    lines = [
        '{"type":"session_meta","payload":{"id":"c1"}}\n',
        _WEB_SEARCH_LINE,
    ]
    result = CodexParser().parse(iter(lines), agent="codex", source="raw", project="p")
    assert result.calls == []
    assert result.malformed == 0


def test_codex_parser_leaves_unjoinable_empty_when_no_web_search() -> None:
    """The base fixture has no web_search_call: the field defaults to an empty map,
    so a parser with nothing to report renders no reconciliation line."""
    assert _codex_calls().unjoinable == {}
