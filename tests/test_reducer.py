import dataclasses
import unittest
from pathlib import Path


from tests.fakes import make_call
from toolbench.passive import (
    OVERSIZED_OUTPUT_TOKENS,
    UNKNOWN_MODEL,
    Reducer,
)
from toolbench.transcript import ParseResult, ToolCall, UsageProvenance

FIXTURES = Path(__file__).parent / "fixtures"


class ReducerNoCorpusListTests(unittest.TestCase):
    def test_reducer_has_no_corpus_wide_call_list_field(self) -> None:
        for f in dataclasses.fields(Reducer):
            type_str = str(f.type)
            self.assertNotIn("ToolCall", type_str, f"field {f.name!r} typed {type_str!r}")

    def test_agent_stats_and_tool_stats_have_no_call_list_field(self) -> None:
        from toolbench.passive import AgentStats, ToolStats

        for cls in (AgentStats, ToolStats):
            for f in dataclasses.fields(cls):
                type_str = str(f.type)
                self.assertNotIn("ToolCall", type_str, f"field {f.name!r} typed {type_str!r}")

class ReducerAbsorbTests(unittest.TestCase):
    def test_accumulates_across_sessions(self) -> None:
        reducer = Reducer()
        reducer.absorb("claude-code", ParseResult(calls=[make_call(name="Read", output_chars=400)], malformed=0))
        reducer.absorb("claude-code", ParseResult(calls=[make_call(name="Read", output_chars=800)], malformed=1))
        self.assertEqual(reducer.sessions_scanned, 2)
        self.assertEqual(reducer.calls_joined, 2)
        self.assertEqual(reducer.malformed_total, 1)
        stats = reducer.tools[("claude-code", "Read")]
        self.assertEqual(stats.calls, 2)
        self.assertEqual(stats.output_tokens, 100 + 200)

    def test_separate_agents_tracked_independently(self) -> None:
        reducer = Reducer()
        reducer.absorb("claude-code", ParseResult(calls=[make_call(agent="claude-code")], malformed=0))
        reducer.absorb("codex", ParseResult(calls=[make_call(agent="codex")], malformed=0))
        self.assertEqual(set(reducer.agents), {"claude-code", "codex"})
        self.assertEqual(reducer.agents["claude-code"].sessions, 1)
        self.assertEqual(reducer.agents["codex"].sessions, 1)

    def test_tools_by_model_splits_same_tool_across_models(self) -> None:
        reducer = Reducer()
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[
                    make_call(name="Read", output_chars=400, model="claude-opus-4-8"),
                    make_call(name="Read", output_chars=800, model="claude-haiku-4-5"),
                ],
                malformed=0,
            ),
        )
        # Same (agent, tool) folds together, but the model dimension separates them.
        self.assertEqual(reducer.tools[("claude-code", "Read")].output_tokens, 100 + 200)
        opus = reducer.tools_by_model[("claude-code", "claude-opus-4-8", "Read")]
        haiku = reducer.tools_by_model[("claude-code", "claude-haiku-4-5", "Read")]
        self.assertEqual(opus.output_tokens, 100)
        self.assertEqual(haiku.output_tokens, 200)
        self.assertEqual(opus.calls, 1)
        self.assertEqual(haiku.calls, 1)

    def test_tools_by_model_normalizes_missing_model_to_unknown(self) -> None:
        reducer = Reducer()
        reducer.absorb("codex", ParseResult(calls=[make_call(name="Bash", model=None)], malformed=0))
        self.assertIn(("codex", UNKNOWN_MODEL, "Bash"), reducer.tools_by_model)

    def test_absorb_folds_unjoinable_by_agent_and_kind(self) -> None:
        """TB-24: unjoinable records accumulate per (agent, kind), never as calls."""
        reducer = Reducer()
        reducer.absorb("codex", ParseResult(calls=[], malformed=0, unjoinable={"web_search_call": 2}))
        reducer.absorb("codex", ParseResult(calls=[], malformed=0, unjoinable={"web_search_call": 3}))
        self.assertEqual(reducer.unjoinable[("codex", "web_search_call")], 5)
        # Not confused with joined calls: none of those sessions had a call.
        self.assertEqual(reducer.calls_joined, 0)

    def test_tools_by_model_tracks_errors_and_cache_hits(self) -> None:
        reducer = Reducer()
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[
                    make_call(name="Bash", model="claude-opus-4-8", error="tool_error"),
                    make_call(name="Bash", model="claude-opus-4-8", usage={"cache_read_input_tokens": 10}),
                ],
                malformed=0,
            ),
        )
        stats = reducer.tools_by_model[("claude-code", "claude-opus-4-8", "Bash")]
        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.cache_hits, 1)

    def test_errors_and_no_result_counted(self) -> None:
        reducer = Reducer()
        calls = [
            make_call(name="Bash", error="tool_error"),
            make_call(name="Bash", no_result=True),
            make_call(name="Bash"),
        ]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        stats = reducer.tools[("claude-code", "Bash")]
        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.no_result, 1)
        self.assertEqual(reducer.inefficiency.failures, 1)

    def test_cache_hit_tracked_but_does_not_gate_calls(self) -> None:
        reducer = Reducer()
        calls = [
            make_call(name="Bash", usage={"cache_read_input_tokens": 500}),
            make_call(name="Bash", usage={"cache_read_input_tokens": 0}),
            make_call(name="Bash", usage=None),
        ]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        stats = reducer.tools[("claude-code", "Bash")]
        self.assertEqual(stats.calls, 3)
        self.assertEqual(stats.cache_hits, 1)

    def test_tool_search_tracked_as_deferral_tax(self) -> None:
        reducer = Reducer()
        calls = [make_call(name="ToolSearch", output_chars=4000)]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.tool_search_calls, 1)
        self.assertEqual(reducer.inefficiency.tool_search_tokens, 1000)

    def test_oversized_output_counted(self) -> None:
        reducer = Reducer()
        big = OVERSIZED_OUTPUT_TOKENS * 4 + 40
        calls = [make_call(name="Read", output_chars=big), make_call(name="Read", output_chars=40)]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.oversized_outputs, 1)

    def test_subagent_fanout_counted(self) -> None:
        reducer = Reducer()
        calls = [make_call(name="Agent"), make_call(name="Read")]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.subagent_fanout, 1)

    def test_subagent_fanout_counts_codex_spawn_agent(self) -> None:
        """codex is the only agent in the corpus that spawns subagents, and the
        fan-out callout was measured with its data absent (TB-12). `wait_agent`
        awaits an existing subagent and is not a fan-out."""
        reducer = Reducer()
        calls = [
            make_call(name="spawn_agent"),
            make_call(name="wait_agent"),
            make_call(name="exec_command"),
        ]
        reducer.absorb("codex", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.subagent_fanout, 1)
        self.assertEqual(reducer.inefficiency.subagent_by_tool, {"spawn_agent": 1})

    def test_churn_counts_consecutive_same_tool_bad_repeats(self) -> None:
        reducer = Reducer()
        calls = [
            make_call(name="Bash", error="tool_error"),
            make_call(name="Bash", error="tool_error"),
            make_call(name="Bash", error="tool_error"),
            make_call(name="Read"),
        ]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.churn_retries, 2)

    def test_churn_not_counted_for_healthy_repeats(self) -> None:
        reducer = Reducer()
        calls = [make_call(name="Read"), make_call(name="Read"), make_call(name="Read")]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.churn_retries, 0)

    def test_failures_attributed_to_owning_tool(self) -> None:
        reducer = Reducer()
        calls = [
            make_call(name="Bash", error="tool_error"),
            make_call(name="Read", error="tool_error"),
            make_call(name="Bash", error="tool_error"),
            make_call(name="Read"),
        ]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.failures_by_tool, {"Bash": 2, "Read": 1})

    def test_oversized_outputs_attributed_to_owning_tool(self) -> None:
        reducer = Reducer()
        big = OVERSIZED_OUTPUT_TOKENS * 4 + 40
        calls = [
            make_call(name="Read", output_chars=big),
            make_call(name="Bash", output_chars=big),
            make_call(name="Read", output_chars=big),
            make_call(name="Read", output_chars=40),
        ]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.oversized_by_tool, {"Read": 2, "Bash": 1})

    def test_churn_attributed_to_owning_tool(self) -> None:
        reducer = Reducer()
        calls = [
            make_call(name="Bash", error="tool_error"),
            make_call(name="Bash", error="tool_error"),
            make_call(name="Bash", error="tool_error"),
            make_call(name="Read"),
        ]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.churn_by_tool, {"Bash": 2})

    def test_attribution_accumulates_across_sessions(self) -> None:
        reducer = Reducer()
        calls = [make_call(name="Bash", error="tool_error")]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        reducer.absorb("codex", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.failures_by_tool, {"Bash": 2})

class SessionGrainCacheCounterTests(unittest.TestCase):
    """TB-20/S32: session-grain cache_read_tokens is counted once per session,
    never per call, and never conflated with the per-call UsageProvenance arms."""

    def test_measured_hit_increments_both_counters(self) -> None:
        reducer = Reducer()
        reducer.absorb(
            "hermes",
            ParseResult(calls=[make_call(agent="hermes")], malformed=0, session_cache_read_tokens=42),
        )
        stats = reducer.agents["hermes"]
        self.assertEqual(stats.sessions_with_cache_data, 1)
        self.assertEqual(stats.sessions_with_cache_hit, 1)

    def test_measured_zero_increments_measured_but_not_hit(self) -> None:
        reducer = Reducer()
        reducer.absorb(
            "hermes",
            ParseResult(calls=[make_call(agent="hermes")], malformed=0, session_cache_read_tokens=0),
        )
        stats = reducer.agents["hermes"]
        self.assertEqual(stats.sessions_with_cache_data, 1)
        self.assertEqual(stats.sessions_with_cache_hit, 0)

    def test_unmeasured_session_increments_neither_counter(self) -> None:
        reducer = Reducer()
        reducer.absorb(
            "hermes",
            ParseResult(calls=[make_call(agent="hermes")], malformed=0, session_cache_read_tokens=None),
        )
        stats = reducer.agents["hermes"]
        self.assertEqual(stats.sessions_with_cache_data, 0)
        self.assertEqual(stats.sessions_with_cache_hit, 0)

    def test_claude_code_session_default_never_touches_the_counters(self) -> None:
        # ParseResult.session_cache_read_tokens defaults to None for every
        # producer but parse_hermes_session -- a real Claude Code session must
        # not accidentally register as "session-grain measured".
        reducer = Reducer()
        reducer.absorb("claude-code", ParseResult(calls=[make_call()], malformed=0))
        stats = reducer.agents["claude-code"]
        self.assertEqual(stats.sessions_with_cache_data, 0)
        self.assertEqual(stats.sessions_with_cache_hit, 0)

    def test_counters_accumulate_across_sessions_one_increment_each_regardless_of_call_count(
        self,
    ) -> None:
        # The ticket's hard constraint: a session-grain figure must be counted
        # once per SESSION, never once per call (that would fabricate a
        # per-call denominator the data does not have).
        reducer = Reducer()
        reducer.absorb(
            "hermes",
            ParseResult(
                calls=[make_call(agent="hermes"), make_call(agent="hermes"), make_call(agent="hermes")],
                malformed=0,
                session_cache_read_tokens=99,
            ),
        )
        reducer.absorb(
            "hermes",
            ParseResult(calls=[make_call(agent="hermes")], malformed=0, session_cache_read_tokens=0),
        )
        stats = reducer.agents["hermes"]
        self.assertEqual(stats.sessions_with_cache_data, 2)
        self.assertEqual(stats.sessions_with_cache_hit, 1)

class UsageMissingCounterTests(unittest.TestCase):
    def _absorb(self, *calls: ToolCall) -> Reducer:
        reducer = Reducer()
        reducer.absorb("claude-code", ParseResult(calls=list(calls), malformed=0))
        return reducer

    def test_present_usage_does_not_increment(self) -> None:
        r = self._absorb(make_call(usage={"input_tokens": 1}))
        self.assertEqual(r.tools[("claude-code", "Read")].usage_missing, 0)

    def test_every_absent_arm_increments(self) -> None:
        for arm in (
            UsageProvenance.ABSENT_BY_SCHEMA,
            UsageProvenance.ABSENT_BY_EXPORT,
            UsageProvenance.ABSENT_UNEXPECTED,
        ):
            with self.subTest(arm=arm):
                r = self._absorb(make_call(usage=None, usage_provenance=arm))
                self.assertEqual(r.tools[("claude-code", "Read")].usage_missing, 1)

    def test_empty_usage_dict_is_a_measured_zero_not_a_miss(self) -> None:
        r = self._absorb(make_call(usage={}))
        stats = r.tools[("claude-code", "Read")]
        self.assertEqual(stats.usage_missing, 0)
        self.assertEqual(stats.cache_hits, 0)

