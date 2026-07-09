import dataclasses
import io
import json
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from toolbench.passive import (
    OVERSIZED_OUTPUT_TOKENS,
    UNKNOWN_MODEL,
    Reducer,
    _apply_date_range,
    _is_subagent_path,
    filter_subagents,
    main,
    parse_args,
    render_report,
)
from toolbench.sources import SessionRef
from toolbench.transcript import ParseResult, ToolCall

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


if __name__ == "__main__":
    unittest.main()
