import io
import json
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.fakes import FakeRunner, completed, make_call
from toolbench.adapters import UnknownSchema
from toolbench.freeze import MANIFEST_VERSION, read_manifest, write_manifest
from toolbench.passive import (
    Reducer,
    _apply_date_range,
    _discover_refs,
    _parse_ref,
    classify_skip,
    corpus_fingerprint,
    filter_subagents,
    main,
    parse_args,
    skip_record_for,
    tally_skips,
)
from toolbench.sources import (
    AGENTSVIEW_TIMEOUT_S,
    AgentCensus,
    AgentsViewTimeout,
    MissingSourceExport,
    NonTranscriptExport,
    Runner,
    SessionRef,
    SkipReason,
    SkipRecord,
)
from toolbench.transcript import BranchUsage, ParseResult

FIXTURES = Path(__file__).parent / "fixtures"


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

    def test_session_cache_creation_tokens_survives_date_filtering(self) -> None:
        """S39: the TB-25 survival invariant extends to the creation sum. It holds by
        construction today -- `_apply_date_range` rebuilds via `replace()`, so a field
        added to ParseResult carries through untouched -- and this pins it, so trading
        `replace()` back for a hand-listed reconstruction cannot silently reset the
        creation column to None (measured-zero and unmeasured are not the same, S32)."""
        result = ParseResult(
            calls=[make_call(ts="2026-06-01T00:00:00Z")],
            malformed=0,
            session_cache_read_tokens=42,
            session_cache_creation_tokens=7,
        )
        filtered = _apply_date_range(result, "2026-07-01", None)
        self.assertEqual(len(filtered.calls), 0)
        self.assertEqual(filtered.session_cache_read_tokens, 42)
        self.assertEqual(filtered.session_cache_creation_tokens, 7)

    def test_measured_zero_cache_is_not_reset_to_unmeasured_by_date_filtering(self) -> None:
        """S32/S39: `0` is a measured value, `None` is not. A falsy-check reconstruction
        (`x if x else None`) would pass the =42 tests above and still corrupt this one."""
        result = ParseResult(
            calls=[make_call(ts="2026-06-01T00:00:00Z")],
            malformed=0,
            session_cache_read_tokens=0,
            session_cache_creation_tokens=0,
        )
        filtered = _apply_date_range(result, "2026-07-01", None)
        self.assertEqual(filtered.session_cache_read_tokens, 0)
        self.assertEqual(filtered.session_cache_creation_tokens, 0)
        self.assertIsNotNone(filtered.session_cache_read_tokens)
        self.assertIsNotNone(filtered.session_cache_creation_tokens)

    def test_usage_by_branch_survives_date_filtering(self) -> None:
        """S40 inherits the TB-25 invariant: usage_by_branch is session-grain, not a
        per-call value, so it passes through --date-from/--date-to intact even when
        every call is filtered out."""
        result = ParseResult(
            calls=[make_call(ts="2026-06-01T00:00:00Z")],
            malformed=0,
            usage_by_branch={"feat/tb-21": BranchUsage(read=300, creation=30, messages=1)},
        )
        filtered = _apply_date_range(result, "2026-07-01", None)
        self.assertEqual(len(filtered.calls), 0)
        self.assertEqual(filtered.usage_by_branch["feat/tb-21"].read, 300)
        self.assertEqual(filtered.usage_by_branch["feat/tb-21"].creation, 30)


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

    def test_run_manifest_and_tickets_flags(self) -> None:
        args = parse_args(["--run-manifest", "run.json", "--tickets", "3"])
        self.assertEqual(args.run_manifest, "run.json")
        self.assertEqual(args.tickets, 3)

    def test_run_manifest_defaults_to_none(self) -> None:
        args = parse_args([])
        self.assertIsNone(args.run_manifest)
        self.assertIsNone(args.tickets)

    def test_tickets_zero_is_rejected(self) -> None:
        """S39/S40: `--tickets 0` cannot normalize. Reject it at the CLI rather than
        silently skipping normalization -- a per-ticket figure quietly missing from
        the report is how a benchmark comparison gets made against the wrong number."""
        with self.assertRaises(SystemExit):
            parse_args(["--tickets", "0"])

class SubagentFilterTests(unittest.TestCase):
    def test_filter_subagents_uses_is_subagent_flag(self) -> None:
        """CQ 3.2: exclude-subagents filters the discovery flag, not a path substring."""
        refs = [
            SessionRef(
                agent="claude-code",
                source="raw",
                project="proj",
                session_id="s1",
                path="/x/proj/session.jsonl",
                is_subagent=True,
            ),
            SessionRef(
                agent="claude-code",
                source="raw",
                project="proj",
                session_id="s2",
                path="/x/proj/session.jsonl",
                is_subagent=False,
            ),
            SessionRef(
                agent="claude",
                source="agentsview",
                project="p",
                session_id="s3",
                path=None,
                is_subagent=False,
            ),
        ]
        kept = filter_subagents(refs)
        self.assertEqual([r.session_id for r in kept], ["s2", "s3"])

    def test_filter_subagents_does_not_infer_from_path_substring(self) -> None:
        """A path containing /subagents/ without the flag is kept — discovery owns the tag."""
        refs = [
            SessionRef(
                agent="claude-code",
                source="raw",
                project="proj",
                session_id="s1",
                path="/x/proj/subagents/y.jsonl",
                is_subagent=False,
            ),
        ]
        kept = filter_subagents(refs)
        self.assertEqual([r.session_id for r in kept], ["s1"])

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

    def test_auto_continues_and_reports_skipped_source_when_agentsview_vanishes_mid_discovery(
        self,
    ) -> None:
        # Regression: `discover_agentsview` (TB-33) runs its parent-probe pass and
        # per-agent census EAGERLY, inside the `iter_sessions(...)` CALL rather than
        # lazily during ref iteration. Call [0] is the `_probe_agentsview` availability
        # probe -- it succeeds, so agentsview looks alive. Call [1] is the eager
        # parent-probe pass inside `discover_agentsview`, and it raises
        # `FileNotFoundError` -- agentsview vanished moments after the probe. If
        # `_discover_refs` did not wrap the `iter_sessions(...)` call itself in its
        # try/except, this would surface as `main`'s fatal source error (exit 1)
        # instead of degrading gracefully to a `MISSING_SOURCE` skip (exit 0).
        probe = {"sessions": [], "next_cursor": "", "total": 0}
        runner = FakeRunner([
            completed(stdout=json.dumps(probe)),
            FileNotFoundError("agentsview vanished"),
        ])
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
                # Parent probe, per-agent census (--limit 1, one agent seen: "claude")
                # + the run-scoped archive total, then the full listing (TB-31, TB-33),
                # then the session export.
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
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


class ZeroMatchCensusDisclosureTests(unittest.TestCase):
    """TB-34: by the zero-match early return, `main` has already built a full
    `AgentCensus` (via `_discover_refs`/`iter_sessions`) -- discarding it there was
    the one place TB-33's disclosure never reached, leaving a narrow window
    indistinguishable from a truly empty archive. The disclosure must be additive:
    the original "no sessions matched" line is never replaced, only extended."""

    def test_never_reached_agent_is_named_even_when_nothing_matched(self) -> None:
        probe_payload = {
            "sessions": [{"id": "old-1", "project": "p", "agent": "claude-code"}],
            "next_cursor": "",
            "total": 1,
        }
        empty_payload = {"sessions": [], "next_cursor": "", "total": 0}
        runner = FakeRunner(
            [
                # Parent probe (sees claude-code), per-agent census (--limit 1) + the
                # run-scoped archive total, then the full listing -- which this run's
                # window (e.g. an overly narrow `--since`) reaches with zero refs, so
                # `reducer.calls_joined` never leaves 0 (TB-31, TB-33).
                completed(stdout=json.dumps(probe_payload)),
                completed(stdout=_json_total(42)),
                completed(stdout=_json_total(42)),
                completed(stdout=json.dumps(empty_payload)),
            ]
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--index-source", "agentsview"], runner=runner)
        self.assertEqual(code, 0)
        message = out.getvalue()
        # The original message survives byte-for-byte -- the disclosure is additive,
        # never a replacement.
        self.assertIn(
            "toolbench.passive: no sessions matched the given selection.\n", message
        )
        self.assertIn(
            "Present in the archive, not reached by this window: claude-code (42 sessions)",
            message,
        )

    def test_unenumerated_archive_residual_is_named_even_when_nothing_matched(self) -> None:
        empty_payload = {"sessions": [], "next_cursor": "", "total": 0}
        runner = FakeRunner(
            [
                # Parent probe sees no agents at all (every session is a child, invisible
                # to the child-excluded probe listing), so `_agent_census` makes no
                # per-agent `_list_total` calls -- only the run-scoped archive total,
                # which the archive still answers non-zero for. The full listing then
                # also comes back empty, so nothing is ever absorbed.
                completed(stdout=json.dumps(empty_payload)),
                completed(stdout=_json_total(17)),
                completed(stdout=json.dumps(empty_payload)),
            ]
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--index-source", "agentsview"], runner=runner)
        self.assertEqual(code, 0)
        message = out.getvalue()
        self.assertIn(
            "toolbench.passive: no sessions matched the given selection.\n", message
        )
        self.assertIn(
            "Reconciliation: 17 archive sessions belong to no agent we enumerated",
            message,
        )


def _exclude_subagents_population_runner() -> "Runner":
    """A realistic `agentsview` double for scenario (A) of TB-33 Finding 1.

    `alpha` is 2 parent sessions, 0 children. `beta` is 1 parent, 9 children. Unlike
    `FakeRunner`, which returns a scripted response regardless of the argv it was sent,
    this inspects `--include-children` and `--agent` and answers as the REAL agentsview
    would -- which is what makes it possible to fail on the pre-fix code: pre-fix, the
    census always requested `_ALL_INCLUDES` (parents + children) no matter what
    `--exclude-subagents` said, so `beta`'s census total came back 10, not 1, even
    though only its 1 parent could ever be scanned under that flag.
    """
    totals = {
        ("alpha", True): 2,
        ("alpha", False): 2,
        ("beta", True): 10,
        ("beta", False): 1,
        ("all", True): 12,
        ("all", False): 3,
    }
    probe_page = _json_page(
        {"id": "alpha-p1", "project": "p", "agent": "alpha"},
        {"id": "alpha-p2", "project": "p", "agent": "alpha"},
        {"id": "beta-p1", "project": "p", "agent": "beta"},
    )
    full_page = _json_page(
        {"id": "alpha-p1", "project": "p", "agent": "alpha"},
        {"id": "alpha-p2", "project": "p", "agent": "alpha"},
        {"id": "beta-p1", "project": "p", "agent": "beta"},
        *(
            {"id": f"beta-c{i}", "project": "p", "agent": "beta"}
            for i in range(9)
        ),
    )
    raw_text = (FIXTURES / "sample.jsonl").read_text()

    def runner(argv: list[str]) -> "subprocess.CompletedProcess[str]":
        if argv[:3] == ["agentsview", "session", "export"]:
            return completed(stdout=raw_text)
        has_children = "--include-children" in argv
        agent = argv[argv.index("--agent") + 1] if "--agent" in argv else "all"
        if argv[argv.index("--limit") + 1] == "1":
            return completed(stdout=_json_total(totals[(agent, has_children)]))
        return completed(stdout=full_page if has_children else probe_page)

    return runner


def _json_page(*sessions: dict[str, str]) -> str:
    return json.dumps({"sessions": list(sessions), "next_cursor": "", "total": len(sessions)})


def _json_total(total: int) -> str:
    return json.dumps({"sessions": [], "next_cursor": "", "total": total})


class ExcludeSubagentsCensusPopulationTests(unittest.TestCase):
    """The census denominator must describe the SAME population `--exclude-subagents`
    leaves in the numerator (TB-33 Finding 1). Models failure scenario (A) from the
    ticket: no `--limit` at all, an agent (`beta`) whose archive is mostly children.
    """

    def test_fully_scanned_population_renders_100_percent_and_no_uneven_warning(self) -> None:
        runner = _exclude_subagents_population_runner()
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--index-source", "agentsview", "--exclude-subagents"], runner=runner)
        self.assertEqual(code, 0)
        report = out.getvalue()

        # Both agents were scanned to 100% of the population THIS RUN measures (parents
        # only) -- not a misleading "1 of 10 (10.0%)" for beta, whose denominator would
        # be inflated by 9 children this run structurally cannot ever reach.
        self.assertIn("alpha | 2 of 2 (100.0%)", report)
        self.assertIn("beta | 1 of 1 (100.0%)", report)
        self.assertNotIn("Sampling is uneven", report)
        self.assertIn("Subagents included: no", report)


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
                # Parent probe, per-agent census (--limit 1, one agent seen: "claude")
                # + the run-scoped archive total, then the full listing (TB-31, TB-33).
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
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

class RunManifestMainTests(unittest.TestCase):
    def test_malformed_run_manifest_exits_1_with_a_clear_message(self) -> None:
        """The ticket originally pointed --run-manifest at agents.md (markdown).
        Feeding one in must fail clearly, not with a stack trace."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.md"
            path.write_text("# Agents\n\n| Role | Ticket |\n", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main(["--run-manifest", str(path)])
            self.assertEqual(code, 1)
            self.assertIn("not valid JSON", err.getvalue())

    def test_non_utf8_run_manifest_exits_1_with_a_clear_message(self) -> None:
        """UnicodeDecodeError subclasses ValueError, not OSError -- it must not
        escape as an uncaught traceback (S23)."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            path.write_bytes(b"\xff\xfe\x00invalid")
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main(["--run-manifest", str(path)])
            self.assertEqual(code, 1)
            self.assertIn("not valid UTF-8", err.getvalue())

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
                # Parent probe, per-agent census (--limit 1, two agents seen, sorted:
                # "claude" then "cowork") + the run-scoped archive total, then the full
                # listing (TB-31, TB-33).
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
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
        # Parent probe, per-agent census (--limit 1, one agent seen: "cowork") + the
        # run-scoped archive total, then the full listing (TB-31, TB-33), then export.
        runner = FakeRunner(
            [
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
                completed(stdout="SQLite format 3\x00"),
            ]
        )
        with redirect_stdout(io.StringIO()):
            main(["--index-source", "agentsview"], runner=runner)
        self.assertEqual(set(tmp_root.glob("*.jsonl")) - before, set())

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
    _refs, _fallback, skips, census, _truncated = _discover_refs(
        args, "/definitely/not/a/real/root", runner
    )
    assert skips, "a missing raw root must be recorded as a skip"
    assert all(isinstance(s, SkipRecord) for s in skips)
    assert skips[0].reason is SkipReason.MISSING_SOURCE
    assert census.unavailable_reason is not None, (
        "a discovery that never completed must not report a measured zero census"
    )

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
                # Parent probe, per-agent census (--limit 1, two agents seen, sorted:
                # "claude" then "cursor") + the run-scoped archive total, then the full
                # listing (TB-31, TB-33).
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
                completed(stdout=json.dumps(payload)),
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
            # Parent probe, per-agent census (--limit 1, one agent seen: "claude") +
            # the run-scoped archive total, then the full listing (TB-31, TB-33).
            runner = FakeRunner(
                [
                    completed(stdout=self._payload()),
                    completed(stdout=self._payload()),
                    completed(stdout=self._payload()),
                    completed(stdout=self._payload()),
                    completed(stdout=good),
                    completed(stdout=good),
                ]
            )
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--index-source", "agentsview"], runner=runner)
            reports.append(out.getvalue())
        self.assertEqual(self._fingerprint_line(reports[0]), self._fingerprint_line(reports[1]))

    def test_a_vanished_session_moves_the_fingerprint(self) -> None:
        good = (FIXTURES / "sample.jsonl").read_text()
        # run 1: both sessions scan. Parent probe, per-agent census (--limit 1, one
        # agent seen: "claude") + the run-scoped archive total, then the full listing
        # (TB-31, TB-33).
        r1 = FakeRunner(
            [
                completed(stdout=self._payload()),
                completed(stdout=self._payload()),
                completed(stdout=self._payload()),
                completed(stdout=self._payload()),
                completed(stdout=good),
                completed(stdout=good),
            ]
        )
        # run 2: good-2's transcript has aged out of the retention window.
        r2 = FakeRunner(
            [
                completed(stdout=self._payload()),
                completed(stdout=self._payload()),
                completed(stdout=self._payload()),
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
        # Parent probe, per-agent census (--limit 1, one agent seen: "claude") + the
        # run-scoped archive total, then the full listing (TB-31, TB-33).
        r1 = FakeRunner(
            [
                completed(stdout=self._payload()),
                completed(stdout=self._payload()),
                completed(stdout=self._payload()),
                completed(stdout=self._payload()),
                completed(stdout=good),
                completed(stdout=good),
            ]
        )
        r2 = FakeRunner(
            [
                completed(stdout=self._payload()),
                completed(stdout=self._payload()),
                completed(stdout=self._payload()),
                completed(stdout=self._payload()),
                completed(stdout=grown),
                completed(stdout=good),
            ]
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
            # Parent probe, per-agent census (--limit 1, one agent seen: "claude") +
            # the run-scoped archive total, then the full listing (TB-31, TB-33).
            runner = FakeRunner(
                [
                    completed(stdout=self._payload()),
                    completed(stdout=self._payload()),
                    completed(stdout=self._payload()),
                    completed(stdout=self._payload()),
                    completed(stdout=good),
                    completed(stdout=good),
                ]
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


class FreezeReplayCensusTests(unittest.TestCase):
    """TB-22/TB-33 pinned the ref list without a denominator, deliberately: persisting a
    census was a manifest FORMAT change out of that ticket's scope, so the gap was
    STATED rather than silently read as zero. TB-37 closes it: a v2 manifest persists
    the census taken at freeze time, so replay can disclose REAL fractions -- but they
    are HISTORICAL (archive size as of freeze time, not today), and that caveat must be
    on the page, not just true in principle."""

    def test_v2_replay_discloses_real_historical_fractions(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects" / "proj"
            root.mkdir(parents=True)
            shutil.copy(FIXTURES / "sample.jsonl", root / "s1.jsonl")
            manifest = str(Path(tmp) / "freeze.json")

            argv = ["--index-source", "raw", "--all", "--freeze", manifest]
            # First run discovers, writes the manifest AND (TB-37) the census it saw.
            main(argv, root=str(Path(tmp) / "projects"))
            m = read_manifest(manifest)
            assert m.census is not None
            self.assertIsNone(m.census.unavailable_reason)
            self.assertEqual(m.census.archive_total, 1)

            # Second run replays it -- discovery is bypassed, but the PERSISTED census
            # is not, so real fractions render instead of "unavailable".
            out = io.StringIO()
            with redirect_stdout(out):
                main(argv, root=str(Path(tmp) / "projects"))

            report = out.getvalue()
            self.assertNotIn("Sampling fractions unavailable", report)
            self.assertIn("1 of 1 (100.0%)", report)
            # The historical-denominator caveat is required, not optional (TB-37): a v2
            # census must never read as "current".
            self.assertIn("Historical denominator", report)
            self.assertIn("freeze time", report)
            self.assertIn("frozen corpus", report)

    def test_replay_with_opposite_subagent_filter_does_not_reuse_wrong_census(self) -> None:
        for freeze_excludes_subagents in (False, True):
            with self.subTest(freeze_excludes_subagents=freeze_excludes_subagents):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp) / "projects"
                    project = root / "proj"
                    subagents = project / "session-parent" / "subagents"
                    subagents.mkdir(parents=True)
                    shutil.copy(FIXTURES / "sample.jsonl", project / "parent.jsonl")
                    shutil.copy(FIXTURES / "sample.jsonl", subagents / "child.jsonl")
                    manifest = str(Path(tmp) / "freeze.json")

                    freeze_argv = ["--index-source", "raw", "--all", "--freeze", manifest]
                    if freeze_excludes_subagents:
                        freeze_argv.append("--exclude-subagents")
                    main(freeze_argv, root=str(root))

                    replay_argv = ["--index-source", "raw", "--all", "--freeze", manifest]
                    if not freeze_excludes_subagents:
                        replay_argv.append("--exclude-subagents")
                    out = io.StringIO()
                    with redirect_stdout(out):
                        code = main(replay_argv, root=str(root))

                    self.assertEqual(code, 0)
                    report = out.getvalue()
                    self.assertIn("Sampling fractions unavailable", report)
                    self.assertIn("freeze-time census", report)
                    self.assertIn("subagents", report)
                    self.assertIn("| claude-code | unknown |", report)
                    self.assertNotIn("(50.0%)", report)
                    self.assertNotIn("(200.0%)", report)

    def test_v1_manifest_replay_degrades_gracefully_named_by_version(self) -> None:
        """A manifest frozen before TB-37 has no `census` key at all -- replay must not
        crash, and the disclosure names the MANIFEST VERSION specifically, not
        "freezing" in general, so a future format's own gap is never confused with
        this one (fix sketch item 3)."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects" / "proj"
            root.mkdir(parents=True)
            shutil.copy(FIXTURES / "sample.jsonl", root / "s1.jsonl")
            manifest = Path(tmp) / "freeze.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": "toolbench-freeze-1",
                        "fingerprint": "x",
                        "count": 1,
                        "refs": [
                            {
                                "agent": "claude-code",
                                "source": "raw",
                                "project": "proj",
                                "session_id": "s1",
                                "path": str(root / "s1.jsonl"),
                                "is_subagent": False,
                            }
                        ],
                    }
                )
            )
            argv = ["--index-source", "raw", "--all", "--freeze", str(manifest)]
            out = io.StringIO()
            with redirect_stdout(out):
                main(argv, root=str(Path(tmp) / "projects"))

            report = out.getvalue()
            self.assertIn("Sampling fractions unavailable", report)
            self.assertIn("toolbench-freeze-1", report)
            self.assertNotIn("Historical denominator", report)

    def test_v2_manifest_with_no_census_degrades_same_as_v1(self) -> None:
        """A v2-format manifest can still be written without a census (e.g. the freeze
        run's own discovery census failed) -- `read_manifest` treats KEY ABSENCE the
        same regardless of the version string stamped on the file."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects" / "proj"
            root.mkdir(parents=True)
            shutil.copy(FIXTURES / "sample.jsonl", root / "s1.jsonl")
            manifest = str(Path(tmp) / "freeze.json")
            argv = ["--index-source", "raw", "--all", "--freeze", manifest]
            main(argv, root=str(Path(tmp) / "projects"))  # real v2 manifest + census

            # Rewrite with the SAME refs, no census -- simulates a v2 freeze whose own
            # census attempt failed at freeze time.
            m = read_manifest(manifest)
            write_manifest(manifest, m.refs, m.fingerprint, census=None)

            out = io.StringIO()
            with redirect_stdout(out):
                main(argv, root=str(Path(tmp) / "projects"))

            report = out.getvalue()
            self.assertIn("Sampling fractions unavailable", report)
            self.assertIn(MANIFEST_VERSION, report)

    def test_v2_replay_propagates_a_freeze_time_census_failure(self) -> None:
        """The census can itself be UNAVAILABLE at freeze time (e.g. discovery's own
        census call errored). TB-37 persists and propagates that reason on replay
        rather than laundering it into the generic "no denominator" text -- a
        measurement that was ATTEMPTED AND FAILED is not the same fact as one that was
        never attempted."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects" / "proj"
            root.mkdir(parents=True)
            shutil.copy(FIXTURES / "sample.jsonl", root / "s1.jsonl")
            manifest = str(Path(tmp) / "freeze.json")
            argv = ["--index-source", "raw", "--all", "--freeze", manifest]
            main(argv, root=str(Path(tmp) / "projects"))

            m = read_manifest(manifest)
            failed_census = AgentCensus(
                totals={}, archive_total=0, unavailable_reason="boom: census call failed"
            )
            write_manifest(manifest, m.refs, m.fingerprint, census=failed_census)

            out = io.StringIO()
            with redirect_stdout(out):
                main(argv, root=str(Path(tmp) / "projects"))

            report = out.getvalue()
            self.assertIn("Sampling fractions unavailable", report)
            self.assertIn("boom: census call failed", report)


class SubagentExclusionAcrossIndexSourcesTests(unittest.TestCase):
    """TB-31: `--exclude-subagents` must move the corpus fingerprint on BOTH index
    sources, and the provenance line must report what was actually filtered.

    It used to be a silent no-op on the AgentsView path: the listing never asked for
    child sessions, so no ref was ever stamped `is_subagent` and the filter had nothing
    to remove -- while the report still flipped to "Subagents included: no". Identical
    fingerprints with the flag on and off, and an unearned claim beside them.
    """

    _PARENT = {"id": "good-1", "project": "p", "agent": "claude"}
    _CHILD = {"id": "agent-child-1", "project": "p", "agent": "claude"}

    def _listing(self, *sessions: dict[str, str]) -> str:
        return json.dumps({"sessions": list(sessions), "next_cursor": "", "total": len(sessions)})

    def _run(self, argv: list[str], exports: int) -> str:
        transcript = (FIXTURES / "sample.jsonl").read_text()
        runner = FakeRunner(
            [
                completed(stdout=self._listing(self._PARENT)),                 # parent probe
                # per-agent census (--limit 1, one agent seen: "claude") + the
                # run-scoped archive total (TB-33).
                completed(stdout=self._listing(self._PARENT)),
                completed(stdout=self._listing(self._PARENT)),
                completed(stdout=self._listing(self._PARENT, self._CHILD)),    # full listing
                *[completed(stdout=transcript) for _ in range(exports)],
            ]
        )
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--index-source", "agentsview", *argv], runner=runner), 0)
        return out.getvalue()

    def _line(self, report: str, needle: str) -> str:
        return next(ln for ln in report.splitlines() if needle in ln)

    def test_excluding_subagents_moves_the_fingerprint_on_the_agentsview_path(self) -> None:
        included = self._run([], exports=2)
        excluded = self._run(["--exclude-subagents"], exports=1)
        self.assertNotEqual(
            self._line(included, "Corpus fingerprint:"),
            self._line(excluded, "Corpus fingerprint:"),
        )

    def test_the_child_is_the_session_that_leaves_the_corpus(self) -> None:
        self.assertIn("scanned: 2", self._run([], exports=2))
        self.assertIn("scanned: 1", self._run(["--exclude-subagents"], exports=1))

    def test_provenance_line_reports_what_was_actually_filtered(self) -> None:
        self.assertIn(
            "Subagents included: yes (1 of 2 discovered are subagent sessions)",
            self._run([], exports=2),
        )
        self.assertIn(
            "Subagents included: no (1 of 2 discovered excluded)",
            self._run(["--exclude-subagents"], exports=1),
        )


class LimitTruncationSignalTests(unittest.TestCase):
    """Passing `--limit` is not evidence that it CUT anything (roborev #98/#101, TB-35).

    The report may only name `--limit` as the cause of an uneven spread when the limit
    actually truncated the corpus. The flag's presence cannot establish that: `--limit
    9000` over an 8778-session archive stops nothing. So truncation is OBSERVED at
    discovery -- when the ref loop stops on the limit, we ask the iterator for one more
    ref, and only a ref that exists proves sessions were left behind.
    """

    def _root_with(self, n: int) -> str:
        tmp = Path(tempfile.mkdtemp())
        project = tmp / "proj-a"
        project.mkdir()
        for i in range(n):
            (project / f"sess-{i}.jsonl").write_text("{}\n")
        self.addCleanup(shutil.rmtree, tmp)
        return str(tmp)

    def test_limit_below_corpus_size_observes_truncation(self) -> None:
        args = parse_args(["--index-source", "raw", "--limit", "2"])
        refs, _, _, _, truncated = _discover_refs(args, self._root_with(5), None)

        self.assertEqual(len(refs), 2)
        self.assertTrue(truncated)

    def test_limit_equal_to_corpus_size_truncates_nothing(self) -> None:
        # THE case the flag alone cannot see, and the reason this signal exists. The loop
        # DOES break on the limit -- `len(refs) >= args.limit` is true -- but there was no
        # sixth session to pull. Nothing was cut, so nothing may be blamed on the limit.
        args = parse_args(["--index-source", "raw", "--limit", "5"])
        refs, _, _, _, truncated = _discover_refs(args, self._root_with(5), None)

        self.assertEqual(len(refs), 5)
        self.assertFalse(truncated)

    def test_no_limit_never_truncates(self) -> None:
        args = parse_args(["--index-source", "raw"])
        _, _, _, _, truncated = _discover_refs(args, self._root_with(5), None)

        self.assertFalse(truncated)


class LimitTruncationProbePopulationTests(unittest.TestCase):
    """The probe must ask about the population the REPORT counts (roborev #103).

    The refs listing always yields children -- they have to be discovered before
    `filter_subagents` can drop them (sources.py, `_ALL_INCLUDES`). So under
    `--exclude-subagents` the ref sitting just past the limit may be a child the report
    never counts, and taking that as truncation would blame `--limit` for a gap in a
    population it did not cut. The probe therefore skips exactly what the report skips.
    """

    def _root(self) -> str:
        """3 parents, then 2 subagents -- in `sorted(rglob(...))` order."""
        tmp = Path(tempfile.mkdtemp())
        project = tmp / "proj-a"
        project.mkdir()
        for i in range(3):
            (project / f"sess-{i}.jsonl").write_text("{}\n")
        # <project>/<session>/subagents/<agent>.jsonl -- the real layout (TB-29). "zzz-"
        # keeps these last in the sorted listing, so they are what a limit of 3 leaves behind.
        subagents = project / "zzz-session" / "subagents"
        subagents.mkdir(parents=True)
        for i in range(2):
            (subagents / f"agent-{i}.jsonl").write_text("{}\n")
        self.addCleanup(shutil.rmtree, tmp)
        return str(tmp)

    def test_only_excluded_subagents_beyond_the_limit_is_not_truncation(self) -> None:
        # THE case: the limit stops the listing with 2 refs still behind it, but both are
        # children `--exclude-subagents` drops. Nothing the report counts was left behind,
        # so the limit cut nothing FROM THE REPORTED POPULATION and may not be named.
        args = parse_args(["--index-source", "raw", "--exclude-subagents", "--limit", "3"])
        refs, _, _, _, truncated = _discover_refs(args, self._root(), None)

        self.assertEqual(len(refs), 3)
        self.assertFalse(truncated)

    def test_a_parent_beyond_the_limit_is_truncation(self) -> None:
        # The control on the test above: the probe must still SEE a left-behind parent
        # rather than becoming an unconditional False.
        args = parse_args(["--index-source", "raw", "--exclude-subagents", "--limit", "2"])
        refs, _, _, _, truncated = _discover_refs(args, self._root(), None)

        self.assertEqual(len(refs), 2)
        self.assertTrue(truncated)

    def test_subagents_beyond_the_limit_count_when_they_are_included(self) -> None:
        # Same corpus, same limit, no `--exclude-subagents`: now the children ARE the
        # reported population, so leaving them behind IS truncation. The probe tracks the
        # flag because the population does.
        args = parse_args(["--index-source", "raw", "--limit", "3"])
        _, _, _, _, truncated = _discover_refs(args, self._root(), None)

        self.assertTrue(truncated)


class LimitTruncationProbeFailureTests(unittest.TestCase):
    """A probe that cannot answer must say so -- not answer wrong (roborev #103).

    The probe runs AFTER this run already holds every ref it asked for, and it may cost a
    fresh page. If that page fails, the run is still complete: the failure may not crash
    it, may not fabricate a skip, and may not zero the census. Nor may it return `False`,
    which the report renders as "`--limit N` truncated nothing" -- a measurement nobody
    took. Unobserved is `None`.
    """

    def _script(self, failure: object, *, auto: bool = False) -> FakeRunner:
        sessions = [
            {"id": "s-0", "project": "p", "agent": "claude"},
            {"id": "s-1", "project": "p", "agent": "claude"},
        ]
        probe = {"sessions": sessions, "next_cursor": "", "total": 2}
        census = {"sessions": [], "next_cursor": "", "total": 4}
        # Page 1 fills the limit exactly and advertises a page 2 -- so the probe's `next()`
        # must go back to the runner, which is where the failure lands.
        page_1 = {"sessions": sessions, "next_cursor": "page-2", "total": 4}
        responses: list[object] = [
            completed(stdout=json.dumps(probe)),  # parent probe (TB-31)
            completed(stdout=json.dumps(census)),  # per-agent census (TB-33)
            completed(stdout=json.dumps(census)),  # run-scoped archive total
            completed(stdout=json.dumps(page_1)),  # the listing this run consumed
            failure,  # the probe's extra page
        ]
        if auto:  # `auto` asks whether agentsview is there at all before using it
            responses.insert(
                0,
                completed(stdout=json.dumps({"sessions": [], "next_cursor": "", "total": 0})),
            )
        return FakeRunner(responses)  # type: ignore[arg-type]

    def test_a_failed_page_leaves_truncation_unobserved(self) -> None:
        # rc != 0 -> RuntimeError out of `_agentsview_pages`, which `_discover_refs` never
        # caught: a complete run died on its own diagnostic.
        args = parse_args(["--index-source", "agentsview", "--limit", "2"])
        runner = self._script(completed(returncode=1, stderr="daemon down"))
        refs, _, skips, census, truncated = _discover_refs(args, "/nonexistent", runner)

        self.assertIsNone(truncated)
        self.assertEqual([r.session_id for r in refs], ["s-0", "s-1"])
        self.assertEqual(skips, [])
        self.assertEqual(census.totals, {"claude": 4})

    def test_a_vanished_source_at_the_probe_does_not_fabricate_a_skip(self) -> None:
        # The insidious one. `FileNotFoundError` IS caught under `auto` -- by the guard for
        # a source that vanishes DURING discovery -- so a run already holding every ref it
        # asked for got rewritten into a MISSING_SOURCE skip with an empty census. The refs
        # were never in doubt; only the probe failed.
        args = parse_args(["--index-source", "auto", "--limit", "2"])
        runner = self._script(FileNotFoundError("agentsview vanished"), auto=True)
        refs, _, skips, census, truncated = _discover_refs(args, "/nonexistent", runner)

        self.assertIsNone(truncated)
        self.assertEqual([r.session_id for r in refs], ["s-0", "s-1"])
        self.assertEqual(skips, [])
        self.assertEqual(census.totals, {"claude": 4})
        self.assertIsNone(census.unavailable_reason)

    def test_a_mid_listing_failure_before_the_limit_still_raises(self) -> None:
        # The guard is scoped to `auto`; an EXPLICIT `--index-source agentsview` is a
        # demand, not a preference (TB-38 keeps this untouched -- see
        # `MidListingAutoFallbackTests.test_explicit_agentsview_mode_still_raises`
        # below for the direct pin). A listing that dies while refs are still being
        # collected under an explicit `agentsview` request leaves an INCOMPLETE
        # sample with nowhere sanctioned to fall back to, and that is a real
        # discovery failure -- it must keep raising rather than be downgraded to a
        # shrug about truncation.
        args = parse_args(["--index-source", "agentsview", "--limit", "9"])
        runner = self._script(completed(returncode=1, stderr="daemon down"))

        with self.assertRaises(RuntimeError):
            _discover_refs(args, "/nonexistent", runner)


class MidListingAutoFallbackTests(unittest.TestCase):
    """TB-38: `auto` must degrade to raw not only when the S10 probe itself fails, but
    when AgentsView answers that probe and then breaks somewhere in the pagination that
    follows it -- inside `discover_agentsview`'s eager parent-probe pass (TB-31), which
    runs as part of the `iter_sessions(...)` call in `_discover_refs`, or lazily while
    the full listing (`_yield_refs`) is drained by the `for ref in refs_iter` loop.

    Before this ticket, both a nonzero exit and an `AgentsViewTimeout` (TB-32) escaped
    uncaught into `main`'s `except (FileNotFoundError, RuntimeError)` guard -- a fatal
    exit 1, even though the raw filesystem was right there and `--index-source auto`
    promises exactly this degrade (S10). `tests/test_sources.py`'s
    `test_mid_discovery_timeout_is_fatal_like_any_other_source_error` pinned that as a
    known, deliberately out-of-scope gap for TB-32 and named this ticket as the one that
    would close it; it has been rewritten to match.
    """

    def _root_with_one_session(self) -> str:
        tmp = Path(tempfile.mkdtemp())
        project = tmp / "proj-a"
        project.mkdir()
        (project / "raw-sess.jsonl").write_text("{}\n")
        self.addCleanup(shutil.rmtree, tmp)
        return str(tmp)

    def _probe_ok(self) -> subprocess.CompletedProcess[str]:
        # `_probe_agentsview`'s `--limit 1` health check (S10): answered, healthy.
        return completed(stdout=json.dumps({"sessions": [], "next_cursor": "", "total": 0}))

    def test_nonzero_exit_mid_listing_falls_back_to_raw(self) -> None:
        """The pre-existing failure mode (unrelated to TB-32's timeout work): a daemon
        that answers the probe and then exits nonzero during the parent-probe pass."""
        args = parse_args(["--index-source", "auto"])
        runner = FakeRunner([self._probe_ok(), completed(returncode=1, stderr="daemon down")])

        refs, fallback_reason, skips, census, truncated = _discover_refs(
            args, self._root_with_one_session(), runner
        )

        self.assertEqual([r.source for r in refs], ["raw"])
        assert fallback_reason is not None
        self.assertIn("mid-listing", fallback_reason)
        self.assertIn("daemon down", fallback_reason)
        self.assertEqual(len(skips), 1)
        self.assertIs(skips[0].reason, SkipReason.EXPORT_FAILED)
        self.assertEqual(census.totals, {"claude-code": 1})
        self.assertIsNone(census.unavailable_reason)
        # No `--limit` was passed, so the raw rescan's own `_collect_refs` observes
        # nothing to truncate -- an earned `False`, not the `None` a failed truncation
        # PROBE would leave (roborev #103; that is a different signal entirely).
        self.assertFalse(truncated)

    def test_timeout_mid_listing_falls_back_to_raw(self) -> None:
        """TB-32's failure mode, now given the same recovery as a nonzero exit rather
        than the fatal exit it shared with it before this ticket."""
        args = parse_args(["--index-source", "auto"])
        runner = FakeRunner(
            [self._probe_ok(), AgentsViewTimeout("agentsview timed out after 60.0s")]
        )

        refs, fallback_reason, skips, census, truncated = _discover_refs(
            args, self._root_with_one_session(), runner
        )

        self.assertEqual([r.source for r in refs], ["raw"])
        assert fallback_reason is not None
        self.assertIn("mid-listing", fallback_reason)
        self.assertIn("timed out", fallback_reason)
        self.assertEqual(len(skips), 1)
        self.assertIs(skips[0].reason, SkipReason.EXPORT_TIMEOUT)
        self.assertEqual(census.totals, {"claude-code": 1})
        self.assertIsNone(census.unavailable_reason)
        self.assertFalse(truncated)

    def test_malformed_json_mid_listing_falls_back_to_raw(self) -> None:
        """A zero-exit response can still be unusable; auto mode must recover from
        malformed listing JSON just as it does from nonzero exits and timeouts."""
        args = parse_args(["--index-source", "auto"])
        runner = FakeRunner([self._probe_ok(), completed(stdout="not-json")])

        refs, fallback_reason, skips, census, truncated = _discover_refs(
            args, self._root_with_one_session(), runner
        )

        self.assertEqual([r.source for r in refs], ["raw"])
        assert fallback_reason is not None
        self.assertIn("mid-listing", fallback_reason)
        self.assertIn("Expecting value", fallback_reason)
        self.assertEqual(len(skips), 1)
        self.assertIs(skips[0].reason, SkipReason.EXPORT_FAILED)
        self.assertEqual(census.totals, {"claude-code": 1})
        self.assertIsNone(census.unavailable_reason)
        self.assertFalse(truncated)

    def test_malformed_parent_probe_row_falls_back_to_raw(self) -> None:
        args = parse_args(["--index-source", "auto"])
        malformed = json.dumps({
            "sessions": [{"project": "p", "agent": "claude"}],
            "next_cursor": "",
            "total": 1,
        })
        runner = FakeRunner([self._probe_ok(), completed(stdout=malformed)])

        refs, fallback_reason, skips, census, truncated = _discover_refs(
            args, self._root_with_one_session(), runner
        )

        self.assertEqual([r.source for r in refs], ["raw"])
        assert fallback_reason is not None
        self.assertIn("id", fallback_reason)
        self.assertEqual(len(skips), 1)
        self.assertIs(skips[0].reason, SkipReason.EXPORT_FAILED)
        self.assertEqual(census.totals, {"claude-code": 1})
        self.assertFalse(truncated)

    def test_malformed_lazy_full_listing_row_falls_back_to_raw(self) -> None:
        args = parse_args(["--index-source", "auto"])
        malformed = json.dumps({
            "sessions": [{"id": "s1", "agent": "claude"}],
            "next_cursor": "",
            "total": 1,
        })
        runner = FakeRunner([
            self._probe_ok(),
            completed(stdout=json.dumps({"sessions": [], "next_cursor": "", "total": 0})),
            completed(stdout=json.dumps({"sessions": [], "next_cursor": "", "total": 0})),
            completed(stdout=malformed),
        ])

        refs, fallback_reason, skips, census, truncated = _discover_refs(
            args, self._root_with_one_session(), runner
        )

        self.assertEqual([r.source for r in refs], ["raw"])
        assert fallback_reason is not None
        self.assertIn("project", fallback_reason)
        self.assertEqual(len(skips), 1)
        self.assertIs(skips[0].reason, SkipReason.EXPORT_FAILED)
        self.assertEqual(census.totals, {"claude-code": 1})
        self.assertFalse(truncated)

    def test_explicit_agentsview_mode_still_raises(self) -> None:
        """Scope check: the widened source-failure guard is gated on `auto`. An
        explicit `--index-source agentsview` demand must still surface the failure
        raw, unchanged from TB-32 (`test_explicit_agentsview_does_not_swallow_a_timeout`,
        tests/test_sources.py)."""
        args = parse_args(["--index-source", "agentsview"])
        runner = FakeRunner([AgentsViewTimeout("agentsview timed out after 60.0s")])

        with self.assertRaises(AgentsViewTimeout):
            _discover_refs(args, "/nonexistent", runner)

    def test_explicit_agentsview_malformed_listing_reports_fatal_error(self) -> None:
        runner = FakeRunner([completed(stdout="not-json")])
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            result = main(["--index-source", "agentsview"], runner=runner)

        self.assertEqual(result, 1)
        self.assertIn("fatal source error", stderr.getvalue())
        self.assertIn("Expecting value", stderr.getvalue())

    def test_explicit_agentsview_malformed_row_reports_fatal_error(self) -> None:
        malformed = json.dumps({
            "sessions": [{"project": "p", "agent": "claude"}],
            "next_cursor": "",
            "total": 1,
        })
        runner = FakeRunner([completed(stdout=malformed)])
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            result = main(["--index-source", "agentsview"], runner=runner)

        self.assertEqual(result, 1)
        self.assertIn("fatal source error", stderr.getvalue())
        self.assertIn("id", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_source_vanishing_after_a_healthy_probe_is_unaffected(self) -> None:
        """`FileNotFoundError` gets its own, separate, deliberately-untouched branch in
        `_discover_refs` (TB-33): the daemon answers `_probe_agentsview`'s `--limit 1`
        health check, then the binary itself disappears during the eager parent-probe
        pass that follows. That degrades to an empty, `unavailable_reason`-carrying
        census -- no raw rescan -- because a vanished binary is not evidence the raw
        root is any healthier. TB-38 widens the sibling `RuntimeError`/
        `AgentsViewTimeout` case to rescan raw; this pins that `FileNotFoundError`
        keeps its original, narrower handling.

        (The S10 probe FAILING outright -- `_probe_agentsview` catching
        `FileNotFoundError` on the very first call -- is a third, even earlier branch:
        it is resolved entirely inside `iter_sessions` via the `reason` string and
        never reaches `_discover_refs`'s `except` at all, so it has no counterpart
        here.)
        """
        args = parse_args(["--index-source", "auto"])
        runner = FakeRunner([self._probe_ok(), FileNotFoundError("agentsview vanished")])

        refs, _fallback_reason, skips, census, _truncated = _discover_refs(
            args, self._root_with_one_session(), runner
        )

        self.assertEqual(refs, [])
        self.assertEqual(len(skips), 1)
        self.assertIs(skips[0].reason, SkipReason.MISSING_SOURCE)
        self.assertIsNotNone(census.unavailable_reason)


# -- TB-39: --agentsview-timeout ------------------------------------------------------
#
# TB-32 bounded every agentsview call at 60s. That constant is a compromise, and a
# compromise is wrong for somebody at both ends: a slow-but-healthy daemon gets killed
# (a corpus truncated by OUR default), and an operator debugging a hang cannot give up
# sooner. The flag makes the bound theirs. `0` re-arms the TB-32 hang deliberately --
# which is why the report discloses it rather than merely permitting it.


class AgentsViewTimeoutFlagTests(unittest.TestCase):
    def test_default_is_the_tb32_constant(self) -> None:
        """Absent the flag, behaviour must be byte-for-byte TB-32's."""
        self.assertEqual(parse_args([]).agentsview_timeout, AGENTSVIEW_TIMEOUT_S)

    def test_flag_parses_as_float(self) -> None:
        self.assertEqual(parse_args(["--agentsview-timeout", "5.5"]).agentsview_timeout, 5.5)

    def test_zero_means_unbounded(self) -> None:
        self.assertEqual(parse_args(["--agentsview-timeout", "0"]).agentsview_timeout, 0.0)

    def test_negative_is_rejected_at_parse(self) -> None:
        """A negative ceiling is not a policy choice, it is nonsense. Reject rather than
        silently coerce -- the precedent is `--tickets` (_positive_int, S39)."""
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                parse_args(["--agentsview-timeout", "-1"])

    def test_flag_value_actually_reaches_subprocess_run(self) -> None:
        """The test that matters: not that the flag PARSES, but that the number arrives
        at the syscall. Asserts the bound value on the real default runner main() builds,
        rather than on a fake that would prove nothing."""
        seen: list[float | None] = []

        def spy(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            timeout = kwargs.get("timeout")
            assert timeout is None or isinstance(timeout, float)
            seen.append(timeout)
            raise FileNotFoundError("agentsview")  # bail out immediately; we only want the kwarg

        with unittest.mock.patch("toolbench.sources.subprocess.run", spy):
            with TemporaryDirectory() as tmp:
                with redirect_stdout(io.StringIO()):
                    main(["--agentsview-timeout", "7.5", "--index-source", "auto"], root=tmp)
        self.assertEqual(seen[0], 7.5)

    def test_zero_passes_timeout_none_to_subprocess_run(self) -> None:
        """`timeout=None` is subprocess.run's native 'block forever'. The escape hatch is
        real, not a very large number."""
        seen: list[float | None] = []

        def spy(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            timeout = kwargs.get("timeout")
            assert timeout is None or isinstance(timeout, float)
            seen.append(timeout)
            raise FileNotFoundError("agentsview")

        with unittest.mock.patch("toolbench.sources.subprocess.run", spy):
            with TemporaryDirectory() as tmp:
                with redirect_stdout(io.StringIO()):
                    main(["--agentsview-timeout", "0", "--index-source", "auto"], root=tmp)
        self.assertIsNone(seen[0])

    def test_an_injected_runner_is_never_wrapped(self) -> None:
        """The flag configures the DEFAULT runner; it must not override an explicit seam.
        Every test in this suite injects a runner, so wrapping one would silently change
        what they exercise."""
        runner = FakeRunner([completed(returncode=1, stderr="daemon down")])
        with TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()):
                main(["--agentsview-timeout", "7.5"], runner=runner, root=tmp)
        # The fake ran (it is single-arg and would raise TypeError if handed a timeout).
        self.assertTrue(runner.calls)
