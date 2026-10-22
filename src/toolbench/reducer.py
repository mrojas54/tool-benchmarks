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
    # TB-44: the slice of detached usage whose `cwd` sits inside a manifest worktree.
    # Already INCLUDED in read/creation/input/output above; kept apart only so the
    # report can show how much of the total came via cwd rather than via branch.
    worktree_sessions: int = 0
    worktree_read: int = 0
    worktree_creation: int = 0
    worktree_input: int = 0
    worktree_output: int = 0
    branches_seen: set[str] = field(default_factory=set)
    worktrees_seen: set[str] = field(default_factory=set)

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

    def missing_worktrees(self, manifest: RunManifest) -> list[str]:
        """Manifest worktrees no detached entry ran inside -- the TB-44 mirror of
        `missing_branches`. A typo'd path would otherwise read as a free ticket."""
        return sorted(set(manifest.worktrees) - self.worktrees_seen)


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

        _absorb_session_cache(agent_stats, result)

        # S40: entry-grain run attribution. Kept out of the per-call loop -- cache
        # tokens are billed per message, not per tool call.
        if self.run is not None:
            self._absorb_run(result)

        self._absorb_calls(agent, agent_stats, result)

    def _absorb_calls(self, agent: str, agent_stats: AgentStats, result: ParseResult) -> None:
        """Fold one session's per-call counters.

        Split out of `absorb` so the session-grain counters above can never be reached
        from inside a per-call loop, which would fabricate a per-call denominator
        (S32/S39).
        """
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
        stats = self.run_stats

        # TB-44: a detached entry whose cwd sits inside a manifest worktree is
        # attributed by that path -- a dispatch-time key, like a branch name. Only
        # that slice folds; the rest of HEAD stays in the unattributable bucket.
        via_worktree, matched = self._match_worktrees(result)
        stats.worktrees_seen |= matched

        # TB-28: book the REMAINING detached usage BEFORE the candidate test. A
        # delegator in a detached checkout has no run-branch entry at all, so it never
        # reaches the loop below -- it would early-return and vanish from both the run
        # total and `unattributed`, undercounting the run with no failure signal.
        # Derived by subtraction from `usage_by_branch`, so a parser that stamps no
        # cwd buckets leaves every HEAD token here rather than dropping any.
        detached = result.usage_by_branch.get(DETACHED_BRANCH)
        if detached is not None:
            rest = _minus(detached, via_worktree)
            if _spent_anything(rest):
                stats.detached_sessions += 1
                stats.detached_read += rest.read
                stats.detached_creation += rest.creation
                stats.detached_input += rest.input
                stats.detached_output += rest.output

        in_set = {b for b in result.usage_by_branch if b in self.run.branches}
        if not in_set and not matched:
            return  # not part of this run; contributes to neither total
        stats.candidate_sessions += 1
        stats.branches_seen |= in_set
        if matched:
            stats.worktree_sessions += 1
            stats.worktree_read += via_worktree.read
            stats.worktree_creation += via_worktree.creation
            stats.worktree_input += via_worktree.input
            stats.worktree_output += via_worktree.output
            stats.read += via_worktree.read
            stats.creation += via_worktree.creation
            stats.input += via_worktree.input
            stats.output += via_worktree.output
        for branch, usage in result.usage_by_branch.items():
            if branch in self.run.branches:
                stats.read += usage.read
                stats.creation += usage.creation
                stats.input += usage.input
                stats.output += usage.output
            elif branch == DETACHED_BRANCH:
                continue  # booked above (worktree or detached); `unattributed` means a BRANCH
            else:
                # Straddle spillover: work done in the same session on another branch.
                stats.unattributed_read += usage.read
                stats.unattributed_creation += usage.creation

    def _match_worktrees(self, result: ParseResult) -> tuple[BranchUsage, set[str]]:
        """Sum the detached (`HEAD`) cwd buckets that sit inside a manifest worktree,
        and name the worktrees they matched. Branch-named entries are never re-keyed by
        cwd: a branch already attributes them, and a second key could double-count."""
        assert self.run is not None
        total = BranchUsage()
        matched: set[str] = set()
        if not self.run.worktrees:
            return total, matched
        for (branch, cwd), usage in result.usage_by_branch_cwd.items():
            if branch != DETACHED_BRANCH:
                continue
            worktree = self.run.worktree_for(cwd)
            if worktree is None:
                continue
            matched.add(worktree)
            total.read += usage.read
            total.creation += usage.creation
            total.input += usage.input
            total.output += usage.output
            total.messages += usage.messages
        return total, matched


def _absorb_session_cache(agent_stats: AgentStats, result: ParseResult) -> None:
    """S32/S39 session-grain cache counters, incremented once per session.

    Never called from inside the per-call loop, which would fabricate a per-call
    denominator. Measured when EITHER cache field is non-None (Claude stamps both;
    hermes stamps read only), so a measured-zero is distinguishable from unmeasured.
    """
    if (
        result.session_cache_read_tokens is None
        and result.session_cache_creation_tokens is None
    ):
        return
    agent_stats.sessions_with_cache_data += 1
    read = result.session_cache_read_tokens or 0
    creation = result.session_cache_creation_tokens or 0
    agent_stats.cache_read_tokens_total += read
    agent_stats.cache_creation_tokens_total += creation
    if read > 0 or creation > 0:
        agent_stats.sessions_with_cache_hit += 1


def _spent_anything(usage: BranchUsage) -> bool:
    """Did this bucket cost ANY tokens? Gating the detached blind spot on cache tokens
    alone (read/creation) would let an uncached detached turn -- real input/output, zero
    cache -- fall through the `continue` below AND the booking above, reproducing the
    very silent drop TB-28 exists to close. A measured zero is not a blind spot; an
    uncounted cost is."""
    return bool(usage.read or usage.creation or usage.input or usage.output)


def _minus(whole: BranchUsage, part: BranchUsage) -> BranchUsage:
    """`whole - part`, where `part` is a sub-partition of `whole` (cwd buckets of one
    branch), so no field can go negative."""
    return BranchUsage(
        read=whole.read - part.read,
        creation=whole.creation - part.creation,
        input=whole.input - part.input,
        output=whole.output - part.output,
        messages=whole.messages - part.messages,
    )


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
