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


def _render(reducer: Reducer, census: AgentCensus) -> str:
    return render_report(
        reducer,
        index_source="agentsview",
        fallback_reason=None,
        skips=[],
        include_subagents=True,
        subagents_found=0,
        sessions_discovered=sum(s.sessions for s in reducer.agents.values()),
        since_note=None,
        census=census,
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

    def test_uneven_sampling_line_fires_above_threshold(self) -> None:
        # codex 40/183 = 21.9%; claude 135/8595 = 1.6%. Spread ~13.9x.
        reducer = _reducer_with(claude=135, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)

        out = _render(reducer, census)

        self.assertIn("Sampling is uneven", out)
        self.assertIn("not comparable", out)

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
