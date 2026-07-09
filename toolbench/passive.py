"""Passive analyzer (S11-S15, S19, S23): incremental reducer + report + CLI.

Aggregation streams per parsed session (S11): only per-agent/per-tool
reducers and report counters live globally on `Reducer`. Each session's
`ParseResult.calls` list is folded into those counters and discarded — no
corpus-wide `list[ToolCall]` is ever retained.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from toolbench.sources import (
    IndexSource,
    NonTranscriptExport,
    Runner,
    SessionRef,
    path_looks_binary,
    iter_sessions,
    open_session_jsonl,
)
from toolbench.hermes import parse_hermes_session
from toolbench.transcript import ParseResult, parse_session

OVERSIZED_OUTPUT_TOKENS = 5000
SUBAGENT_TOOL_NAMES = frozenset({"Agent", "Task"})
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


@dataclass
class AgentStats:
    """Aggregate counters for one agent — S14 agent breakdown."""

    sessions: int = 0
    calls: int = 0
    output_tokens: int = 0
    input_tokens: int = 0
    errors: int = 0
    no_result: int = 0


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

        `result.calls` is a per-session list already produced by
        `parse_session`; it is only ever iterated here and never retained.
        """
        self.sessions_scanned += 1
        self.malformed_total += result.malformed
        agent_stats = self.agents.setdefault(agent, AgentStats())
        agent_stats.sessions += 1

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
) -> tuple[list[SessionRef], str | None, list[str]]:
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
    skipped_roots: list[str] = []
    try:
        for ref in refs_iter:
            refs.append(ref)
            if args.limit is not None and len(refs) >= args.limit:
                break
    except FileNotFoundError as exc:
        if args.index_source == "auto":
            skipped_roots.append(str(exc))
        else:
            raise
    return refs, fallback_reason, skipped_roots


def _parse_ref(ref: SessionRef, runner: Runner | None) -> ParseResult:
    """Uniformly parse a raw or AgentsView-sourced session (S11 wiring)."""
    if ref.agent == "hermes" and ref.path is None:
        # `agentsview session export` returns rc=0 and the whole default-profile
        # database for these, so read the archive directly instead (TB-11).
        return parse_hermes_session(
            ref.session_id, agent=ref.agent, source=ref.source, project=ref.project
        )

    if ref.path is not None:
        if path_looks_binary(ref.path):
            raise NonTranscriptExport(f"non-transcript payload (binary content): {ref.path}")
        return parse_session(ref.path, agent=ref.agent, source=ref.source, project=ref.project)

    lines = open_session_jsonl(ref, runner=runner) if runner is not None else open_session_jsonl(ref)
    # `lines` is a generator that may raise on first advance (bad returncode, binary
    # payload). Bind tmp_path before writing so the delete=False file is always
    # reclaimed, rather than stranded by an exception mid-loop.
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    tmp_path = tmp.name
    try:
        with tmp:
            for line in lines:
                tmp.write(line)
        return parse_session(tmp_path, agent=ref.agent, source=ref.source, project=ref.project)
    finally:
        os.unlink(tmp_path)


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
    for agent in sorted(reducer.agents):
        s = reducer.agents[agent]
        lines.append(
            f"| {agent} | {s.sessions} | {s.calls} | {s.output_tokens} | "
            f"{s.input_tokens} | {s.errors} | {s.no_result} |"
        )
    lines.append("")

    lines.append("## Tool Leaderboard")
    lines.append("")
    lines.append("| agent | tool | calls | context_tokens | input_tokens | errors | cache_assisted |")
    lines.append("|---|---|---|---|---|---|---|")
    ranked = sorted(reducer.tools.items(), key=lambda kv: kv[1].output_tokens, reverse=True)
    for (agent, tool), stats in ranked:
        cache_note = "yes" if stats.cache_hits > 0 else "no"
        lines.append(
            f"| {agent} | {tool} | {stats.calls} | {stats.output_tokens} | "
            f"{stats.input_tokens} | {stats.errors} | {cache_note} |"
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
        refs, fallback_reason, skipped_roots = _discover_refs(args, root, runner)
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
            # session from taking the corpus down with it.
            skipped_roots.append(f"{ref.session_id}: {exc}")
            continue
        filtered = _apply_date_range(result, args.date_from, args.date_to)
        reducer.absorb(ref.agent, filtered)

    if reducer.calls_joined == 0:
        suffix = f" (skipped roots: {'; '.join(skipped_roots)})" if skipped_roots else ""
        print(f"toolbench.passive: no sessions matched the given selection.{suffix}")
        return 0

    report = render_report(
        reducer,
        index_source=args.index_source,
        fallback_reason=fallback_reason,
        skipped_roots=skipped_roots,
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
