"""Schema detection over a bounded window (TB-13, Task 2)."""

import pytest

from toolbench.adapters import (
    AmbiguousSchema,
    UnknownSchema,
    detect_parser,
)
from toolbench.parsers import ClaudeParser


def test_detect_returns_claude_parser_and_replays_every_line():
    lines = iter(['{"sessionId":"s1","type":"last-prompt"}\n', '{"sessionId":"s1"}\n'])
    parser, replayed = detect_parser(lines)
    assert isinstance(parser, ClaudeParser)
    assert len(list(replayed)) == 2  # the sniffed line is chained back


def test_detect_skips_preamble_before_the_discriminating_line():
    lines = iter(["\n", "not json\n", '{"unknown":1}\n', '{"sessionId":"s1"}\n'])
    parser, replayed = detect_parser(lines)
    assert isinstance(parser, ClaudeParser)
    assert len(list(replayed)) == 4  # nothing is consumed away


def test_detect_raises_unknown_schema_on_a_codex_line():
    lines = iter(['{"type":"session_meta","payload":{},"timestamp":"t"}\n'])
    with pytest.raises(UnknownSchema):
        detect_parser(lines)


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
