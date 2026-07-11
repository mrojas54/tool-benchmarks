"""Incremental corpus aggregation (S11, S14, S19, S32, TB-24).

Only per-agent / per-tool reducers live here. Each session's `ParseResult.calls`
is folded into counters and discarded — no corpus-wide `list[ToolCall]`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from toolbench.transcript import ParseResult, UsageProvenance

OVERSIZED_OUTPUT_TOKENS = 5000
UNKNOWN_MODEL = "unknown"


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
