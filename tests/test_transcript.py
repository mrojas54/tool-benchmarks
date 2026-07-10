import dataclasses
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

# S1/S2 join and payload-resolution moved to `parsers.py` in TB-13. The
# assertions below are unchanged: only the import site moved.
from toolbench.parsers import ClaudeParser, _result_id, _result_payload
from toolbench.transcript import (
    ToolCall,
    UsageProvenance,
    parse_session,
    result_len,
)

FIXTURES = Path(__file__).parent / "fixtures"


class ResultLenTests(unittest.TestCase):
    def test_string(self) -> None:
        self.assertEqual(result_len("hello world"), 11)

    def test_dict(self) -> None:
        payload = {"status": "ok", "count": 3}
        self.assertEqual(result_len(payload), len('{"status": "ok", "count": 3}'))

    def test_mcp_block_list(self) -> None:
        payload = [
            {"type": "text", "text": "abc"},
            {"type": "text", "text": "defgh"},
        ]
        self.assertEqual(result_len(payload), len("abc") + len("defgh"))  # 8

    def test_block_local_content(self) -> None:
        payload = {
            "content": [
                {"type": "text", "text": "abc"},
                {"type": "text", "text": "defgh"},
            ]
        }
        self.assertEqual(result_len(payload), len("abc") + len("defgh"))  # 8


class ToolCallTests(unittest.TestCase):
    def _make(self, **overrides: object) -> ToolCall:
        fields: dict[str, object] = {
            "agent": "claude-code",
            "source": "raw",
            "project": "tool-benchmarks",
            "name": "Read",
            "input_chars": 42,
            "output_chars": 100,
            "session_id": "sess-1",
            "ts": "2026-07-08T00:00:00Z",
            "usage": None,
            "duration_ms": 12.5,
            "error": None,
            "model": "claude-opus-4-8",
        }
        fields.update(overrides)
        # Mirrors ClaudeParser._provenance so existing tests keep their meaning:
        # usage={...} still reads as a measurement, usage=None as an absence.
        fields.setdefault(
            "usage_provenance",
            UsageProvenance.PRESENT
            if fields["usage"] is not None
            else UsageProvenance.ABSENT_UNEXPECTED,
        )
        return ToolCall(**fields)  # type: ignore[arg-type]

    def test_fields(self) -> None:
        call = self._make()
        self.assertEqual(call.agent, "claude-code")
        self.assertEqual(call.source, "raw")
        self.assertEqual(call.project, "tool-benchmarks")
        self.assertEqual(call.name, "Read")
        self.assertEqual(call.input_chars, 42)
        self.assertEqual(call.output_chars, 100)
        self.assertEqual(call.session_id, "sess-1")
        self.assertEqual(call.ts, "2026-07-08T00:00:00Z")
        self.assertIsNone(call.usage)
        self.assertEqual(call.duration_ms, 12.5)
        self.assertIsNone(call.error)
        self.assertEqual(call.model, "claude-opus-4-8")

    def test_derived_tokens_floor_division(self) -> None:
        call = self._make(input_chars=41, output_chars=101)
        self.assertEqual(call.tokens, 25)  # 101 // 4, not round(101/4)
        self.assertEqual(call.input_tokens, 10)  # 41 // 4, not round(41/4)

    def test_derived_tokens_zero(self) -> None:
        call = self._make(input_chars=0, output_chars=0)
        self.assertEqual(call.tokens, 0)
        self.assertEqual(call.input_tokens, 0)


class ResultIdPayloadTests(unittest.TestCase):
    def test_id_top_level_only(self) -> None:
        entry: dict[str, object] = {"toolUseID": "toolu_1"}
        self.assertEqual(_result_id(entry, None), "toolu_1")

    def test_id_block_local_only(self) -> None:
        entry: dict[str, object] = {}
        block: dict[str, object] = {"tool_use_id": "toolu_2"}
        self.assertEqual(_result_id(entry, block), "toolu_2")

    def test_id_block_local_preferred_when_both_present(self) -> None:
        entry: dict[str, object] = {"toolUseID": "toolu_top"}
        block: dict[str, object] = {"tool_use_id": "toolu_block"}
        self.assertEqual(_result_id(entry, block), "toolu_block")

    def test_id_missing(self) -> None:
        self.assertIsNone(_result_id({}, None))

    def test_payload_top_level_only(self) -> None:
        entry: dict[str, object] = {"toolUseResult": "abc"}
        self.assertEqual(_result_payload(entry, None), ("abc", "top_level"))

    def test_payload_block_local_wins_over_top_level(self) -> None:
        entry: dict[str, object] = {"toolUseResult": {"stale": "decoy"}}
        block: dict[str, object] = {"content": "the real payload"}
        self.assertEqual(
            _result_payload(entry, block), ("the real payload", "block_local")
        )

    def test_payload_missing(self) -> None:
        self.assertEqual(_result_payload({}, None), (None, None))


class ParseSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        result = parse_session(FIXTURES / "sample.jsonl")
        self.result = result
        self.by_name = {call.name: call for call in result.calls}

    def test_malformed_line_counted_and_skipped(self) -> None:
        self.assertEqual(self.result.malformed, 1)

    def test_call_count(self) -> None:
        self.assertEqual(len(self.result.calls), 4)

    def test_string_result_top_level_join(self) -> None:
        call = self.by_name["Bash"]
        expected = "total 0\ndrwxr-xr-x 2 user staff 64 Jul 8 10:00 ."
        self.assertEqual(call.output_chars, len(expected))
        self.assertEqual(call.result_source, "top_level")
        self.assertFalse(call.no_result)

    def test_mcp_block_list_block_local_join(self) -> None:
        call = self.by_name["mcp__search__query"]
        expected = len("result chunk one") + len("result chunk two")
        self.assertEqual(call.output_chars, expected)
        self.assertEqual(call.result_source, "block_local")
        self.assertFalse(call.no_result)

    def test_block_local_content_wins_over_top_level(self) -> None:
        call = self.by_name["Read"]
        self.assertEqual(call.output_chars, len("the real block-local file contents"))
        self.assertEqual(call.result_source, "block_local")
        self.assertFalse(call.no_result)

    def test_interrupted_call_kept_with_no_result(self) -> None:
        call = self.by_name["Write"]
        self.assertEqual(call.output_chars, 0)
        self.assertTrue(call.no_result)
        self.assertIsNone(call.result_source)

    def test_common_fields_stamped(self) -> None:
        call = self.by_name["Bash"]
        self.assertEqual(call.agent, "claude-code")
        self.assertEqual(call.source, "raw")
        self.assertEqual(call.session_id, "sess-001")
        self.assertEqual(call.ts, "2026-07-08T10:00:00Z")

    def test_model_captured_from_message(self) -> None:
        self.assertEqual(self.by_name["Bash"].model, "claude-opus-4-8")

    def test_model_none_when_message_omits_it(self) -> None:
        self.assertIsNone(self.by_name["Read"].model)


class NonUtf8SessionTests(unittest.TestCase):
    """Raw sessions carrying a stray byte parse to completion (TB-10).

    `_parse_ref` sends raw sessions straight to `parse_session`, bypassing
    `open_session_jsonl` — so this is the decode boundary the raw CLI path hits.
    """

    def test_non_utf8_byte_does_not_abort_parse(self) -> None:
        good = (FIXTURES / "sample.jsonl").read_bytes()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sess-bad.jsonl"
            path.write_bytes(good + b'{"type": "assistant", "note": "caf\xa0"}\n')
            result = parse_session(str(path), agent="claude-code", source="raw", project="p")
        # The undamaged calls still land; the mangled line is JSON-valid, so it is
        # simply an entry with no tool_use rather than a malformed-line bump.
        self.assertGreater(len(result.calls), 0)

    def test_non_utf8_byte_breaking_json_counts_as_malformed(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sess-bad.jsonl"
            # 0xa0 lands on the closing quote, so U+FFFD leaves the JSON unparseable.
            path.write_bytes(b'{"type": "assistant"\xa0\n')
            result = parse_session(str(path), agent="claude-code", source="raw", project="p")
        self.assertEqual(result.calls, [])
        self.assertEqual(result.malformed, 1)


class UsageProvenanceTests(unittest.TestCase):
    def test_enum_has_exactly_four_arms(self) -> None:
        self.assertEqual(
            {m.name for m in UsageProvenance},
            {"PRESENT", "ABSENT_BY_SCHEMA", "ABSENT_BY_EXPORT", "ABSENT_UNEXPECTED"},
        )

    def test_tool_call_has_no_default_provenance(self) -> None:
        """A default would silently mark unconverted call sites PRESENT."""
        field = {f.name: f for f in dataclasses.fields(ToolCall)}["usage_provenance"]
        self.assertIs(field.default, dataclasses.MISSING)
        self.assertIs(field.default_factory, dataclasses.MISSING)

    def test_provenance_precedes_the_defaulted_fields(self) -> None:
        names = [f.name for f in dataclasses.fields(ToolCall)]
        self.assertLess(names.index("usage_provenance"), names.index("no_result"))
        self.assertEqual(names[names.index("usage") + 1], "usage_provenance")


class ClaudeProvenanceHookTests(unittest.TestCase):
    def test_dict_usage_is_present(self) -> None:
        self.assertIs(ClaudeParser._provenance({"input_tokens": 1}), UsageProvenance.PRESENT)

    def test_empty_dict_usage_is_present_a_measured_zero(self) -> None:
        """The channel existed and reported nothing. That is a measurement."""
        self.assertIs(ClaudeParser._provenance({}), UsageProvenance.PRESENT)

    def test_missing_usage_is_absent_unexpected(self) -> None:
        self.assertIs(ClaudeParser._provenance(None), UsageProvenance.ABSENT_UNEXPECTED)

    def test_non_dict_usage_is_absent_unexpected(self) -> None:
        self.assertIs(ClaudeParser._provenance("42"), UsageProvenance.ABSENT_UNEXPECTED)


if __name__ == "__main__":
    unittest.main()
