"""Passive analyzer (S11-S15, S19, S23): incremental reducer + report + CLI.

Aggregation streams per parsed session (S11): only per-agent/per-tool
reducers and report counters live globally on `Reducer`. Each session's
`ParseResult.calls` list is folded into those counters and discarded — no
corpus-wide `list[ToolCall]` is ever retained.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from toolbench.adapters import UnknownSchema
from toolbench.registry import pick_adapter
from toolbench.sources import (
    IndexSource,
    MissingSourceExport,
    NonTranscriptExport,
    Runner,
    SessionRef,
    SkipReason,
    SkipRecord,
    iter_sessions,
)
from toolbench.transcript import ParseResult, UsageProvenance

OVERSIZED_OUTPUT_TOKENS = 5000
# `spawn_agent` is codex's fan-out primitive (TB-12). codex is the only agent in
# the corpus that spawns subagents, so before it was parsed the fan-out callout was
# measured with its most relevant agent's data entirely absent. `wait_agent` awaits
# an already-spawned subagent and is not itself a fan-out.
SUBAGENT_TOOL_NAMES = frozenset({"Agent", "Task", "spawn_agent"})
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
        agent_stats = self.agents.setdefault(agent, AgentStats())
        agent_stats.sessions += 1

        # S32: session-grain, incremented once per session here -- never inside
        # the per-call loop below, which would fabricate a per-call denominator.
        if result.session_cache_read_tokens is not None:
            agent_stats.sessions_with_cache_data += 1
            if result.session_cache_read_tokens > 0:
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

            if call.name == "ToolSearch":
                self.inefficiency.tool_search_calls += 1
                self.inefficiency.tool_search_tokens += call.tokens

            if call.tokens >= OVERSIZED_OUTPUT_TOKENS:
                self.inefficiency.oversized_outputs += 1
                _bump(self.inefficiency.oversized_by_tool, call.name)

            if call.name in SUBAGENT_TOOL_NAMES:
                self.inefficiency.subagent_fanout += 1
                _bump(self.inefficiency.subagent_by_tool, call.name)

            if is_bad and prev_bad and call.name == prev_name:
                self.inefficiency.churn_retries += 1
                _bump(self.inefficiency.churn_by_tool, call.name)

            prev_name = call.name
            prev_bad = is_bad


def _bump(counter: dict[str, int], tool: str) -> None:
    counter[tool] = counter.get(tool, 0) + 1


def _top_offender(by_tool: dict[str, int]) -> tuple[str, int] | None:
    """Highest count, ties broken alphabetically so the report is deterministic."""
    if not by_tool:
        return None
    return min(by_tool.items(), key=lambda kv: (-kv[1], kv[0]))


def _callout(label: str, count: int, total_calls: int, by_tool: dict[str, int]) -> str:
    """Render one callout as `N of M calls (P%)`, naming the worst tool."""
    share = (count / total_calls * 100) if total_calls else 0.0
    line = f"- {label}: {count} of {total_calls} calls ({share:.1f}%)"
    top = _top_offender(by_tool)
    if count and top is not None:
        line += f"; top: {top[0]} ({top[1]})"
    return line


def _is_cache_hit(usage: dict[str, object] | None) -> bool:
    """Caveat-only cache signal (S19) — never used for ranking."""
    if not usage:
        return False
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return True
    return False


def _apply_date_range(
    result: ParseResult, date_from: str | None, date_to: str | None
) -> ParseResult:
    """Filter a session's calls to `--date-from`/`--date-to` (per ToolCall.ts)."""
    if date_from is None and date_to is None:
        return result
    kept = [call for call in result.calls if _call_in_range(call.ts, date_from, date_to)]
    return ParseResult(calls=kept, malformed=result.malformed)


def _call_in_range(ts: str, date_from: str | None, date_to: str | None) -> bool:
    if not ts:
        return True
    date_part = ts[:10]
    if date_from is not None and date_part < date_from:
        return False
    if date_to is not None and date_part > date_to:
        return False
    return True


def _is_subagent_path(ref: SessionRef) -> bool:
    return ref.path is not None and "/subagents/" in ref.path


def filter_subagents(refs: list[SessionRef]) -> list[SessionRef]:
    """Remove subagent-path sessions (S13)."""
    return [ref for ref in refs if not _is_subagent_path(ref)]


def classify_skip(exc: BaseException) -> SkipReason:
    """Map a caught skip exception to its typed reason (TB-23).

    The type information exists at the raise site; this reads it one frame later
    instead of destroying it into prose. `MissingSourceExport` and
    `NonTranscriptExport` are flat siblings, so their order here is irrelevant --
    only the bare-`RuntimeError`/`OSError` fallback must come last.
    """
    if isinstance(exc, MissingSourceExport):
        return SkipReason.MISSING_SOURCE
    if isinstance(exc, UnknownSchema):
        return SkipReason.UNKNOWN_SCHEMA
    if isinstance(exc, NonTranscriptExport):
        return SkipReason.NON_TRANSCRIPT
    if isinstance(exc, UnicodeDecodeError):
        return SkipReason.DECODE_ERROR
    return SkipReason.EXPORT_FAILED


def skip_record_for(ref: SessionRef, exc: BaseException) -> SkipRecord:
    """Stamp a skipped session with its identity and typed reason (TB-23)."""
    return SkipRecord(
        session_id=ref.session_id,
        agent=ref.agent,
        reason=classify_skip(exc),
        detail=str(exc),
    )


def tally_skips(skips: Iterable[SkipRecord]) -> dict[SkipReason, int]:
    """Count skips per reason. Answers "how many have no parser?" from typed data."""
    return dict(Counter(s.reason for s in skips))


def _skip_detail_line(skip: SkipRecord) -> str:
    """Flatten one skip to the legacy `<id>: <detail>` prose (root-level skips
    carry no id). Kept only for the current single-line render; TB-21 replaces it
    with a per-reason histogram keyed on `skip.reason`."""
    return f"{skip.session_id}: {skip.detail}" if skip.session_id else skip.detail


@dataclass
class CliArgs:
    """Parsed CLI flags (S12)."""

    agent: str
    all_projects: bool
    project: str | None
    since: str | None
    date_from: str | None
    date_to: str | None
    out: str | None
    limit: int | None
    exclude_subagents: bool
    index_source: str
    verbose: bool


def parse_args(argv: list[str] | None) -> CliArgs:
    parser = argparse.ArgumentParser(prog="toolbench.passive", description="Passive tool-usage analyzer.")
    parser.add_argument("--agent", default="all")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", default=False)
    scope.add_argument("--project", default=None)
    parser.add_argument("--since", default=None)
    parser.add_argument("--date-from", default=None)
    parser.add_argument("--date-to", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--exclude-subagents", action="store_true", default=False)
    parser.add_argument("--index-source", choices=("auto", "agentsview", "raw"), default="auto")
    parser.add_argument("--verbose", action="store_true", default=False)
    ns = parser.parse_args(argv)
    return CliArgs(
        agent=cast(str, ns.agent),
        all_projects=ns.project is None,
        project=cast("str | None", ns.project),
        since=cast("str | None", ns.since),
        date_from=cast("str | None", ns.date_from),
        date_to=cast("str | None", ns.date_to),
        out=cast("str | None", ns.out),
        limit=cast("int | None", ns.limit),
        exclude_subagents=cast(bool, ns.exclude_subagents),
        index_source=cast(str, ns.index_source),
        verbose=cast(bool, ns.verbose),
    )


def _discover_refs(
    args: CliArgs, root: str, runner: Runner | None
) -> tuple[list[SessionRef], str | None, list[SkipRecord]]:
    """Resolve the index-source policy into a bounded list of refs (S10, S23)."""
    project = None if args.all_projects else args.project
    page_limit = args.limit if args.limit is not None else 500
    index_source = cast(IndexSource, args.index_source)
    if runner is not None:
        refs_iter, fallback_reason = iter_sessions(
            index_source=index_source,
            agent=args.agent,
            project=project,
            since=args.since,
            limit=page_limit,
            root=root,
            runner=runner,
        )
    else:
        refs_iter, fallback_reason = iter_sessions(
            index_source=index_source,
            agent=args.agent,
            project=project,
            since=args.since,
            limit=page_limit,
            root=root,
        )

    refs: list[SessionRef] = []
    skips: list[SkipRecord] = []
    try:
        for ref in refs_iter:
            refs.append(ref)
            if args.limit is not None and len(refs) >= args.limit:
                break
    except FileNotFoundError as exc:
        if args.index_source == "auto":
            # A root-level failure has no per-session ref; the absent raw fallback
            # root is itself a missing source (TB-23).
            skips.append(
                SkipRecord(
                    session_id="",
                    agent=args.agent,
                    reason=SkipReason.MISSING_SOURCE,
                    detail=str(exc),
                )
            )
        else:
            raise
    return refs, fallback_reason, skips


def _parse_ref(ref: SessionRef, runner: Runner | None) -> ParseResult:
    """Uniformly parse any session (S11 wiring).

    Every branch this function used to own now lives in the registry: hermes
    claims by source, everything else is content-detected. An unrecognized
    schema raises `UnknownSchema` (a RuntimeError), which `main`'s per-session
    guard demotes to `skipped_roots` -- so an unparseable agent is named in the
    Summary instead of reported as an agent that did no tool work (TB-12).
    """
    return pick_adapter(ref, runner).parse(ref)


def render_report(
    reducer: Reducer,
    *,
    index_source: str,
    fallback_reason: str | None,
    skipped_roots: list[str],
    include_subagents: bool,
    since_note: str | None,
) -> str:
    """Render the five-section report (S14) with provenance (S15)."""
    lines: list[str] = ["# Tool Usage Report", ""]

    lines.append("## Agent Breakdown")
    lines.append("")
    lines.append("| agent | sessions | calls | output_tokens | input_tokens | errors | no_result |")
    lines.append("|---|---|---|---|---|---|---|")
    cache_caveats: list[str] = []
    for agent in sorted(reducer.agents):
        s = reducer.agents[agent]
        lines.append(
            f"| {agent} | {s.sessions} | {s.calls} | {s.output_tokens} | "
            f"{s.input_tokens} | {s.errors} | {s.no_result} |"
        )
        if s.sessions_with_cache_data > 0:
            # S32: session-grain only, orthogonal to the per-call `cache_assisted`
            # column below -- never mixed into that column, never a sixth section.
            cache_caveats.append(
                f"- {agent}: {s.sessions_with_cache_hit} of {s.sessions_with_cache_data} "
                "sessions carry session-grain `cache_read_tokens` > 0 "
                "(S32: session grain only — not attributable to individual tool calls)."
            )
    lines.extend(cache_caveats)
    lines.append("")

    lines.append("## Tool Leaderboard")
    lines.append("")
    lines.append("| agent | tool | calls | context_tokens | input_tokens | errors | cache_assisted |")
    lines.append("|---|---|---|---|---|---|---|")
    ranked = sorted(reducer.tools.items(), key=lambda kv: kv[1].output_tokens, reverse=True)
    for (agent, tool), stats in ranked:
        if stats.cache_hits > 0:
            cache_note = "yes"                        # a hit was observed; blindness elsewhere is irrelevant
        elif stats.usage_missing == 0:
            cache_note = "no"                         # measured, and it was zero
        elif stats.usage_missing == stats.calls:
            cache_note = "n/a"                         # never measurable
        else:
            cache_note = "n/a*"                        # partially measurable; some rows blind
        lines.append(
            f"| {agent} | {tool} | {stats.calls} | {stats.output_tokens} | "
            f"{stats.input_tokens} | {stats.errors} | {cache_note} |"
        )
    lines.append("")
    lines.append(
        "`n/a` = usage channel unavailable for every call (S29); "
        "`n/a*` = unavailable for some. Neither is a measured zero. "
        "Per S19 this flag is caveat-only and never affects ranking."
    )
    lines.append("")

    lines.append("## Model Breakdown")
    lines.append("")
    lines.append("| agent | model | tool | calls | context_tokens | input_tokens | errors |")
    lines.append("|---|---|---|---|---|---|---|")
    # Descending by context tokens; key breaks ties so the table is deterministic.
    ranked_by_model = sorted(
        reducer.tools_by_model.items(), key=lambda kv: (-kv[1].output_tokens, kv[0])
    )
    for (agent, model, tool), stats in ranked_by_model:
        lines.append(
            f"| {agent} | {model} | {tool} | {stats.calls} | {stats.output_tokens} | "
            f"{stats.input_tokens} | {stats.errors} |"
        )
    lines.append("")

    lines.append("## Inefficiency Callouts")
    lines.append("")
    ineff = reducer.inefficiency
    total = reducer.calls_joined
    share = (ineff.tool_search_calls / total * 100) if total else 0.0
    lines.append(
        f"- ToolSearch/deferral tax: {ineff.tool_search_calls} of {total} calls "
        f"({share:.1f}%), {ineff.tool_search_tokens} tokens"
    )
    lines.append(_callout("Failures", ineff.failures, total, ineff.failures_by_tool))
    lines.append(
        _callout(
            f"Oversized outputs (>= {OVERSIZED_OUTPUT_TOKENS} tokens)",
            ineff.oversized_outputs,
            total,
            ineff.oversized_by_tool,
        )
    )
    lines.append(
        _callout("Subagent fan-out calls", ineff.subagent_fanout, total, ineff.subagent_by_tool)
    )
    lines.append(
        _callout(
            "Churn (consecutive-repeat retries)", ineff.churn_retries, total, ineff.churn_by_tool
        )
    )
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Index source: {index_source}")
    lines.append(f"- Sessions scanned: {reducer.sessions_scanned}")
    lines.append(f"- Tool calls joined: {reducer.calls_joined}")
    lines.append(f"- Malformed lines: {reducer.malformed_total}")
    lines.append(f"- Subagents included: {'yes' if include_subagents else 'no'}")
    lines.append(f"- AgentsView fallback reason: {fallback_reason if fallback_reason else 'none'}")
    lines.append(f"- Skipped roots: {'; '.join(skipped_roots) if skipped_roots else 'none'}")
    lines.append("- Note: --since is file-mtime based.")
    if since_note:
        lines.append(f"- --since value used: {since_note}")

    return "\n".join(lines) + "\n"


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner | None = None,
    root: str = "~/.claude/projects",
) -> int:
    args = parse_args(argv)
    try:
        refs, fallback_reason, skips = _discover_refs(args, root, runner)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"toolbench.passive: fatal source error: {exc}", file=sys.stderr)
        return 1

    if args.exclude_subagents:
        refs = filter_subagents(refs)

    reducer = Reducer()
    for ref in refs:
        if args.verbose:
            print(f"scanning {ref.session_id} ({ref.source})", file=sys.stderr)
        try:
            result = _parse_ref(ref, runner)
        except (OSError, RuntimeError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError subclasses ValueError, not OSError/RuntimeError.
            # In-tree readers now decode leniently, so this only fires for a caller
            # who injects a strict-decode `runner`; catching it keeps one bad
            # session from taking the corpus down with it. The reason is typed at the
            # raise site and read back here rather than stringified away (TB-23).
            skips.append(skip_record_for(ref, exc))
            continue
        filtered = _apply_date_range(result, args.date_from, args.date_to)
        reducer.absorb(ref.agent, filtered)

    if reducer.calls_joined == 0:
        suffix = (
            f" (skipped roots: {'; '.join(_skip_detail_line(s) for s in skips)})"
            if skips
            else ""
        )
        print(f"toolbench.passive: no sessions matched the given selection.{suffix}")
        return 0

    report = render_report(
        reducer,
        index_source=args.index_source,
        fallback_reason=fallback_reason,
        skipped_roots=[_skip_detail_line(s) for s in skips],
        include_subagents=not args.exclude_subagents,
        since_note=args.since,
    )
    if args.out:
        Path(args.out).write_text(report)
        print(f"Report written to {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
