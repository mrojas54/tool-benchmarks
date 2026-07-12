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
    MissingSourceExport,
    NonTranscriptExport,
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
    _refs, _fallback, skips = _discover_refs(args, "/definitely/not/a/real/root", runner)
    assert skips, "a missing raw root must be recorded as a skip"
    assert all(isinstance(s, SkipRecord) for s in skips)
    assert skips[0].reason is SkipReason.MISSING_SOURCE

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

