import unittest
from pathlib import Path

from toolbench.transcript import (
    ToolCall,
    _result_id,
    _result_payload,
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
        self.assertEqual(result_len(payload), len("abc") + len("defgh"))

    def test_block_local_content(self) -> None:
        payload = {
            "content": [
                {"type": "text", "text": "abc"},
                {"type": "text", "text": "defgh"},
            ]
        }
        self.assertEqual(result_len(payload), len("abc") + len("defgh"))


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
        }
        fields.update(overrides)
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
        entry = {"toolUseID": "toolu_1"}
        self.assertEqual(_result_id(entry, None), "toolu_1")

    def test_id_block_local_only(self) -> None:
        entry: dict[str, object] = {}
        block = {"tool_use_id": "toolu_2"}
        self.assertEqual(_result_id(entry, block), "toolu_2")

    def test_id_block_local_preferred_when_both_present(self) -> None:
        entry = {"toolUseID": "toolu_top"}
        block = {"tool_use_id": "toolu_block"}
        self.assertEqual(_result_id(entry, block), "toolu_block")

    def test_id_missing(self) -> None:
        self.assertIsNone(_result_id({}, None))

    def test_payload_top_level_only(self) -> None:
        entry = {"toolUseResult": "abc"}
        self.assertEqual(_result_payload(entry, None), ("abc", "top_level"))

    def test_payload_block_local_wins_over_top_level(self) -> None:
        entry = {"toolUseResult": {"stale": "decoy"}}
        block = {"content": "the real payload"}
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


if __name__ == "__main__":
    unittest.main()
