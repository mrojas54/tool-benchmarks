import unittest
from pathlib import Path


from tests.fakes import make_call
from toolbench.passive import (
    CorpusFingerprint,
    Reducer,
    corpus_fingerprint,
    render_report,
    session_signature,
)
from toolbench.reducer import AgentStats
from toolbench.run_manifest import RunManifest
from toolbench.sources import (
    AgentCensus,
    SkipReason,
    SkipRecord,
)
from toolbench.transcript import BranchUsage, ParseResult, ToolCall, UsageProvenance

FIXTURES = Path(__file__).parent / "fixtures"


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
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
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
            subagents_found=0,
            sessions_discovered=0,
            since_note="2026-07-01",
            census=AgentCensus(totals={}, archive_total=0),
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
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
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
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
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
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
        )
        section = report[report.index("## Model Breakdown") : report.index("## Inefficiency Callouts")]
        self.assertIn("| claude-code | claude-opus-4-8 | Read | 1 | 100 |", section)
        self.assertIn("| claude-code | claude-haiku-4-5 | Read | 1 | 2000 |", section)
        # Ranked by context tokens descending: haiku (2000) outranks opus (100).
        self.assertLess(section.index("claude-haiku-4-5"), section.index("claude-opus-4-8"))

    def test_run_with_zero_matching_sessions_reports_clearly(self) -> None:
        """S23: an empty run set is a clear message, not a crash and not a silent
        blank. Every manifest branch is named as matching nothing, which is the
        signature of a manifest pointed at the wrong corpus."""
        manifest = RunManifest(
            run="9", tickets=("TB-1",), branches=frozenset({"never/existed"}), worktrees=()
        )
        reducer = Reducer(run=manifest)
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[],
                malformed=0,
                usage_by_branch={"main": BranchUsage(read=10, creation=1, messages=1)},
            ),
        )
        report = render_report(
            reducer,
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
        )
        self.assertIn("0 candidate sessions", report)
        self.assertIn("matched no entries: never/existed", report)

    def test_run_section_absent_without_a_manifest(self) -> None:
        """No --run-manifest -> the report is exactly what it is today."""
        report = render_report(
            Reducer(),
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
        )
        self.assertNotIn("Run cache tokens", report)

    def test_run_section_prints_read_and_creation_together(self) -> None:
        """S39/S40: never read alone -- a prefix-sharing change trades one for the other."""
        manifest = RunManifest(
            run="2", tickets=("TB-1", "TB-2"), branches=frozenset({"b1"}), worktrees=()
        )
        reducer = Reducer(run=manifest)
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[],
                malformed=0,
                usage_by_branch={
                    "b1": BranchUsage(read=900, creation=90, messages=2),
                    "main": BranchUsage(read=50, creation=5, messages=1),
                },
            ),
        )
        report = render_report(
            reducer,
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
        )
        self.assertIn("Run cache tokens (run 2): read=900 creation=90", report)
        self.assertIn("unattributed: read=50 creation=5", report)
        self.assertIn("1 candidate session", report)

    def test_run_section_normalizes_per_ticket(self) -> None:
        manifest = RunManifest(
            run="2", tickets=("TB-1", "TB-2"), branches=frozenset({"b1"}), worktrees=()
        )
        reducer = Reducer(run=manifest)
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[],
                malformed=0,
                usage_by_branch={"b1": BranchUsage(read=900, creation=90, messages=2)},
            ),
        )
        report = render_report(
            reducer,
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
        )
        self.assertIn("per ticket (2): read=450.0 creation=45.0", report)

    def test_run_section_names_zero_match_branches(self) -> None:
        """A branch matching nothing must be named -- silent, it reads as a free ticket."""
        manifest = RunManifest(
            run="2",
            tickets=("TB-1",),
            branches=frozenset({"b1", "typo/never-pushed"}),
            worktrees=(),
        )
        reducer = Reducer(run=manifest)
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[],
                malformed=0,
                usage_by_branch={"b1": BranchUsage(read=10, creation=1, messages=1)},
            ),
        )
        report = render_report(
            reducer,
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
        )
        self.assertIn("matched no entries: typo/never-pushed", report)


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
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
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
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
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
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
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

    def test_summary_renders_read_and_creation_token_totals(self) -> None:
        """S39 / CQ 1.2: Summary caveat surfaces read + creation together."""
        reducer = Reducer()
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[make_call()],
                malformed=0,
                session_cache_read_tokens=300,
                session_cache_creation_tokens=30,
            ),
        )
        report = render_report(
            reducer,
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
        )
        summary = report[report.index("## Summary") :]
        self.assertIn("Session-grain cache tokens: read=300 creation=30", summary)
        self.assertIn("S39 caveat, not ranked", summary)

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
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
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
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
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
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
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
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
            fingerprint=fingerprint,
        )
        return report[report.index("## Summary") :]

    def test_summary_carries_fingerprint_line(self) -> None:
        fp = corpus_fingerprint(["s1", "s2", "s3"])
        summary = self._summary(fp)
        self.assertIn(f"Corpus fingerprint: {fp.digest} (3 sessions scanned)", summary)

    def test_no_fingerprint_line_when_absent(self) -> None:
        self.assertNotIn("Corpus fingerprint:", self._summary(None))


    def test_run_section_names_the_detached_head_blind_spot(self) -> None:
        """TB-28: a detached-HEAD delegator is invisible to branch attribution. The
        run total silently omits it, so the Summary must SAY the total may be low --
        a benchmark that lies is worse than one that fails."""
        manifest = RunManifest(
            run="2", tickets=("TB-1",), branches=frozenset({"b1"}), worktrees=()
        )
        reducer = Reducer(run=manifest)
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[],
                malformed=0,
                usage_by_branch={"b1": BranchUsage(read=900, creation=90, messages=2)},
            ),
        )
        # A second session, entirely detached: contributes to no branch bucket.
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[],
                malformed=0,
                usage_by_branch={
                    "HEAD": BranchUsage(read=7_000, creation=700, messages=9)
                },
            ),
        )
        report = render_report(
            reducer,
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
        )
        # The run total stays honest -- detached usage is NOT folded in.
        self.assertIn("Run cache tokens (run 2): read=900 creation=90", report)
        # ...and the gap is named rather than swallowed.
        self.assertIn("detached-HEAD (unattributable): read=7000 creation=700", report)
        self.assertIn("run total may be low", report)

    def test_no_detached_line_when_there_is_no_blind_spot(self) -> None:
        """The clean case must stay clean: a caveat that always prints is ignored."""
        manifest = RunManifest(
            run="2", tickets=("TB-1",), branches=frozenset({"b1"}), worktrees=()
        )
        reducer = Reducer(run=manifest)
        reducer.absorb(
            "claude-code",
            ParseResult(
                calls=[],
                malformed=0,
                usage_by_branch={"b1": BranchUsage(read=900, creation=90, messages=2)},
            ),
        )
        report = render_report(
            reducer,
            index_source="raw",
            fallback_reason=None,
            skips=[],
            include_subagents=True,
            subagents_found=0,
            sessions_discovered=0,
            since_note=None,
            census=AgentCensus(totals={}, archive_total=0),
        )
        self.assertNotIn("detached-HEAD", report)


def _reducer_with(**sessions_by_agent: int) -> Reducer:
    r = Reducer()
    for agent, n in sessions_by_agent.items():
        r.agents[agent] = AgentStats(sessions=n, calls=n * 10)
    r.calls_joined = sum(n * 10 for n in sessions_by_agent.values())
    return r


def _render(
    reducer: Reducer,
    census: AgentCensus,
    *,
    skips: list[SkipRecord] | None = None,
    limit: int | None = None,
    truncated: bool = False,
    sampled: dict[str, int] | None = None,
) -> str:
    # `skips` and `limit` default to the two "no signal" values on purpose: an unadorned
    # `_render` is an `--all` run that lost nothing, which is precisely the case the
    # uneven-sampling line used to misreport as `--limit` truncation (TB-33 Finding 4).
    # `sampled` defaults to None -- the typed ABSENCE of a per-agent ref count, which
    # suppresses apportionment rather than fabricating one (TB-35).
    skips = skips or []
    return render_report(
        reducer,
        index_source="agentsview",
        fallback_reason=None,
        skips=skips,
        include_subagents=True,
        subagents_found=0,
        sessions_discovered=sum(s.sessions for s in reducer.agents.values()) + len(skips),
        since_note=None,
        census=census,
        limit=limit,
        limit_truncated=truncated,
        sampled_by_agent=sampled,
    )


class SamplingDisclosureTests(unittest.TestCase):
    """The Agent Breakdown must never render an incomparable table in silence (TB-33)."""

    def test_unreached_agent_gets_a_named_row(self) -> None:
        # cursor is in the archive with 73 sessions and was never scanned. A four-agent
        # table that simply omits it is the bug.
        reducer = _reducer_with(claude=135)
        census = AgentCensus(totals={"claude": 8595, "cursor": 73}, archive_total=8668)

        out = _render(reducer, census)

        self.assertIn("| cursor |", out)
        self.assertIn("0 of 73", out)
        self.assertIn("135 of 8595", out)
        # Absence is STATED, never inferred from a zero.
        self.assertIn("not reached", out.lower())

    def test_agent_whose_every_sampled_session_was_skipped_is_not_called_unreached(self) -> None:
        # TB-33 Finding 2. `sessions == 0` is arrived at by TWO different stories, and the
        # old line told only one of them: cursor was never looked at, while gemini WAS
        # looked at and every session opened failed to parse. Both land at zero sessions.
        # Filing gemini under "not reached ... because we did not look" is flatly false --
        # we looked, and everything we opened broke, which is a parser bug the reader
        # needs to see, not an absence to shrug at. `SkipRecord.agent` tells them apart.
        reducer = _reducer_with(claude=135)
        census = AgentCensus(
            totals={"claude": 8595, "cursor": 73, "gemini": 12}, archive_total=8680
        )
        skips = [
            SkipRecord(
                session_id=f"gemini-{i}",
                agent="gemini",
                reason=SkipReason.NON_TRANSCRIPT,
                detail="binary payload",
            )
            for i in range(4)
        ]

        out = _render(reducer, census, skips=skips)

        # cursor: genuinely never sampled. The original line, still correct, still fires.
        self.assertIn("not reached by this window: cursor (73 sessions)", out)
        # gemini: sampled, and every one died in the parser. Must NOT be filed under
        # "we did not look" -- and must say what actually happened instead.
        self.assertIn("Reached, but every session sampled from them was skipped", out)
        self.assertIn("gemini (4 skipped of 12 in archive)", out)
        self.assertNotIn("gemini (12 sessions)", out)
        # The tally this line points at really is on the page (`skips` is non-empty).
        self.assertIn("Skipped by reason", out)

    def test_uneven_sampling_with_limit_and_no_skips_names_limit(self) -> None:
        # codex 40/183 = 21.9%; claude 135/8595 = 1.6%. Spread ~13.9x. A `--limit` WAS
        # passed and nothing was skipped, so both halves of the claim are observed: the
        # empty skip list rules attrition out, the flag itself confirms truncation, and
        # only here is "re-run without --limit" an honest remedy (TB-33 Finding 4).
        reducer = _reducer_with(claude=135, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)

        out = _render(reducer, census, limit=175, truncated=True)

        self.assertIn("Sampling is uneven", out)
        self.assertIn("not comparable", out)
        self.assertIn("rules out skip attrition", out)
        # "cut the listing short", not "was applied": the flag being passed is not the
        # signal -- discovery watching it drop a ref is (roborev #98/#101).
        self.assertIn("`--limit 175` cut the listing short", out)
        self.assertNotIn("`--limit 175` was applied", out)
        self.assertIn("Re-run without `--limit` for a like-for-like table", out)
        # The dangling pointer: nothing in this report renders a "Skipped by reason"
        # tally when skips is empty, so the line must not send the reader looking for
        # one -- and the section itself must be verifiably absent, not just unnamed.
        self.assertNotIn("Skipped by reason", out)

    def test_uneven_sampling_with_no_limit_and_no_skips_blames_neither_cause(self) -> None:
        # THE REGRESSION (TB-33 Finding 4). Same spread, but this is an `--all` run: no
        # `--limit`, no skips. The old code reached its zero-skip branch, concluded
        # "therefore --limit", and told the reader to re-run without a flag they never
        # passed -- a false cause AND a false remedy, which is the exact species of
        # unearned claim TB-33 exists to delete. With neither signal firing, the line
        # must name neither cause and must not prescribe dropping a flag.
        reducer = _reducer_with(claude=135, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)

        out = _render(reducer, census, limit=None)

        self.assertIn("Sampling is uneven", out)
        self.assertIn("not comparable", out)
        self.assertIn("Neither of the causes this report can name explains it", out)
        self.assertIn("no `--limit` was applied and no sessions were skipped", out)
        self.assertIn("the unevenness is real", out)
        # No false remedy, and no pointer at a tally this report does not render.
        self.assertNotIn("Re-run without `--limit` for a like-for-like table", out)
        self.assertNotIn("Skipped by reason", out)

    def test_uneven_sampling_with_both_limit_and_skips_names_both(self) -> None:
        # Both signals fire, so both causes are named -- and dropping the limit is NOT
        # promised as a fix, because the attrition half would survive it.
        reducer = _reducer_with(claude=135, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)
        skips = [
            SkipRecord(
                session_id=f"codex-skip-{i}",
                agent="codex",
                reason=SkipReason.UNKNOWN_SCHEMA,
                detail="no parser claimed it",
            )
            for i in range(2)
        ]

        out = _render(reducer, census, skips=skips, limit=175, truncated=True)

        self.assertIn("Both causes are live", out)
        self.assertIn("`--limit 175` cut the listing short", out)
        self.assertIn("2 sessions were skipped this run", out)
        self.assertIn("not guaranteed", out)
        # The tally is on the page in this branch, so pointing at it resolves.
        self.assertIn("Skipped by reason", out)

    def test_apportionment_splits_each_agents_loss_between_truncation_and_attrition(self) -> None:
        # TB-35. Naming both causes is not the same as apportioning between them. With
        # both live, the reader still cannot act: is codex's 21.9% an artifact of the
        # limit, or did its archive mostly die in the parser? The split is per agent, and
        # every number rests on its OWN observed signal -- `census.totals` for the archive,
        # the post-filter ref count for what we pulled, `SkipRecord` for attrition.
        reducer = _reducer_with(claude=135, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)
        skips = [
            SkipRecord(
                session_id=f"claude-{i}",
                agent="claude",
                reason=SkipReason.UNKNOWN_SCHEMA,
                detail="no parser claimed it",
            )
            for i in range(15)
        ] + [
            SkipRecord(
                session_id=f"codex-{i}",
                agent="codex",
                reason=SkipReason.NON_TRANSCRIPT,
                detail="binary payload",
            )
            for i in range(5)
        ]

        out = _render(
            reducer,
            census,
            skips=skips,
            limit=195,
            truncated=True,
            sampled={"claude": 150, "codex": 45},
        )

        self.assertIn("Apportionment", out)
        # claude: 8595 - 150 pulled = 8445 the limit never reached.
        self.assertIn("claude: 8595 in archive; 150 sampled, so 8445 never pulled", out)
        self.assertIn("of the 150 sampled, 15 lost to attrition (15 unknown_schema)", out)
        self.assertIn("135 reached the table", out)
        # codex: the spread's high row. Truncation dominates its loss too -- 138 of 183.
        self.assertIn("codex: 183 in archive; 45 sampled, so 138 never pulled", out)
        self.assertIn("of the 45 sampled, 5 lost to attrition (5 non_transcript)", out)
        self.assertIn("40 reached the table", out)

    def test_apportionment_attributes_skip_reasons_to_the_right_agent(self) -> None:
        # TB-35's one genuine data requirement: the tally must be keyed by (agent, reason),
        # not reason alone. A reason-keyed tally would report BOTH agents' reasons under
        # each agent, and the remedy differs by reason -- unknown_schema attrition is
        # fixable (write a parser and the spread closes), missing_source attrition is
        # permanent (the transcripts are gone). Mixing them tells the reader nothing.
        reducer = _reducer_with(claude=135, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)
        skips = [
            SkipRecord(
                session_id="claude-0",
                agent="claude",
                reason=SkipReason.DECODE_ERROR,
                detail="invalid utf-8",
            ),
            *[
                SkipRecord(
                    session_id=f"codex-u{i}",
                    agent="codex",
                    reason=SkipReason.UNKNOWN_SCHEMA,
                    detail="no parser claimed it",
                )
                for i in range(3)
            ],
            *[
                SkipRecord(
                    session_id=f"codex-m{i}",
                    agent="codex",
                    reason=SkipReason.MISSING_SOURCE,
                    detail="transcript gone",
                )
                for i in range(2)
            ],
        ]

        out = _render(
            reducer,
            census,
            skips=skips,
            limit=195,
            truncated=True,
            sampled={"claude": 136, "codex": 45},
        )

        # codex's five losses split by reason, ordered by count. Three are recoverable,
        # two are not, and the reader can only tell because the key carries both fields.
        self.assertIn("5 lost to attrition (3 unknown_schema, 2 missing_source)", out)
        # claude's sole loss is its OWN reason -- codex's reasons must not leak into it.
        self.assertIn("1 lost to attrition (1 decode_error)", out)
        self.assertNotIn("1 lost to attrition (1 unknown_schema)", out)

    def test_truncation_counts_refs_not_reached_plus_skipped(self) -> None:
        # The case that makes the whole design decision load-bearing, and the ONLY input on
        # which the two candidate formulas disagree. claude pulled 150 refs: 100 reached the
        # reducer, 20 were skipped, and 30 parsed fine but yielded zero calls -- so they
        # produced NO reducer session and NO SkipRecord. They fall through both counters.
        #
        #   correct   (total - sampled)           = 8595 - 150           = 8445
        #   forbidden (total - reached - skipped) = 8595 - 100 - 20      = 8475
        #
        # The forbidden form silently bills those 30 real, successfully-parsed sessions to
        # `--limit` truncation -- inventing truncation that never happened. A ref was either
        # pulled or it was not, and that is the only thing we count.
        reducer = _reducer_with(claude=100, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)
        skips = [
            SkipRecord(
                session_id=f"claude-{i}",
                agent="claude",
                reason=SkipReason.UNKNOWN_SCHEMA,
                detail="no parser claimed it",
            )
            for i in range(20)
        ]

        out = _render(
            reducer,
            census,
            skips=skips,
            limit=195,
            truncated=True,
            sampled={"claude": 150, "codex": 45},
        )

        self.assertIn("8445 never pulled", out)
        self.assertNotIn("8475", out)

    def test_apportionment_is_absent_when_per_agent_sampling_was_not_recorded(self) -> None:
        # Typed absence, same habit as `unavailable_reason` and `SkipReason`: a caller that
        # did not record per-agent ref counts gets NO apportionment, never a fabricated one
        # reconstructed from reached+skipped. A ref that parses to zero calls produces
        # neither a reducer session nor a SkipRecord, so that reconstruction would quietly
        # bill real sessions to truncation.
        reducer = _reducer_with(claude=135, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)

        out = _render(reducer, census, limit=195, truncated=True, sampled=None)

        self.assertIn("Sampling is uneven", out)
        self.assertNotIn("Apportionment", out)
        self.assertNotIn("never pulled", out)

    def test_unsampled_remainder_without_a_limit_is_drift_not_truncation(self) -> None:
        # The whole discipline of this function in one case. No `--limit` was passed, so
        # the pages drained to exhaustion and truncation is ruled out BY ITS OWN SIGNAL.
        # A gap between the census and the refs we hold is therefore drift between the two
        # calls -- and calling it truncation would be the very inference-from-absence that
        # 24d9c0f deleted from this function.
        reducer = _reducer_with(claude=135, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)
        skips = [
            SkipRecord(
                session_id="codex-0",
                agent="codex",
                reason=SkipReason.EXPORT_FAILED,
                detail="exit 1",
            )
        ]

        # An `--all` run drains its cursor to exhaustion, so `sampled` matches the census
        # except for whatever moved between the two calls: claude is square, codex is one
        # session short. Real drift is small -- a large remainder here would mean something
        # else is wrong, and it still would not be truncation.
        out = _render(
            reducer, census, skips=skips, limit=None, sampled={"claude": 8595, "codex": 182}
        )

        self.assertIn("no `--limit` was applied", out.lower())
        self.assertIn("codex: 183 in archive; 182 sampled, 1 unaccounted", out)
        self.assertIn("drift", out)
        # Truncation is never named on a run that could not have truncated.
        self.assertNotIn("never pulled", out)

    def test_a_limit_that_truncated_nothing_is_not_named_as_a_cause(self) -> None:
        # roborev #98/#101. The 2x2 dispatch keyed truncation on `limit is not None` -- the
        # presence of the FLAG, not evidence it bit. `--limit 9000` over an 8778-session
        # archive stops nothing, yet the report would still pin the spread on it and tell
        # the reader to re-run without it. Same inference-from-absence 24d9c0f deleted, one
        # level up: there, no-skips implied the limit; here, a-limit-was-passed implies it.
        reducer = _reducer_with(claude=135, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)

        out = _render(reducer, census, limit=9000, truncated=False)

        self.assertIn("Sampling is uneven", out)
        self.assertIn("truncated nothing", out)
        # The remedy is the tell: dropping a flag that cut nothing changes nothing.
        self.assertNotIn("Re-run without `--limit` for a like-for-like table", out)

    def test_apportionment_without_truncation_does_not_blame_the_limit(self) -> None:
        # Same signal, the other renderer. A remainder under a non-biting limit is drift,
        # not truncation, and must not be filed as "never pulled".
        reducer = _reducer_with(claude=135, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)

        out = _render(
            reducer,
            census,
            limit=9000,
            truncated=False,
            sampled={"claude": 8590, "codex": 183},
        )

        self.assertIn("unaccounted", out)
        self.assertIn("`--limit 9000` truncated nothing", out)
        self.assertNotIn("never pulled", out)

    def test_summary_does_not_call_an_all_skipped_agent_unreached(self) -> None:
        # roborev #98/#101. TB-33 Finding 2 removed this false claim from the sampling
        # notes but left it standing in the Summary tail, which still keyed off
        # `scanned == 0` alone. So one report said BOTH "reached, but every session was
        # skipped" (notes) and "not reached by this window" (Summary) about gemini. The
        # contradiction is the bug: a reader who scrolls believes the second one.
        reducer = _reducer_with(claude=135)
        census = AgentCensus(totals={"claude": 8595, "gemini": 12}, archive_total=8607)
        skips = [
            SkipRecord(
                session_id=f"gemini-{i}",
                agent="gemini",
                reason=SkipReason.NON_TRANSCRIPT,
                detail="binary payload",
            )
            for i in range(4)
        ]

        out = _render(reducer, census, skips=skips)

        self.assertIn("gemini: 0 of 12", out)
        self.assertNotIn("gemini: 0 of 12 (0.0%) — not reached by this window", out)
        self.assertIn("all 4 sampled sessions were skipped", out)

    def test_summary_still_calls_a_genuinely_unreached_agent_unreached(self) -> None:
        # The other half: cursor really was never looked at, and the Summary must keep
        # saying so. Suppressing the tail for everyone would trade one false claim for a
        # silence -- the exact failure TB-33 exists to prevent.
        reducer = _reducer_with(claude=135)
        census = AgentCensus(totals={"claude": 8595, "cursor": 73}, archive_total=8668)

        out = _render(reducer, census)

        self.assertIn("cursor: 0 of 73 (0.0%) — not reached by this window", out)

    def test_even_sampling_emits_no_warning_line(self) -> None:
        # Both at ~1.6%: the table IS comparable, so say nothing.
        reducer = _reducer_with(claude=100, codex=10)
        census = AgentCensus(totals={"claude": 6250, "codex": 625}, archive_total=6875)

        out = _render(reducer, census)

        self.assertNotIn("Sampling is uneven", out)

    def test_residual_is_named(self) -> None:
        reducer = _reducer_with(claude=100)
        census = AgentCensus(totals={"claude": 8595}, archive_total=8700)

        out = _render(reducer, census)

        self.assertIn("105", out)
        self.assertIn("belong to no agent", out)

    def test_unavailable_census_says_why_and_renders_unknown(self) -> None:
        reducer = _reducer_with(claude=100)
        census = AgentCensus(
            totals={}, archive_total=0, unavailable_reason="agentsview exited 1: daemon down"
        )

        out = _render(reducer, census)

        self.assertIn("unknown", out)
        self.assertIn("daemon down", out)
        # A dropped column would be the exact sin this ticket closes.
        self.assertIn("| claude |", out)

    def test_summary_lists_every_agents_sampling_fraction(self) -> None:
        reducer = _reducer_with(claude=135)
        census = AgentCensus(totals={"claude": 8595, "cursor": 73}, archive_total=8668)

        out = _render(reducer, census)
        summary = out.split("## Summary")[1]

        self.assertIn("claude: 135 of 8595", summary)
        self.assertIn("cursor: 0 of 73", summary)

    def test_total_zero_with_nonzero_scanned_is_not_a_silent_zero(self) -> None:
        # _agent_census gathers the universe and each agent's total in SEPARATE,
        # non-atomic agentsview invocations: claude can be seen in the probe listing
        # with 5 scanned sessions and then report total=0 from the later scoped call.
        # Printing "0 of 0" over that nonzero `scanned` would tell the reader claude
        # did nothing, when it actually scanned 5 sessions -- the exact silent zero
        # this ticket exists to close.
        reducer = _reducer_with(claude=5)
        census = AgentCensus(totals={"claude": 0}, archive_total=0)

        out = _render(reducer, census)

        self.assertIn("5 of 0", out)
        self.assertNotIn("0 of 0", out)

    def test_uneven_sampling_line_fires_on_skip_attrition_with_no_limit(self) -> None:
        # TB-33 Finding 2: --limit is NOT the only cause. `delta` parsed all 4 of its
        # discovered sessions; `epsilon` discovered 4 but only PARSED 1 -- 3 were
        # skipped (non-transcript exports). No --limit was ever passed: a census
        # whose totals exceed the reducer's `sessions` is exactly what attrition
        # looks like to the renderer, since `stats.sessions` only counts sessions
        # that scanned AND parsed.
        reducer = _reducer_with(delta=4, epsilon=1)
        census = AgentCensus(totals={"delta": 4, "epsilon": 4}, archive_total=8)
        skips = [
            SkipRecord(
                session_id=f"epsilon-skip-{i}",
                agent="epsilon",
                reason=SkipReason.NON_TRANSCRIPT,
                detail="binary payload",
            )
            for i in range(3)
        ]

        out = render_report(
            reducer,
            index_source="agentsview",
            fallback_reason=None,
            skips=skips,
            include_subagents=True,
            subagents_found=0,
            sessions_discovered=sum(s.sessions for s in reducer.agents.values()) + len(skips),
            since_note=None,
            census=census,
        )

        self.assertIn("Sampling is uneven", out)
        self.assertIn("not comparable", out)
        # The old wording asserted --limit was THE cause and prescribed re-running
        # without it as THE remedy -- a no-op here, since no --limit was ever passed.
        # The new line must not repeat that false attribution.
        self.assertNotIn("Re-run without --limit for a like-for-like table", out)
        self.assertNotIn("Re-run without `--limit` for a like-for-like table", out)
        self.assertIn("skip attrition", out.lower())
        # Names the skip count and points at the tally...
        self.assertIn("3 sessions were skipped this run", out)
        self.assertIn('"Skipped by reason" tally below', out)
        # ...and this time the pointer resolves: the tally really is on the page,
        # because `skips` is non-empty (`render_report` only renders it `if skips:`).
        self.assertIn("Skipped by reason", out)
        self.assertIn("non_transcript: 3", out)

    def test_total_none_for_agent_absent_from_census_names_unknown_denominator(self) -> None:
        # cursor was scanned (its sessions are all children, say), so the
        # child-excluded probe listing never saw it: cursor is absent from
        # census.totals entirely -- which is NOT the same thing as a total of zero,
        # and must not be rendered as one. The reconciliation `residual` line names
        # the aggregate discrepancy this causes.
        reducer = _reducer_with(claude=100, cursor=20)
        census = AgentCensus(totals={"claude": 8595}, archive_total=8600)

        out = _render(reducer, census)

        self.assertIn("20 of unknown", out)
        self.assertIn("Reconciliation: 5 archive sessions belong to no agent", out)
