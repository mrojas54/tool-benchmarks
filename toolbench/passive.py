"""Passive analyzer (S11-S15, S19, S23): incremental reducer + report + CLI.

Aggregation streams per parsed session (S11): only per-agent/per-tool
reducers and report counters live globally on `Reducer`. Each session's
`ParseResult.calls` list is folded into those counters and discarded — no
corpus-wide `list[ToolCall]` is ever retained.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from toolbench.adapters import UnknownSchema
from toolbench.freeze import read_manifest, write_manifest
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


@dataclass(frozen=True)
class CorpusFingerprint:
    """Identity of the scanned corpus (TB-22, S36).

    A `digest` over a per-session *signature* for every scanned session -- the
    sessions that actually produced the report's numbers -- plus their `count`.
    Two runs whose fingerprints match scanned the same sessions with the same
    content, so a numeric delta between their reports is attributable to code,
    not to the corpus moving underneath.

    The signature carries both mechanisms the corpus drifts by (see the ticket):
    a session's identity catches the sliding-window TAIL DELETION (a transcript
    ages out and its id leaves the set), and its call and malformed-line counts
    catch the live session's APPEND (transcripts are append-only, so both counts
    are exact proxies for content growth -- including an append that lands as a
    malformed line rather than a new valid call). An id-only digest, or one
    folding calls alone, would match across an append while a rendered number
    moved and falsely reassure a reader diffing the two reports -- the one outcome
    the ticket says must not survive.

    The scanned set, not the discovered set, is the basis: a discovered-set
    digest could match while transcripts slid scanned->skipped. The count travels
    alongside so a hash collision cannot hide a size change.
    """

    digest: str
    count: int


def corpus_fingerprint(signatures: Iterable[str]) -> CorpusFingerprint:
    """Order-independent fingerprint of a set of per-session signatures (S36).

    Sorted before hashing so discovery/paging order can never move the digest --
    only the membership or content of the scanned set can. `session_signature`
    builds the per-session strings; this stays a pure set-hash so its callers
    decide what a signature contains (the manifest freezes identity alone).
    """
    items = sorted(signatures)
    h = hashlib.sha256()
    for sig in items:
        h.update(sig.encode("utf-8"))
        h.update(b"\n")
    return CorpusFingerprint(digest=h.hexdigest()[:16], count=len(items))


def session_signature(
    session_id: str, call_count: int, malformed: int, unjoinable: int = 0
) -> str:
    """One scanned session's fingerprint contribution: identity + content (S36).

    Tab-joins the id with every number the Summary renders for this session's
    content -- its call count, its malformed-line count, and its unjoinable-record
    count -- so a session that grows moves the corpus fingerprint even though its id
    is unchanged (append-only transcripts -> every count is exact). Folding
    `call_count` alone would miss an append that lands as a malformed line;
    likewise, folding only calls and malformed would miss an appended
    `web_search_call`, which moves "Unjoinable tool records" while `len(calls)` and
    "Malformed lines" stay put -- and the digest would falsely match while a rendered
    number differs, the one outcome S36 forbids (TB-24).
    """
    return f"{session_id}\t{call_count}\t{malformed}\t{unjoinable}"


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
    # Only `calls` is date-filterable. Every other field is session-grain and passes
    # through intact: `malformed` and `unjoinable` are counts of seen records (TB-24),
    # and `session_cache_read_tokens` is the S32 cache stat -- the session was still
    # measured even when all its calls fall outside the range (TB-25). `replace` keeps
    # all non-overridden fields automatically, so a field added later cannot be
    # silently dropped by a hand-listed reconstruction.
    return replace(result, calls=kept)


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
    freeze: str | None


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
    parser.add_argument(
        "--freeze",
        default=None,
        metavar="MANIFEST",
        help="Pin the discovered corpus: write the ref list once, replay it on "
        "later runs, and name refs that have since vanished (TB-22).",
    )
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
        freeze=cast("str | None", ns.freeze),
    )


def _discover_refs(
    args: CliArgs, root: str, runner: Runner | None
) -> tuple[list[SessionRef], str | None, list[SkipRecord]]:
    """Resolve the index-source policy into a bounded list of refs (S10, S23)."""
    project = None if args.all_projects else args.project
    page_limit = args.limit if args.limit is not None else 500
    index_source = cast(IndexSource, args.index_source)
    refs_iter, fallback_reason = iter_sessions(
        index_source=index_source,
        agent=args.agent,
        project=project,
        since=args.since,
        limit=page_limit,
        root=root,
        runner=runner,
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
    skips: list[SkipRecord],
    include_subagents: bool,
    since_note: str | None,
    verbose: bool = False,
    fingerprint: CorpusFingerprint | None = None,
    freeze_note: str | None = None,
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
    # Reconcile discovery so `scanned` is never mistaken for the corpus size: a
    # discovered session either scanned or skipped, and every skip is one SkipRecord
    # (TB-21). `discovered` is derived, not a separate count that could drift.
    scanned = reducer.sessions_scanned
    skipped = len(skips)
    lines.append(
        f"- Sessions discovered: {scanned + skipped} / scanned: {scanned} / skipped: {skipped}"
    )
    if fingerprint is not None:
        # Identity of the set that produced the numbers above: two reports whose
        # fingerprints match are diffable; a delta between them is code, not the
        # corpus moving underneath (TB-22, S36).
        lines.append(
            f"- Corpus fingerprint: {fingerprint.digest} ({fingerprint.count} sessions scanned)"
        )
    if freeze_note is not None:
        lines.append(f"- {freeze_note}")
    if skips:
        # Keyed on the typed SkipReason (S34), not a substring scan of prose. A dead
        # index entry (missing_source) and a parser gap (unknown_schema) are counted
        # in separate buckets so the actionable one is never buried under the rest.
        lines.append("- Skipped by reason:")
        for reason, count in _reasons_by_count(skips):
            lines.append(f"  - {reason.value}: {count}")
    lines.append(f"- Tool calls joined: {reducer.calls_joined}")
    lines.append(f"- Malformed lines: {reducer.malformed_total}")
    if reducer.unjoinable:
        # Records a parser saw but structurally could not join (TB-24): named here so
        # codex's ~4% web-search undercount is never a silent zero. Attributed by
        # agent/kind (TB-23's typed-bucket ethos), sorted for a stable diff. Absent
        # entirely when there is nothing to report.
        total = sum(reducer.unjoinable.values())
        lines.append(f"- Unjoinable tool records (seen, not joined): {total}")
        for (agent_name, kind), count in sorted(reducer.unjoinable.items()):
            lines.append(f"  - {agent_name}/{kind}: {count}")
    lines.append(f"- Subagents included: {'yes' if include_subagents else 'no'}")
    lines.append(f"- AgentsView fallback reason: {fallback_reason if fallback_reason else 'none'}")
    lines.append("- Note: --since is file-mtime based.")
    if since_note:
        lines.append(f"- --since value used: {since_note}")

    if verbose and skips:
        # Individual ids live here, never in the default report -- 1600 ids on one
        # line is what made the pre-TB-21 report impossible to tally (TB-21).
        lines.append("")
        lines.append("### Skipped sessions (detail)")
        lines.append("")
        for skip in skips:
            ident = skip.session_id or "(root)"
            lines.append(f"- {ident} [{skip.agent}] {skip.reason.value}: {skip.detail}")

    return "\n".join(lines) + "\n"


def _reasons_by_count(skips: list[SkipRecord]) -> list[tuple[SkipReason, int]]:
    """Skip reasons highest-count-first; ties break on the reason's value so the
    histogram is deterministic."""
    tally = tally_skips(skips)
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0].value))


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner | None = None,
    root: str = "~/.claude/projects",
) -> int:
    args = parse_args(argv)

    # `--freeze` pins the discovered set: absent manifest -> discover and write it
    # once; present manifest -> replay it, bypassing live discovery so the input
    # set cannot drift between runs (TB-22, S37).
    freeze_path = args.freeze
    replaying = freeze_path is not None and Path(freeze_path).expanduser().exists()

    refs: list[SessionRef]
    fallback_reason: str | None
    skips: list[SkipRecord]
    if replaying:
        assert freeze_path is not None
        manifest = read_manifest(freeze_path)
        refs, fallback_reason, skips = manifest.refs, None, []
    else:
        try:
            refs, fallback_reason, skips = _discover_refs(args, root, runner)
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"toolbench.passive: fatal source error: {exc}", file=sys.stderr)
            return 1
        if freeze_path is not None:
            write_manifest(
                freeze_path, refs, corpus_fingerprint(r.session_id for r in refs).digest
            )

    if args.exclude_subagents:
        refs = filter_subagents(refs)

    reducer = Reducer()
    scanned_sigs: list[str] = []
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
        # Signature after date filtering: the report counts these calls, so the
        # fingerprint must fold the same post-filter count -- plus the malformed and
        # unjoinable counts, which the Summary also renders and date-filtering leaves
        # intact (S36, TB-24).
        scanned_sigs.append(
            session_signature(
                ref.session_id,
                len(filtered.calls),
                filtered.malformed,
                sum(filtered.unjoinable.values()),
            )
        )

    fingerprint = corpus_fingerprint(scanned_sigs)

    freeze_note: str | None = None
    if freeze_path is not None:
        if replaying:
            # A frozen ref that no longer loads (missing_source) has vanished from
            # disk since the freeze -- name the count so a shrinking scanned set is
            # never mistaken for a code effect (TB-22).
            vanished = sum(1 for s in skips if s.reason is SkipReason.MISSING_SOURCE)
            freeze_note = (
                f"Replaying frozen corpus: {freeze_path} ({vanished} vanished since freeze)"
            )
        else:
            freeze_note = f"Corpus frozen to: {freeze_path}"

    if reducer.calls_joined == 0:
        if skips:
            tally = ", ".join(f"{r.value}={c}" for r, c in _reasons_by_count(skips))
            suffix = f" (skipped {len(skips)}: {tally})"
        else:
            suffix = ""
        print(f"toolbench.passive: no sessions matched the given selection.{suffix}")
        return 0

    report = render_report(
        reducer,
        index_source=args.index_source,
        fallback_reason=fallback_reason,
        skips=skips,
        include_subagents=not args.exclude_subagents,
        since_note=args.since,
        verbose=args.verbose,
        fingerprint=fingerprint,
        freeze_note=freeze_note,
    )
    if args.out:
        Path(args.out).write_text(report)
        print(f"Report written to {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
