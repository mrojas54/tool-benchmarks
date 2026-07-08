import unittest

from toolbench.records import ToolCall, result_len


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


if __name__ == "__main__":
    unittest.main()
