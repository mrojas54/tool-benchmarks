import dataclasses
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.fakes import FakeRunner, completed, make_call
from toolbench.adapters import UnknownSchema
from toolbench.freeze import read_manifest, write_manifest
from toolbench.passive import (
    OVERSIZED_OUTPUT_TOKENS,
    UNKNOWN_MODEL,
    CorpusFingerprint,
    Reducer,
    _apply_date_range,
    _is_subagent_path,
    _parse_ref,
    corpus_fingerprint,
    filter_subagents,
    main,
    parse_args,
    render_report,
    session_signature,
)
from toolbench.passive import (
    _discover_refs,
    classify_skip,
    skip_record_for,
    tally_skips,
)
from toolbench.sources import (
    MissingSourceExport,
    NonTranscriptExport,
    SessionRef,
    SkipReason,
    SkipRecord,
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


class DateRangeFilterTests(unittest.TestCase):
    def test_no_bounds_returns_same_calls(self) -> None:
        result = ParseResult(calls=[make_call(ts="2026-07-01T00:00:00Z")], malformed=2)
        filtered = _apply_date_range(result, None, None)
        self.assertEqual(len(filtered.calls), 1)
        self.assertEqual(filtered.malformed, 2)

    def test_filters_before_date_from(self) -> None:
        result = ParseResult(
            calls=[make_call(ts="2026-06-01T00:00:00Z"), make_call(ts="2026-07-05T00:00:00Z")],
            malformed=0,
        )
        filtered = _apply_date_range(result, "2026-07-01", None)
        self.assertEqual(len(filtered.calls), 1)
        self.assertEqual(filtered.calls[0].ts, "2026-07-05T00:00:00Z")

    def test_filters_after_date_to(self) -> None:
        result = ParseResult(
            calls=[make_call(ts="2026-06-01T00:00:00Z"), make_call(ts="2026-07-05T00:00:00Z")],
            malformed=0,
        )
        filtered = _apply_date_range(result, None, "2026-06-30")
        self.assertEqual(len(filtered.calls), 1)
        self.assertEqual(filtered.calls[0].ts, "2026-06-01T00:00:00Z")

    def test_empty_ts_is_kept(self) -> None:
        result = ParseResult(calls=[make_call(ts="")], malformed=0)
        filtered = _apply_date_range(result, "2026-07-01", "2026-07-31")
        self.assertEqual(len(filtered.calls), 1)

    def test_unjoinable_survives_date_filtering(self) -> None:
        """TB-24: unjoinable is a count of seen records, not date-filterable calls --
        it passes through the filter intact, exactly as `malformed` does."""
        result = ParseResult(
            calls=[make_call(ts="2026-06-01T00:00:00Z")],
            malformed=0,
            unjoinable={"web_search_call": 4},
        )
        filtered = _apply_date_range(result, "2026-07-01", None)
        self.assertEqual(len(filtered.calls), 0)  # the one call is filtered out
        self.assertEqual(filtered.unjoinable, {"web_search_call": 4})  # the count is not

    def test_session_cache_read_tokens_survives_date_filtering(self) -> None:
        """TB-25: the S32 session-grain cache stat is not a per-call value and must
        pass through date filtering intact, even when every call is filtered out --
        the session was still measured. Previously the field was silently reset to
        None whenever a date range was active, undercounting the cache caveat."""
        result = ParseResult(
            calls=[make_call(ts="2026-06-01T00:00:00Z")],
            malformed=0,
            session_cache_read_tokens=42,
        )
        filtered = _apply_date_range(result, "2026-07-01", None)
        self.assertEqual(len(filtered.calls), 0)  # the one call is filtered out
        self.assertEqual(filtered.session_cache_read_tokens, 42)  # the stat survives


class CliParsingTests(unittest.TestCase):
    def test_default_scope_is_agent_all_and_all_projects(self) -> None:
        args = parse_args([])
        self.assertEqual(args.agent, "all")
        self.assertTrue(args.all_projects)
        self.assertIsNone(args.project)
        self.assertEqual(args.index_source, "auto")
        self.assertFalse(args.exclude_subagents)
        self.assertFalse(args.verbose)
        self.assertIsNone(args.limit)
        self.assertIsNone(args.out)

    def test_agent_flag(self) -> None:
        args = parse_args(["--agent", "codex"])
        self.assertEqual(args.agent, "codex")

    def test_project_flag_disables_all_projects(self) -> None:
        args = parse_args(["--project", "tool-benchmarks"])
        self.assertFalse(args.all_projects)
        self.assertEqual(args.project, "tool-benchmarks")

    def test_all_and_project_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--all", "--project", "x"])

    def test_since_and_date_range_are_distinct_flags(self) -> None:
        args = parse_args(["--since", "2026-07-01", "--date-from", "2026-07-02", "--date-to", "2026-07-03"])
        self.assertEqual(args.since, "2026-07-01")
        self.assertEqual(args.date_from, "2026-07-02")
        self.assertEqual(args.date_to, "2026-07-03")

    def test_out_and_limit(self) -> None:
        args = parse_args(["--out", "report.md", "--limit", "200"])
        self.assertEqual(args.out, "report.md")
        self.assertEqual(args.limit, 200)

    def test_exclude_subagents_flag(self) -> None:
        args = parse_args(["--exclude-subagents"])
        self.assertTrue(args.exclude_subagents)

    def test_index_source_choices(self) -> None:
        for choice in ("auto", "agentsview", "raw"):
            args = parse_args(["--index-source", choice])
            self.assertEqual(args.index_source, choice)
        with self.assertRaises(SystemExit):
            parse_args(["--index-source", "bogus"])

    def test_verbose_flag(self) -> None:
        args = parse_args(["--verbose"])
        self.assertTrue(args.verbose)


class SubagentFilterTests(unittest.TestCase):
    def test_is_subagent_path(self) -> None:
        ref = SessionRef(agent="claude-code", source="raw", project="p", session_id="s1", path="/x/subagents/y.jsonl")
        self.assertTrue(_is_subagent_path(ref))

    def test_non_subagent_path(self) -> None:
        ref = SessionRef(agent="claude-code", source="raw", project="p", session_id="s1", path="/x/y.jsonl")
        self.assertFalse(_is_subagent_path(ref))

    def test_no_path_is_not_subagent(self) -> None:
        ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="s1", path=None)
        self.assertFalse(_is_subagent_path(ref))

    def test_filter_subagents_removes_matching_paths(self) -> None:
        refs = [
            SessionRef(agent="claude-code", source="raw", project="p", session_id="s1", path="/x/subagents/y.jsonl"),
            SessionRef(agent="claude-code", source="raw", project="p", session_id="s2", path="/x/y.jsonl"),
        ]
        kept = filter_subagents(refs)
        self.assertEqual([r.session_id for r in kept], ["s2"])


class RenderReportTests(unittest.TestCase):
    def _reducer(self) -> Reducer:
        reducer = Reducer()
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[
                    make_call(name="Read", output_chars=400),
                    make_call(name="Bash", output_chars=8000, usage={"cache_read_input_tokens": 10}),
                ],
                malformed=1,
            ),
        )
        return reducer

    def test_five_sections_present_in_order(self) -> None:
        report = render_report(
            self._reducer(),
            index_source="auto",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            since_note=None,
        )
        headers = [
            "## Agent Breakdown",
            "## Tool Leaderboard",
            "## Model Breakdown",
            "## Inefficiency Callouts",
            "## Summary",
        ]
        indices = [report.index(h) for h in headers]
        self.assertEqual(indices, sorted(indices))

    def test_provenance_fields_present(self) -> None:
        report = render_report(
            self._reducer(),
            index_source="raw",
            fallback_reason="agentsview exited 1: daemon down",
            skips=[SkipRecord("nonexistent", "claude", SkipReason.MISSING_SOURCE, "/nonexistent")],
            include_subagents=False,
            since_note="2026-07-01",
        )
        for expected in (
            "Index source: raw",
            "Sessions discovered:",
            "scanned:",
            "skipped:",
            "Skipped by reason:",
            "missing_source: 1",
            "Tool calls joined:",
            "Malformed lines:",
            "Subagents included: no",
            "AgentsView fallback reason: agentsview exited 1: daemon down",
            "--since is file-mtime based",
        ):
            self.assertIn(expected, report)

    def _callout_reducer(self) -> Reducer:
        """Two consecutive Bash failures then a clean Read: 3 calls, 2 failures, 1 churn."""
        reducer = Reducer()
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[
                    make_call(name="Bash", error="tool_error"),
                    make_call(name="Bash", error="tool_error"),
                    make_call(name="Read"),
                ],
                malformed=0,
            ),
        )
        return reducer

    def _callouts(self, reducer: Reducer) -> str:
        report = render_report(
            reducer,
            index_source="auto",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            since_note=None,
        )
        start = report.index("## Inefficiency Callouts")
        return report[start : report.index("## Summary")]

    def test_callouts_carry_denominator_and_percentage(self) -> None:
        section = self._callouts(self._callout_reducer())
        self.assertIn("Failures: 2 of 3 calls (66.7%)", section)
        self.assertIn("Churn (consecutive-repeat retries): 1 of 3 calls (33.3%)", section)

    def test_callouts_name_top_offending_tool(self) -> None:
        section = self._callouts(self._callout_reducer())
        self.assertIn("Failures: 2 of 3 calls (66.7%); top: Bash (2)", section)
        self.assertIn("Churn (consecutive-repeat retries): 1 of 3 calls (33.3%); top: Bash (1)", section)

    def test_zero_count_callout_omits_top_offender(self) -> None:
        section = self._callouts(self._callout_reducer())
        self.assertIn("Subagent fan-out calls: 0 of 3 calls (0.0%)", section)
        self.assertNotIn("Subagent fan-out calls: 0 of 3 calls (0.0%); top:", section)

    def test_top_offender_ties_break_alphabetically(self) -> None:
        reducer = Reducer()
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[
                    make_call(name="Write", error="tool_error"),
                    make_call(name="Bash", error="tool_error"),
                ],
                malformed=0,
            ),
        )
        self.assertIn("top: Bash (1)", self._callouts(reducer))

    def test_leaderboard_ranked_by_output_tokens_not_call_count_or_cache(self) -> None:
        reducer = Reducer()
        # "Bash" gets fewer calls but far more output tokens; cache hit shouldn't matter.
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[
                    make_call(name="Read", output_chars=40),
                    make_call(name="Read", output_chars=40),
                    make_call(name="Read", output_chars=40),
                    make_call(name="Bash", output_chars=40000, usage={"cache_read_input_tokens": 999}),
                ],
                malformed=0,
            ),
        )
        report = render_report(
            reducer,
            index_source="auto",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            since_note=None,
        )
        leaderboard = report[report.index("## Tool Leaderboard") : report.index("## Model Breakdown")]
        self.assertLess(leaderboard.index("Bash"), leaderboard.index("Read"))

    def test_model_breakdown_rows_split_by_model(self) -> None:
        reducer = Reducer()
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[
                    make_call(name="Read", output_chars=400, model="claude-opus-4-8"),
                    make_call(name="Read", output_chars=8000, model="claude-haiku-4-5"),
                ],
                malformed=0,
            ),
        )
        report = render_report(
            reducer,
            index_source="auto",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            since_note=None,
        )
        section = report[report.index("## Model Breakdown") : report.index("## Inefficiency Callouts")]
        self.assertIn("| claude-code | claude-opus-4-8 | Read | 1 | 100 |", section)
        self.assertIn("| claude-code | claude-haiku-4-5 | Read | 1 | 2000 |", section)
        # Ranked by context tokens descending: haiku (2000) outranks opus (100).
        self.assertLess(section.index("claude-haiku-4-5"), section.index("claude-opus-4-8"))


class UnjoinableReconciliationRenderTests(unittest.TestCase):
    """TB-24 / S38: recognized-but-unjoinable tool records are surfaced in the
    Summary, attributed by agent/kind, so codex's web-search undercount is named."""

    def _summary(self, reducer: Reducer) -> str:
        report = render_report(
            reducer,
            index_source="auto",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            since_note=None,
        )
        return report[report.index("## Summary") :]

    def test_line_present_with_total_and_attribution(self) -> None:
        reducer = Reducer()
        reducer.absorb("codex", ParseResult(calls=[make_call(agent="codex")], malformed=0,
                                            unjoinable={"web_search_call": 138}))
        summary = self._summary(reducer)
        self.assertIn("Unjoinable tool records (seen, not joined): 138", summary)
        self.assertIn("codex/web_search_call: 138", summary)

    def test_line_absent_when_nothing_unjoinable(self) -> None:
        reducer = Reducer()
        reducer.absorb("claude-code", ParseResult(calls=[make_call()], malformed=0))
        self.assertNotIn("Unjoinable tool records", self._summary(reducer))


class MainExitContractTests(unittest.TestCase):
    def test_strict_raw_missing_root_exits_1(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--index-source", "raw"], root="/definitely/not/a/real/root")
        self.assertEqual(code, 1)
        self.assertNotEqual(err.getvalue(), "")

    def test_empty_selection_exits_0(self) -> None:
        with TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--index-source", "raw"], root=tmp)
            self.assertEqual(code, 0)
            self.assertIn("no sessions matched", out.getvalue())

    def test_auto_continues_and_reports_skipped_root_when_raw_fallback_missing(self) -> None:
        runner = FakeRunner([FileNotFoundError("no agentsview")])
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--index-source", "auto"], runner=runner, root="/definitely/not/a/real/root")
        self.assertEqual(code, 0)
        message = out.getvalue()
        self.assertIn("no sessions matched", message)
        self.assertIn("skipped 1: missing_source=1", message)

    def test_end_to_end_raw_report(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "-Users-me-tool-benchmarks"
            proj.mkdir()
            shutil.copy(FIXTURES / "sample.jsonl", proj / "sess-001.jsonl")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--index-source", "raw"], root=tmp)
            self.assertEqual(code, 0)
            report = out.getvalue()
            for header in (
                "## Agent Breakdown",
                "## Tool Leaderboard",
                "## Model Breakdown",
                "## Inefficiency Callouts",
                "## Summary",
            ):
                self.assertIn(header, report)
            self.assertIn("claude-code", report)
            self.assertIn("Malformed lines: 1", report)

    def test_out_flag_writes_file_instead_of_stdout(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            shutil.copy(FIXTURES / "sample.jsonl", proj / "sess-001.jsonl")
            out_path = Path(tmp) / "report.md"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["--index-source", "raw", "--out", str(out_path)], root=tmp)
            self.assertEqual(code, 0)
            self.assertTrue(out_path.exists())
            self.assertIn("## Summary", out_path.read_text())

    def test_exclude_subagents_removes_subagent_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            sub = proj / "subagents"
            sub.mkdir()
            shutil.copy(FIXTURES / "sample.jsonl", sub / "sess-002.jsonl")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--index-source", "raw", "--exclude-subagents"], root=tmp)
            self.assertEqual(code, 0)
            self.assertIn("no sessions matched", out.getvalue())

    def test_strict_agentsview_nonzero_exit_exits_1(self) -> None:
        runner = FakeRunner([completed(stderr="daemon down", returncode=1)])
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--index-source", "agentsview"], runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("daemon down", err.getvalue())

    def test_agentsview_source_wired_through_temp_file_bridge(self) -> None:
        raw_text = (FIXTURES / "sample.jsonl").read_text()
        payload = {
            "sessions": [{"id": "abc123", "project": "proj-a", "agent": "claude"}],
            "next_cursor": "",
            "total": 1,
        }
        runner = FakeRunner(
            [
                completed(stdout=json.dumps(payload)),
                completed(stdout=raw_text),
            ]
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--index-source", "agentsview"], runner=runner)
        self.assertEqual(code, 0)
        report = out.getvalue()
        self.assertIn("## Summary", report)
        self.assertIn("Malformed lines: 1", report)


class NonUtf8SessionTests(unittest.TestCase):
    """One corrupt session must not abort the corpus scan (TB-10)."""

    def test_raw_session_with_non_utf8_byte_still_produces_report(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            good = (FIXTURES / "sample.jsonl").read_bytes()
            # Real bytes on disk, not a hand-built ToolCall: the TB-8 retrospective
            # showed fixture-only tests cannot exercise the discovery/decode path.
            (proj / "sess-bad.jsonl").write_bytes(good.replace(b"total 0", b"total \xa0"))
            (proj / "sess-good.jsonl").write_bytes(good)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--index-source", "raw"], root=tmp)
        self.assertEqual(code, 0)
        report = out.getvalue()
        self.assertIn("## Summary", report)
        self.assertIn("scanned: 2", report)

    def test_export_decode_error_demotes_session_to_skipped_root(self) -> None:
        # Guards the injected-runner seam: a caller supplying a strict-decode
        # runner still gets a report for every session that did decode.
        raw_text = (FIXTURES / "sample.jsonl").read_text()
        payload = {
            "sessions": [
                {"id": "bad-session", "project": "p", "agent": "claude"},
                {"id": "good-session", "project": "p", "agent": "claude"},
            ],
            "next_cursor": "",
            "total": 2,
        }
        runner = FakeRunner(
            [
                completed(stdout=json.dumps(payload)),
                UnicodeDecodeError("utf-8", b"\xa0", 0, 1, "invalid start byte"),
                completed(stdout=raw_text),
            ]
        )
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main(["--index-source", "agentsview", "--verbose"], runner=runner)
        self.assertEqual(code, 0)
        report = out.getvalue()
        self.assertIn("## Summary", report)
        self.assertIn("scanned: 1", report)
        self.assertIn("decode_error: 1", report)
        # the skipped id is available under --verbose
        self.assertIn("bad-session", report)


class NonTranscriptExportTests(unittest.TestCase):
    """Binary payloads demote to skipped_roots, keeping `Malformed lines` honest (TB-10).

    The guard is agent-agnostic and stays that way. Hermes was the agent that first
    exposed it, but hermes now bypasses `session export` entirely (TB-11), so the
    example here uses an agent that still takes the export path.
    """

    def test_binary_session_is_skipped_not_absorbed_as_malformed(self) -> None:
        raw_text = (FIXTURES / "sample.jsonl").read_text()
        sqlite_payload = "SQLite format 3\x00\x10\x00\x02tablemessages\x00" * 50
        payload = {
            "sessions": [
                {"id": "cowork-1", "project": "cowork", "agent": "cowork"},
                {"id": "good-session", "project": "p", "agent": "claude"},
            ],
            "next_cursor": "",
            "total": 2,
        }
        runner = FakeRunner(
            [
                completed(stdout=json.dumps(payload)),
                completed(stdout=sqlite_payload),
                completed(stdout=raw_text),
            ]
        )
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main(["--index-source", "agentsview", "--verbose"], runner=runner)
        self.assertEqual(code, 0)
        report = out.getvalue()
        self.assertIn("non_transcript: 1", report)
        self.assertIn("scanned: 1", report)
        # the skipped id is available under --verbose
        self.assertIn("cowork-1", report)
        # The 1 malformed line is the fixture's own; none of the binary leaks in.
        self.assertIn("Malformed lines: 1", report)

    def test_rejected_export_leaves_no_temp_file_behind(self) -> None:
        # _parse_ref binds tmp_path only after the write loop, so a raise from the
        # line generator strands a delete=False NamedTemporaryFile.
        # Not a hermes session: hermes never reaches the temp-file path now (TB-11),
        # so this would pass vacuously and stop guarding the leak it was written for.
        tmp_root = Path(tempfile.gettempdir())
        before = set(tmp_root.glob("*.jsonl"))
        payload = {
            "sessions": [{"id": "cowork-1", "project": "cowork", "agent": "cowork"}],
            "next_cursor": "",
            "total": 1,
        }
        runner = FakeRunner([completed(stdout=json.dumps(payload)), completed(stdout="SQLite format 3\x00")])
        with redirect_stdout(io.StringIO()):
            main(["--index-source", "agentsview"], runner=runner)
        self.assertEqual(set(tmp_root.glob("*.jsonl")) - before, set())


# --- TB-13: _parse_ref delegates to the registry; unknown schemas raise --------


def _ok_export(stdout: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_parse_ref_parses_an_agentsview_claude_session_without_a_temp_file() -> None:
    ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="c:1", path=None)
    body = (
        '{"sessionId":"s1","timestamp":"t0","message":{"content":'
        '[{"type":"tool_use","id":"u1","name":"Grep","input":{}}]}}\n'
        '{"sessionId":"s1","timestamp":"t1","message":{"content":'
        '[{"type":"tool_result","tool_use_id":"u1","content":"hit"}]}}\n'
    )
    result = _parse_ref(ref, runner=lambda argv: _ok_export(body))
    assert len(result.calls) == 1
    assert result.calls[0].name == "Grep"


def test_parse_ref_returns_codex_calls_instead_of_a_healthy_zero() -> None:
    """Was `..._raises_unknown_schema_for_codex_instead_of_returning_zero`.

    The ticket's whole arc in one test: codex went silent-zero (TB-12 filed) ->
    loud UnknownSchema (TB-13 seam) -> parsed (TB-12 fixed).
    """
    ref = SessionRef(
        agent="codex", source="agentsview", project="p", session_id="codex:1", path=None
    )
    body = (
        '{"type":"session_meta","payload":{"session_id":"c1"},"timestamp":"t"}\n'
        '{"type":"response_item","timestamp":"t","payload":'
        '{"type":"function_call","name":"exec_command","arguments":"{}","call_id":"k1"}}\n'
        '{"type":"response_item","timestamp":"t","payload":'
        '{"type":"function_call_output","call_id":"k1","output":"ok"}}\n'
    )
    result = _parse_ref(ref, runner=lambda argv: _ok_export(body))
    assert len(result.calls) == 1
    assert result.calls[0].name == "exec_command"
    assert result.malformed == 0


def test_parse_ref_still_raises_unknown_schema_for_an_unregistered_schema() -> None:
    """Registering codex must not weaken the guarantee TB-13 bought: an unrecognized
    schema raises rather than returning a healthy zero. cursor is still unregistered."""
    ref = SessionRef(
        agent="cursor", source="agentsview", project="p", session_id="cursor:1", path=None
    )
    body = '{"role":"user","message":{}}\n'
    with pytest.raises(UnknownSchema):
        _parse_ref(ref, runner=lambda argv: _ok_export(body))


def test_passive_no_longer_imports_tempfile() -> None:
    import toolbench.passive as p

    assert not hasattr(p, "tempfile"), "the NamedTemporaryFile round-trip must be gone"


def test_unknown_schema_lands_in_skipped_roots_not_as_a_zero_row() -> None:
    # UnknownSchema is a RuntimeError, so main()'s existing guard demotes it.
    ref = SessionRef(
        agent="codex", source="agentsview", project="p", session_id="codex:1", path=None
    )
    reducer = Reducer()
    skipped: list[str] = []
    try:
        _parse_ref(ref, runner=lambda argv: _ok_export('{"role":"user","message":{}}\n'))
    except (OSError, RuntimeError, UnicodeDecodeError) as exc:
        skipped.append(str(exc))
    assert skipped, "an unparseable session must be skipped, never counted as 0 calls"
    assert reducer.calls_joined == 0


# --- TB-23: skips carry a machine-readable reason, not stringified prose --------


def test_classify_skip_maps_missing_source_export() -> None:
    assert classify_skip(MissingSourceExport("gone")) is SkipReason.MISSING_SOURCE


def test_classify_skip_maps_unknown_schema() -> None:
    assert classify_skip(UnknownSchema("no parser claimed")) is SkipReason.UNKNOWN_SCHEMA


def test_classify_skip_maps_non_transcript_export() -> None:
    assert classify_skip(NonTranscriptExport("binary")) is SkipReason.NON_TRANSCRIPT


def test_classify_skip_maps_unicode_decode_error() -> None:
    exc = UnicodeDecodeError("utf-8", b"\xa0", 0, 1, "invalid start byte")
    assert classify_skip(exc) is SkipReason.DECODE_ERROR


def test_classify_skip_maps_bare_runtimeerror_to_export_failed() -> None:
    # A non-zero `export` for a reason other than a missing source is a real,
    # distinct failure -- not a dead index entry and not a parser gap.
    assert classify_skip(RuntimeError("database is locked")) is SkipReason.EXPORT_FAILED


def test_skip_record_for_stamps_the_refs_identity_and_the_typed_reason() -> None:
    ref = SessionRef(agent="codex", source="agentsview", project="p", session_id="cx:1", path=None)
    rec = skip_record_for(ref, UnknownSchema("no parser claimed"))
    assert rec == SkipRecord(
        session_id="cx:1",
        agent="codex",
        reason=SkipReason.UNKNOWN_SCHEMA,
        detail="no parser claimed",
    )


def test_tally_skips_answers_how_many_have_no_parser_without_parsing_prose() -> None:
    # The exact question TB-23 exists to make answerable: count the actionable
    # parser gaps without a regex over rendered error messages.
    skips = [
        SkipRecord("a", "claude", SkipReason.MISSING_SOURCE, "source file not found"),
        SkipRecord("b", "codex", SkipReason.UNKNOWN_SCHEMA, "no parser claimed"),
        SkipRecord("c", "cursor", SkipReason.UNKNOWN_SCHEMA, "no parser claimed"),
        SkipRecord("d", "hermes", SkipReason.NON_TRANSCRIPT, "binary content"),
    ]
    tally = tally_skips(skips)
    assert tally[SkipReason.UNKNOWN_SCHEMA] == 2
    assert tally[SkipReason.MISSING_SOURCE] == 1
    assert tally[SkipReason.NON_TRANSCRIPT] == 1


def test_discover_refs_records_a_missing_root_as_a_typed_skip() -> None:
    # auto mode, agentsview unavailable, raw fallback root absent: the discovery-level
    # FileNotFoundError becomes a typed SkipRecord rather than a bare string.
    args = parse_args(["--index-source", "auto"])
    runner = FakeRunner([FileNotFoundError("no agentsview")])
    _refs, _fallback, skips = _discover_refs(args, "/definitely/not/a/real/root", runner)
    assert skips, "a missing raw root must be recorded as a skip"
    assert all(isinstance(s, SkipRecord) for s in skips)
    assert skips[0].reason is SkipReason.MISSING_SOURCE


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


class CacheNoteRenderTests(unittest.TestCase):
    def _note(self, *calls: ToolCall) -> str:
        reducer = Reducer()
        reducer.absorb("claude-code", ParseResult(calls=list(calls), malformed=0))
        report = render_report(
            reducer,
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            since_note=None,
        )
        row = next(line for line in report.splitlines() if "| Read |" in line)
        return row.rstrip("|").rsplit("|", 1)[-1].strip()

    def test_yes_when_a_hit_was_observed(self) -> None:
        self.assertEqual(self._note(make_call(usage={"cache_read_input_tokens": 5})), "yes")

    def test_no_when_usage_was_available_and_zero_hits(self) -> None:
        self.assertEqual(self._note(make_call(usage={"input_tokens": 1})), "no")

    def test_na_when_no_call_could_be_measured(self) -> None:
        self.assertEqual(
            self._note(make_call(usage=None, usage_provenance=UsageProvenance.ABSENT_BY_EXPORT)),
            "n/a",
        )

    def test_na_star_when_only_some_calls_could_be_measured(self) -> None:
        """A trace export and a real transcript share one (agent, tool) bucket.

        Synthetic by necessity: no natural trace corpus carries enough tool calls
        to form a mixed bucket. This is the case a scalar enum cannot express.
        """
        self.assertEqual(
            self._note(
                make_call(usage={"input_tokens": 1}),
                make_call(usage=None, usage_provenance=UsageProvenance.ABSENT_BY_EXPORT),
            ),
            "n/a*",
        )

    def test_yes_survives_surrounding_blindness(self) -> None:
        """One observed hit is a positive existence proof."""
        self.assertEqual(
            self._note(
                make_call(usage={"cache_read_input_tokens": 5}),
                make_call(usage=None, usage_provenance=UsageProvenance.ABSENT_BY_EXPORT),
            ),
            "yes",
        )


class SessionGrainCacheCaveatRenderTests(unittest.TestCase):
    """TB-20/S32: the Agent Breakdown section (S14 §1) carries a session-grain
    caveat line, orthogonal to the Tool Leaderboard's per-call cache column."""

    def _agent_breakdown(self, reducer: Reducer) -> str:
        report = render_report(
            reducer,
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            since_note=None,
        )
        return report[report.index("## Agent Breakdown") : report.index("## Tool Leaderboard")]

    def test_caveat_line_present_with_correct_ratio(self) -> None:
        reducer = Reducer()
        reducer.absorb(
            "hermes",
            ParseResult(calls=[make_call(agent="hermes")], malformed=0, session_cache_read_tokens=5),
        )
        reducer.absorb(
            "hermes",
            ParseResult(calls=[make_call(agent="hermes")], malformed=0, session_cache_read_tokens=0),
        )
        section = self._agent_breakdown(reducer)
        self.assertIn("hermes: 1 of 2 sessions carry session-grain", section)
        self.assertIn("cache_read_tokens", section)

    def test_caveat_line_absent_when_no_session_grain_data(self) -> None:
        reducer = Reducer()
        reducer.absorb("claude-code", ParseResult(calls=[make_call()], malformed=0))
        section = self._agent_breakdown(reducer)
        self.assertNotIn("session-grain", section)

    def test_caveat_mentions_not_attributable_per_call(self) -> None:
        # The ticket's hard constraint, made visible in the report itself.
        reducer = Reducer()
        reducer.absorb(
            "hermes",
            ParseResult(calls=[make_call(agent="hermes")], malformed=0, session_cache_read_tokens=5),
        )
        section = self._agent_breakdown(reducer)
        self.assertIn("not attributable to individual tool calls", section)

    def test_five_sections_still_in_order_with_caveat_present(self) -> None:
        reducer = Reducer()
        reducer.absorb(
            "hermes",
            ParseResult(calls=[make_call(agent="hermes")], malformed=0, session_cache_read_tokens=5),
        )
        report = render_report(
            reducer,
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            since_note=None,
        )
        headers = [
            "## Agent Breakdown",
            "## Tool Leaderboard",
            "## Model Breakdown",
            "## Inefficiency Callouts",
            "## Summary",
        ]
        indices = [report.index(h) for h in headers]
        self.assertEqual(indices, sorted(indices))

    def test_tool_leaderboard_cache_column_unaffected_by_session_grain_hit(self) -> None:
        """The core acceptance proof: a real session-grain hit must NOT leak into
        the per-call `cache_assisted` column, which stays `n/a` -- hermes calls
        genuinely carry no per-call usage (ABSENT_BY_SCHEMA), regardless of what
        the session row says."""
        reducer = Reducer()
        reducer.absorb(
            "hermes",
            ParseResult(
                calls=[
                    make_call(
                        agent="hermes",
                        usage=None,
                        usage_provenance=UsageProvenance.ABSENT_BY_SCHEMA,
                    )
                ],
                malformed=0,
                session_cache_read_tokens=999,
            ),
        )
        report = render_report(
            reducer,
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            since_note=None,
        )
        leaderboard = report[report.index("## Tool Leaderboard") : report.index("## Model Breakdown")]
        row = next(line for line in leaderboard.splitlines() if "| hermes |" in line)
        cache_note = row.rstrip("|").rsplit("|", 1)[-1].strip()
        self.assertEqual(cache_note, "n/a")


class DiscoveryReconciliationRenderTests(unittest.TestCase):
    """TB-21: the Summary reconciles discovery and renders skips as a per-reason
    histogram keyed on the typed SkipReason (S34), not a one-line 1600-entry blob."""

    def _reducer(self, scanned: int) -> Reducer:
        reducer = Reducer()
        for _ in range(scanned):
            reducer.absorb("claude-code", ParseResult(calls=[make_call()], malformed=0))
        return reducer

    def _summary(
        self, reducer: Reducer, skips: list[SkipRecord], *, verbose: bool = False
    ) -> str:
        report = render_report(
            reducer,
            index_source="agentsview",
            fallback_reason=None,
            skips=skips,
            include_subagents=True,
            since_note=None,
            verbose=verbose,
        )
        return report[report.index("## Summary") :]

    def test_summary_reconciles_discovered_scanned_skipped(self) -> None:
        skips = [
            SkipRecord("a", "claude", SkipReason.MISSING_SOURCE, "x"),
            SkipRecord("b", "codex", SkipReason.UNKNOWN_SCHEMA, "y"),
            SkipRecord("c", "cursor", SkipReason.UNKNOWN_SCHEMA, "z"),
        ]
        summary = self._summary(self._reducer(2), skips)
        self.assertIn("Sessions discovered: 5 / scanned: 2 / skipped: 3", summary)

    def test_histogram_lists_each_reason_sorted_by_count_desc(self) -> None:
        skips = [
            SkipRecord("a", "claude", SkipReason.MISSING_SOURCE, "x"),
            SkipRecord("b", "codex", SkipReason.UNKNOWN_SCHEMA, "y"),
            SkipRecord("c", "cursor", SkipReason.UNKNOWN_SCHEMA, "z"),
        ]
        summary = self._summary(self._reducer(1), skips)
        self.assertIn("Skipped by reason:", summary)
        self.assertIn("unknown_schema: 2", summary)
        self.assertIn("missing_source: 1", summary)
        # the actionable bucket (2) outranks the dead-index bucket (1)
        self.assertLess(summary.index("unknown_schema: 2"), summary.index("missing_source: 1"))

    def test_no_histogram_when_nothing_skipped(self) -> None:
        summary = self._summary(self._reducer(1), [])
        self.assertNotIn("Skipped by reason:", summary)
        self.assertIn("Sessions discovered: 1 / scanned: 1 / skipped: 0", summary)

    def test_old_single_line_skipped_roots_blob_is_gone(self) -> None:
        skips = [SkipRecord("a", "claude", SkipReason.MISSING_SOURCE, "x")]
        summary = self._summary(self._reducer(1), skips)
        self.assertNotIn("Skipped roots:", summary)

    def test_individual_ids_appear_only_under_verbose(self) -> None:
        skips = [SkipRecord("sess-xyz", "codex", SkipReason.UNKNOWN_SCHEMA, "no parser claimed")]
        default = self._summary(self._reducer(1), skips, verbose=False)
        self.assertNotIn("sess-xyz", default)
        verbose = self._summary(self._reducer(1), skips, verbose=True)
        self.assertIn("Skipped sessions (detail)", verbose)
        self.assertIn("sess-xyz", verbose)
        self.assertIn("no parser claimed", verbose)


class DiscoveryReconciliationMainTests(unittest.TestCase):
    """TB-21 end-to-end: a scanned session, a dead index entry, and an unparseable
    session must reconcile in the Summary, with reasons typed and ids off by default."""

    def test_main_report_reconciles_a_mixed_discovery(self) -> None:
        good = (FIXTURES / "sample.jsonl").read_text()
        payload = {
            "sessions": [
                {"id": "good-1", "project": "p", "agent": "claude"},
                {"id": "dead-1", "project": "p", "agent": "claude"},
                {"id": "cursor-1", "project": "p", "agent": "cursor"},
            ],
            "next_cursor": "",
            "total": 3,
        }
        runner = FakeRunner(
            [
                completed(stdout=json.dumps(payload)),
                completed(stdout=good),
                completed(returncode=1, stderr="fatal: source file not found: /x/dead-1.jsonl"),
                completed(stdout='{"role":"user","message":{}}\n'),
            ]
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--index-source", "agentsview"], runner=runner)
        self.assertEqual(code, 0)
        report = out.getvalue()
        self.assertIn("Sessions discovered: 3 / scanned: 1 / skipped: 2", report)
        self.assertIn("missing_source: 1", report)
        self.assertIn("unknown_schema: 1", report)
        # the skipped session ids stay out of the default report
        self.assertNotIn("dead-1", report)
        self.assertNotIn("cursor-1", report)


class CorpusFingerprintTests(unittest.TestCase):
    """TB-22 / S36: a fingerprint over the scanned session ids identifies the
    corpus that produced the numbers, so two reports can be checked for identical
    inputs before a delta between them is attributed to code."""

    def test_fingerprint_is_order_independent(self) -> None:
        # Discovery/paging order must never move the digest -- only membership can.
        a = corpus_fingerprint(["s3", "s1", "s2"])
        b = corpus_fingerprint(["s1", "s2", "s3"])
        self.assertEqual(a.digest, b.digest)
        self.assertEqual(a, b)

    def test_count_is_the_number_of_ids(self) -> None:
        self.assertEqual(corpus_fingerprint(["a", "b", "c"]).count, 3)
        self.assertEqual(corpus_fingerprint([]).count, 0)

    def test_membership_change_changes_the_digest(self) -> None:
        base = corpus_fingerprint(["a", "b", "c"])
        dropped = corpus_fingerprint(["a", "b"])  # a session aged out mid-scan
        added = corpus_fingerprint(["a", "b", "c", "d"])
        self.assertNotEqual(base.digest, dropped.digest)
        self.assertNotEqual(base.digest, added.digest)

    def test_empty_and_populated_differ(self) -> None:
        self.assertNotEqual(corpus_fingerprint([]).digest, corpus_fingerprint(["a"]).digest)

    def test_a_grown_session_moves_the_digest_with_the_same_ids(self) -> None:
        # The live session appends a call between runs: same id set, different
        # content. The fingerprint must move -- an id-only digest would falsely
        # match and let a reader attribute the delta to code (the "must not
        # survive" outcome). session_signature folds the call count to catch it.
        before = corpus_fingerprint([session_signature("live", 10, 0), session_signature("s2", 3, 0)])
        after = corpus_fingerprint([session_signature("live", 11, 0), session_signature("s2", 3, 0)])
        self.assertNotEqual(before.digest, after.digest)
        self.assertEqual(before.count, after.count)  # same number of sessions

    def test_a_malformed_line_moves_the_digest_with_the_same_call_count(self) -> None:
        # An append can land as a malformed line rather than a new valid call:
        # call_count is unchanged but the Summary's "Malformed lines" moves. The
        # fingerprint must fold malformed too, or it would falsely match while a
        # rendered number differs.
        before = corpus_fingerprint([session_signature("live", 10, 0)])
        after = corpus_fingerprint([session_signature("live", 10, 1)])
        self.assertNotEqual(before.digest, after.digest)

    def test_an_appended_web_search_call_moves_the_digest(self) -> None:
        # TB-24 adds a rendered number ("Unjoinable tool records"). A web_search_call
        # append leaves call_count and malformed unchanged but moves that number, so
        # the signature must fold the unjoinable total or the fingerprint would falsely
        # match while a rendered number differs -- the S36 outcome that must not survive.
        before = corpus_fingerprint([session_signature("live", 10, 0, 0)])
        after = corpus_fingerprint([session_signature("live", 10, 0, 1)])
        self.assertNotEqual(before.digest, after.digest)


class CorpusFingerprintRenderTests(unittest.TestCase):
    """S36: the Summary carries the fingerprint line so a reader can compare inputs."""

    def _reducer(self, scanned: int) -> Reducer:
        reducer = Reducer()
        for _ in range(scanned):
            reducer.absorb("claude-code", ParseResult(calls=[make_call()], malformed=0))
        return reducer

    def _summary(self, fingerprint: CorpusFingerprint | None) -> str:
        report = render_report(
            self._reducer(3),
            index_source="agentsview",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            since_note=None,
            fingerprint=fingerprint,
        )
        return report[report.index("## Summary") :]

    def test_summary_carries_fingerprint_line(self) -> None:
        fp = corpus_fingerprint(["s1", "s2", "s3"])
        summary = self._summary(fp)
        self.assertIn(f"Corpus fingerprint: {fp.digest} (3 sessions scanned)", summary)

    def test_no_fingerprint_line_when_absent(self) -> None:
        self.assertNotIn("Corpus fingerprint:", self._summary(None))


class CorpusFingerprintMainTests(unittest.TestCase):
    """S36 end-to-end: an unchanged scanned set yields an identical fingerprint
    line; a session that vanishes mid-scan moves it."""

    def _payload(self) -> str:
        return json.dumps(
            {
                "sessions": [
                    {"id": "good-1", "project": "p", "agent": "claude"},
                    {"id": "good-2", "project": "p", "agent": "claude"},
                ],
                "next_cursor": "",
                "total": 2,
            }
        )

    def _fingerprint_line(self, report: str) -> str:
        for line in report.splitlines():
            if "Corpus fingerprint:" in line:
                return line
        raise AssertionError("no fingerprint line in report")

    def test_two_identical_runs_produce_the_same_fingerprint_line(self) -> None:
        good = (FIXTURES / "sample.jsonl").read_text()
        reports = []
        for _ in range(2):
            runner = FakeRunner(
                [completed(stdout=self._payload()), completed(stdout=good), completed(stdout=good)]
            )
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--index-source", "agentsview"], runner=runner)
            reports.append(out.getvalue())
        self.assertEqual(self._fingerprint_line(reports[0]), self._fingerprint_line(reports[1]))

    def test_a_vanished_session_moves_the_fingerprint(self) -> None:
        good = (FIXTURES / "sample.jsonl").read_text()
        # run 1: both sessions scan.
        r1 = FakeRunner(
            [completed(stdout=self._payload()), completed(stdout=good), completed(stdout=good)]
        )
        # run 2: good-2's transcript has aged out of the retention window.
        r2 = FakeRunner(
            [
                completed(stdout=self._payload()),
                completed(stdout=good),
                completed(returncode=1, stderr="fatal: source file not found: /x/good-2.jsonl"),
            ]
        )
        outs = []
        for runner in (r1, r2):
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--index-source", "agentsview"], runner=runner)
            outs.append(out.getvalue())
        self.assertNotEqual(self._fingerprint_line(outs[0]), self._fingerprint_line(outs[1]))
        self.assertIn("(2 sessions scanned)", self._fingerprint_line(outs[0]))
        self.assertIn("(1 sessions scanned)", self._fingerprint_line(outs[1]))

    def test_a_grown_session_moves_the_fingerprint_with_the_same_id_set(self) -> None:
        # Same discovered ids both runs, but good-1's append-only transcript grows
        # by a call. Scanned count is unchanged (2 sessions), yet the fingerprint
        # must move so the two reports are not falsely declared comparable.
        good = (FIXTURES / "sample.jsonl").read_text()
        extra = (
            '{"type":"assistant","sessionId":"sess-001","timestamp":"2026-07-08T10:00:09Z",'
            '"message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_009",'
            '"name":"Bash","input":{"command":"pwd"}}],"usage":{"input_tokens":5,'
            '"output_tokens":1},"model":"claude-opus-4-8"}}\n'
        )
        grown = good + extra
        r1 = FakeRunner(
            [completed(stdout=self._payload()), completed(stdout=good), completed(stdout=good)]
        )
        r2 = FakeRunner(
            [completed(stdout=self._payload()), completed(stdout=grown), completed(stdout=good)]
        )
        outs = []
        for runner in (r1, r2):
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--index-source", "agentsview"], runner=runner)
            outs.append(out.getvalue())
        self.assertNotEqual(self._fingerprint_line(outs[0]), self._fingerprint_line(outs[1]))
        # both still scanned 2 sessions -- the move is content, not membership
        self.assertIn("(2 sessions scanned)", self._fingerprint_line(outs[0]))
        self.assertIn("(2 sessions scanned)", self._fingerprint_line(outs[1]))


class CorpusFreezeMainTests(unittest.TestCase):
    """TB-22 / S37: `--freeze <manifest>` pins the discovered set (write-once),
    replays it on later runs, and names refs that have vanished since the freeze."""

    def _payload(self) -> str:
        return json.dumps(
            {
                "sessions": [
                    {"id": "good-1", "project": "p", "agent": "claude"},
                    {"id": "good-2", "project": "p", "agent": "claude"},
                ],
                "next_cursor": "",
                "total": 2,
            }
        )

    def test_first_run_writes_the_manifest(self) -> None:
        good = (FIXTURES / "sample.jsonl").read_text()
        with TemporaryDirectory() as d:
            manifest = str(Path(d) / "corpus.manifest")
            runner = FakeRunner(
                [completed(stdout=self._payload()), completed(stdout=good), completed(stdout=good)]
            )
            with redirect_stdout(io.StringIO()):
                code = main(["--index-source", "agentsview", "--freeze", manifest], runner=runner)
            self.assertEqual(code, 0)
            self.assertTrue(Path(manifest).exists())
            m = read_manifest(manifest)
            self.assertEqual({r.session_id for r in m.refs}, {"good-1", "good-2"})

    def test_replay_uses_frozen_refs_not_live_discovery(self) -> None:
        good = (FIXTURES / "sample.jsonl").read_text()
        with TemporaryDirectory() as d:
            manifest = str(Path(d) / "corpus.manifest")
            refs = [
                SessionRef("claude", "agentsview", "p", "good-1", None),
                SessionRef("claude", "agentsview", "p", "good-2", None),
            ]
            write_manifest(manifest, refs, corpus_fingerprint(["good-1", "good-2"]).digest)
            # Only exports -- no `session list` call, because inputs come from the manifest.
            runner = FakeRunner([completed(stdout=good), completed(stdout=good)])
            with redirect_stdout(io.StringIO()):
                code = main(["--index-source", "agentsview", "--freeze", manifest], runner=runner)
            self.assertEqual(code, 0)
            self.assertTrue(all("list" not in argv for argv in runner.calls))

    def test_replay_reports_refs_that_vanished_since_freeze(self) -> None:
        good = (FIXTURES / "sample.jsonl").read_text()
        with TemporaryDirectory() as d:
            manifest = str(Path(d) / "corpus.manifest")
            refs = [
                SessionRef("claude", "agentsview", "p", "good-1", None),
                SessionRef("claude", "agentsview", "p", "gone-2", None),
            ]
            write_manifest(manifest, refs, corpus_fingerprint(["good-1", "gone-2"]).digest)
            runner = FakeRunner(
                [
                    completed(stdout=good),
                    completed(returncode=1, stderr="fatal: source file not found: /x/gone-2.jsonl"),
                ]
            )
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--index-source", "agentsview", "--freeze", manifest, "--verbose"], runner=runner)
            report = out.getvalue()
            self.assertIn("vanished since freeze", report)
            self.assertIn("1", self._vanished_line(report))
            self.assertIn("gone-2", report)  # named under --verbose

    def _vanished_line(self, report: str) -> str:
        for line in report.splitlines():
            if "vanished since freeze" in line:
                return line
        raise AssertionError("no vanished line")

    def test_two_replays_are_byte_identical_when_nothing_vanished(self) -> None:
        good = (FIXTURES / "sample.jsonl").read_text()
        with TemporaryDirectory() as d:
            manifest = str(Path(d) / "corpus.manifest")
            refs = [
                SessionRef("claude", "agentsview", "p", "good-1", None),
                SessionRef("claude", "agentsview", "p", "good-2", None),
            ]
            write_manifest(manifest, refs, corpus_fingerprint(["good-1", "good-2"]).digest)
            outs = []
            for _ in range(2):
                runner = FakeRunner([completed(stdout=good), completed(stdout=good)])
                out = io.StringIO()
                with redirect_stdout(out):
                    main(["--index-source", "agentsview", "--freeze", manifest], runner=runner)
                outs.append(out.getvalue())
            self.assertEqual(outs[0], outs[1])


if __name__ == "__main__":
    unittest.main()
