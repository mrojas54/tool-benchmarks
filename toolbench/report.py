"""Markdown report rendering and corpus fingerprinting (S14, S15, S36, TB-22)."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from toolbench.reducer import OVERSIZED_OUTPUT_TOKENS, AgentStats, Reducer
from toolbench.sources import AgentCensus, SkipReason, SkipRecord

# Ratio of the largest per-agent sampling fraction to the smallest, above which
# cross-agent numbers stop being comparable and the report says so. Arbitrary but
# STATED -- and it can only fire when a `--limit` actually truncated the corpus, so it
# never nags a full run.
SPREAD_THRESHOLD = 4.0


def _sampled_cell(scanned: int, total: int | None, unavailable: bool) -> str:
    """One agent's `sampled` cell: the numerator, its denominator, and the fraction."""
    if unavailable:
        return "unknown"
    if total is None:
        # Scanned, but absent from the census universe -- i.e. an agent the child-excluded
        # probe never saw. `residual` names it in aggregate; this names it in place.
        return f"{scanned} of unknown"
    if total == 0:
        # `scanned` is NOT dropped here: an agent can be seen in the probe listing and
        # then report total=0 from the later scoped call (the two are separate,
        # non-atomic agentsview invocations). Printing "0 of 0" over a nonzero
        # `scanned` would be the exact silent zero this ticket exists to close.
        return f"{scanned} of 0"
    return f"{scanned} of {total} ({scanned / total * 100:.1f}%)"


def _sampling_spread(reducer: Reducer, census: AgentCensus) -> float | None:
    """max/min sampling fraction across agents with >= 1 scanned session.

    Agents with zero scanned sessions are excluded: their fraction is 0, which would send
    the ratio to infinity and drown the real signal. They are disclosed by name instead.
    """
    fractions = [
        stats.sessions / total
        for agent, stats in reducer.agents.items()
        if stats.sessions and (total := census.totals.get(agent))
    ]
    if len(fractions) < 2:
        return None
    return max(fractions) / min(fractions)


def _sampling_notes(reducer: Reducer, census: AgentCensus) -> list[str]:
    """Disclosure that belongs BESIDE the table, not forty lines below it (TB-33).

    A reader forming a calls/session ratio across two rows never scrolls to the Summary,
    so the qualification has to sit where the comparison is made.
    """
    if census.unavailable_reason is not None:
        return [
            f"- Sampling fractions unavailable: {census.unavailable_reason}. Each row above "
            "may rest on a different fraction of its agent's archive; this run cannot say."
        ]

    notes: list[str] = []
    unreached = sorted(
        agent
        for agent, total in census.totals.items()
        if total > 0 and reducer.agents.get(agent, AgentStats()).sessions == 0
    )
    if unreached:
        named = ", ".join(f"{a} ({census.totals[a]} sessions)" for a in unreached)
        notes.append(
            f"- Present in the archive, not reached by this window: {named}. Their rows are "
            "zeros because we did not look, not because they did no work."
        )

    spread = _sampling_spread(reducer, census)
    if spread is not None and spread >= SPREAD_THRESHOLD:
        notes.append(
            f"- **Sampling is uneven ({spread:.1f}x spread).** Each row is a different "
            "fraction of a different-sized population, so any ratio formed ACROSS rows "
            "(calls/session, tokens/call, error rate) mixes sampling depth into the "
            "comparison and is not comparable. Re-run without --limit for a like-for-like "
            "table."
        )

    if census.residual > 0:
        notes.append(
            f"- Reconciliation: {census.residual} archive sessions belong to no agent we "
            "enumerated. The census universe comes from the child-excluded probe listing, "
            "so an agent whose sessions are ALL children is invisible to it -- the "
            "denominators above are incomplete."
        )
    return notes


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


def tally_skips(skips: Iterable[SkipRecord]) -> dict[SkipReason, int]:
    """Count skips per reason. Answers "how many have no parser?" from typed data."""
    return dict(Counter(s.reason for s in skips))


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


def _reasons_by_count(skips: list[SkipRecord]) -> list[tuple[SkipReason, int]]:
    """Skip reasons highest-count-first; ties break on the reason's value so the
    histogram is deterministic."""
    tally = tally_skips(skips)
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0].value))


def render_report(
    reducer: Reducer,
    *,
    index_source: str,
    fallback_reason: str | None,
    skips: list[SkipRecord],
    include_subagents: bool,
    subagents_found: int,
    sessions_discovered: int,
    since_note: str | None,
    census: AgentCensus,
    verbose: bool = False,
    fingerprint: CorpusFingerprint | None = None,
    freeze_note: str | None = None,
    run_tickets: int | None = None,
) -> str:
    """Render the five-section report (S14) with provenance (S15)."""
    lines: list[str] = ["# Tool Usage Report", ""]

    lines.append("## Agent Breakdown")
    lines.append("")
    lines.append(
        "| agent | sampled | sessions | calls | output_tokens | input_tokens | errors | no_result |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    cache_caveats: list[str] = []
    # Union, not `reducer.agents`: an agent the window never reached has no AgentStats at
    # all, and dropping its row is the headline bug (TB-33).
    for agent in sorted(set(reducer.agents) | set(census.totals)):
        s = reducer.agents.get(agent, AgentStats())
        sampled = _sampled_cell(
            s.sessions, census.totals.get(agent), census.unavailable_reason is not None
        )
        lines.append(
            f"| {agent} | {sampled} | {s.sessions} | {s.calls} | {s.output_tokens} | "
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
    lines.extend(_sampling_notes(reducer, census))
    lines.append("")

    lines.append("## Tool Leaderboard")
    lines.append("")
    lines.append("| agent | tool | calls | context_tokens | input_tokens | errors | cache_assisted |")
    lines.append("|---|---|---|---|---|---|---|")
    ranked = sorted(reducer.tools.items(), key=lambda kv: kv[1].output_tokens, reverse=True)
    for (agent, tool), stats in ranked:
        if stats.cache_hits > 0:
            cache_note = "yes"  # a hit was observed; blindness elsewhere is irrelevant
        elif stats.usage_missing == 0:
            cache_note = "no"  # measured, and it was zero
        elif stats.usage_missing == stats.calls:
            cache_note = "n/a"  # never measurable
        else:
            cache_note = "n/a*"  # partially measurable; some rows blind
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
    if census.unavailable_reason is None and census.totals:
        lines.append("- Sampling (scanned of each agent's own archive):")
        for agent in sorted(census.totals):
            agent_total = census.totals[agent]
            scanned_agent = reducer.agents.get(agent, AgentStats()).sessions
            pct = f"{scanned_agent / agent_total * 100:.1f}%" if agent_total else "n/a"
            tail = " — not reached by this window" if scanned_agent == 0 else ""
            lines.append(f"  - {agent}: {scanned_agent} of {agent_total} ({pct}){tail}")
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
    cache_read_total = sum(s.cache_read_tokens_total for s in reducer.agents.values())
    cache_creation_total = sum(s.cache_creation_tokens_total for s in reducer.agents.values())
    measured_cache_sessions = sum(s.sessions_with_cache_data for s in reducer.agents.values())
    if measured_cache_sessions > 0:
        # S39: read + creation together — a prefix-sharing change trades one for the
        # other, so a read delta alone misleads. Caveat only; never ranks.
        lines.append(
            f"- Session-grain cache tokens: read={cache_read_total} "
            f"creation={cache_creation_total} "
            f"({measured_cache_sessions} measured sessions; S39 caveat, not ranked)"
        )
    if reducer.run is not None:
        # S40: per-run cache cost. Caveat only -- never ranked, never folded into an
        # inefficiency ratio (S19). Read and creation always together (S39).
        run_stats = reducer.run_stats
        lines.append(
            f"- Run cache tokens (run {reducer.run.run}): "
            f"read={run_stats.read} creation={run_stats.creation} "
            f"({run_stats.candidate_sessions} candidate session"
            f"{'' if run_stats.candidate_sessions == 1 else 's'}; S40 caveat, not ranked)"
        )
        tickets = run_tickets if run_tickets is not None else reducer.run.ticket_count
        if tickets > 0:
            norm = run_stats.per_ticket(tickets)
            lines.append(
                f"  - per ticket ({tickets}): "
                f"read={norm['cache_read']:.1f} creation={norm['cache_creation']:.1f}"
            )
        if run_stats.unattributed_read or run_stats.unattributed_creation:
            # Straddle spillover: same-session work on branches outside the run. A
            # large value means the run total is a narrow slice of what was spent.
            lines.append(
                f"  - unattributed: read={run_stats.unattributed_read} "
                f"creation={run_stats.unattributed_creation} "
                f"(same-session work off the run's branches)"
            )
        if run_stats.detached_sessions:
            # TB-28: usage from DETACHED checkouts (gitBranch="HEAD"). Unattributable
            # by construction -- "HEAD" matches no manifest branch -- so it is neither
            # counted in the run nor discardable: a detached delegator and unrelated
            # detached work look identical. Name it so the run total is never read as
            # complete when it may not be (S23/S38).
            # input/output are shown HERE though the run headline is cache-only (S40):
            # an uncached detached turn has zero cache and real input/output, and a
            # bare "read=0 creation=0" would read as "nothing to see" -- a lie by
            # omission on the one line whose whole job is to disclose what was missed.
            lines.append(
                f"  - detached-HEAD (unattributable): "
                f"read={run_stats.detached_read} "
                f"creation={run_stats.detached_creation} "
                f"input={run_stats.detached_input} "
                f"output={run_stats.detached_output} "
                f"({run_stats.detached_sessions} session"
                f"{'' if run_stats.detached_sessions == 1 else 's'}; "
                f"may include run delegators -- run total may be low)"
            )
        missing = run_stats.missing_branches(reducer.run)
        if missing:
            lines.append(f"  - matched no entries: {', '.join(missing)}")
    if reducer.unjoinable:
        # Records a parser saw but structurally could not join (TB-24): named here so
        # codex's ~4% web-search undercount is never a silent zero. Attributed by
        # agent/kind (TB-23's typed-bucket ethos), sorted for a stable diff. Absent
        # entirely when there is nothing to report.
        unjoinable_total = sum(reducer.unjoinable.values())
        lines.append(f"- Unjoinable tool records (seen, not joined): {unjoinable_total}")
        for (agent_name, kind), count in sorted(reducer.unjoinable.items()):
            lines.append(f"  - {agent_name}/{kind}: {count}")
    # Earned, not asserted (TB-31). This line used to render straight from the CLI flag,
    # so it printed "Subagents included: no" on the AgentsView path -- which never listed
    # a subagent and therefore could not have excluded one. Reporting the count actually
    # stamped at discovery means the claim is falsifiable: a `no` beside `0 of N` says
    # the index found no subagents, not that the filter did its job.
    subagent_note = f"{subagents_found} of {sessions_discovered} discovered"
    if include_subagents:
        lines.append(f"- Subagents included: yes ({subagent_note} are subagent sessions)")
    else:
        lines.append(f"- Subagents included: no ({subagent_note} excluded)")
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
