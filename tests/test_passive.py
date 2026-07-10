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

from toolbench.adapters import UnknownSchema
from toolbench.passive import (
    OVERSIZED_OUTPUT_TOKENS,
    UNKNOWN_MODEL,
    Reducer,
    _apply_date_range,
    _is_subagent_path,
    _parse_ref,
    filter_subagents,
    main,
    parse_args,
    render_report,
)
from toolbench.sources import SessionRef
from toolbench.transcript import ParseResult, ToolCall, UsageProvenance

FIXTURES = Path(__file__).parent / "fixtures"


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Scripted subprocess-runner seam, mirroring test_sources.py's FakeRunner."""

    def __init__(self, responses: list[subprocess.CompletedProcess[str] | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if not self._responses:
            raise AssertionError(f"FakeRunner exhausted, unexpected call: {argv}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_call(**overrides: object) -> ToolCall:
    fields: dict[str, object] = {
        "agent": "claude-code",
        "source": "raw",
        "project": "tool-benchmarks",
        "name": "Read",
        "input_chars": 40,
        "output_chars": 400,
        "session_id": "sess-1",
        "ts": "2026-07-08T00:00:00Z",
        "usage": None,
        "duration_ms": None,
        "error": None,
        "model": "claude-opus-4-8",
    }
    fields.update(overrides)
    # Mirrors ClaudeParser._provenance so existing tests keep their meaning.
    fields.setdefault(
        "usage_provenance",
        UsageProvenance.PRESENT
        if fields["usage"] is not None
        else UsageProvenance.ABSENT_UNEXPECTED,
    )
    return ToolCall(**fields)  # type: ignore[arg-type]


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
            skipped_roots=[],
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
            skipped_roots=["/nonexistent"],
            include_subagents=False,
            since_note="2026-07-01",
        )
        for expected in (
            "Index source: raw",
            "Sessions scanned:",
            "Tool calls joined:",
            "Malformed lines:",
            "Subagents included: no",
            "AgentsView fallback reason: agentsview exited 1: daemon down",
            "/nonexistent",
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
            skipped_roots=[],
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
            skipped_roots=[],
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
            skipped_roots=[],
            include_subagents=True,
            since_note=None,
        )
        section = report[report.index("## Model Breakdown") : report.index("## Inefficiency Callouts")]
        self.assertIn("| claude-code | claude-opus-4-8 | Read | 1 | 100 |", section)
        self.assertIn("| claude-code | claude-haiku-4-5 | Read | 1 | 2000 |", section)
        # Ranked by context tokens descending: haiku (2000) outranks opus (100).
        self.assertLess(section.index("claude-haiku-4-5"), section.index("claude-opus-4-8"))


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
        self.assertIn("skipped roots", out.getvalue())

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
        runner = FakeRunner([_completed(stderr="daemon down", returncode=1)])
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
                _completed(stdout=json.dumps(payload)),
                _completed(stdout=raw_text),
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
        self.assertIn("Sessions scanned: 2", report)

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
                _completed(stdout=json.dumps(payload)),
                UnicodeDecodeError("utf-8", b"\xa0", 0, 1, "invalid start byte"),
                _completed(stdout=raw_text),
            ]
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--index-source", "agentsview"], runner=runner)
        self.assertEqual(code, 0)
        report = out.getvalue()
        self.assertIn("## Summary", report)
        self.assertIn("bad-session", report)
        self.assertIn("Sessions scanned: 1", report)


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
                _completed(stdout=json.dumps(payload)),
                _completed(stdout=sqlite_payload),
                _completed(stdout=raw_text),
            ]
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--index-source", "agentsview"], runner=runner)
        self.assertEqual(code, 0)
        report = out.getvalue()
        self.assertIn("cowork-1", report)
        self.assertIn("Sessions scanned: 1", report)
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
        runner = FakeRunner([_completed(stdout=json.dumps(payload)), _completed(stdout="SQLite format 3\x00")])
        with redirect_stdout(io.StringIO()):
            main(["--index-source", "agentsview"], runner=runner)
        self.assertEqual(set(tmp_root.glob("*.jsonl")) - before, set())


# --- TB-13: _parse_ref delegates to the registry; unknown schemas raise --------


def _ok_export(stdout: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_parse_ref_parses_an_agentsview_claude_session_without_a_temp_file():
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


def test_parse_ref_raises_unknown_schema_for_codex_instead_of_returning_zero():
    ref = SessionRef(
        agent="codex", source="agentsview", project="p", session_id="codex:1", path=None
    )
    body = '{"type":"session_meta","payload":{},"timestamp":"t"}\n'
    with pytest.raises(UnknownSchema):
        _parse_ref(ref, runner=lambda argv: _ok_export(body))


def test_passive_no_longer_imports_tempfile():
    import toolbench.passive as p

    assert not hasattr(p, "tempfile"), "the NamedTemporaryFile round-trip must be gone"


def test_unknown_schema_lands_in_skipped_roots_not_as_a_zero_row():
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
            skipped_roots=[],
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
            skipped_roots=[],
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
            skipped_roots=[],
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
            skipped_roots=[],
            include_subagents=True,
            since_note=None,
        )
        leaderboard = report[report.index("## Tool Leaderboard") : report.index("## Model Breakdown")]
        row = next(line for line in leaderboard.splitlines() if "| hermes |" in line)
        cache_note = row.rstrip("|").rsplit("|", 1)[-1].strip()
        self.assertEqual(cache_note, "n/a")


if __name__ == "__main__":
    unittest.main()
