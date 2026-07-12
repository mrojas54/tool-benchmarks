import dataclasses
import unittest
from pathlib import Path

import pytest

from tests.fakes import make_call
from toolbench.passive import (
    OVERSIZED_OUTPUT_TOKENS,
    UNKNOWN_MODEL,
    Reducer,
)
from toolbench.reducer import RunStats
from toolbench.run_manifest import RunManifest
from toolbench.transcript import BranchUsage, ParseResult, ToolCall, UsageProvenance

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
        calls = [make_call(name="ToolSearch", output_chars=4000, is_deferral=True)]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.tool_search_calls, 1)
        self.assertEqual(reducer.inefficiency.tool_search_tokens, 1000)

    def test_deferral_tax_counts_flag_not_tool_name(self) -> None:
        """CQ 3.1: reducer is schema-neutral — only the parse-time tag counts."""
        reducer = Reducer()
        calls = [
            make_call(name="ToolSearch", output_chars=4000, is_deferral=False),
            make_call(name="deferred_lookup", output_chars=800, is_deferral=True),
        ]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.tool_search_calls, 1)
        self.assertEqual(reducer.inefficiency.tool_search_tokens, 200)

    def test_oversized_output_counted(self) -> None:
        reducer = Reducer()
        big = OVERSIZED_OUTPUT_TOKENS * 4 + 40
        calls = [make_call(name="Read", output_chars=big), make_call(name="Read", output_chars=40)]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.oversized_outputs, 1)

    def test_subagent_fanout_counted(self) -> None:
        reducer = Reducer()
        calls = [make_call(name="Agent", is_subagent_fanout=True), make_call(name="Read")]
        reducer.absorb("claude-code", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.subagent_fanout, 1)

    def test_subagent_fanout_counts_flag_not_tool_name(self) -> None:
        """CQ 3.1: Agent/Task/spawn_agent policy lives at parse time, not absorb."""
        reducer = Reducer()
        calls = [
            make_call(name="Agent", is_subagent_fanout=False),
            make_call(name="spawn_agent", is_subagent_fanout=False),
            make_call(name="custom_fanout", is_subagent_fanout=True),
        ]
        reducer.absorb("codex", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.subagent_fanout, 1)
        self.assertEqual(reducer.inefficiency.subagent_by_tool, {"custom_fanout": 1})

    def test_subagent_fanout_counts_codex_spawn_agent(self) -> None:
        """codex is the only agent in the corpus that spawns subagents, and the
        fan-out callout was measured with its data absent (TB-12). `wait_agent`
        awaits an existing subagent and is not a fan-out."""
        reducer = Reducer()
        calls = [
            make_call(name="spawn_agent", is_subagent_fanout=True),
            make_call(name="wait_agent"),
            make_call(name="exec_command"),
        ]
        reducer.absorb("codex", ParseResult(calls=calls, malformed=0))
        self.assertEqual(reducer.inefficiency.subagent_fanout, 1)
        self.assertEqual(reducer.inefficiency.subagent_by_tool, {"spawn_agent": 1})

    def test_reducer_does_not_export_subagent_name_policy(self) -> None:
        """CQ 3.1: agent-specific frozensets must not live on the reducer."""
        import toolbench.reducer as reducer_mod

        self.assertFalse(hasattr(reducer_mod, "SUBAGENT_TOOL_NAMES"))

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


def _manifest(*branches: str, tickets: tuple[str, ...] = ("TB-1", "TB-2")) -> RunManifest:
    return RunManifest(
        run="2", tickets=tickets, branches=frozenset(branches), worktrees=()
    )


def test_run_fold_sums_only_in_set_branches() -> None:
    reducer = Reducer(run=_manifest("feat/tb-18", "tb-19-pytest-gate"))
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={
                "feat/tb-18": BranchUsage(read=300, creation=30, messages=1),
                "tb-19-pytest-gate": BranchUsage(read=100, creation=10, messages=1),
                "main": BranchUsage(read=999, creation=99, messages=1),
            },
        ),
    )
    assert reducer.run_stats.read == 400
    assert reducer.run_stats.creation == 40
    assert reducer.run_stats.unattributed_read == 999
    assert reducer.run_stats.unattributed_creation == 99


def test_straddling_session_does_not_donate_its_whole_total() -> None:
    """S40 COUNTER-TRAP. A session that touches a run branch for ONE entry must
    contribute only that entry's usage -- not its session total. This is precisely
    the over-count the ticket's original 'fold the run's session set' framing would
    have shipped, and 29/158 real sessions straddle, so it is not hypothetical."""
    reducer = Reducer(run=_manifest("feat/tb-21"))
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            # Session total is 10_400 read; only 400 of it was spent on the run.
            usage_by_branch={
                "feat/tb-21": BranchUsage(read=400, creation=40, messages=1),
                "main": BranchUsage(read=10_000, creation=1_000, messages=40),
            },
            session_cache_read_tokens=10_400,
            session_cache_creation_tokens=1_040,
        ),
    )
    assert reducer.run_stats.read == 400  # NOT 10_400
    assert reducer.run_stats.creation == 40  # NOT 1_040
    assert reducer.run_stats.candidate_sessions == 1


def test_non_candidate_session_contributes_nothing_not_even_unattributed() -> None:
    """`unattributed` is scoped to CANDIDATE sessions (those touching >=1 run branch).
    A session that never touches the run is simply not part of it -- counting its
    usage as `unattributed` would drown the figure in unrelated main-branch work and
    make it alarming noise on every run."""
    reducer = Reducer(run=_manifest("feat/tb-21"))
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={"main": BranchUsage(read=5_000, creation=500, messages=9)},
        ),
    )
    assert reducer.run_stats.read == 0
    assert reducer.run_stats.unattributed_read == 0
    assert reducer.run_stats.candidate_sessions == 0


def test_missing_branches_are_reported_not_silently_zero() -> None:
    """A manifest branch that matches zero entries is the signature of a typo'd or
    renamed branch. Silent, it reads as 'this ticket cost nothing' (S23/S38)."""
    manifest = _manifest("feat/tb-18", "typo/nonexistent")
    reducer = Reducer(run=manifest)
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={"feat/tb-18": BranchUsage(read=10, creation=1, messages=1)},
        ),
    )
    assert reducer.run_stats.missing_branches(manifest) == ["typo/nonexistent"]


def test_run_fold_is_inert_without_a_manifest() -> None:
    """No --run-manifest -> no run accounting. The existing report is unchanged."""
    reducer = Reducer()
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={"main": BranchUsage(read=5, creation=1, messages=1)},
        ),
    )
    assert reducer.run_stats.read == 0
    assert reducer.run_stats.candidate_sessions == 0


def test_per_ticket_normalizes_and_rejects_zero() -> None:
    stats = RunStats(read=900, creation=90, input=30, output=60)
    assert stats.per_ticket(3)["cache_read"] == 300.0
    assert stats.per_ticket(3)["cache_creation"] == 30.0
    with pytest.raises(ValueError, match="tickets"):
        stats.per_ticket(0)


def test_prefix_sharing_trap_read_drop_offset_by_creation_rise() -> None:
    """S39/S40: read and creation travel together. A 'win' that drops read 500 while
    raising creation 500 moved no tokens -- reading the read delta alone would call
    it a 50% improvement."""
    before = RunStats(read=1_000, creation=100)
    after = RunStats(read=500, creation=600)
    assert after.read < before.read  # looks like a win
    assert after.total_cache == before.total_cache  # it is not



def test_detached_head_session_is_named_not_silently_dropped() -> None:
    """TB-28 REGRESSION. A delegator working in a DETACHED checkout stamps
    gitBranch="HEAD" on every entry. "HEAD" is not a branch name, so it can never
    match a manifest -- the session has no in-set branch, hits the `not in_set`
    early return, and its usage lands in NEITHER the run total NOR `unattributed`.
    The run number comes out low with no warning: the project's signature
    'confidently wrong number'. We cannot attribute it (SPEC S40: neither branch nor
    cwd partitions sessions cleanly), so we NAME it instead (S23/S38)."""
    reducer = Reducer(run=_manifest("feat/tb-21"))
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={"HEAD": BranchUsage(read=7_000, creation=700, messages=12)},
        ),
    )
    # Still not attributed -- guessing an owner would be a fabricated number.
    assert reducer.run_stats.read == 0
    assert reducer.run_stats.candidate_sessions == 0
    # ...but no longer invisible.
    assert reducer.run_stats.detached_sessions == 1
    assert reducer.run_stats.detached_read == 7_000
    assert reducer.run_stats.detached_creation == 700


def test_detached_usage_in_a_candidate_session_is_detached_not_unattributed() -> None:
    """TB-28, second leak. When a candidate session ALSO carries HEAD entries, the
    old fold booked them as `unattributed` -- whose docstring promises "work on
    another branch". HEAD is not another branch; it is the absence of one. Routing it
    to its own bucket keeps `unattributed` meaning exactly what it claims."""
    reducer = Reducer(run=_manifest("feat/tb-21"))
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={
                "feat/tb-21": BranchUsage(read=400, creation=40, messages=1),
                "main": BranchUsage(read=900, creation=90, messages=3),
                "HEAD": BranchUsage(read=50, creation=5, messages=1),
            },
        ),
    )
    assert reducer.run_stats.read == 400
    assert reducer.run_stats.unattributed_read == 900  # `main` only -- NOT 950
    assert reducer.run_stats.unattributed_creation == 90
    assert reducer.run_stats.detached_read == 50
    assert reducer.run_stats.detached_creation == 5
    assert reducer.run_stats.detached_sessions == 1


def test_zero_usage_detached_entries_do_not_raise_a_false_alarm() -> None:
    """A HEAD session that spent nothing is not a blind spot -- reporting it would
    train the operator to ignore the line that matters."""
    reducer = Reducer(run=_manifest("feat/tb-21"))
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={"HEAD": BranchUsage(read=0, creation=0, messages=2)},
        ),
    )
    assert reducer.run_stats.detached_sessions == 0
    assert reducer.run_stats.detached_read == 0
