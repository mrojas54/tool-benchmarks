"""Incremental corpus aggregation (S11, S14, S19, S32, TB-24).

Only per-agent / per-tool reducers live here. Each session's `ParseResult.calls`
is folded into counters and discarded — no corpus-wide `list[ToolCall]`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from toolbench.run_manifest import RunManifest
from toolbench.transcript import BranchUsage, ParseResult, UsageProvenance

OVERSIZED_OUTPUT_TOKENS = 5000
UNKNOWN_MODEL = "unknown"
# git stamps a literal "HEAD" as the branch when the checkout is detached, so this is
# a real gitBranch value that is not a branch name -- and no manifest can list it.
DETACHED_BRANCH = "HEAD"


@dataclass
class ToolStats:
    """Aggregate counters for one (agent, tool) pair — S14 tool leaderboard."""

    calls: int = 0
    output_tokens: int = 0
    input_tokens: int = 0
    errors: int = 0
    no_result: int = 0
    cache_hits: int = 0
    usage_missing: int = 0


@dataclass
class AgentStats:
    """Aggregate counters for one agent — S14 agent breakdown."""

    sessions: int = 0
    calls: int = 0
    output_tokens: int = 0
    input_tokens: int = 0
    errors: int = 0
    no_result: int = 0
    sessions_with_cache_data: int = 0  # S32: session-grain, counted once per session
    sessions_with_cache_hit: int = 0
    # S39: summed session-grain cache tokens (caveat only; never ranks).
    cache_read_tokens_total: int = 0
    cache_creation_tokens_total: int = 0


@dataclass
class RunStats:
    """One orchestration run's cache cost (S40) — caveat only, never ranked (S19).

    Attribution is per-ENTRY, by that entry's `gitBranch`. `unattributed` is the
    usage on non-run branches *within candidate sessions* (sessions with >=1 entry
    on a run branch) — the straddle spillover, and nothing else. Scoped corpus-wide
    it would be dominated by unrelated `main` work and read as noise on every run.
    """

    read: int = 0
    creation: int = 0
    input: int = 0
    output: int = 0
    candidate_sessions: int = 0
    unattributed_read: int = 0
    unattributed_creation: int = 0
    # TB-28: usage on entries stamped gitBranch="HEAD" (a detached checkout). "HEAD"
    # is the ABSENCE of a branch, not a branch, so it can never match a manifest and
    # cannot be attributed to a run -- but a delegator in a detached worktree is
    # indistinguishable from unrelated detached work, so it cannot be disclaimed
    # either. Named, never folded into `read` (S23/S38: report the gap, never a
    # silent zero -- and never a fabricated attribution).
    detached_sessions: int = 0
    detached_read: int = 0
    detached_creation: int = 0
    detached_input: int = 0
    detached_output: int = 0
    branches_seen: set[str] = field(default_factory=set)

    @property
    def total_cache(self) -> int:
        """read + creation. The prefix-sharing invariant: a read drop offset by a
        creation rise moved no tokens, so read alone is never the metric (S39)."""
        return self.read + self.creation

    def per_ticket(self, tickets: int) -> dict[str, float]:
        """Normalize by ticket count so runs of different size compare."""
        if tickets <= 0:
            raise ValueError("tickets must be > 0 to normalize per ticket")
        return {
            "cache_read": self.read / tickets,
            "cache_creation": self.creation / tickets,
            "total_cache": self.total_cache / tickets,
        }

    def missing_branches(self, manifest: RunManifest) -> list[str]:
        """Manifest branches that matched zero entries — a typo'd or renamed branch
        would otherwise read as a ticket that cost nothing (S23/S38: name the gap)."""
        return sorted(manifest.branches - self.branches_seen)


@dataclass
class InefficiencyCounters:
    """S14 inefficiency-callout counters.

    Each scalar count carries a `*_by_tool` breakdown so a callout can name
    its top offender; a bare total tells an operator nothing to act on.
    """

    tool_search_calls: int = 0
    tool_search_tokens: int = 0
    failures: int = 0
    oversized_outputs: int = 0
    subagent_fanout: int = 0
    churn_retries: int = 0
    failures_by_tool: dict[str, int] = field(default_factory=dict)
    oversized_by_tool: dict[str, int] = field(default_factory=dict)
    churn_by_tool: dict[str, int] = field(default_factory=dict)
    subagent_by_tool: dict[str, int] = field(default_factory=dict)


@dataclass
class Reducer:
    """Incremental corpus aggregator (S11). Never stores a corpus-wide call list."""

    sessions_scanned: int = 0
    calls_joined: int = 0
    malformed_total: int = 0
    # (agent, record kind) -> count of tool records a parser saw but could not join
    # (TB-24). Never folded into `calls_joined`: these are surfaced apart, not counted
    # as calls, so corpus counts and every inefficiency ratio stay unchanged.
    unjoinable: dict[tuple[str, str], int] = field(default_factory=dict)
    agents: dict[str, AgentStats] = field(default_factory=dict)
    tools: dict[tuple[str, str], ToolStats] = field(default_factory=dict)
    tools_by_model: dict[tuple[str, str, str], ToolStats] = field(default_factory=dict)
    inefficiency: InefficiencyCounters = field(default_factory=InefficiencyCounters)
    run: RunManifest | None = None
    run_stats: RunStats = field(default_factory=RunStats)

    def absorb(self, agent: str, result: ParseResult) -> None:
        """Fold one parsed session's calls into the running counters.

        `result.calls` is a per-session list already produced by the ref's
        adapter; it is only ever iterated here and never retained.
        """
        self.sessions_scanned += 1
        self.malformed_total += result.malformed
        # TB-24: fold recognized-but-unjoinable records by (agent, kind). Kept out
        # of the per-call loop below so they never touch a call-derived counter.
        for kind, count in result.unjoinable.items():
            key = (agent, kind)
            self.unjoinable[key] = self.unjoinable.get(key, 0) + count
        agent_stats = self.agents.setdefault(agent, AgentStats())
        agent_stats.sessions += 1

        # S32/S39: session-grain, incremented once per session here -- never inside
        # the per-call loop below, which would fabricate a per-call denominator.
        # Measured when either cache field is non-None (Claude stamps both;
        # hermes stamps read only).
        if (
            result.session_cache_read_tokens is not None
            or result.session_cache_creation_tokens is not None
        ):
            agent_stats.sessions_with_cache_data += 1
            read = result.session_cache_read_tokens or 0
            creation = result.session_cache_creation_tokens or 0
            agent_stats.cache_read_tokens_total += read
            agent_stats.cache_creation_tokens_total += creation
            if read > 0 or creation > 0:
                agent_stats.sessions_with_cache_hit += 1

        # S40: entry-grain run attribution. Kept out of the per-call loop -- cache
        # tokens are billed per message, not per tool call.
        if self.run is not None:
            self._absorb_run(result)

        prev_name: str | None = None
        prev_bad = False
        for call in result.calls:
            self.calls_joined += 1
            agent_stats.calls += 1
            agent_stats.output_tokens += call.tokens
            agent_stats.input_tokens += call.input_tokens

            tool_stats = self.tools.setdefault((agent, call.name), ToolStats())
            tool_stats.calls += 1
            tool_stats.output_tokens += call.tokens
            tool_stats.input_tokens += call.input_tokens

            model_key = (agent, call.model or UNKNOWN_MODEL, call.name)
            model_stats = self.tools_by_model.setdefault(model_key, ToolStats())
            model_stats.calls += 1
            model_stats.output_tokens += call.tokens
            model_stats.input_tokens += call.input_tokens

            is_bad = call.error is not None or call.no_result
            if call.error is not None:
                tool_stats.errors += 1
                model_stats.errors += 1
                agent_stats.errors += 1
                self.inefficiency.failures += 1
                _bump(self.inefficiency.failures_by_tool, call.name)
            if call.no_result:
                tool_stats.no_result += 1
                model_stats.no_result += 1
                agent_stats.no_result += 1

            if _is_cache_hit(call.usage):
                tool_stats.cache_hits += 1
                model_stats.cache_hits += 1

            if call.usage_provenance is not UsageProvenance.PRESENT:
                # Every flavour of absence means the same thing here: not measurable.
                # The arms differ for diagnostics, not for this flag.
                tool_stats.usage_missing += 1
                model_stats.usage_missing += 1

            if call.is_deferral:
                self.inefficiency.tool_search_calls += 1
                self.inefficiency.tool_search_tokens += call.tokens

            if call.tokens >= OVERSIZED_OUTPUT_TOKENS:
                self.inefficiency.oversized_outputs += 1
                _bump(self.inefficiency.oversized_by_tool, call.name)

            if call.is_subagent_fanout:
                self.inefficiency.subagent_fanout += 1
                _bump(self.inefficiency.subagent_by_tool, call.name)

            if is_bad and prev_bad and call.name == prev_name:
                self.inefficiency.churn_retries += 1
                _bump(self.inefficiency.churn_by_tool, call.name)

            prev_name = call.name
            prev_bad = is_bad

    def _absorb_run(self, result: ParseResult) -> None:
        """Fold one session into the run totals (S40). Only *candidate* sessions --
        those with at least one entry on a run branch -- contribute anything."""
        assert self.run is not None

        # TB-28: book detached-HEAD usage BEFORE the candidate test. A delegator in a
        # detached checkout has no run-branch entry at all, so it never reaches the
        # loop below -- it would early-return and vanish from both the run total and
        # `unattributed`, undercounting the run with no failure signal.
        detached = result.usage_by_branch.get(DETACHED_BRANCH)
        if detached is not None and _spent_anything(detached):
            self.run_stats.detached_sessions += 1
            self.run_stats.detached_read += detached.read
            self.run_stats.detached_creation += detached.creation
            self.run_stats.detached_input += detached.input
            self.run_stats.detached_output += detached.output

        in_set = {b for b in result.usage_by_branch if b in self.run.branches}
        if not in_set:
            return  # not part of this run; contributes to neither total
        self.run_stats.candidate_sessions += 1
        self.run_stats.branches_seen |= in_set
        for branch, usage in result.usage_by_branch.items():
            if branch in self.run.branches:
                self.run_stats.read += usage.read
                self.run_stats.creation += usage.creation
                self.run_stats.input += usage.input
                self.run_stats.output += usage.output
            elif branch == DETACHED_BRANCH:
                continue  # already booked as detached; `unattributed` means a BRANCH
            else:
                # Straddle spillover: work done in the same session on another branch.
                self.run_stats.unattributed_read += usage.read
                self.run_stats.unattributed_creation += usage.creation


def _spent_anything(usage: BranchUsage) -> bool:
    """Did this bucket cost ANY tokens? Gating the detached blind spot on cache tokens
    alone (read/creation) would let an uncached detached turn -- real input/output, zero
    cache -- fall through the `continue` below AND the booking above, reproducing the
    very silent drop TB-28 exists to close. A measured zero is not a blind spot; an
    uncounted cost is."""
    return bool(usage.read or usage.creation or usage.input or usage.output)


def _bump(counter: dict[str, int], tool: str) -> None:
    counter[tool] = counter.get(tool, 0) + 1


def _is_cache_hit(usage: dict[str, object] | None) -> bool:
    """Caveat-only cache signal (S19) — never used for ranking."""
    if not usage:
        return False
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return True
    return False
