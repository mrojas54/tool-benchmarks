"""Schema detection over a bounded window (TB-13, Task 2)."""

from pathlib import Path

import pytest

from toolbench.adapters import (
    AmbiguousSchema,
    UnknownSchema,
    detect_parser,
)
from toolbench.parsers import ClaudeParser, CodexParser, HermesTraceParser

FIXTURES = Path(__file__).parent / "fixtures"


def _lines(name: str):
    return iter((FIXTURES / name).read_text(encoding="utf-8").splitlines(keepends=True))


def test_detect_returns_claude_parser_and_replays_every_line():
    lines = iter(['{"sessionId":"s1","type":"last-prompt"}\n', '{"sessionId":"s1"}\n'])
    parser, replayed = detect_parser(lines)
    # not a subclass: HermesTraceParser is-a ClaudeParser, so isinstance is too weak
    assert type(parser) is ClaudeParser
    assert len(list(replayed)) == 2  # the sniffed line is chained back


def test_detect_skips_preamble_before_the_discriminating_line():
    lines = iter(["\n", "not json\n", '{"unknown":1}\n', '{"sessionId":"s1"}\n'])
    parser, replayed = detect_parser(lines)
    # not a subclass: HermesTraceParser is-a ClaudeParser, so isinstance is too weak
    assert type(parser) is ClaudeParser
    assert len(list(replayed)) == 4  # nothing is consumed away


def test_detect_claims_a_codex_line():
    """Was `..._raises_unknown_schema_on_a_codex_line`. TB-12 registered the parser;
    `..._on_a_cursor_line` below still guards the UnknownSchema path."""
    lines = iter(['{"type":"session_meta","payload":{},"timestamp":"t"}\n'])
    parser, _ = detect_parser(lines)
    assert type(parser) is CodexParser


def test_detect_raises_unknown_schema_on_a_cursor_line():
    lines = iter(['{"role":"user","message":{}}\n'])
    with pytest.raises(UnknownSchema):
        detect_parser(lines)


def test_detect_is_bounded_and_does_not_read_past_the_window():
    lines = iter(['{"filler":1}\n'] * 500)
    with pytest.raises(UnknownSchema):
        detect_parser(lines, window=100)


def test_detect_raises_unknown_schema_on_empty_input():
    with pytest.raises(UnknownSchema):
        detect_parser(iter([]))


def test_detect_raises_ambiguous_when_two_parsers_claim_one_line(monkeypatch):
    class Greedy(ClaudeParser):
        schema_tag = "greedy"

    monkeypatch.setattr("toolbench.adapters.PARSERS", (ClaudeParser, Greedy))
    with pytest.raises(AmbiguousSchema):
        detect_parser(iter(['{"sessionId":"s1"}\n']))


def test_unknown_and_ambiguous_are_runtime_errors():
    # passive.main's guard catches RuntimeError; this is why no guard edit is needed.
    assert issubclass(UnknownSchema, RuntimeError)
    assert issubclass(AmbiguousSchema, RuntimeError)


# --- TB-13 Task 6: golden fixtures pinning the four observed line-0 shapes -----


def test_claude_fixture_detects_as_claude_despite_control_preamble():
    parser, _ = detect_parser(_lines("schema_claude.jsonl"))
    # not a subclass: HermesTraceParser is-a ClaudeParser, so isinstance is too weak
    assert type(parser) is ClaudeParser


def test_cowork_fixture_detects_as_claude_with_no_registry_entry_of_its_own():
    parser, _ = detect_parser(_lines("schema_cowork.jsonl"))
    # not a subclass: HermesTraceParser is-a ClaudeParser, so isinstance is too weak
    assert type(parser) is ClaudeParser


def test_codex_fixture_detects_as_codex():
    """Was `..._raises_unknown_schema_until_tb_12`. TB-12 registered the parser."""
    parser, _ = detect_parser(_lines("schema_codex.jsonl"))
    assert type(parser) is CodexParser


def test_claude_fixture_still_detects_as_claude_with_codex_registered():
    """Guards the AmbiguousSchema invariant: adding a parser must not steal claude's lines."""
    parser, _ = detect_parser(_lines("schema_claude.jsonl"))
    assert type(parser) is ClaudeParser


def test_cursor_fixture_raises_unknown_schema():
    with pytest.raises(UnknownSchema):
        detect_parser(_lines("schema_cursor.jsonl"))


def test_golden_claude_fixture_parses_to_exactly_one_joined_call():
    parser, replayed = detect_parser(_lines("schema_claude.jsonl"))
    result = parser.parse(replayed, agent="claude", source="raw", project="p")
    assert result.malformed == 0
    assert len(result.calls) == 1
    call = result.calls[0]
    assert (call.name, call.output_chars, call.no_result) == ("Bash", 5, False)
    assert call.result_source == "block_local"


def test_golden_cowork_fixture_drains_its_unmatched_call():
    parser, replayed = detect_parser(_lines("schema_cowork.jsonl"))
    result = parser.parse(replayed, agent="cowork", source="agentsview", project="p")
    assert len(result.calls) == 1
    assert result.calls[0].no_result is True  # S6


def test_hermes_trace_fixture_detects_as_hermes_trace_not_claude() -> None:
    parser, _ = detect_parser(_lines("schema_hermes_trace.jsonl"))
    assert type(parser) is HermesTraceParser


def test_claude_and_hermes_trace_predicates_partition() -> None:
    """detect_parser raises AmbiguousSchema if two parsers claim one line, so these
    two predicates must never overlap. Verified against the whole local archive:
    0 of 4,061 real transcripts carry a top-level version of "hermes-agent"."""
    claude_line: dict[str, object] = {"sessionId": "s1", "version": "2.1.205"}
    trace_line: dict[str, object] = {"sessionId": "s1", "version": "hermes-agent"}
    assert ClaudeParser.claims_line(claude_line)
    assert not HermesTraceParser.claims_line(claude_line)
    assert HermesTraceParser.claims_line(trace_line)
    assert not ClaudeParser.claims_line(trace_line)


def test_claude_claims_a_line_with_no_version_field() -> None:
    """Real transcripts open with a preamble record that carries no `version`.
    Measured: 400 of 400 sampled transcripts have no `version` on line 1, and
    detect_parser decides on the first line a single parser claims."""
    assert ClaudeParser.claims_line({"sessionId": "s1"})


def test_hermes_trace_needs_session_id_too() -> None:
    assert not HermesTraceParser.claims_line({"version": "hermes-agent"})
